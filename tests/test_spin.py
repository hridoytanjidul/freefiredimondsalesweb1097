"""Spin wheel: on/off, who may spin, each prize type, the cooldown, the daily budget,
admin settings, and many clicks at once still giving exactly one spin."""
from support import (UID_TOPUP, StoreTest, ago, balance, buy, client, in_parallel, last_order, ledger_ok, make_user,
                     messages, one, pkg, post, product_id, rows, run, settings, token, value)


def only_prize(label):
    """Make one prize the only possible result."""
    run("UPDATE spin_prizes SET weight = 0, is_active = 1")
    run("UPDATE spin_prizes SET weight = 10 WHERE id = (SELECT MIN(id) FROM spin_prizes WHERE label = ?)", (label,))


def spin(c):
    return c.post("/api/spin", headers={"X-CSRF-Token": token(c)})


def eligible_user(name="Spinner"):
    uid = make_user(name)
    run("INSERT INTO orders (user_id, product_name, package_name, price, player_id, payment_method, payment_status, status) "
        "VALUES (?, 'UID TopUp (BD)', '25 Diamond', 22, '1234567890', 'wallet', 'paid', 'completed')", (uid,))
    return uid


class SpinTests(StoreTest):
    def setUp(self):
        settings(spin_enabled="1", spin_min_orders="1", spin_cooldown_hours="24", spin_daily_budget="0")
        run("UPDATE spin_prizes SET weight = 10, is_active = 1")

    def test_off_means_hidden_and_refused(self):
        settings(spin_enabled="0")
        self.assertEqual(client().get("/spin").status_code, 404)
        self.assertEqual(spin(client(eligible_user())).status_code, 403)
        self.assertNotIn('href="/spin"', client().get("/").get_data(as_text=True))

    def test_who_may_spin(self):
        self.assertEqual(client().get("/spin").status_code, 200)
        self.assertEqual(spin(client()).status_code, 401)                          # visitors
        newbie = client(make_user("Newbie"))
        r = spin(newbie)
        self.assertEqual((r.status_code, r.get_json()["state"]), (409, "ineligible"))
        self.assertEqual(newbie.get("/api/spin/check").get_json()["state"], "ineligible")
        settings(spin_min_orders="0")
        self.assertEqual(spin(newbie).status_code, 200)

    def test_wallet_prize(self):
        only_prize("৳10")
        uid = eligible_user()
        d = spin(client(uid)).get_json()
        self.assertEqual((d["ok"], d["type"], d["amount"], d["label"]), (True, "wallet", 10, "৳10"))
        prizes = rows("SELECT id FROM spin_prizes WHERE is_active = 1 ORDER BY sort_order, id")
        self.assertEqual(prizes[d["index"]]["id"], d["prize_id"])                 # the wheel stops on the right segment
        self.assertEqual(balance(uid), 10)
        self.assertEqual(value("SELECT type FROM transactions WHERE user_id = ?", (uid,)), "spin")
        self.assertTrue(ledger_ok(uid))
        s = one("SELECT * FROM spins WHERE user_id = ?", (uid,))
        self.assertEqual((s["prize_type"], s["amount"]), ("wallet", 10))

    def test_coupon_prize_is_personal_and_usable(self):
        only_prize("10% OFF")
        uid = eligible_user("Winner")
        d = spin(client(uid)).get_json()
        self.assertEqual(d["type"], "coupon")
        self.assertRegex(d["coupon_code"], r"^SPIN-[A-Z2-9]{6}$")
        c = one("SELECT * FROM coupons WHERE code = ?", (d["coupon_code"],))
        self.assertEqual((c["user_id"], c["usage_limit"], c["value"], c["max_discount"], c["is_public"]), (uid, 1, 10, 30, 0))
        self.assertTrue(ago(days=-6) < c["ends_at"] < ago(days=-8))               # valid for 7 days
        self.assertEqual(balance(uid), 0)
        run("UPDATE users SET balance = 500 WHERE id = ?", (uid,))
        run("INSERT INTO transactions (user_id, amount, type, balance_after) VALUES (?, 500, 'deposit', 500)", (uid,))
        other = make_user("Not winner", balance=500)
        r = buy(client(other), product_id(UID_TOPUP), pkg(UID_TOPUP, "240 Diamond"), coupon_code=d["coupon_code"])
        self.assertIn("সঠিক নয়", " ".join(messages(r)))
        buy(client(uid), product_id(UID_TOPUP), pkg(UID_TOPUP, "240 Diamond"), coupon_code=d["coupon_code"])
        self.assertEqual(last_order(uid)["price"], 143)

    def test_try_again(self):
        only_prize("Try Again")
        uid = eligible_user()
        d = spin(client(uid)).get_json()
        self.assertEqual((d["type"], d["amount"], d["coupon_code"]), ("nothing", 0, None))
        self.assertEqual(balance(uid), 0)

    def test_cooldown(self):
        uid = eligible_user()
        c = client(uid)
        self.assertEqual(spin(c).status_code, 200)
        r = spin(c)
        self.assertEqual((r.status_code, r.get_json()["state"]), (409, "cooldown"))
        self.assertGreater(r.get_json()["secs"], 23 * 3600)
        self.assertFalse(c.get("/api/spin/check").get_json()["canSpin"])
        run("UPDATE spins SET created_at = ? WHERE user_id = ?", (ago(hours=25), uid))
        self.assertTrue(c.get("/api/spin/check").get_json()["canSpin"])
        self.assertEqual(spin(c).status_code, 200)

    def test_daily_budget_stops_cash_prizes(self):
        run("DELETE FROM spins")                                                    # other tests' wins today
        run("UPDATE spin_prizes SET weight = 0")
        run("UPDATE spin_prizes SET weight = 10 WHERE label IN ('৳10', 'Try Again')")
        settings(spin_daily_budget="5")                                             # ৳10 would exceed it
        users = [eligible_user("Budget %d" % i) for i in range(6)]
        for uid in users:
            self.assertEqual(spin(client(uid)).get_json()["type"], "nothing")
        self.assertEqual(sum(balance(u) for u in users), 0)
        settings(spin_daily_budget="10")
        only_prize("৳10")
        self.assertEqual(spin(client(eligible_user("First today"))).get_json()["amount"], 10)
        d = spin(client(eligible_user("Over budget")))
        self.assertEqual(d.status_code, 503)                                        # nothing left to win

    def test_many_clicks_one_spin(self):
        only_prize("৳10")
        uid = eligible_user("Clicker")
        clients = [client(uid) for _ in range(10)]
        codes = in_parallel(lambda c: spin(c).status_code, [(c,) for c in clients])
        self.assertEqual(sorted(codes).count(200), 1)
        self.assertEqual(value("SELECT COUNT(*) FROM spins WHERE user_id = ?", (uid,)), 1)
        self.assertEqual(balance(uid), 10)

    def test_admin_settings_and_prizes(self):
        post(self.admin, "/admin/spin", {"spin_enabled": "1", "spin_cooldown_hours": "12", "spin_min_orders": "2",
                                         "spin_daily_budget": "100"})
        self.assertEqual(value("SELECT value FROM settings WHERE key = 'spin_cooldown_hours'"), "12")
        r = post(self.admin, "/admin/spin", {"spin_cooldown_hours": "0", "spin_min_orders": "1", "spin_daily_budget": "0"})
        self.assertIn("সঠিকভাবে", " ".join(messages(r)))
        base = {"label": "৳20", "prize_type": "wallet", "amount": "20", "coupon_type": "percent", "weight": "5",
                "color": "#123456", "valid_days": "7", "is_active": "1"}
        for bad, expected in ((dict(label=""), "লেখা দিন"), (dict(amount="0"), "পরিমাণ"),
                              (dict(prize_type="coupon", amount="150"), "১০০"), (dict(weight="-1"), "weight")):
            r = post(self.admin, "/admin/spin/prizes", dict(base, **bad))
            self.assertIn(expected, " ".join(messages(r)), bad)
        post(self.admin, "/admin/spin/prizes", dict(base, label="A very long label indeed", color="red"))
        p = one("SELECT * FROM spin_prizes ORDER BY id DESC LIMIT 1")
        self.assertEqual((len(p["label"]), p["color"], p["amount"]), (14, "#7c3aed", 20))   # trimmed, safe colour
        post(self.admin, "/admin/spin/prizes/%d" % p["id"], dict(base, weight="0"))
        self.assertEqual(value("SELECT weight FROM spin_prizes WHERE id = ?", (p["id"],)), 0)
        post(self.admin, "/admin/spin/prizes/%d" % p["id"], {"delete": "1"})
        self.assertIsNone(one("SELECT 1 FROM spin_prizes WHERE id = ?", (p["id"],)))
        self.assertEqual(self.admin.get("/admin/spin").status_code, 200)
