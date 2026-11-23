"""Shared checkout logic used by normal products and flash sales:
player-ID checks, manual Send Money checks, the UddoktaPay gateway, and order creation."""
import sqlite3

from flask import flash, g, redirect, request, url_for

import coupons
import payments
from checkout_errors import CheckoutError
from db import InsufficientBalance, cancel_order, credit, debit, deliver_voucher, get_db, q, tx
from helpers import BD_PHONE_RE, TRX_RE, clean_digits, clean_trx, to_int


def active_methods():
    return q("SELECT * FROM payment_methods WHERE is_active = 1 ORDER BY sort_order, id")


def trx_already_used(trx):
    return (q("SELECT 1 FROM orders WHERE trx_id = ? COLLATE NOCASE", (trx,), one=True)
            or q("SELECT 1 FROM wallet_requests WHERE trx_id = ? COLLATE NOCASE", (trx,), one=True))


def validate_manual_payment(form):
    """Returns (method_row, sender, trx) or raises CheckoutError."""
    method = q("SELECT * FROM payment_methods WHERE id = ? AND is_active = 1",
               (to_int(form.get("method_id"), 0),), one=True)
    if not method:
        raise CheckoutError("পেমেন্ট মেথড (বিকাশ/নগদ/রকেট) সিলেক্ট করুন।")
    sender = clean_digits(form.get("sender_number"))
    if not BD_PHONE_RE.match(sender):
        raise CheckoutError("যে নম্বর থেকে টাকা পাঠিয়েছেন সেটি সঠিকভাবে দিন (যেমন 01XXXXXXXXX)।")
    trx = clean_trx(form.get("trx_id"))
    if not TRX_RE.match(trx):
        raise CheckoutError("সঠিক Transaction ID দিন।")
    if trx_already_used(trx):
        raise CheckoutError("এই Transaction ID আগেই ব্যবহার করা হয়েছে।")
    return method, sender, trx


def clean_player_id(product, raw):
    if product["kind"] == "voucher":
        return ""          # vouchers deliver a code; no game ID needed
    raw = (raw or "").strip()
    if product["numeric_id"]:
        pid = clean_digits(raw)
        if not 5 <= len(pid) <= 15:
            raise CheckoutError("সঠিক Player ID দিন (শুধু সংখ্যা)।")
        return pid
    if not 3 <= len(raw) <= 120:
        raise CheckoutError("সঠিক তথ্য দিন।")
    return raw


# --------------------------------------------------------------------------- gateway (UddoktaPay)

def start_gateway_payment(purpose, ref_id, amount):
    with tx() as db:
        gp_id = db.execute(
            "INSERT INTO gateway_payments (user_id, purpose, ref_id, amount) VALUES (?,?,?,?)",
            (g.user["id"], purpose, ref_id, amount)).lastrowid
    return payments.create_checkout(
        g.settings,
        full_name=g.user["name"],
        email=g.user["email"],
        amount=amount,
        metadata={"gp_id": str(gp_id)},
        redirect_url=url_for("payment_return", gp=gp_id, _external=True),
        cancel_url=url_for("payment_failed", _external=True),
        webhook_url=url_for("payment_webhook", _external=True),
    )


def settle_gateway_payment(gp_id, invoice_id):
    """Verify an invoice with the gateway and apply its effect exactly once.
    Returns (ok, purpose)."""
    if not invoice_id:
        return False, None
    data = payments.verify(g.settings, invoice_id)
    if str(data.get("status", "")).upper() != "COMPLETED":
        return False, None
    meta = data.get("metadata") or {}
    if str(meta.get("gp_id")) != str(gp_id):
        return False, None
    try:
        paid_amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        paid_amount = 0.0
    method = str(data.get("payment_method") or "gateway")[:30]
    sender = str(data.get("sender_number") or "")[:20]
    trx = str(data.get("transaction_id") or "")[:40]

    with tx() as db:
        gp = db.execute("SELECT * FROM gateway_payments WHERE id = ?", (gp_id,)).fetchone()
        if gp is None:
            return False, None
        if gp["status"] == "paid":
            return True, gp["purpose"]
        if paid_amount + 0.001 < gp["amount"]:
            return False, gp["purpose"]
        clash = db.execute("SELECT 1 FROM gateway_payments WHERE invoice_id = ? AND id <> ?",
                           (invoice_id, gp_id)).fetchone()
        if clash:
            return False, gp["purpose"]
        db.execute("UPDATE gateway_payments SET status='paid', invoice_id=?, method=?, sender_number=?, trx_id=? "
                   "WHERE id = ?", (invoice_id, method, sender, trx, gp_id))
        if gp["purpose"] == "order":
            order = db.execute("SELECT * FROM orders WHERE id = ?", (gp["ref_id"],)).fetchone()
            if order is None or order["status"] == "cancelled":
                # Paid too late (order already expired/cancelled): never lose the customer's money.
                credit(db, gp["user_id"], gp["amount"], "refund",
                       "Payment for cancelled order #%s added to wallet" % gp["ref_id"])
            else:
                db.execute("UPDATE orders SET payment_status='paid', sender_number=?, updated_at=datetime('now') "
                           "WHERE id = ? AND payment_status = 'awaiting'", (sender, gp["ref_id"]))
                if order["is_voucher"]:
                    deliver_voucher(db, order["id"])
        else:
            credit(db, gp["user_id"], gp["amount"], "deposit",
                   "Add money via %s (%s)" % (method, trx or invoice_id))
    return True, gp["purpose"]


