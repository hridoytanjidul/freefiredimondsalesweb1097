"""Flash sales: a package sold at a lower price for a limited time and limited stock."""
from flask import Blueprint, abort, flash, g, redirect, render_template, url_for

import payments
from checkout import CheckoutError, active_methods, place_order
from db import expire_unpaid_gateway_orders, q, tx
from helpers import login_required, seconds_until

bp = Blueprint("flash", __name__)

SELECT = """
SELECT f.*, k.name AS package_name, k.price AS regular_price, k.in_stock AS package_in_stock,
       p.id AS product_id, p.name AS product_name, p.slug, p.image, p.input_label, p.numeric_id,
       p.description, p.delivery_note, p.kind,
       CASE WHEN datetime('now') < f.starts_at THEN 'upcoming'
            WHEN datetime('now') >= f.ends_at THEN 'ended'
            WHEN f.sold >= f.stock THEN 'soldout'
            ELSE 'live' END AS state
FROM flash_sales f
JOIN packages k ON k.id = f.package_id
JOIN products p ON p.id = k.product_id
"""

STATE_MESSAGES = {
    "upcoming": "ফ্ল্যাশ সেল এখনো শুরু হয়নি।",
    "ended": "ফ্ল্যাশ সেল শেষ হয়ে গেছে।",
    "soldout": "দুঃখিত, এই ফ্ল্যাশ সেলের স্টক শেষ!",
}


def decorate(row):
    d = dict(row)
    d["left"] = max(0, d["stock"] - d["sold"])
    d["pct_sold"] = min(100, round(d["sold"] * 100 / d["stock"])) if d["stock"] else 100
    d["off"] = max(0, round((d["regular_price"] - d["sale_price"]) * 100 / d["regular_price"])) if d["regular_price"] else 0
    d["secs"] = seconds_until(d["starts_at"] if d["state"] == "upcoming" else d["ends_at"])
    return d


def visible_sales(limit=None):
    """Live sales first (soonest ending), then sold-out ones until they end, then upcoming."""
    sql = (SELECT + " WHERE f.is_active = 1 AND p.is_active = 1 AND f.ends_at > datetime('now') "
           "ORDER BY CASE WHEN datetime('now') < f.starts_at THEN 2 WHEN f.sold >= f.stock THEN 1 ELSE 0 END, "
           "f.ends_at, f.id")
    if limit:
        sql += " LIMIT %d" % int(limit)
    return [decorate(r) for r in q(sql)]


def has_visible_sales():
    return q("SELECT 1 FROM flash_sales WHERE is_active = 1 AND ends_at > datetime('now') LIMIT 1", one=True) is not None


def get_sale(fid):
    row = q(SELECT + " WHERE f.id = ? AND f.is_active = 1 AND p.is_active = 1", (fid,), one=True)
    return decorate(row) if row else None


@bp.get("/flashsale")
def index():
    return render_template("flashsale.html", sales=visible_sales())


@bp.get("/flashsale/<int:fid>")
@bp.get("/flashsale/<int:fid>/<slug>")
def detail(fid, slug=None):
    f = get_sale(fid)
    if f is None:
        abort(404)
    mine = 0
    if g.user:
        mine = q("SELECT COUNT(*) AS n FROM orders WHERE user_id = ? AND flash_sale_id = ? AND status <> 'cancelled'",
                 (g.user["id"], fid), one=True)["n"]
    return render_template("flash_detail.html", f=f, mine=mine, methods=active_methods(),
                           gateway=payments.enabled(g.settings),
                           id_checker=bool(g.settings.get("ff_id_checker_url", "").strip()))


@bp.post("/flashsale/<int:fid>/order")
@login_required
def order(fid):
    with tx() as db:
        expire_unpaid_gateway_orders(db)   # free stock held by abandoned gateway payments
    f = get_sale(fid)
    if f is None:
        abort(404)
    back = url_for("flash.detail", fid=fid, slug=f["slug"])
    if f["state"] != "live":
        flash(STATE_MESSAGES[f["state"]], "error")
        return redirect(back)
    if not f["package_in_stock"]:
        flash("এই প্যাকেজটি এখন স্টকে নেই।", "error")
        return redirect(back)

    uid, limit = g.user["id"], f["per_user_limit"]

    def reserve(db):
        if limit:
            n = db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND flash_sale_id = ? "
                           "AND status <> 'cancelled'", (uid, fid)).fetchone()[0]
            if n >= limit:
                raise CheckoutError("এই ফ্ল্যাশ সেলে একজন সর্বোচ্চ %d বার কিনতে পারবেন।" % limit)
        cur = db.execute("UPDATE flash_sales SET sold = sold + 1 WHERE id = ? AND is_active = 1 AND sold < stock "
                         "AND datetime('now') >= starts_at AND datetime('now') < ends_at", (fid,))
        if cur.rowcount != 1:
            raise CheckoutError("দুঃখিত, স্টক শেষ অথবা সময় শেষ হয়ে গেছে।")

    product = {"id": f["product_id"], "name": f["product_name"], "numeric_id": f["numeric_id"], "kind": f["kind"]}
    # Flash prices are already discounted, so coupons don't stack on top.
    return place_order(product, f["package_id"], f["package_name"], f["sale_price"], back,
                       flash_sale_id=fid, reserve=reserve, allow_coupon=False)
