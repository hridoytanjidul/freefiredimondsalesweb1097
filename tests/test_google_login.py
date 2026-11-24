"""Sign in with Google against a fake Google: the PKCE/state/nonce checks, new and linked
accounts, every rejected token, banned users, and two simultaneous first logins."""
import base64
import hashlib
import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

import google_auth
from support import PASSWORD, StoreTest, client, in_parallel, make_user, messages, one, post, run, settings, value

CLIENT_ID = "1234-test.apps.googleusercontent.com"


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


class FakeGoogle:
    """Stands in for Google's token endpoint. Each authorization code maps to the claims it returns."""

    def __init__(self):
        self.codes, self.challenges, self.down = {}, {}, False

    def post(self, url, timeout=None, data=None):
        if self.down:
            raise requests.ConnectionError("down")
        assert url == google_auth.TOKEN_URL
        code = data["code"]
        verifier_hash = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert verifier_hash == self.challenges[code], "PKCE verifier does not match the challenge"
        assert data["client_secret"] == "secret" and data["grant_type"] == "authorization_code"
        return mock.Mock(json=lambda: {"id_token": "%s.%s.sig" % (b64({"alg": "RS256"}), b64(self.codes[code]))})


class GoogleLoginTests(StoreTest):
    def setUp(self):
        settings(google_client_id=CLIENT_ID, google_client_secret="secret")
        self.google = FakeGoogle()
        p = mock.patch.object(google_auth.requests, "post", self.google.post)
        p.start()
        self.addCleanup(p.stop)

    def start(self, c, nxt=None):
        r = c.get("/auth/google" + ("?next=" + nxt if nxt else ""))
        self.assertEqual(r.status_code, 302)
        return {k: v[0] for k, v in parse_qs(urlparse(r.headers["Location"]).query).items()}

    def sign_in(self, c=None, code="code-1", nxt=None, state=None, **claim_changes):
        c = c or client()
        params = self.start(c, nxt)
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": int(time.time()) + 600,
                  "nonce": params["nonce"], "sub": "google-sub-1", "email": "player@gmail.com",
                  "email_verified": True, "name": "Google Player", "picture": "https://lh3.googleusercontent.com/a/x"}
        claims.update(claim_changes)
        for k in [k for k, v in claims.items() if v is None]:
            del claims[k]
        self.google.codes[code] = claims
        self.google.challenges[code] = params["code_challenge"]
        r = c.get("/social-callback?state=%s&code=%s" % (state or params["state"], code))
        return c, r

    def logged_in(self, c):
        return b"wallet-chip" in c.get("/").data

    def test_off_without_settings(self):
        settings(google_client_secret="")
        self.assertEqual(client().get("/auth/google").status_code, 404)
        self.assertNotIn("Continue with Google", client().get("/login").get_data(as_text=True))

    def test_redirect_to_google(self):
        self.assertIn("Continue with Google", client().get("/login").get_data(as_text=True))
        p = self.start(client())
        self.assertEqual((p["client_id"], p["response_type"], p["scope"], p["code_challenge_method"]),
                         (CLIENT_ID, "code", "openid email profile", "S256"))
        self.assertTrue(p["redirect_uri"].endswith("/social-callback"))
        self.assertTrue(p["state"] and p["nonce"] and p["code_challenge"])

    def test_new_account(self):
        run("DELETE FROM users WHERE email = 'player@gmail.com' OR google_sub = 'google-sub-1'")
        c, r = self.sign_in(nxt="/shop")
        self.assertTrue(r.headers["Location"].endswith("/shop"))
        self.assertTrue(self.logged_in(c))
        u = one("SELECT * FROM users WHERE google_sub = 'google-sub-1'")
        self.assertEqual((u["email"], u["name"], u["password_hash"]), ("player@gmail.com", "Google Player", "!google"))
        self.assertTrue(u["avatar"].startswith("https://"))
        c2, _ = self.sign_in(code="code-again")                                   # returning user: same account
        self.assertTrue(self.logged_in(c2))
        self.assertEqual(value("SELECT COUNT(*) FROM users WHERE google_sub = 'google-sub-1'"), 1)

    def test_existing_email_account_is_linked(self):
        uid = make_user("Has Password", "linkme@gmail.com")
        c, _ = self.sign_in(code="link", sub="sub-link", email="LinkMe@gmail.com")
        self.assertTrue(self.logged_in(c))
        self.assertEqual(value("SELECT google_sub FROM users WHERE id = ?", (uid,)), "sub-link")
        self.assertEqual(value("SELECT COUNT(*) FROM users WHERE lower(email) = 'linkme@gmail.com'"), 1)
        r = post(client(), "/login", {"identity": "linkme@gmail.com", "password": PASSWORD}, follow=False)
        self.assertEqual(r.status_code, 302)                                      # password login still works

    def test_rejected_tokens(self):
        cases = {
            "unverified": dict(email_verified=False),
            "wrong-app": dict(aud="someone-else.apps.googleusercontent.com"),
            "wrong-issuer": dict(iss="https://evil.example"),
            "expired": dict(exp=int(time.time()) - 10),
            "replayed-nonce": dict(nonce="not-the-one-we-sent"),
            "no-sub": dict(sub=None),
            "no-email": dict(email=None),
        }
        before = value("SELECT COUNT(*) FROM users")
        for name, change in cases.items():
            kw = {"sub": "sub-" + name, "email": name + "@gmail.com"}
            kw.update(change)
            c, r = self.sign_in(code="bad-" + name, **kw)
            self.assertIn("/login", r.headers["Location"], name)
            self.assertFalse(self.logged_in(c), name)
        c, r = self.sign_in(code="bad-state", sub="sub-state", email="state@gmail.com", state="forged")
        self.assertFalse(self.logged_in(c))
        c = client()
        self.start(c)
        r = c.get("/social-callback?error=access_denied")
        self.assertIn("বাতিল", " ".join(messages(c.get("/login"))) + " ".join(messages(r)))
        self.assertEqual(client().get("/social-callback?state=x&code=y").status_code, 302)   # no session: refused
        self.assertEqual(value("SELECT COUNT(*) FROM users"), before)

    def test_google_unreachable(self):
        self.google.down = True
        c, r = self.sign_in(code="down", sub="sub-down", email="down@gmail.com")
        self.assertIn("/login", r.headers["Location"])
        self.assertFalse(self.logged_in(c))

    def test_email_tied_to_another_google_account(self):
        run("INSERT INTO users (name, email, password_hash, google_sub) VALUES ('Taken', 'taken@gmail.com', '!google', 'sub-original')")
        c, r = self.sign_in(code="clash", sub="sub-impostor", email="taken@gmail.com")
        self.assertIn("অন্য একটি Google", " ".join(messages(c.get("/login"))))   # read before another page shows it
        self.assertFalse(self.logged_in(c))

    def test_banned_and_unsafe_picture(self):
        run("INSERT INTO users (name, email, password_hash, google_sub, is_banned) VALUES ('Ban', 'ban@gmail.com', '!google', 'sub-ban', 1)")
        c, _ = self.sign_in(code="ban", sub="sub-ban", email="ban@gmail.com")
        self.assertFalse(self.logged_in(c))
        c, _ = self.sign_in(code="pic", sub="sub-pic", email="pic@gmail.com", picture="http://insecure.example/x.png")
        self.assertTrue(self.logged_in(c))
        self.assertIsNone(value("SELECT avatar FROM users WHERE google_sub = 'sub-pic'"))

    def test_two_first_logins_at_once_make_one_account(self):
        pairs = []
        for i in range(2):
            c = client()
            params = self.start(c)
            code = "race-%d" % i
            self.google.codes[code] = {"iss": "accounts.google.com", "aud": CLIENT_ID, "exp": int(time.time()) + 600,
                                       "nonce": params["nonce"], "sub": "sub-race", "email": "race@gmail.com",
                                       "email_verified": "true", "name": "Racer"}
            self.google.challenges[code] = params["code_challenge"]
            pairs.append((c, "/social-callback?state=%s&code=%s" % (params["state"], code)))
        in_parallel(lambda c, url: c.get(url).status_code, pairs)
        self.assertEqual(value("SELECT COUNT(*) FROM users WHERE google_sub = 'sub-race'"), 1)
        self.assertTrue(all(self.logged_in(c) for c, _ in pairs))
