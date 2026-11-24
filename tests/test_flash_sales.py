"""Flash sales: admin setup, sale states, sale price, per-customer limit, no coupon stacking,
stock returned on cancel or abandoned payment, and many buyers racing for the last items."""
from datetime import datetime, timedelta

from support import (UID_TOPUP, StoreTest, admin_order, ago, balance, client, in_parallel, last_order, make_user,
                     messages, one, pkg, post, run, value)


def bd_input(**delta):
    """A datetime-local value in Bangladesh time, as the admin form sends it."""
    return (datetime.utcnow() + timedelta(hours=6) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M")


def make_sale(package_id, price=30, stock=5, per_user=1, start=None, end=None, active=1):
    return run("INSERT INTO flash_sales (package_id, sale_price, stock, per_user_limit, starts_at, ends_at, is_active) "
               "VALUES (?,?,?,?,?,?,?)", (package_id, price, stock, per_user, start or ago(minutes=5),
                                          end or ago(hours=-2), active))


def flash_buy(c, fid, **extra):
    data = {"player_id": "1234567890", "pay_option": "wallet"}
    data.update(extra)
    return post(c, "/flashsale/%d/order" % fid, data)


class FlashSaleTests(StoreTest):
    def setUp(self):
        self.p50 = pkg(UID_TOPUP, "50 Diamond")        # regular ৳40

    def test_admin_creates_sale_and_validation(self):
        base = {"package_id": str(self.p50), "sale_price": "30", "stock": "5", "per_user_limit": "1",
                "starts_at": bd_input(minutes=-1), "ends_at": bd_input(hours=3), "is_active": "1"}
        before = value("SELECT COUNT(*) FROM flash_sales")
        for bad, expected in ((dict(sale_price=""), "দাম"), (dict(stock="0"), "Stock"),
                              (dict(ends_at=bd_input(hours=-3)), "পরে"), (dict(package_id="0"), "প্যাকেজ")):
            r = post(self.admin, "/admin/flash-sales/new", dict(base, **bad))
            self.assertIn(expected, " ".join(messages(r)), bad)
        self.assertEqual(value("SELECT COUNT(*) FROM flash_sales"), before)
        post(self.admin, "/admin/flash-sales/new", base)
        f = one("SELECT * FROM flash_sales ORDER BY id DESC LIMIT 1")
        self.assertEqual((f["package_id"], f["sale_price"], f["stock"], f["sold"]), (self.p50, 30, 5, 0))
        starts = datetime.strptime(f["starts_at"], "%Y-%m-%d %H:%M:%S")
        self.assertLess(abs((datetime.utcnow() - starts).total_seconds()), 180)   # BD time typed, stored as UTC

    def test_live_sale_sells_at_sale_price_once_per_customer(self):
        fid = make_sale(self.p50, price=30, stock=5, per_user=1)
        uid = make_user("Flash buyer", balance=200)
        c = client(uid)
        self.assertIn("50 Diamond", client().get("/").get_data(as_text=True))
        self.assertEqual(client().get("/flashsale/%d" % fid).status_code, 200)
        flash_buy(c, fid)
        o = last_order(uid)
        self.assertEqual((o["price"], o["flash_sale_id"], o["package_id"]), (30, fid, self.p50))
        self.assertEqual(balance(uid), 170)
        r = flash_buy(c, fid)                                            # limit 1 per customer
        self.assertIn("সর্বোচ্চ 1", " ".join(messages(r)))
        self.assertEqual(value("SELECT sold FROM flash_sales WHERE id = ?", (fid,)), 1)
        self.assertEqual(balance(uid), 170)
        self.assertIn("#%d" % o["id"], self.admin.get("/admin/orders?f=all&flash=%d" % fid).get_data(as_text=True))

    def test_unlimited_per_customer(self):
        fid = make_sale(self.p50, stock=3, per_user=0)
        c = client(make_user("Bulk", balance=500))
        for _ in range(3):
            flash_buy(c, fid)
        r = flash_buy(c, fid)
        self.assertIn("স্টক শেষ", " ".join(messages(r)))
        self.assertEqual(value("SELECT sold FROM flash_sales WHERE id = ?", (fid,)), 3)

    def test_not_started_ended_and_inactive(self):
        upcoming = make_sale(self.p50, start=ago(hours=-1), end=ago(hours=-3))
        ended = make_sale(self.p50, start=ago(hours=3), end=ago(hours=1))
        hidden = make_sale(self.p50, active=0)
        c = client(make_user("Early", balance=500))
        self.assertIn("শুরু হয়নি", " ".join(messages(flash_buy(c, upcoming))))
        self.assertIn("শেষ", " ".join(messages(flash_buy(c, ended))))
        self.assertEqual(flash_buy(c, hidden).status_code, 404)
        self.assertEqual(value("SELECT SUM(sold) FROM flash_sales WHERE id IN (?,?,?)", (upcoming, ended, hidden)), 0)
        listing = client().get("/flashsale").get_data(as_text=True)
        self.assertIn("/flashsale/%d" % upcoming, listing)              # shown as coming soon
        self.assertNotIn("/flashsale/%d/" % ended, listing)

    def test_coupons_do_not_stack_on_sale_price(self):
        run("INSERT INTO coupons (code, discount_type, value, is_public) VALUES ('FLASH50', 'percent', 50, 1)")
        fid = make_sale(self.p50, price=30)
        uid = make_user("Stacker", balance=100)
        flash_buy(client(uid), fid, coupon_code="FLASH50")
        o = last_order(uid)
        self.assertEqual((o["price"], o["discount"], o["coupon_id"]), (30, 0, None))
        self.assertEqual(value("SELECT used FROM coupons WHERE code = 'FLASH50'"), 0)

    def test_cancel_gives_stock_back(self):
        fid = make_sale(self.p50, stock=1)
        uid = make_user("Canceller", balance=100)
        flash_buy(client(uid), fid)
        self.assertEqual(value("SELECT sold FROM flash_sales WHERE id = ?", (fid,)), 1)
        admin_order(self.admin, last_order(uid)["id"], "cancel", refund=True)
        self.assertEqual(value("SELECT sold FROM flash_sales WHERE id = ?", (fid,)), 0)
        self.assertEqual(balance(uid), 100)
        other = make_user("Next in line", balance=100)
        flash_buy(client(other), fid)
        self.assertEqual(last_order(other)["flash_sale_id"], fid)

    def test_abandoned_payment_frees_stock(self):
        fid = make_sale(self.p50, stock=1)
        ghost = make_user("Abandoned")
        run("UPDATE flash_sales SET sold = 1 WHERE id = ?", (fid,))
        run("INSERT INTO orders (user_id, product_name, package_name, price, player_id, payment_method, payment_status, "
            "flash_sale_id, created_at) VALUES (?, 'UID TopUp (BD)', '50 Diamond', 30, '1234567890', 'gateway', 'awaiting', ?, ?)",
            (ghost, fid, ago(minutes=40)))
        uid = make_user("Patient", balance=100)
        flash_buy(client(uid), fid)
        self.assertEqual(last_order(uid)["flash_sale_id"], fid)
        self.assertEqual(last_order(ghost)["status"], "cancelled")
        self.assertEqual(value("SELECT sold FROM flash_sales WHERE id = ?", (fid,)), 1)

    def test_many_buyers_cannot_oversell(self):
        fid = make_sale(self.p50, stock=3, per_user=1)
        buyers = [make_user("Racer %d" % i, balance=100) for i in range(12)]
        in_parallel(lambda uid: flash_buy(client(uid), fid).status_code, [(u,) for u in buyers])
        self.assertEqual(value("SELECT COUNT(*) FROM orders WHERE flash_sale_id = ?", (fid,)), 3)
        self.assertEqual(value("SELECT sold FROM flash_sales WHERE id = ?", (fid,)), 3)
        charged = [u for u in buyers if balance(u) == 70]
        self.assertEqual(len(charged), 3)
        self.assertTrue(all(balance(u) in (70, 100) for u in buyers))
