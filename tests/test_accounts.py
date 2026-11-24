"""Accounts: register, login, logout, profile, password, CSRF, redirects and access control."""
from support import (PASSWORD, StoreTest, client, make_user, messages, one, post, run, token, value)


def register(c, **fields):
    data = {"name": "Rahim Uddin", "email": "rahim@x.com", "phone": "", "password": "secret1", "password2": "secret1"}
    data.update(fields)
    return post(c, "/register", data)


def login(c, identity, password=PASSWORD, nxt=None):
    data = {"identity": identity, "password": password}
    if nxt:
        data["next"] = nxt
    return post(c, "/login", data, follow=False)


class RegisterTests(StoreTest):
    def test_register_logs_in_and_stores_lowercase_email(self):
        c = client()
        r = register(c, email="Rahim.New@X.com", phone="01712345678")
        self.assertIn("অ্যাকাউন্ট তৈরি হয়েছে", " ".join(messages(r)))
        u = one("SELECT * FROM users WHERE email = 'rahim.new@x.com'")
        self.assertIsNotNone(u)
        self.assertEqual(u["phone"], "01712345678")
        self.assertEqual(u["balance"], 0)
        self.assertIn(b"wallet-chip", c.get("/").data)          # logged in

    def test_register_rejects_bad_input(self):
        make_user("Taken", "taken@x.com", phone="01811111111")
        cases = [
            ({"name": "A"}, "নাম"),
            ({"email": "not-an-email"}, "ইমেইল"),
            ({"phone": "12345"}, "মোবাইল"),
            ({"password": "123", "password2": "123"}, "৬ অক্ষর"),
            ({"password2": "different"}, "মিলছে না"),
            ({"email": "TAKEN@x.com"}, "আগেই অ্যাকাউন্ট"),
            ({"phone": "01811111111", "email": "fresh@x.com"}, "আগেই অ্যাকাউন্ট"),
        ]
        before = value("SELECT COUNT(*) FROM users")
        for fields, expected in cases:
            r = register(client(), **dict({"email": "bad%d@x.com" % len(fields)}, **fields))
            self.assertIn(expected, " ".join(messages(r)), fields)
        self.assertEqual(value("SELECT COUNT(*) FROM users"), before)


class LoginTests(StoreTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uid = make_user("Karim", "karim@x.com", phone="01922222222")

    def test_login_by_email_any_case_and_by_phone(self):
        for ident in ("karim@x.com", "KARIM@X.COM", "01922222222", "019-2222-2222"):
            c = client()
            r = login(c, ident)
            self.assertEqual(r.status_code, 302, ident)
            self.assertIn(b"wallet-chip", c.get("/").data, ident)

    def test_wrong_password_and_unknown_user(self):
        for ident, pw in (("karim@x.com", "wrong-pass"), ("nobody@x.com", PASSWORD)):
            c = client()
            r = post(c, "/login", {"identity": ident, "password": pw})
            self.assertIn("ভুল", " ".join(messages(r)))
            self.assertNotIn(b"wallet-chip", c.get("/").data)

    def test_banned_user_cannot_log_in_and_is_logged_out(self):
        uid = make_user("Banned", "banned@x.com")
        c = client(uid)
        self.assertIn(b"wallet-chip", c.get("/").data)
        run("UPDATE users SET is_banned = 1 WHERE id = ?", (uid,))
        self.assertNotIn(b"wallet-chip", c.get("/").data)             # thrown out on the next page
        r = post(client(), "/login", {"identity": "banned@x.com", "password": PASSWORD})
        self.assertIn("বন্ধ", " ".join(messages(r)))

    def test_google_only_account_gets_hint(self):
        run("INSERT INTO users (name, email, password_hash, google_sub) VALUES ('G', 'g@x.com', '!google', 'sub-1')")
        r = post(client(), "/login", {"identity": "g@x.com", "password": "anything"})
        self.assertIn("Google", " ".join(messages(r)))

    def test_next_only_redirects_inside_the_site(self):
        cases = {"/shop": "/shop", "//evil.com": "/", "https://evil.com": "/", "/\\evil.com": "/"}
        for nxt, expected in cases.items():
            r = login(client(), "karim@x.com", nxt=nxt)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith(expected), (nxt, r.headers["Location"]))
            self.assertNotIn("evil", r.headers["Location"])

    def test_logout(self):
        c = client(self.uid)
        post(c, "/logout")
        self.assertNotIn(b"wallet-chip", c.get("/").data)
        self.assertEqual(c.get("/profile").status_code, 302)

    def test_login_rotates_csrf_token(self):
        c = client()
        before = token(c)
        login(c, "karim@x.com")
        self.assertNotEqual(token(c), before)


