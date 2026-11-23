"""Sign in with Google (OpenID Connect authorization-code flow with PKCE).

Setup: Google Cloud Console -> APIs & Services -> Credentials -> Create OAuth client ID
(type "Web application"). Add the redirect URI shown in Admin -> Settings -> Google Login,
then paste the Client ID and Client Secret there.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, flash, g, redirect, request, session, url_for

from db import tx
from helpers import safe_next

bp = Blueprint("google", __name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class GoogleLoginError(Exception):
    pass


def enabled(settings):
    return bool((settings.get("google_client_id") or "").strip()
                and (settings.get("google_client_secret") or "").strip())


def redirect_uri():
    return url_for("google.callback", _external=True)


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else None
    except (AttributeError, IndexError, ValueError):
        return None


def _fail(message="Google দিয়ে লগইন করা যায়নি। আবার চেষ্টা করুন।"):
    flash(message, "error")
    return redirect(url_for("login"))


@bp.get("/auth/google")
def login():
    s = g.settings
    if not enabled(s):
        abort(404)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session["g_oauth"] = {"state": state, "nonce": nonce, "verifier": verifier,
                          "next": safe_next(request.args.get("next"))}
    params = {
        "client_id": s["google_client_id"].strip(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return redirect(AUTH_URL + "?" + urlencode(params))


@bp.get("/social-callback")
def callback():
    s = g.settings
    data = session.pop("g_oauth", None)
    if not enabled(s) or not data:
        return _fail()
    if request.args.get("error"):
        return _fail("Google লগইন বাতিল করা হয়েছে।")
    if not hmac.compare_digest(request.args.get("state", ""), data["state"]):
        return _fail()
    code = request.args.get("code")
    if not code:
        return _fail()

    client_id = s["google_client_id"].strip()
    try:
        r = requests.post(TOKEN_URL, timeout=15, data={
            "code": code,
            "client_id": client_id,
            "client_secret": s["google_client_secret"].strip(),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
            "code_verifier": data["verifier"],
        })
        token = r.json()
    except (requests.RequestException, ValueError):
        return _fail()
    claims = _jwt_claims(token.get("id_token")) if isinstance(token, dict) else None
    if not claims:
        return _fail()

    # The ID token came straight from Google's token endpoint over TLS, authenticated with our
    # client secret, so (OpenID Connect Core 3.1.3.7) we validate its claims instead of its signature.
    aud = claims.get("aud")
    valid = (
        (aud == client_id or (isinstance(aud, list) and client_id in aud))
        and claims.get("iss") in ISSUERS
        and int(claims.get("exp") or 0) > time.time()
        and hmac.compare_digest(str(claims.get("nonce") or ""), data["nonce"])
        and claims.get("email_verified") in (True, "true")
        and claims.get("sub") and claims.get("email")
    )
    if not valid:
        return _fail()

    try:
        user = _find_or_create(claims)
    except GoogleLoginError as e:
        return _fail(str(e))
    if user["is_banned"]:
        return _fail("আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে। সাপোর্টে যোগাযোগ করুন।")

    nxt = data.get("next")
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    flash("স্বাগতম, %s!" % user["name"], "success")
    return redirect(nxt or url_for("home"))


def _find_or_create(claims):
    sub = str(claims["sub"])
    email = str(claims["email"]).strip().lower()[:120]
    name = str(claims.get("name") or email.split("@")[0]).strip()[:60] or "Player"
    picture = claims.get("picture")
    picture = picture if isinstance(picture, str) and picture.startswith("https://") else None

    with tx() as db:
        user = db.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
        if user is None:
            user = db.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
            if user is not None:
                if user["google_sub"] and user["google_sub"] != sub:
                    raise GoogleLoginError("এই ইমেইলটি অন্য একটি Google অ্যাকাউন্টের সাথে যুক্ত।")
                # Google has verified this email, so it is safe to link it to the existing account.
                db.execute("UPDATE users SET google_sub = ? WHERE id = ?", (sub, user["id"]))
            else:
                # "!google" is not a valid hash, so password login stays off until they set one.
                uid = db.execute(
                    "INSERT INTO users (name, email, password_hash, google_sub, avatar) VALUES (?,?,?,?,?)",
                    (name, email, "!google", sub, picture)).lastrowid
                user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if picture and user["avatar"] != picture:
            db.execute("UPDATE users SET avatar = ? WHERE id = ?", (picture, user["id"]))
        return db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
