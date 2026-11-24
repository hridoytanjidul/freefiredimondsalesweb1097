"""Instant Pay (UddoktaPay) with a fake gateway: orders and wallet top-ups are paid exactly once,
whatever arrives first (browser return or webhook), and bad or late payments are handled safely."""
from unittest import mock

import payments
from support import (UID_TOPUP, StoreTest, admin_order, ago, balance, buy, client, last_order, ledger_ok, make_user,
                     messages, one, pkg, post, product_id, run, settings, value)

KEY = "KEY123"


class FakeGateway:
    def __init__(self):
        self.checkouts, self.invoices, self.fail_start = [], {}, False

    def create_checkout(self, s, **kw):
        if self.fail_start:
            raise payments.GatewayError("gateway down")
        self.checkouts.append(kw)
        return "https://pay.example/checkout/%d" % len(self.checkouts)

    def verify(self, s, invoice_id):
        return self.invoices.get(invoice_id, {"status": "PENDING"})

    def pay(self, invoice_id, gp_id, amount, status="COMPLETED"):
        self.invoices[invoice_id] = {"status": status, "metadata": {"gp_id": str(gp_id)}, "amount": "%.2f" % amount,
                                     "payment_method": "bkash", "sender_number": "01711000000",
                                     "transaction_id": "TX-" + invoice_id}