# --------------------------------------------------------------------------- order creation

ORDER_INSERT = (
    "INSERT INTO orders (user_id, product_id, package_id, product_name, package_name, price, player_id, "
    "payment_method, payment_status, sender_number, trx_id, flash_sale_id, coupon_id, coupon_code, discount, "
    "is_voucher) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def reserve_voucher_code(db, package_id, order_id):
    """Hold the next code in stock for this order (inside the checkout transaction)."""
    vc = db.execute("SELECT id FROM voucher_codes WHERE package_id = ? AND status = 'available' ORDER BY id LIMIT 1",
                    (package_id,)).fetchone()
    if vc is None:
        raise CheckoutError("দুঃখিত, এই ভাউচারটি এখন স্টকে নেই।")
    db.execute("UPDATE voucher_codes SET status = 'reserved', order_id = ? WHERE id = ?", (order_id, vc["id"]))


def place_order(product, package_id, package_name, price, back, flash_sale_id=None, reserve=None,
                allow_coupon=True):
    """Create an order for the logged-in user and take payment the way they chose
    (form field pay_option = wallet | gateway | manual).

    `price` is the list price; a coupon (form field coupon_code) lowers what is charged.
    `reserve(db)` runs inside the same transaction just before the order is written, so a
    flash sale's stock is taken atomically with the order (and given back if anything fails).
    Voucher products also reserve a code in that transaction, delivered once payment is confirmed."""
    form = request.form
    uid = g.user["id"]
    option = form.get("pay_option")
    is_voucher = product["kind"] == "voucher"
    coupon_code = coupons.normalize(form.get("coupon_code")) if allow_coupon else ""
    try:
        player_id = clean_player_id(product, form.get("player_id"))
        discount = 0
        if coupon_code:
            _, discount = coupons.evaluate(get_db(), coupon_code, product["id"], price, uid)  # early, friendly check
        charge = price - discount

        def insert(db, method, pay_status, sender=None, trx=None):
            if reserve:
                reserve(db)
            coupon_id = coupon_name = None
            if coupon_code:
                c, off = coupons.claim(db, coupon_code, product["id"], price, uid)
                if off != discount:
                    raise CheckoutError("কুপনের তথ্য পরিবর্তন হয়েছে। আবার চেষ্টা করুন।")
                coupon_id, coupon_name = c["id"], c["code"]
            order_id = db.execute(ORDER_INSERT, (
                uid, product["id"], package_id, product["name"], package_name, charge, player_id, method,
                pay_status, sender, trx, flash_sale_id, coupon_id, coupon_name,
                discount, 1 if is_voucher else 0)).lastrowid
            if is_voucher:
                reserve_voucher_code(db, package_id, order_id)
            return order_id

        if option == "wallet":
            with tx() as db:
                order_id = insert(db, "wallet", "paid")
                debit(db, uid, charge, "purchase", "Order #%d — %s (%s)" % (order_id, package_name, product["name"]))
                if is_voucher:
                    deliver_voucher(db, order_id)
            if is_voucher:
                flash("অর্ডার #%d সম্পন্ন! আপনার কোড নিচে দেখুন।" % order_id, "success")
                return redirect(url_for("my_vouchers"))
            flash("অর্ডার #%d সফলভাবে করা হয়েছে! কিছুক্ষণের মধ্যে টপআপ হয়ে যাবে।" % order_id, "success")
            return redirect(url_for("my_orders"))

        if option == "gateway" and payments.enabled(g.settings):
            with tx() as db:
                order_id = insert(db, "gateway", "awaiting")
            try:
                return redirect(start_gateway_payment("order", order_id, charge))
            except payments.GatewayError as e:
                with tx() as db:
                    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                    cancel_order(db, order, "Payment could not be started", refund=False)
                raise CheckoutError(str(e))

        if option == "manual":
            method, sender, trx = validate_manual_payment(form)
            with tx() as db:
                order_id = insert(db, method["name"], "unverified", sender, trx)
            if is_voucher:
                flash("অর্ডার #%d জমা হয়েছে। পেমেন্ট যাচাইয়ের পর কোডটি এখানে দেখাবে।" % order_id, "success")
                return redirect(url_for("my_vouchers"))
            flash("অর্ডার #%d জমা হয়েছে। পেমেন্ট যাচাইয়ের পর টপআপ করা হবে।" % order_id, "success")
            return redirect(url_for("my_orders"))

        raise CheckoutError("পেমেন্ট অপশন সিলেক্ট করুন।")
    except InsufficientBalance:
        flash("আপনার ওয়ালেটে যথেষ্ট ব্যালেন্স নেই। আগে Add Money করুন।", "error")
    except CheckoutError as e:
        flash(str(e), "error")
    except sqlite3.IntegrityError:
        flash("এই Transaction ID আগেই ব্যবহার করা হয়েছে।", "error")
    return redirect(back)
