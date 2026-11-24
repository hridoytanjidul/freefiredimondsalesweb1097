"""Wallet and orders: Add Money, wallet orders, manual Send Money, admin verify/complete/cancel,
refunds, TrxID reuse, balance adjustments, and many orders racing for one balance."""
from support import (UID_TOPUP, StoreTest, admin_order, balance, buy, client, in_parallel, last_order, ledger_ok,
                     make_user, messages, one, pkg, post, product_id, rows, run, settings, value)


def add_money(c, amount, trx, sender="01711000000", method_id=1, option="manual"):
    return post(c, "/profile/addwallet", {"amount": str(amount), "pay_option": option, "method_id": str(method_id),
                                          "sender_number": sender, "trx_id": trx})


class AddMoneyTests(StoreTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        run("UPDATE payment_methods SET number = '01700000001' WHERE name = 'bKash'")

    def test_request_then_admin_approves_once(self):
        uid = make_user("Depositor")
        c = client(uid)
        r = add_money(c, 500, "abc123xyz")
        self.assertIn("রিকোয়েস্ট জমা", " ".join(messages(r)))
        w = one("SELECT * FROM wallet_requests WHERE user_id = ?", (uid,))
        self.assertEqual((w["amount"], w["status"], w["trx_id"], w["method"]), (500, "pending", "ABC123XYZ", "bKash"))
        self.assertEqual(balance(uid), 0)
        post(self.admin, "/admin/wallet/%d/update" % w["id"], {"action": "approve", "amount": "500"})
        self.assertEqual(balance(uid), 500)
        post(self.admin, "/admin/wallet/%d/update" % w["id"], {"action": "approve", "amount": "500"})   # double click
        self.assertEqual(balance(uid), 500)
        self.assertEqual(value("SELECT COUNT(*) FROM transactions WHERE user_id = ? AND type = 'deposit'", (uid,)), 1)
        self.assertTrue(ledger_ok(uid))
        self.assertIn("500", client(uid).get("/profile/transaction").get_data(as_text=True))

    def test_admin_can_approve_a_different_amount_or_reject(self):
        uid = make_user("Partial")
        c = client(uid)
        add_money(c, 300, "PART111111")
        add_money(c, 200, "REJ2222222")
        ws = rows("SELECT id, trx_id FROM wallet_requests WHERE user_id = ? ORDER BY id", (uid,))
        post(self.admin, "/admin/wallet/%d/update" % ws[0]["id"], {"action": "approve", "amount": "250"})
        post(self.admin, "/admin/wallet/%d/update" % ws[1]["id"], {"action": "reject", "note": "Not received"})
        self.assertEqual(balance(uid), 250)
        self.assertEqual(value("SELECT status FROM wallet_requests WHERE id = ?", (ws[1]["id"],)), "rejected")
        post(self.admin, "/admin/wallet/%d/update" % ws[1]["id"], {"action": "approve"})   # can't approve after reject
        self.assertEqual(balance(uid), 250)

    def test_bad_requests_are_refused(self):
        uid = make_user("Bad")
        c = client(uid)
        settings(min_wallet_amount=20, max_wallet_amount=25000)
        cases = [
            (dict(amount=5, trx="LOWAMT1234"), "মধ্যে হতে হবে"),
            (dict(amount=30000, trx="HIGHAMT123"), "মধ্যে হতে হবে"),
            (dict(amount=100, trx="BADSENDER1", sender="12345"), "নম্বর"),
            (dict(amount=100, trx="ab"), "Transaction ID"),
            (dict(amount=100, trx="GOODTRX123", method_id=999), "পেমেন্ট মেথড"),
        ]
        for kw, expected in cases:
            r = add_money(c, **kw)
            self.assertIn(expected, " ".join(messages(r)), kw)
        self.assertEqual(value("SELECT COUNT(*) FROM wallet_requests WHERE user_id = ?", (uid,)), 0)

    def test_trx_id_can_only_be_used_once_anywhere(self):
        a, b = client(make_user("A")), client(make_user("B"))
        add_money(a, 100, "SAMETRX999")
        r = add_money(b, 100, "sametrx999")                   # other case, other customer
        self.assertIn("আগেই ব্যবহার", " ".join(messages(r)))
        r = buy(b, product_id(UID_TOPUP), pkg(UID_TOPUP, "25 Diamond"), pay="manual", method_id="1",
                sender_number="01711000000", trx_id="SameTrx999")   # used for an order
        self.assertIn("আগেই ব্যবহার", " ".join(messages(r)))
        self.assertEqual(value("SELECT COUNT(*) FROM wallet_requests WHERE upper(trx_id) = 'SAMETRX999'"), 1)

    def test_at_most_three_pending_requests(self):
        c = client(make_user("Many"))
        for i in range(3):
            add_money(c, 100, "PENDING%04d" % i)
        r = add_money(c, 100, "PENDING9999")
        self.assertIn("৩টি", " ".join(messages(r)))

    def test_switched_off_method_cannot_be_used(self):
        run("UPDATE payment_methods SET is_active = 0 WHERE name = 'Nagad'")
        nagad = value("SELECT id FROM payment_methods WHERE name = 'Nagad'")
        c = client(make_user("Switched off"))
        page = c.get("/profile/addwallet").get_data(as_text=True)
        self.assertNotIn("<i></i>Nagad", page)
        self.assertIn("<i></i>bKash", page)
        r = add_money(c, 100, "NAGADTRX12", method_id=nagad)
        self.assertIn("পেমেন্ট মেথড", " ".join(messages(r)))
        run("UPDATE payment_methods SET is_active = 1 WHERE name = 'Nagad'")


class OrderTests(StoreTest):
    def setUp(self):
        self.pid = product_id(UID_TOPUP)
        self.p115 = pkg(UID_TOPUP, "115 Diamond")    # ৳79

    def test_wallet_order_needs_enough_balance(self):
        uid = make_user("Poor", balance=50)
        r = buy(client(uid), self.pid, self.p115)
        self.assertIn("যথেষ্ট ব্যালেন্স নেই", " ".join(messages(r)))
        self.assertIsNone(last_order(uid))
        self.assertEqual(balance(uid), 50)

    def test_wallet_order_charges_once_and_records_ledger(self):
        uid = make_user("Buyer", balance=500)
        r = buy(client(uid), self.pid, self.p115, player_id="  12345 67890 ")
        self.assertIn("সফলভাবে", " ".join(messages(r)))
        o = last_order(uid)
        self.assertEqual((o["price"], o["player_id"], o["payment_status"], o["status"], o["payment_method"]),
                         (79, "1234567890", "paid", "pending", "wallet"))
        self.assertEqual(balance(uid), 421)
        self.assertEqual(value("SELECT amount FROM transactions WHERE user_id = ? AND type = 'purchase'", (uid,)), -79)
        self.assertTrue(ledger_ok(uid))
        self.assertIn("#%d" % o["id"], client(uid).get("/profile/order").get_data(as_text=True))

    def test_order_input_checks(self):
        uid = make_user("Checks", balance=1000)
        c = client(uid)
        other = value("SELECT id FROM packages WHERE product_id <> ? LIMIT 1", (self.pid,))
        cases = [
            (dict(package_id=other), "প্যাকেজ সিলেক্ট"),          # package from another product
            (dict(package_id=0), "প্যাকেজ সিলেক্ট"),
            (dict(player_id="12"), "Player ID"),
            (dict(player_id="1" * 16), "Player ID"),
            (dict(pay="bitcoin"), "পেমেন্ট অপশন"),
        ]
        for kw, expected in cases:
            args = dict(package_id=self.p115)
            args.update(kw)
            pay = args.pop("pay", "wallet")
            r = buy(c, self.pid, args.pop("package_id"), pay=pay, **args)
            self.assertIn(expected, " ".join(messages(r)), kw)
        self.assertIsNone(last_order(uid))
        self.assertEqual(balance(uid), 1000)

    def test_out_of_stock_and_hidden_products(self):
        uid = make_user("Stock", balance=1000)
        c = client(uid)
        run("UPDATE packages SET in_stock = 0 WHERE id = ?", (self.p115,))
        r = buy(c, self.pid, self.p115)
        self.assertIn("স্টকে নেই", " ".join(messages(r)))
        run("UPDATE packages SET in_stock = 1 WHERE id = ?", (self.p115,))
        run("UPDATE products SET is_active = 0 WHERE id = ?", (self.pid,))
        self.assertEqual(c.get("/topup/%d" % self.pid).status_code, 404)
        self.assertEqual(buy(c, self.pid, self.p115).status_code, 404)
        run("UPDATE products SET is_active = 1 WHERE id = ?", (self.pid,))
        self.assertEqual(balance(uid), 1000)

    def test_visitor_must_log_in_to_order(self):
        before = value("SELECT COUNT(*) FROM orders")
        r = buy(client(), self.pid, self.p115)
        self.assertIn("/login", r.request.path)
        self.assertEqual(value("SELECT COUNT(*) FROM orders"), before)

    def test_admin_complete_and_cancel_rules(self):
        uid = make_user("Flow", balance=500)
        c = client(uid)
        buy(c, self.pid, self.p115)
        o1 = last_order(uid)
        admin_order(self.admin, o1["id"], "complete")
        self.assertEqual(value("SELECT status FROM orders WHERE id = ?", (o1["id"],)), "completed")
        r = admin_order(self.admin, o1["id"], "cancel", refund=True)           # completed can't be cancelled
        self.assertIn("আগেই", " ".join(messages(r)))
        self.assertEqual(balance(uid), 421)

        buy(c, self.pid, self.p115)
        o2 = last_order(uid)
        admin_order(self.admin, o2["id"], "cancel", note="Wrong Player ID", refund=True)
        self.assertEqual(balance(uid), 421)                                    # 342 + 79 back
        admin_order(self.admin, o2["id"], "cancel", refund=True)               # twice: no double refund
        self.assertEqual(balance(uid), 421)
        self.assertIn("Wrong Player ID", c.get("/profile/order").get_data(as_text=True))
        r = admin_order(self.admin, o2["id"], "complete")
        self.assertIn("আগেই", " ".join(messages(r)))
        self.assertTrue(ledger_ok(uid))

    def test_cancel_without_refund_keeps_money_with_store(self):
        uid = make_user("NoRefund", balance=100)
        buy(client(uid), self.pid, self.p115)
        admin_order(self.admin, last_order(uid)["id"], "cancel", note="Fraud", refund=False)
        self.assertEqual(balance(uid), 21)

    def test_manual_send_money_order(self):
        uid = make_user("Manual")
        c = client(uid)
        r = buy(c, self.pid, self.p115, pay="manual", method_id="1", sender_number="01711000000", trx_id="manualtrx1")
        self.assertIn("জমা হয়েছে", " ".join(messages(r)))
        o = last_order(uid)
        self.assertEqual((o["payment_status"], o["trx_id"], o["payment_method"]), ("unverified", "MANUALTRX1", "bKash"))
        self.assertIn("#%d" % o["id"], self.admin.get("/admin/orders?f=verify").get_data(as_text=True))
        admin_order(self.admin, o["id"], "verify")
        self.assertEqual(value("SELECT payment_status FROM orders WHERE id = ?", (o["id"],)), "paid")
        admin_order(self.admin, o["id"], "complete")
        self.assertEqual(value("SELECT status FROM orders WHERE id = ?", (o["id"],)), "completed")
        self.assertEqual(balance(uid), 0)

    def test_cancelling_unverified_order_never_refunds(self):
        uid = make_user("Unverified")
        buy(client(uid), self.pid, self.p115, pay="manual", method_id="1", sender_number="01711000000", trx_id="FAKETRX777")
        admin_order(self.admin, last_order(uid)["id"], "cancel", note="Payment not found", refund=True)
        self.assertEqual(balance(uid), 0)

    def test_many_orders_cannot_overspend_one_wallet(self):
        uid = make_user("Racer", balance=100)
        pid, p50 = self.pid, pkg(UID_TOPUP, "50 Diamond")      # ৳40 each: only 2 fit in ৳100
        clients = [client(uid) for _ in range(8)]
        in_parallel(lambda c: buy(c, pid, p50).status_code, [(c,) for c in clients])
        self.assertEqual(value("SELECT COUNT(*) FROM orders WHERE user_id = ?", (uid,)), 2)
        self.assertEqual(balance(uid), 20)
        self.assertTrue(ledger_ok(uid))


class AdminMoneyTests(StoreTest):
    def test_balance_adjustments(self):
        uid = make_user("Adjust", balance=100)
        post(self.admin, "/admin/users/%d/balance" % uid, {"amount": "50", "note": "Bonus"})
        post(self.admin, "/admin/users/%d/balance" % uid, {"amount": "-30", "note": "Correction"})
        self.assertEqual(balance(uid), 120)
        r = post(self.admin, "/admin/users/%d/balance" % uid, {"amount": "-1000"})
        self.assertIn("কম", " ".join(messages(r)))
        r = post(self.admin, "/admin/users/%d/balance" % uid, {"amount": "0"})
        self.assertIn("পরিমাণ দিন", " ".join(messages(r)))
        self.assertEqual(balance(uid), 120)
        self.assertTrue(ledger_ok(uid))
        self.assertIn("Bonus", client(uid).get("/profile/transaction").get_data(as_text=True))

    def test_order_search_and_dashboard(self):
        uid = make_user("Searchable", "find.me@x.com", balance=200)
        buy(client(uid), product_id(UID_TOPUP), pkg(UID_TOPUP, "25 Diamond"), player_id="5554443332")
        oid = last_order(uid)["id"]
        for q in ("5554443332", "FIND.ME", "#%d" % oid, "Searchable"):
            self.assertIn("#%d" % oid, self.admin.get("/admin/orders?f=all&q=" + q).get_data(as_text=True), q)
        self.assertEqual(self.admin.get("/admin/orders?f=all&q=%25").status_code, 200)
        self.assertEqual(self.admin.get("/admin/").status_code, 200)

    def test_recent_orders_and_leaderboard(self):
        uid = make_user("Leader Person", balance=1000)
        buy(client(uid), product_id(UID_TOPUP), pkg(UID_TOPUP, "240 Diamond"))
        admin_order(self.admin, last_order(uid)["id"], "complete")
        settings(show_recent_orders="1")
        self.assertIn("240 Diamond", [x["item"] for x in client().get("/api/recent-orders").get_json()])
        settings(show_recent_orders="0")
        self.assertEqual(client().get("/api/recent-orders").get_json(), [])
        self.assertIn("Leader Person", client().get("/leaderboard").get_data(as_text=True))
