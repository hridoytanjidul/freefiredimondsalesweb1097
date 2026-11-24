"""Pages, uploads, the admin catalogue and settings, the Free Fire ID checker and the Vercel safety checks."""
import io
import os
import re
import subprocess
import sys
from unittest import mock

import requests

from support import (PNG, ROOT, StoreTest, VOUCHER, client, make_user, messages, one, pkg, post, product_id,
                     settings, value)

PUBLIC = ["/", "/shop", "/topup", "/topup/1", "/flashsale", "/leaderboard", "/free-fire-id-checker", "/contactus",
          "/about-us", "/terms-and-conditions", "/privacy-policy", "/refund-return-policy", "/delete-account-policy",
          "/tutorial", "/login", "/register", "/robots.txt", "/paymentsuccess", "/paymentfailed",
          "/static/css/style.css", "/static/js/app.js", "/static/img/favicon.svg"]
PROFILE = ["/profile", "/profile/addwallet", "/profile/order", "/profile/transaction", "/profile/giftvoucher"]
ADMIN = ["/admin/", "/admin/orders", "/admin/orders?f=all", "/admin/wallet", "/admin/wallet?status=approved", "/admin/products",
         "/admin/products/new", "/admin/products/1", "/admin/categories", "/admin/banners", "/admin/payment-methods",
         "/admin/users", "/admin/settings", "/admin/pages", "/admin/pages/about", "/admin/vouchers", "/admin/coupons",
         "/admin/coupons/new", "/admin/coupons?personal=1", "/admin/spin", "/admin/flash-sales", "/admin/flash-sales/new"]


def upload(admin, path, field, data=PNG, filename="pic.png", **fields):
    form = dict(fields)
    form[field] = (io.BytesIO(data), filename)
    return post(admin, path, form, content_type="multipart/form-data")


class PageTests(StoreTest):
    def test_every_page_opens(self):
        uid = make_user("Viewer")
        for path in PUBLIC:
            r = client().get(path)
            r.close()
            self.assertEqual(r.status_code, 200, path)
        for path in PROFILE:
            self.assertEqual(client(uid).get(path).status_code, 200, path)
        for path in ADMIN + ["/admin/users/%d" % uid]:
            self.assertEqual(self.admin.get(path).status_code, 200, path)

    def test_not_found_and_wrong_product_type(self):
        r = client().get("/no-such-page")
        self.assertEqual(r.status_code, 404)
        self.assertIn("খুঁজে পাওয়া যায়নি", r.get_data(as_text=True))
        self.assertEqual(client().get("/topup/99999").status_code, 404)
        r = client().get("/topup/%d" % product_id(VOUCHER))                 # voucher opened as a top-up link
        self.assertEqual(r.status_code, 301)
        self.assertIn("/giftvoucher/", r.headers["Location"])

    def test_home_shows_catalogue(self):
        page = client().get("/").get_data(as_text=True)
        for name in ("UID TopUp (BD)", "Weekly / Monthly", "Free Fire Voucher"):
            self.assertIn(name, page)
        self.assertIn("BDT 79", client().get("/topup/1").get_data(as_text=True))