def last_gp(uid):
    return one("SELECT * FROM gateway_payments WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,))


class GatewayTests(StoreTest):
    def setUp(self):
        settings(uddoktapay_base_url="https://sandbox.example", uddoktapay_api_key=KEY)
        self.gw = FakeGateway()
        patches = [mock.patch.object(payments, "create_checkout", self.gw.create_checkout),
                   mock.patch.object(payments, "verify", self.gw.verify)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.pid, self.p115 = product_id(UID_TOPUP), pkg(UID_TOPUP, "115 Diamond")

    def webhook(self, invoice_id, gp_id, key=KEY):
        return client().post("/payment/webhook", json={"invoice_id": invoice_id, "metadata": {"gp_id": str(gp_id)}},
                             headers={"RT-UDDOKTAPAY-API-KEY": key})

    def back_from_gateway(self, gp_id, invoice_id, c=None):
        return (c or client()).get("/payment/return?gp=%d&invoice_id=%s" % (gp_id, invoice_id))

    def order_by_gateway(self, uid):
        r = post(client(uid), "/topup/%d/order" % self.pid,
                 {"package_id": self.p115, "player_id": "1234567890", "pay_option": "gateway"}, follow=False)
        return r, last_order(uid), last_gp(uid)

    def test_order_paid_once_by_return_and_webhook(self):
        uid = make_user("Gw buyer")
        r, o, gp = self.order_by_gateway(uid)
        self.assertEqual(r.headers["Location"], "https://pay.example/checkout/1")
        self.assertEqual((o["payment_status"], o["payment_method"], gp["amount"], gp["purpose"]), ("awaiting", "gateway", 79, "order"))
        sent = self.gw.checkouts[-1]
        self.assertEqual((sent["amount"], sent["metadata"]), (79, {"gp_id": str(gp["id"])}))
        self.assertTrue(sent["webhook_url"].endswith("/payment/webhook"))
        self.assertIn("#%d" % o["id"], self.admin.get("/admin/orders?f=unpaid").get_data(as_text=True))
        r = admin_order(self.admin, o["id"], "complete")                     # not paid yet
        self.assertIn("এখনও হয়নি", " ".join(messages(r)))

        self.gw.pay("INV-1", gp["id"], 79)
        r = self.back_from_gateway(gp["id"], "INV-1")
        self.assertIn("/paymentsuccess", r.headers["Location"])
        self.assertEqual(self.webhook("INV-1", gp["id"]).get_json(), {"ok": True})
        self.assertEqual(self.back_from_gateway(gp["id"], "INV-1").status_code, 302)   # again: no change
        o = one("SELECT * FROM orders WHERE id = ?", (o["id"],))
        self.assertEqual((o["payment_status"], o["status"]), ("paid", "pending"))
        self.assertEqual(value("SELECT COUNT(*) FROM gateway_payments WHERE status = 'paid' AND id = ?", (gp["id"],)), 1)
        self.assertEqual(balance(uid), 0)                                     # nothing credited to the wallet
        self.assertIn("#%d" % o["id"], self.admin.get("/admin/orders?f=deliver").get_data(as_text=True))

    def test_wallet_top_up_credited_once(self):
        uid = make_user("Gw topup")
        r = post(client(uid), "/profile/addwallet", {"amount": "300", "pay_option": "gateway"}, follow=False)
        self.assertTrue(r.headers["Location"].startswith("https://pay.example/"))
        gp = last_gp(uid)
        self.assertEqual((gp["purpose"], gp["amount"]), ("wallet", 300))
        self.gw.pay("INV-W", gp["id"], 300)
        self.webhook("INV-W", gp["id"])
        self.back_from_gateway(gp["id"], "INV-W")
        self.webhook("INV-W", gp["id"])
        self.assertEqual(balance(uid), 300)
        self.assertTrue(ledger_ok(uid))

    def test_bad_payments_are_not_accepted(self):
        uid = make_user("Gw bad")
        _, o, gp = self.order_by_gateway(uid)
        self.gw.pay("INV-PEND", gp["id"], 79, status="PENDING")
        self.gw.pay("INV-SHORT", gp["id"], 50)                               # paid less than the price
        self.gw.invoices["INV-OTHER"] = dict(self.gw.invoices["INV-SHORT"], amount="79.00", metadata={"gp_id": "999999"})
        for inv in ("INV-PEND", "INV-SHORT", "INV-OTHER", "INV-UNKNOWN", ""):
            r = self.back_from_gateway(gp["id"], inv)
            self.assertIn("/paymentfailed", r.headers["Location"], inv)
        self.assertEqual(value("SELECT payment_status FROM orders WHERE id = ?", (o["id"],)), "awaiting")

    def test_one_invoice_cannot_pay_two_orders(self):
        uid = make_user("Gw reuse")
        _, _, gp1 = self.order_by_gateway(uid)
        _, o2, gp2 = self.order_by_gateway(uid)
        self.gw.pay("INV-R", gp1["id"], 79)
        self.back_from_gateway(gp1["id"], "INV-R")
        self.gw.invoices["INV-R"]["metadata"] = {"gp_id": str(gp2["id"])}   # replayed for the second order
        self.assertIn("/paymentfailed", self.back_from_gateway(gp2["id"], "INV-R").headers["Location"])
        self.assertEqual(value("SELECT payment_status FROM orders WHERE id = ?", (o2["id"],)), "awaiting")

    def test_webhook_needs_the_api_key_and_no_csrf(self):
        uid = make_user("Gw hook")
        _, o, gp = self.order_by_gateway(uid)
        self.gw.pay("INV-H", gp["id"], 79)
        self.assertEqual(self.webhook("INV-H", gp["id"], key="wrong").status_code, 401)
        self.assertEqual(value("SELECT payment_status FROM orders WHERE id = ?", (o["id"],)), "awaiting")
        self.assertEqual(self.webhook("INV-H", gp["id"]).status_code, 200)    # no CSRF token needed
        self.assertEqual(value("SELECT payment_status FROM orders WHERE id = ?", (o["id"],)), "paid")

    def test_gateway_off(self):
        settings(uddoktapay_api_key="")
        self.assertEqual(self.webhook("INV-X", 1).status_code, 404)
        self.assertIn("/paymentfailed", client().get("/payment/return?gp=1&invoice_id=X").headers["Location"])
        uid = make_user("Gw off")
        r = buy(client(uid), self.pid, self.p115, pay="gateway")
        self.assertIn("পেমেন্ট অপশন", " ".join(messages(r)))
        self.assertNotIn('value="gateway"', client(uid).get("/topup/%d" % self.pid).get_data(as_text=True))

    def test_failed_start_cancels_the_order(self):
        self.gw.fail_start = True
        uid = make_user("Gw down")
        r = buy(client(uid), self.pid, self.p115, pay="gateway")
        self.assertIn("gateway down", " ".join(messages(r)))
        self.assertEqual(last_order(uid)["status"], "cancelled")

    def test_unpaid_orders_expire_and_late_payment_goes_to_wallet(self):
        uid = make_user("Gw late")
        _, o, gp = self.order_by_gateway(uid)
        run("UPDATE orders SET created_at = ? WHERE id = ?", (ago(minutes=45), o["id"]))
        self.admin.get("/admin/")                                             # the dashboard tidies up expired ones
        self.assertEqual(value("SELECT status FROM orders WHERE id = ?", (o["id"],)), "cancelled")
        self.gw.pay("INV-LATE", gp["id"], 79)
        self.back_from_gateway(gp["id"], "INV-LATE")
        self.assertEqual(balance(uid), 79)                                    # the money isn't lost
        self.assertEqual(value("SELECT status FROM orders WHERE id = ?", (o["id"],)), "cancelled")
        self.assertTrue(ledger_ok(uid))

    def test_recent_unpaid_orders_are_kept(self):
        uid = make_user("Gw fresh")
        _, o, _ = self.order_by_gateway(uid)
        run("UPDATE orders SET created_at = ? WHERE id = ?", (ago(minutes=10), o["id"]))
        self.admin.get("/admin/")
        self.assertEqual(value("SELECT status FROM orders WHERE id = ?", (o["id"],)), "pending")
