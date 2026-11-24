"""Coupons (every rule, preview API, cancel, racing buyers) and gift vouchers (code upload,
instant and manual delivery, cancel, racing for the last code, package deletion)."""
from support import (UID_TOPUP, VOUCHER, StoreTest, admin_order, ago, balance, buy, client, in_parallel, last_order,
                     make_user, messages, one, pkg, post, product_id, rows, run, value)


def make_coupon(code, dtype="percent", val=10, **fields):
    cols = {"code": code, "discount_type": dtype, "value": val}
    cols.update(fields)
    return run("INSERT INTO coupons (%s) VALUES (%s)" % (", ".join(cols), ",".join("?" * len(cols))), tuple(cols.values()))


def preview(c, code, package_id):
    return post(c, "/api/coupon/check", {"code": code, "package_id": package_id}, follow=False)


class CouponTests(StoreTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pid = product_id(UID_TOPUP)
        cls.p25, cls.p115, cls.p240 = (pkg(UID_TOPUP, n) for n in ("25 Diamond", "115 Diamond", "240 Diamond"))  # 22, 79, 158

    def test_admin_create_validate_edit_delete(self):
        base = {"code": "save10", "discount_type": "percent", "value": "10", "max_discount": "0", "min_order": "0",
                "usage_limit": "0", "per_user_limit": "1", "is_active": "1", "is_public": "1"}
        post(self.admin, "/admin/coupons/new", base)
        c = one("SELECT * FROM coupons WHERE code = 'SAVE10'")
        self.assertIsNotNone(c)                                          # codes are stored in capitals
        for bad, expected in ((dict(code="Save10"), "আগেই আছে"), (dict(code="a b!"), "অক্ষরের"),
                              (dict(code="PCT", value="150"), "1-100"),
                              (dict(code="TIME", starts_at="2030-01-02T10:00", ends_at="2030-01-01T10:00"), "পরে")):
            r = post(self.admin, "/admin/coupons/new", dict(base, **bad))
            self.assertIn(expected, " ".join(messages(r)), bad)
        self.assertEqual(value("SELECT COUNT(*) FROM coupons WHERE code IN ('PCT', 'TIME')"), 0)
        post(self.admin, "/admin/coupons/%d" % c["id"], dict(base, value="15"))
        self.assertEqual(value("SELECT value FROM coupons WHERE id = ?", (c["id"],)), 15)
        post(self.admin, "/admin/coupons/%d" % c["id"], {"delete": "1"})
        self.assertIsNone(one("SELECT 1 FROM coupons WHERE id = ?", (c["id"],)))

    def test_discount_rules(self):
        uid = make_user("Rules")
        other = make_user("Someone else")
        c = client(uid)
        make_coupon("PCT10", "percent", 10)
        make_coupon("CAP50", "percent", 50, max_discount=20)
        make_coupon("FIX50", "fixed", 50)
        make_coupon("FIX500", "fixed", 500)
        make_coupon("MIN100", "fixed", 10, min_order=100)
        make_coupon("ONLYVOUCH", "fixed", 5, product_id=product_id(VOUCHER))
        make_coupon("LATER", "fixed", 5, starts_at=ago(hours=-5))
        make_coupon("OLD", "fixed", 5, ends_at=ago(hours=1))
        make_coupon("OFF", "fixed", 5, is_active=0)
        make_coupon("GONE", "fixed", 5, usage_limit=3, used=3)
        make_coupon("THEIRS", "fixed", 5, user_id=other)
        make_coupon("MINE", "fixed", 7, user_id=uid)
        ok = {("PCT10", self.p240): 15, ("CAP50", self.p240): 20, ("FIX50", self.p240): 50,
              ("FIX500", self.p25): 21, ("MIN100", self.p240): 10, ("pct10", self.p240): 15, ("MINE", self.p240): 7}
        for (code, k), off in ok.items():
            d = preview(c, code, k).get_json()
            self.assertTrue(d["ok"], (code, d))
            self.assertEqual(d["discount"], off, code)
            self.assertEqual(d["final"], d["price"] - off)
            self.assertGreaterEqual(d["final"], 1)                          # never free
        bad = {"MIN100": (self.p115, "কমপক্ষে"), "ONLYVOUCH": (self.p240, "এই প্রোডাক্টে"), "LATER": (self.p240, "চালু হয়নি"),
               "OLD": (self.p240, "মেয়াদ"), "OFF": (self.p240, "সঠিক নয়"), "GONE": (self.p240, "সীমা শেষ"),
               "THEIRS": (self.p240, "সঠিক নয়"), "NOPE": (self.p240, "সঠিক নয়")}
        for code, (k, expected) in bad.items():
            d = preview(c, code, k).get_json()
            self.assertFalse(d["ok"], code)
            self.assertIn(expected, d["error"], code)
        self.assertEqual(preview(client(), "PCT10", self.p240).status_code, 401)   # visitors must log in
        self.assertEqual(preview(c, "x!", self.p240).status_code, 400)

    def test_order_with_coupon_and_cancel(self):
        cid = make_coupon("ORDER20", "percent", 20, per_user_limit=1, is_public=1)
        uid = make_user("Coupon buyer", balance=500)
        c = client(uid)
        self.assertIn('data-coupon="ORDER20"', c.get("/topup/%d" % self.pid).get_data(as_text=True))
        buy(c, self.pid, self.p240, coupon_code="order20")
        o = last_order(uid)
        self.assertEqual((o["price"], o["discount"], o["coupon_id"], o["coupon_code"]), (127, 31, cid, "ORDER20"))
        self.assertEqual(balance(uid), 373)
        self.assertEqual(value("SELECT used FROM coupons WHERE id = ?", (cid,)), 1)
        r = buy(c, self.pid, self.p240, coupon_code="ORDER20")                 # once per customer
        self.assertIn("সর্বোচ্চ", " ".join(messages(r)))
        self.assertEqual(balance(uid), 373)
        admin_order(self.admin, o["id"], "cancel", refund=True)
        self.assertEqual(balance(uid), 500)                                     # refund = what was paid
        self.assertEqual(value("SELECT used FROM coupons WHERE id = ?", (cid,)), 0)
        buy(c, self.pid, self.p240, coupon_code="ORDER20")                      # usable again after cancel
        self.assertEqual(last_order(uid)["coupon_id"], cid)

    def test_bad_coupon_blocks_the_order_without_charging(self):
        uid = make_user("Bad code", balance=500)
        before = value("SELECT COUNT(*) FROM orders")
        r = buy(client(uid), self.pid, self.p240, coupon_code="DOESNOTEXIST")
        self.assertIn("সঠিক নয়", " ".join(messages(r)))
        self.assertEqual(value("SELECT COUNT(*) FROM orders"), before)
        self.assertEqual(balance(uid), 500)

    def test_personal_coupon_only_for_its_owner(self):
        owner, other = make_user("Owner", balance=500), make_user("Other", balance=500)
        make_coupon("PERSONAL1", "fixed", 10, user_id=owner, usage_limit=1)
        self.assertIn('data-coupon="PERSONAL1"', client(owner).get("/topup/%d" % self.pid).get_data(as_text=True))
        self.assertNotIn("PERSONAL1", client(other).get("/topup/%d" % self.pid).get_data(as_text=True))
        r = buy(client(other), self.pid, self.p240, coupon_code="PERSONAL1")
        self.assertIn("সঠিক নয়", " ".join(messages(r)))
        buy(client(owner), self.pid, self.p240, coupon_code="PERSONAL1")
        self.assertEqual(last_order(owner)["price"], 148)

    def test_limited_coupon_cannot_be_overused(self):
        cid = make_coupon("RACE2", "fixed", 10, usage_limit=2, per_user_limit=0)
        buyers = [make_user("Coupon racer %d" % i, balance=200) for i in range(8)]
        in_parallel(lambda uid: buy(client(uid), self.pid, self.p240, coupon_code="RACE2").status_code,
                    [(u,) for u in buyers])
        self.assertEqual(value("SELECT used FROM coupons WHERE id = ?", (cid,)), 2)
        self.assertEqual(value("SELECT COUNT(*) FROM orders WHERE coupon_id = ?", (cid,)), 2)
        self.assertEqual(sorted(balance(u) for u in buyers).count(200 - 148), 2)
        self.assertEqual(sum(1 for u in buyers if balance(u) == 200), 6)        # the rest were not charged


class VoucherTests(StoreTest):
    def setUp(self):
        self.vid = product_id(VOUCHER)
        self.k25 = pkg(VOUCHER, "25 Diamond Voucher")    # ৳22
        run("DELETE FROM voucher_codes WHERE package_id = ? AND status = 'available'", (self.k25,))

    def add_codes(self, *codes, kid=None):
        return post(self.admin, "/admin/packages/%d/codes" % (kid or self.k25), {"action": "add", "codes": "\n".join(codes)})

    def stock(self, kid=None):
        return value("SELECT COUNT(*) FROM voucher_codes WHERE package_id = ? AND status = 'available'", (kid or self.k25,))

    def test_upload_skips_duplicates(self):
        r = self.add_codes("AAA-1", "BBB-2", "AAA-1", "  ", "CCC-3")
        self.assertIn("3 টি কোড যোগ", " ".join(messages(r)))
        r = self.add_codes("CCC-3", "DDD-4")
        self.assertIn("1 টি কোড যোগ", " ".join(messages(r)))
        self.assertEqual(self.stock(), 4)

    def test_no_codes_means_stock_out(self):
        uid = make_user("Too early", balance=100)
        self.assertIn("Stock Out", client(uid).get("/giftvoucher/%d" % self.vid).get_data(as_text=True))
        r = buy(client(uid), self.vid, self.k25, player_id="")
        self.assertIn("স্টকে নেই", " ".join(messages(r)))
        self.assertEqual(balance(uid), 100)

    def test_wallet_purchase_delivers_code_instantly(self):
        self.add_codes("WALLET-CODE-1", "WALLET-CODE-2")
        uid = make_user("Instant", balance=100)
        c = client(uid)
        r = buy(c, self.vid, self.k25, player_id="")
        self.assertIn("/profile/giftvoucher", r.request.path)
        o = last_order(uid)
        self.assertEqual((o["status"], o["payment_status"], o["voucher_code"], o["player_id"], o["price"]),
                         ("completed", "paid", "WALLET-CODE-1", "", 22))
        self.assertEqual(value("SELECT status FROM voucher_codes WHERE code = 'WALLET-CODE-1'"), "delivered")
        self.assertIn("WALLET-CODE-1", c.get("/profile/giftvoucher").get_data(as_text=True))
        buy(c, self.vid, self.k25, player_id="")
        self.assertEqual(last_order(uid)["voucher_code"], "WALLET-CODE-2")    # next code, never the same one
        self.assertEqual(balance(uid), 56)

    def test_manual_purchase_holds_code_until_verified(self):
        self.add_codes("HOLD-1", "HOLD-2")
        uid = make_user("Manual voucher")
        c = client(uid)
        buy(c, self.vid, self.k25, player_id="", pay="manual", method_id="1", sender_number="01711000000", trx_id="VCHTRX0001")
        o = last_order(uid)
        self.assertEqual(value("SELECT status FROM voucher_codes WHERE order_id = ?", (o["id"],)), "reserved")
        self.assertNotIn("HOLD-1", c.get("/profile/giftvoucher").get_data(as_text=True))
        admin_order(self.admin, o["id"], "verify")
        o = one("SELECT * FROM orders WHERE id = ?", (o["id"],))
        self.assertEqual((o["status"], o["voucher_code"]), ("completed", "HOLD-1"))
        self.assertIn("HOLD-1", c.get("/profile/giftvoucher").get_data(as_text=True))

        buy(c, self.vid, self.k25, player_id="", pay="manual", method_id="1", sender_number="01711000000", trx_id="VCHTRX0002")
        o2 = last_order(uid)
        self.assertEqual(self.stock(), 0)                                    # HOLD-2 is held for this order
        admin_order(self.admin, o2["id"], "cancel", note="No payment", refund=True)
        self.assertEqual(self.stock(), 1)                                    # back in stock
        self.assertEqual(value("SELECT status FROM voucher_codes WHERE code = 'HOLD-2'"), "available")
        self.assertEqual(balance(uid), 0)

    def test_many_buyers_one_code(self):
        self.add_codes("LAST-ONE")
        buyers = [make_user("Voucher racer %d" % i, balance=100) for i in range(6)]
        in_parallel(lambda uid: buy(client(uid), self.vid, self.k25, player_id="").status_code, [(u,) for u in buyers])
        winners = rows("SELECT user_id FROM orders WHERE voucher_code = 'LAST-ONE'")
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(1 for u in buyers if balance(u) == 100), 5)    # nobody else was charged
        self.assertEqual(value("SELECT COUNT(*) FROM voucher_codes WHERE code = 'LAST-ONE'"), 1)

    def test_code_management_and_package_deletion(self):
        kid = run("INSERT INTO packages (product_id, name, price) VALUES (?, 'Temp Voucher', 10)", (self.vid,))
        self.add_codes("TEMP-1", "TEMP-2", "TEMP-3", kid=kid)
        uid = make_user("Keeper", balance=100)
        buy(client(uid), self.vid, kid, player_id="")
        delivered = value("SELECT id FROM voucher_codes WHERE code = 'TEMP-1'")
        spare = value("SELECT id FROM voucher_codes WHERE code = 'TEMP-2'")
        post(self.admin, "/admin/packages/%d/codes" % kid, {"action": "delete", "code_id": str(delivered)})
        post(self.admin, "/admin/packages/%d/codes" % kid, {"action": "delete", "code_id": str(spare)})
        self.assertIsNotNone(one("SELECT 1 FROM voucher_codes WHERE id = ?", (delivered,)))   # sold codes stay
        self.assertIsNone(one("SELECT 1 FROM voucher_codes WHERE id = ?", (spare,)))
        self.assertIn("Temp Voucher", self.admin.get("/admin/").get_data(as_text=True))       # low-stock warning
        post(self.admin, "/admin/packages/%d/update" % kid, {"delete": "1"})
        self.assertIsNone(one("SELECT 1 FROM packages WHERE id = ?", (kid,)))
        self.assertIn("TEMP-1", client(uid).get("/profile/giftvoucher").get_data(as_text=True))  # customer keeps it