class UploadTests(StoreTest):
    def test_banner_upload_served_from_database(self):
        upload(self.admin, "/admin/banners", "image", link="", sort_order="1")
        b = one("SELECT * FROM banners ORDER BY id DESC LIMIT 1")
        stored = one("SELECT mime, data FROM uploads WHERE name = ?", (b["image"],))
        self.assertEqual((stored["mime"], bytes(stored["data"])), ("image/png", PNG))
        self.assertIn("/media/" + b["image"], client().get("/").get_data(as_text=True))
        r = client().get("/media/" + b["image"])
        self.assertEqual((r.status_code, r.data, r.mimetype), (200, PNG, "image/png"))
        self.assertIn("s-maxage", r.headers["Cache-Control"])
        post(self.admin, "/admin/banners/%d/update" % b["id"], {"delete": "1"})
        r = client().get("/media/" + b["image"])
        self.assertEqual((r.status_code, r.mimetype), (404, "text/plain"))

    def test_only_real_images_are_accepted(self):
        before = value("SELECT COUNT(*) FROM uploads")
        for data, filename in ((b"<?php system($_GET['x']); ?>", "x.png"), (PNG, "x.svg"), (PNG, "x.html")):
            r = upload(self.admin, "/admin/banners", "image", data=data, filename=filename)
            self.assertTrue(messages(r), filename)
        self.assertEqual(value("SELECT COUNT(*) FROM uploads"), before)

    def test_logo_and_product_image(self):
        upload(self.admin, "/admin/settings", "logo", site_name="TopUp BD", theme_color="#663cbc",
               min_wallet_amount="20", max_wallet_amount="25000")
        logo = value("SELECT value FROM settings WHERE key = 'logo'")
        page = client().get("/").get_data(as_text=True)
        self.assertIn("/media/" + logo, page)
        og = re.search(r'property="og:image" content="([^"]+)"', page).group(1)
        self.assertEqual(client().get(og.replace("http://localhost", "")).status_code, 200)
        upload(self.admin, "/admin/products/1", "image", name="UID TopUp (BD)", category_id="1", kind="topup",
               numeric_id="1", is_active="1")
        img = value("SELECT image FROM products WHERE id = 1")
        self.assertEqual(client().get("/media/" + img).status_code, 200)


class AdminCatalogueTests(StoreTest):
    def test_categories(self):
        post(self.admin, "/admin/categories", {"name": "New Games", "sort_order": "9"})
        cid = value("SELECT id FROM categories WHERE name = 'New Games'")
        post(self.admin, "/admin/categories/%d/update" % cid, {"name": "Renamed", "sort_order": "9", "is_active": "1"})
        self.assertEqual(value("SELECT name FROM categories WHERE id = ?", (cid,)), "Renamed")
        r = post(self.admin, "/admin/categories/1/update", {"delete": "1"})       # has products
        self.assertIn("প্রোডাক্ট আছে", " ".join(messages(r)))
        self.assertIsNotNone(one("SELECT 1 FROM categories WHERE id = 1"))
        post(self.admin, "/admin/categories/%d/update" % cid, {"delete": "1"})
        self.assertIsNone(one("SELECT 1 FROM categories WHERE id = ?", (cid,)))

    def test_products_and_packages(self):
        r = post(self.admin, "/admin/products/new", {"name": "X", "category_id": "1"})
        self.assertIn("নাম", " ".join(messages(r)))
        post(self.admin, "/admin/products/new", {"name": "Test Product", "category_id": "1", "kind": "topup",
                                                 "numeric_id": "1", "is_active": "1"})
        pid = product_id("Test Product")
        r = post(self.admin, "/admin/products/%d/packages" % pid, {"name": "Pack", "price": "0"})
        self.assertIn("দাম", " ".join(messages(r)))
        post(self.admin, "/admin/products/%d/packages" % pid, {"name": "Pack", "price": "55", "in_stock": "1"})
        kid = pkg("Test Product", "Pack")
        post(self.admin, "/admin/packages/%d/update" % kid, {"name": "Pack", "price": "60", "in_stock": "1"})
        self.assertEqual(value("SELECT price FROM packages WHERE id = ?", (kid,)), 60)
        self.assertIn("Test Product", client().get("/shop").get_data(as_text=True))
        uid = make_user("Bought it", balance=100)
        post(client(uid), "/topup/%d/order" % pid, {"package_id": kid, "player_id": "1234567890", "pay_option": "wallet"})
        post(self.admin, "/admin/products/%d/delete" % pid, {})
        self.assertIsNone(one("SELECT 1 FROM products WHERE id = ?", (pid,)))
        o = one("SELECT * FROM orders WHERE user_id = ?", (uid,))
        self.assertEqual((o["product_name"], o["package_name"], o["price"]), ("Test Product", "Pack", 60))   # old orders stay

    def test_pages_and_payment_numbers(self):
        post(self.admin, "/admin/pages/about", {"title": "About Our Shop", "content": "<p>We sell diamonds.</p>"})
        page = client().get("/about-us").get_data(as_text=True)
        self.assertIn("About Our Shop", page)
        self.assertIn("We sell diamonds.", page)
        mid = value("SELECT id FROM payment_methods WHERE name = 'bKash'")
        post(self.admin, "/admin/payment-methods/%d/update" % mid,
             {"name": "bKash", "number": "01700000002", "account_type": "Personal", "color": "#e2136e", "is_active": "1"})
        self.assertIn("01700000002", client(make_user("Payer")).get("/profile/addwallet").get_data(as_text=True))
        r = post(self.admin, "/admin/payment-methods/%d/update" % mid, {"name": "bKash", "number": ""})
        self.assertIn("নম্বর দিন", " ".join(messages(r)))
        self.assertEqual(value("SELECT number FROM payment_methods WHERE id = ?", (mid,)), "01700000002")

    def test_settings_validation(self):
        base = {"site_name": "My Store", "theme_color": "#112233", "min_wallet_amount": "50", "max_wallet_amount": "5000",
                "telegram_support": "https://t.me/mystore"}
        cases = [(dict(site_name=""), "নাম"), (dict(theme_color="red"), "কালার"), (dict(min_wallet_amount="900", max_wallet_amount="100"), "Add Money"),
                 (dict(telegram_support="javascript:alert(1)"), "https://"), (dict(uddoktapay_base_url="ftp://x"), "https://")]
        for bad, expected in cases:
            r = post(self.admin, "/admin/settings", dict(base, **bad))
            self.assertIn(expected, " ".join(messages(r)), bad)
        post(self.admin, "/admin/settings", base)
        page = client().get("/").get_data(as_text=True)
        self.assertIn("My Store", page)
        self.assertIn("--primary: #112233", page)
        self.assertIn("https://t.me/mystore", page)
        self.assertEqual(value("SELECT value FROM settings WHERE key = 'min_wallet_amount'"), "50")