class ProfileTests(StoreTest):
    def test_edit_name_and_phone(self):
        uid = make_user("Old Name")
        make_user("Other", phone="01733333333")
        c = client(uid)
        post(c, "/profile", {"action": "info", "name": "New Name", "phone": "01744444444"})
        u = one("SELECT name, phone FROM users WHERE id = ?", (uid,))
        self.assertEqual((u["name"], u["phone"]), ("New Name", "01744444444"))
        r = post(c, "/profile", {"action": "info", "name": "New Name", "phone": "01733333333"})
        self.assertIn("অন্য অ্যাকাউন্টে", " ".join(messages(r)))
        self.assertEqual(value("SELECT phone FROM users WHERE id = ?", (uid,)), "01744444444")

    def test_change_password_needs_current_one(self):
        uid = make_user("Pw", "pw@x.com")
        c = client(uid)
        r = post(c, "/profile", {"action": "password", "current": "wrong", "new": "newpass1", "new2": "newpass1"})
        self.assertIn("ভুল", " ".join(messages(r)))
        post(c, "/profile", {"action": "password", "current": PASSWORD, "new": "newpass1", "new2": "newpass1"})
        self.assertEqual(login(client(), "pw@x.com", "newpass1").status_code, 302)
        self.assertNotEqual(login(client(), "pw@x.com", PASSWORD).status_code, 302)

    def test_google_user_can_set_a_password(self):
        uid = run("INSERT INTO users (name, email, password_hash, google_sub) VALUES ('Gee', 'gee@x.com', '!google', 'sub-9')")
        post(client(uid), "/profile", {"action": "password", "new": "mypass12", "new2": "mypass12"})
        self.assertEqual(login(client(), "gee@x.com", "mypass12").status_code, 302)


class SecurityTests(StoreTest):
    def test_post_without_or_with_wrong_csrf_is_rejected(self):
        uid = make_user("Csrf")
        c = client(uid)
        self.assertEqual(c.post("/profile", data={"action": "info", "name": "Hacked"}).status_code, 400)
        self.assertEqual(c.post("/profile", data={"action": "info", "name": "Hacked", "csrf_token": "nope"}).status_code, 400)
        self.assertEqual(value("SELECT name FROM users WHERE id = ?", (uid,)), "Csrf")

    def test_access_control(self):
        cust = client(make_user("Cust"))
        visitor = client()
        for path in ("/profile", "/profile/order", "/profile/addwallet", "/profile/transaction", "/profile/giftvoucher"):
            r = visitor.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"])
        for path in ("/admin/", "/admin/orders", "/admin/settings", "/admin/users"):
            self.assertEqual(cust.get(path).status_code, 403, path)
            self.assertEqual(visitor.get(path).status_code, 302, path)
            self.assertEqual(self.admin.get(path).status_code, 200, path)
        self.assertEqual(cust.post("/admin/users/1/toggle", data={"field": "is_admin", "csrf_token": token(cust)}).status_code, 403)

    def test_admin_cannot_change_own_admin_or_ban(self):
        for field in ("is_admin", "is_banned"):
            post(self.admin, "/admin/users/%d/toggle" % self.admin_id, {"field": field})
        u = one("SELECT is_admin, is_banned FROM users WHERE id = ?", (self.admin_id,))
        self.assertEqual((u["is_admin"], u["is_banned"]), (1, 0))

    def test_admin_can_make_and_remove_admin(self):
        uid = make_user("Helper")
        post(self.admin, "/admin/users/%d/toggle" % uid, {"field": "is_admin"})
        self.assertEqual(client(uid).get("/admin/").status_code, 200)
        post(self.admin, "/admin/users/%d/toggle" % uid, {"field": "is_admin"})
        self.assertEqual(client(uid).get("/admin/").status_code, 403)

    def test_security_headers(self):
        r = client().get("/")
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertTrue(r.headers.get("Referrer-Policy"))
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
