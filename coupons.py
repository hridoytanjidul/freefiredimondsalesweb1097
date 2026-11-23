"""Coupon codes: validation, discount maths, and the checkout "Apply" API."""
import re

from flask import Blueprint, g, jsonify, request

from checkout_errors import CheckoutError
from db import get_db, q
from helpers import to_int

bp = Blueprint("coupons", __name__)

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,30}$")

TIMING_SQL = ("CASE WHEN starts_at IS NOT NULL AND datetime('now') < starts_at THEN 'upcoming' "
              "WHEN ends_at IS NOT NULL AND datetime('now') >= ends_at THEN 'expired' ELSE 'ok' END AS timing")


def normalize(code):
    return re.sub(r"\s", "", code or "").upper()


def discount_for(coupon, price):
    """Taka off for this price. A coupon can never make an order free (minimum ৳1 is paid)."""
    if coupon["discount_type"] == "percent":
        off = price * coupon["value"] // 100
        if coupon["max_discount"]:
            off = min(off, coupon["max_discount"])
    else:
        off = coupon["value"]
    return max(0, min(off, price - 1))


def label(coupon):
    if coupon["discount_type"] == "percent":
        text = "%d%% OFF" % coupon["value"]
        if coupon["max_discount"]:
            text += " (max ৳%d)" % coupon["max_discount"]
        return text
    return "৳%d OFF" % coupon["value"]


def evaluate(db, code, product_id, price, user_id):
    """Check a code for this product/price/user. Returns (coupon_row, discount) or raises CheckoutError."""
    code = normalize(code)
    c = db.execute("SELECT *, " + TIMING_SQL + " FROM coupons WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
    if c is None or not c["is_active"] or (c["user_id"] and c["user_id"] != user_id):
        raise CheckoutError("কুপন কোডটি সঠিক নয়।")   # someone else's personal coupon looks like an unknown code
    if c["timing"] == "upcoming":
        raise CheckoutError("এই কুপন এখনো চালু হয়নি।")
    if c["timing"] == "expired":
        raise CheckoutError("এই কুপনের মেয়াদ শেষ হয়ে গেছে।")
    if c["product_id"] and c["product_id"] != product_id:
        raise CheckoutError("এই কুপন এই প্রোডাক্টে ব্যবহার করা যাবে না।")
    if price < c["min_order"]:
        raise CheckoutError("এই কুপন ব্যবহার করতে কমপক্ষে ৳%d এর প্যাকেজ নিতে হবে।" % c["min_order"])
    if c["usage_limit"] and c["used"] >= c["usage_limit"]:
        raise CheckoutError("এই কুপনের ব্যবহার সীমা শেষ।")
    if c["per_user_limit"] and user_id:
        n = db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND coupon_id = ? AND status <> 'cancelled'",
                       (user_id, c["id"])).fetchone()[0]
        if n >= c["per_user_limit"]:
            raise CheckoutError("আপনি এই কুপন সর্বোচ্চ সংখ্যকবার ব্যবহার করেছেন।")
    off = discount_for(c, price)
    if off <= 0:
        raise CheckoutError("এই প্যাকেজে কুপনটি কোনো ছাড় দেয় না।")
    return c, off


def claim(db, code, product_id, price, user_id):
    """Use a coupon inside the checkout transaction. Re-checks everything, then counts the use
    atomically so a limited coupon can't be over-used by simultaneous orders."""
    c, off = evaluate(db, code, product_id, price, user_id)
    cur = db.execute("UPDATE coupons SET used = used + 1 WHERE id = ? AND (usage_limit = 0 OR used < usage_limit)",
                     (c["id"],))
    if cur.rowcount != 1:
        raise CheckoutError("এই কুপনের ব্যবহার সীমা শেষ।")
    return c, off


def public_coupons(product_id, user_id=None):
    """Coupons shown as tappable chips on a product page: public ones, plus the viewer's own
    unused personal coupons (e.g. won on the spin wheel)."""
    return q("SELECT * FROM (SELECT *, " + TIMING_SQL + " FROM coupons) WHERE is_active = 1 "
             "AND ((is_public = 1 AND user_id IS NULL) OR (user_id IS NOT NULL AND user_id = ?)) "
             "AND timing = 'ok' AND (product_id IS NULL OR product_id = ?) "
             "AND (usage_limit = 0 OR used < usage_limit) ORDER BY user_id IS NULL, id DESC LIMIT 6",
             (user_id or -1, product_id))


@bp.post("/api/coupon/check")
def check():
    """Preview a coupon for the selected package (the real check happens again at checkout)."""
    if g.user is None:
        return jsonify(ok=False, error="কুপন ব্যবহার করতে আগে লগইন করুন।"), 401
    code = normalize(request.form.get("code"))
    if not CODE_RE.match(code):
        return jsonify(ok=False, error="কুপন কোডটি সঠিক নয়।"), 400
    pkg = q("SELECT k.*, p.is_active AS product_active FROM packages k JOIN products p ON p.id = k.product_id "
            "WHERE k.id = ?", (to_int(request.form.get("package_id"), 0),), one=True)
    if pkg is None or not pkg["product_active"]:
        return jsonify(ok=False, error="আগে একটি প্যাকেজ সিলেক্ট করুন।"), 400
    try:
        c, off = evaluate(get_db(), code, pkg["product_id"], pkg["price"], g.user["id"])
    except CheckoutError as e:
        return jsonify(ok=False, error=str(e)), 200
    return jsonify(ok=True, code=c["code"], discount=off, price=pkg["price"], final=pkg["price"] - off,
                   label=label(c))