class IdCheckerTests(StoreTest):
    def test_off(self):
        settings(ff_id_checker_url="")
        self.assertEqual(client().get("/api/check-id?uid=1234567890").status_code, 503)

    def test_lookups(self):
        settings(ff_id_checker_url="https://checker.example/info?uid={uid}")
        reply = mock.Mock(json=lambda: {"data": {"basicInfo": {"nickname": "ProPlayer"}}})
        with mock.patch("requests.get", return_value=reply) as get:
            d = client().get("/api/check-id?uid=1234567890").get_json()
        self.assertEqual(d, {"ok": True, "name": "ProPlayer"})
        self.assertEqual(get.call_args[0][0], "https://checker.example/info?uid=1234567890")
        self.assertEqual(client().get("/api/check-id?uid=12").status_code, 400)
        with mock.patch("requests.get", return_value=mock.Mock(json=lambda: {"error": "none"})):
            self.assertEqual(client().get("/api/check-id?uid=1234567890").status_code, 404)
        with mock.patch("requests.get", side_effect=requests.ConnectionError()):
            self.assertEqual(client().get("/api/check-id?uid=1234567890").status_code, 502)


class VercelGuardTests(StoreTest):
    def start_app(self, **env):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("DATABASE_URL", "POSTGRES_URL", "DATABASE_PATH", "SECRET_KEY", "VERCEL")}
        clean.update(env)
        return subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=clean, capture_output=True, text=True)

    def test_refuses_to_start_without_database_or_secret(self):
        p = self.start_app(VERCEL="1", SECRET_KEY="x")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Set DATABASE_URL", p.stderr)
        p = self.start_app(VERCEL="1", DATABASE_URL="postgresql://u@127.0.0.1:1/x")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SECRET_KEY", p.stderr)
