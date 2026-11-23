"""Admin panel (/admin). Only users with is_admin = 1 can open it."""
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import coupons as coupon_lib
from db import (InsufficientBalance, cancel_order, credit, debit, deliver_voucher, expire_unpaid_gateway_orders, q,
                save_settings, tx)
from helpers import (HEX_COLOR_RE, admin_required, bd_input_to_utc, delete_upload, safe_link, safe_next,
                     save_upload, slugify, to_int, utc_to_bd_input)

bp = Blueprint("admin", __name__, url_prefix="/admin")


class AdminError(Exception):
    pass


@bp.before_request
@admin_required
def _guard():
    return None


def _page():
    return max(to_int(request.args.get("page"), 1) or 1, 1)


def _today_utc_start():
    """Start of today in Bangladesh time (UTC+6), expressed as a UTC timestamp string."""
    bd_now = datetime.utcnow() + timedelta(hours=6)
    bd_midnight = bd_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (bd_midnight - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")


# =========================================================================== dashboard

@bp.get("/")
def dashboard():
    with tx() as db:
        expire_unpaid_gateway_orders(db)
    today = _today_utc_start()
    stats = {
        "to_deliver": q("SELECT COUNT(*) n FROM orders WHERE status IN ('pending','processing') "
                        "AND payment_status = 'paid'", one=True)["n"],
        "to_verify": q("SELECT COUNT(*) n FROM orders WHERE status IN ('pending','processing') "
                       "AND payment_status = 'unverified'", one=True)["n"],
        "wallet_pending": q("SELECT COUNT(*) n FROM wallet_requests WHERE status = 'pending'", one=True)["n"],
        "today_sales": q("SELECT COALESCE(SUM(price),0) s FROM orders WHERE status = 'completed' AND updated_at >= ?",
                         (today,), one=True)["s"],
        "today_orders": q("SELECT COUNT(*) n FROM orders WHERE created_at >= ?", (today,), one=True)["n"],
        "total_sales": q("SELECT COALESCE(SUM(price),0) s FROM orders WHERE status = 'completed'", one=True)["s"],
        "users": q("SELECT COUNT(*) n FROM users", one=True)["n"],
        "wallet_total": q("SELECT COALESCE(SUM(balance),0) s FROM users", one=True)["s"],
    }
    placeholder_numbers = q("SELECT name FROM payment_methods WHERE is_active = 1 AND number LIKE '%X%'")
    low_stock = q("SELECT * FROM (SELECT k.id, k.name, p.id AS product_id, p.name AS product_name, "
                  "(SELECT COUNT(*) FROM voucher_codes v WHERE v.package_id = k.id AND v.status = 'available') AS n "
                  "FROM packages k JOIN products p ON p.id = k.product_id "
                  "WHERE p.kind = 'voucher' AND p.is_active = 1 AND k.in_stock = 1) WHERE n < 3 ORDER BY n, product_id")
    queue = q("SELECT o.*, u.name AS user_name FROM orders o JOIN users u ON u.id = o.user_id "
              "WHERE o.status IN ('pending','processing') AND o.payment_status IN ('paid','unverified') "
              "ORDER BY o.id ASC LIMIT 15")
    return render_template("admin/dashboard.html", stats=stats, queue=queue, low_stock=low_stock,
                           placeholder_numbers=placeholder_numbers, nav="dashboard")


# =========================================================================== orders

ORDER_FILTERS = {
    "deliver": ("o.status IN ('pending','processing') AND o.payment_status = 'paid'", "To deliver"),
    "verify": ("o.status IN ('pending','processing') AND o.payment_status = 'unverified'", "To verify"),
    "completed": ("o.status = 'completed'", "Completed"),
    "cancelled": ("o.status = 'cancelled'", "Cancelled"),
    "unpaid": ("o.payment_status = 'awaiting' AND o.status <> 'cancelled'", "Unpaid (gateway)"),
    "all": ("1 = 1", "All"),
}


@bp.get("/orders")
def orders():
    f = request.args.get("f", "deliver")
    if f not in ORDER_FILTERS:
        f = "deliver"
    where, _ = ORDER_FILTERS[f]
    args = []
    flash_id = to_int(request.args.get("flash"))
    if flash_id:
        where += " AND o.flash_sale_id = ?"
        args.append(flash_id)
    search = (request.args.get("q") or "").strip()
    if search:
        where += " AND (o.player_id LIKE ? OR o.trx_id LIKE ? OR u.email LIKE ? OR u.name LIKE ? OR o.id = ?)"
        like = "%" + search + "%"
        args += [like, like, like, like, to_int(search.lstrip("#"), -1)]
    page, per = _page(), 30
    rows = q("SELECT o.*, u.name AS user_name, u.email AS user_email FROM orders o JOIN users u ON u.id = o.user_id "
             "WHERE " + where + " ORDER BY o.id DESC LIMIT ? OFFSET ?", tuple(args) + (per + 1, (page - 1) * per))
    counts = {k: q("SELECT COUNT(*) n FROM orders o WHERE " + v[0], one=True)["n"]
              for k, v in ORDER_FILTERS.items() if k in ("deliver", "verify", "unpaid")}
    return render_template("admin/orders.html", rows=rows[:per], has_next=len(rows) > per, page=page,
                           f=f, filters=ORDER_FILTERS, counts=counts, search=search, flash_id=flash_id, nav="orders")


@bp.get("/orders/<int:oid>")
def order_detail(oid):
    o = q("SELECT o.*, u.name AS user_name, u.email AS user_email, u.phone AS user_phone, u.balance AS user_balance "
          "FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?", (oid,), one=True)
    if o is None:
        abort(404)
    gp = q("SELECT * FROM gateway_payments WHERE purpose = 'order' AND ref_id = ? ORDER BY id DESC", (oid,))
    vc = q("SELECT * FROM voucher_codes WHERE order_id = ? ORDER BY id DESC LIMIT 1", (oid,), one=True)
    return render_template("admin/order_detail.html", o=o, gp=gp, vc=vc, nav="orders")


@bp.post("/orders/<int:oid>/update")
def order_update(oid):
    action = request.form.get("action")
    note = (request.form.get("note") or "").strip()[:300]
    refund = request.form.get("refund") == "1"
    try:
        with tx() as db:
            o = db.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            if o is None:
                abort(404)
            if o["status"] in ("completed", "cancelled"):
                raise AdminError("এই অর্ডার আগেই %s হয়েছে।" % o["status"])
            if action == "verify":
                if o["payment_status"] != "unverified":
                    raise AdminError("এই অর্ডারের পেমেন্ট যাচাইয়ের দরকার নেই।")
                db.execute("UPDATE orders SET payment_status='paid', updated_at=datetime('now') WHERE id = ?", (oid,))
                msg = "পেমেন্ট যাচাই করা হয়েছে।"
                if o["is_voucher"]:
                    if not deliver_voucher(db, oid):
                        raise AdminError("স্টকে কোনো কোড নেই — আগে কোড আপলোড করুন।")
                    msg += " কোড কাস্টমারকে দেওয়া হয়েছে।"
            elif action == "processing":
                db.execute("UPDATE orders SET status='processing', updated_at=datetime('now') WHERE id = ?", (oid,))
                msg = "অর্ডার Processing করা হয়েছে।"
            elif action == "complete":
                if o["payment_status"] == "awaiting":
                    raise AdminError("গেটওয়ে পেমেন্ট এখনও হয়নি — Complete করা যাবে না।")
                if o["is_voucher"] and not deliver_voucher(db, oid):
                    raise AdminError("স্টকে কোনো কোড নেই — আগে কোড আপলোড করুন।")
                db.execute("UPDATE orders SET status='completed', payment_status='paid', admin_note=?, "
                           "updated_at=datetime('now') WHERE id = ?", (note or o["admin_note"], oid))
                msg = "অর্ডার #%d Complete হয়েছে।" % oid
            elif action == "cancel":
                refunded = cancel_order(db, o, note or "Order cancelled", refund=refund)
                msg = "অর্ডার #%d Cancel হয়েছে।" % oid
                if refunded:
                    msg += " %d টাকা ওয়ালেটে ফেরত দেওয়া হয়েছে।" % refunded
            else:
                raise AdminError("Unknown action")
    except AdminError as e:
        flash(str(e), "error")
    else:
        flash(msg, "success")
    return redirect(safe_next(request.form.get("back")) or url_for("admin.order_detail", oid=oid))


# =========================================================================== wallet requests

@bp.get("/wallet")
def wallet_requests():
    status = request.args.get("status", "pending")
    if status not in ("pending", "approved", "rejected"):
        status = "pending"
    page, per = _page(), 30
    rows = q("SELECT w.*, u.name AS user_name, u.email AS user_email FROM wallet_requests w "
             "JOIN users u ON u.id = w.user_id WHERE w.status = ? ORDER BY w.id DESC LIMIT ? OFFSET ?",
             (status, per + 1, (page - 1) * per))
    gw = q("SELECT gp.*, u.name AS user_name FROM gateway_payments gp JOIN users u ON u.id = gp.user_id "
           "WHERE gp.status = 'paid' ORDER BY gp.id DESC LIMIT 10")
    return render_template("admin/wallet.html", rows=rows[:per], has_next=len(rows) > per, page=page,
                           status=status, gw=gw, nav="wallet")


@bp.post("/wallet/<int:wid>/update")
def wallet_update(wid):
    action = request.form.get("action")
    note = (request.form.get("note") or "").strip()[:300]
    try:
        with tx() as db:
            w = db.execute("SELECT * FROM wallet_requests WHERE id = ?", (wid,)).fetchone()
            if w is None:
                abort(404)
            if w["status"] != "pending":
                raise AdminError("এই রিকোয়েস্ট আগেই %s হয়েছে।" % w["status"])
            if action == "approve":
                amount = to_int(request.form.get("amount"), w["amount"], lo=1, hi=1000000)
                db.execute("UPDATE wallet_requests SET status='approved', amount=?, admin_note=?, "
                           "updated_at=datetime('now') WHERE id = ?", (amount, note or None, wid))
                credit(db, w["user_id"], amount, "deposit", "Add money via %s (%s)" % (w["method"], w["trx_id"]))
                msg = "%d টাকা যোগ করা হয়েছে।" % amount
            elif action == "reject":
                db.execute("UPDATE wallet_requests SET status='rejected', admin_note=?, updated_at=datetime('now') "
                           "WHERE id = ?", (note or "Payment not found", wid))
                msg = "রিকোয়েস্ট Reject করা হয়েছে।"
            else:
                raise AdminError("Unknown action")
    except AdminError as e:
        flash(str(e), "error")
    else:
        flash(msg, "success")
    return redirect(url_for("admin.wallet_requests"))


# =========================================================================== products & packages

@bp.get("/products")
def products():
    rows = q("SELECT p.*, c.name AS category, (SELECT COUNT(*) FROM packages k WHERE k.product_id = p.id) AS pkg_count "
             "FROM products p LEFT JOIN categories c ON c.id = p.category_id ORDER BY c.sort_order, p.sort_order, p.id")
    return render_template("admin/products.html", rows=rows, nav="products")


def _product_from_form(form, existing=None):
    name = (form.get("name") or "").strip()[:80]
    if len(name) < 2:
        raise AdminError("প্রোডাক্টের নাম দিন।")
    cat = to_int(form.get("category_id"))
    if not q("SELECT 1 FROM categories WHERE id = ?", (cat,), one=True):
        raise AdminError("একটি ক্যাটাগরি সিলেক্ট করুন।")
    return {
        "name": name,
        "slug": slugify(name),
        "category_id": cat,
        "tag_line": (form.get("tag_line") or "").strip()[:30] or None,
        "delivery_note": (form.get("delivery_note") or "").strip()[:60] or None,
        "input_label": (form.get("input_label") or "").strip()[:80] or "এখানে গেমের Player ID দিন",
        "numeric_id": 1 if form.get("numeric_id") == "1" else 0,
        "description": form.get("description") or "",
        "sort_order": to_int(form.get("sort_order"), 0),
        "is_active": 1 if form.get("is_active") == "1" else 0,
        "kind": "voucher" if form.get("kind") == "voucher" else "topup",
        "redeem_link": _redeem_link(form.get("redeem_link")),
    }


def _redeem_link(value):
    try:
        return safe_link(value) or None
    except ValueError as e:
        raise AdminError(str(e))


@bp.route("/products/new", methods=["GET", "POST"])
def product_new():
    cats = q("SELECT * FROM categories ORDER BY sort_order, id")
    if request.method == "POST":
        try:
            data = _product_from_form(request.form)
            data["image"] = save_upload(request.files.get("image"), "product")
        except (AdminError, ValueError) as e:
            flash(str(e), "error")
            return render_template("admin/product_form.html", p=request.form, cats=cats, pkgs=[], nav="products")
        cols = ", ".join(data)
        with tx() as db:
            pid = db.execute("INSERT INTO products (%s) VALUES (%s)" % (cols, ",".join("?" * len(data))),
                             tuple(data.values())).lastrowid
        flash("প্রোডাক্ট তৈরি হয়েছে। এখন প্যাকেজ যোগ করুন।", "success")
        return redirect(url_for("admin.product_edit", pid=pid))
    return render_template("admin/product_form.html", p={"is_active": 1, "numeric_id": 1}, cats=cats, pkgs=[],
                           nav="products")


@bp.route("/products/<int:pid>", methods=["GET", "POST"])
def product_edit(pid):
    p = q("SELECT * FROM products WHERE id = ?", (pid,), one=True)
    if p is None:
        abort(404)
    cats = q("SELECT * FROM categories ORDER BY sort_order, id")
    if request.method == "POST":
        try:
            data = _product_from_form(request.form, p)
            new_img = save_upload(request.files.get("image"), "product")
        except (AdminError, ValueError) as e:
            flash(str(e), "error")
            return redirect(url_for("admin.product_edit", pid=pid))
        old_img = p["image"]
        if new_img:
            data["image"] = new_img
        elif request.form.get("remove_image") == "1":
            data["image"] = None
        with tx() as db:
            db.execute("UPDATE products SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in data),
                       tuple(data.values()) + (pid,))
        if "image" in data and old_img and old_img != data["image"]:
            delete_upload(old_img)
        flash("প্রোডাক্ট আপডেট হয়েছে।", "success")
        return redirect(url_for("admin.product_edit", pid=pid))
    pkgs = q("SELECT k.*, "
             "(SELECT COUNT(*) FROM voucher_codes v WHERE v.package_id = k.id AND v.status = 'available') AS codes_left, "
             "(SELECT COUNT(*) FROM voucher_codes v WHERE v.package_id = k.id AND v.status = 'delivered') AS codes_sold "
             "FROM packages k WHERE k.product_id = ? ORDER BY k.sort_order, k.id", (pid,))
    return render_template("admin/product_form.html", p=p, cats=cats, pkgs=pkgs, nav="products")


@bp.post("/products/<int:pid>/delete")
def product_delete(pid):
    p = q("SELECT * FROM products WHERE id = ?", (pid,), one=True)
    if p is None:
        abort(404)
    if q("SELECT 1 FROM voucher_codes v JOIN packages k ON k.id = v.package_id WHERE k.product_id = ? "
         "AND v.status = 'reserved' LIMIT 1", (pid,), one=True):
        flash("এই প্রোডাক্টের কিছু অর্ডারের পেমেন্ট যাচাই বাকি — আগে সেগুলো Complete/Cancel করুন।", "error")
        return redirect(url_for("admin.product_edit", pid=pid))
    with tx() as db:
        db.execute("DELETE FROM products WHERE id = ?", (pid,))
    delete_upload(p["image"])
    flash("প্রোডাক্ট ডিলিট হয়েছে (পুরনো অর্ডারগুলো থাকবে)।", "success")
    return redirect(url_for("admin.products"))


def _package_from_form(form):
    name = (form.get("name") or "").strip()[:60]
    price = to_int(form.get("price"), None, lo=1, hi=1000000)
    if not name or price is None:
        raise AdminError("প্যাকেজের নাম ও সঠিক দাম দিন।")
    return name, price, 1 if form.get("in_stock") == "1" else 0, to_int(form.get("sort_order"), 0)


@bp.post("/products/<int:pid>/packages")
def package_add(pid):
    if not q("SELECT 1 FROM products WHERE id = ?", (pid,), one=True):
        abort(404)
    try:
        name, price, stock, order = _package_from_form(request.form)
    except AdminError as e:
        flash(str(e), "error")
    else:
        with tx() as db:
            db.execute("INSERT INTO packages (product_id, name, price, in_stock, sort_order) VALUES (?,?,?,?,?)",
                       (pid, name, price, stock, order))
        flash("প্যাকেজ যোগ হয়েছে।", "success")
    return redirect(url_for("admin.product_edit", pid=pid) + "#packages")


@bp.post("/packages/<int:kid>/update")
def package_update(kid):
    k = q("SELECT * FROM packages WHERE id = ?", (kid,), one=True)
    if k is None:
        abort(404)
    if request.form.get("delete") == "1":
        if q("SELECT 1 FROM voucher_codes WHERE package_id = ? AND status = 'reserved' LIMIT 1", (kid,), one=True):
            flash("এই প্যাকেজের কিছু অর্ডারের পেমেন্ট যাচাই বাকি — আগে সেগুলো Complete/Cancel করুন।", "error")
            return redirect(url_for("admin.product_edit", pid=k["product_id"]) + "#packages")
        with tx() as db:
            db.execute("DELETE FROM packages WHERE id = ?", (kid,))
        flash("প্যাকেজ ডিলিট হয়েছে।", "success")
    else:
        try:
            name, price, stock, order = _package_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
        else:
            with tx() as db:
                db.execute("UPDATE packages SET name=?, price=?, in_stock=?, sort_order=? WHERE id = ?",
                           (name, price, stock, order, kid))
            flash("প্যাকেজ আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.product_edit", pid=k["product_id"]) + "#packages")


# =========================================================================== categories

@bp.route("/categories", methods=["GET", "POST"])
def categories():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:60]
        if not name:
            flash("ক্যাটাগরির নাম দিন।", "error")
        else:
            with tx() as db:
                db.execute("INSERT INTO categories (name, sort_order, is_active) VALUES (?,?,1)",
                           (name, to_int(request.form.get("sort_order"), 0)))
            flash("ক্যাটাগরি যোগ হয়েছে।", "success")
        return redirect(url_for("admin.categories"))
    rows = q("SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id) AS n FROM categories c "
             "ORDER BY sort_order, id")
    return render_template("admin/categories.html", rows=rows, nav="categories")


@bp.post("/categories/<int:cid>/update")
def category_update(cid):
    if request.form.get("delete") == "1":
        try:
            with tx() as db:
                db.execute("DELETE FROM categories WHERE id = ?", (cid,))
            flash("ক্যাটাগরি ডিলিট হয়েছে।", "success")
        except sqlite3.IntegrityError:
            flash("এই ক্যাটাগরিতে প্রোডাক্ট আছে — আগে প্রোডাক্টগুলো সরান।", "error")
    else:
        name = (request.form.get("name") or "").strip()[:60]
        if not name:
            flash("ক্যাটাগরির নাম দিন।", "error")
        else:
            with tx() as db:
                db.execute("UPDATE categories SET name=?, sort_order=?, is_active=? WHERE id = ?",
                           (name, to_int(request.form.get("sort_order"), 0),
                            1 if request.form.get("is_active") == "1" else 0, cid))
            flash("ক্যাটাগরি আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.categories"))


# =========================================================================== banners

@bp.route("/banners", methods=["GET", "POST"])
def banners():
    if request.method == "POST":
        try:
            img = save_upload(request.files.get("image"), "banner")
            if not img:
                raise ValueError("ব্যানারের ছবি সিলেক্ট করুন।")
            link = safe_link(request.form.get("link"))
        except ValueError as e:
            flash(str(e), "error")
        else:
            with tx() as db:
                db.execute("INSERT INTO banners (image, link, sort_order) VALUES (?,?,?)",
                           (img, link or None, to_int(request.form.get("sort_order"), 0)))
            flash("ব্যানার যোগ হয়েছে।", "success")
        return redirect(url_for("admin.banners"))
    rows = q("SELECT * FROM banners ORDER BY sort_order, id")
    return render_template("admin/banners.html", rows=rows, nav="banners")


@bp.post("/banners/<int:bid>/update")
def banner_update(bid):
    b = q("SELECT * FROM banners WHERE id = ?", (bid,), one=True)
    if b is None:
        abort(404)
    if request.form.get("delete") == "1":
        with tx() as db:
            db.execute("DELETE FROM banners WHERE id = ?", (bid,))
        delete_upload(b["image"])
        flash("ব্যানার ডিলিট হয়েছে।", "success")
    else:
        try:
            link = safe_link(request.form.get("link"))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("admin.banners"))
        with tx() as db:
            db.execute("UPDATE banners SET link=?, sort_order=?, is_active=? WHERE id = ?",
                       (link or None, to_int(request.form.get("sort_order"), 0),
                        1 if request.form.get("is_active") == "1" else 0, bid))
        flash("ব্যানার আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.banners"))


# =========================================================================== payment methods

def _method_from_form(form):
    name = (form.get("name") or "").strip()[:30]
    number = (form.get("number") or "").strip()[:20]
    if not name or not number:
        raise AdminError("মেথডের নাম ও নম্বর দিন।")
    color = (form.get("color") or "").strip()
    if not HEX_COLOR_RE.match(color):
        color = "#663cbc"
    return (name, number, (form.get("account_type") or "Personal").strip()[:20], color,
            (form.get("instructions") or "").strip()[:300], to_int(form.get("sort_order"), 0),
            1 if form.get("is_active") == "1" else 0)


@bp.route("/payment-methods", methods=["GET", "POST"])
def payment_methods():
    if request.method == "POST":
        try:
            vals = _method_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
        else:
            with tx() as db:
                db.execute("INSERT INTO payment_methods (name, number, account_type, color, instructions, sort_order, "
                           "is_active) VALUES (?,?,?,?,?,?,?)", vals)
            flash("পেমেন্ট মেথড যোগ হয়েছে।", "success")
        return redirect(url_for("admin.payment_methods"))
    rows = q("SELECT * FROM payment_methods ORDER BY sort_order, id")
    return render_template("admin/payment_methods.html", rows=rows, nav="methods")


@bp.post("/payment-methods/<int:mid>/update")
def payment_method_update(mid):
    if request.form.get("delete") == "1":
        with tx() as db:
            db.execute("DELETE FROM payment_methods WHERE id = ?", (mid,))
        flash("পেমেন্ট মেথড ডিলিট হয়েছে।", "success")
    else:
        try:
            vals = _method_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
        else:
            with tx() as db:
                db.execute("UPDATE payment_methods SET name=?, number=?, account_type=?, color=?, instructions=?, "
                           "sort_order=?, is_active=? WHERE id = ?", vals + (mid,))
            flash("পেমেন্ট মেথড আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.payment_methods"))


# =========================================================================== users

@bp.get("/users")
def users():
    search = (request.args.get("q") or "").strip()
    page, per = _page(), 30
    where, args = "1 = 1", []
    if search:
        like = "%" + search + "%"
        where = "(name LIKE ? OR email LIKE ? OR phone LIKE ? OR id = ?)"
        args = [like, like, like, to_int(search, -1)]
    rows = q("SELECT * FROM users WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
             tuple(args) + (per + 1, (page - 1) * per))
    return render_template("admin/users.html", rows=rows[:per], has_next=len(rows) > per, page=page,
                           search=search, nav="users")


@bp.get("/users/<int:uid>")
def user_detail(uid):
    u = q("SELECT * FROM users WHERE id = ?", (uid,), one=True)
    if u is None:
        abort(404)
    orders = q("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 20", (uid,))
    txs = q("SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 20", (uid,))
    spent = q("SELECT COALESCE(SUM(price),0) s FROM orders WHERE user_id = ? AND status = 'completed'",
              (uid,), one=True)["s"]
    return render_template("admin/user_detail.html", u=u, orders=orders, txs=txs, spent=spent, nav="users")


@bp.post("/users/<int:uid>/balance")
def user_balance(uid):
    amount = to_int(request.form.get("amount"))
    note = (request.form.get("note") or "").strip()[:200] or "Adjusted by admin"
    if not amount:
        flash("টাকার পরিমাণ দিন (কমাতে চাইলে মাইনাস দিন, যেমন -50)।", "error")
        return redirect(url_for("admin.user_detail", uid=uid))
    try:
        with tx() as db:
            if amount > 0:
                credit(db, uid, amount, "adjustment", note)
            else:
                debit(db, uid, -amount, "adjustment", note)
    except InsufficientBalance:
        flash("ইউজারের ব্যালেন্স এর চেয়ে কম।", "error")
    else:
        flash("ব্যালেন্স আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.user_detail", uid=uid))


@bp.post("/users/<int:uid>/toggle")
def user_toggle(uid):
    field = request.form.get("field")
    if field not in ("is_banned", "is_admin"):
        abort(400)
    if uid == g.user["id"]:
        flash("নিজের অ্যাকাউন্টে এটি করা যাবে না।", "error")
        return redirect(url_for("admin.user_detail", uid=uid))
    with tx() as db:
        db.execute("UPDATE users SET %s = 1 - %s WHERE id = ?" % (field, field), (uid,))
    flash("আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.user_detail", uid=uid))


# =========================================================================== settings

TEXT_FIELDS = ["site_name", "site_tagline", "meta_description", "notice_text", "helpline_title",
               "helpline_hours", "footer_text", "popup_text", "popup_button"]
LINK_FIELDS = ["telegram_support", "telegram_group", "app_link", "popup_link"]
URL_FIELDS = ["ff_id_checker_url", "uddoktapay_base_url"]
CHECKBOXES = ["notice_enabled", "popup_enabled", "show_recent_orders"]
IMAGE_FIELDS = ["logo", "favicon", "popup_image"]


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        f = request.form
        vals = {}
        try:
            for k in TEXT_FIELDS:
                vals[k] = (f.get(k) or "").strip()
            if not vals["site_name"]:
                raise ValueError("সাইটের নাম দিন।")
            for k in LINK_FIELDS:
                vals[k] = safe_link(f.get(k))
            for k in URL_FIELDS:
                v = (f.get(k) or "").strip()
                if v and not v.lower().startswith(("https://", "http://")):
                    raise ValueError("%s অবশ্যই https:// দিয়ে শুরু হতে হবে।" % k)
                vals[k] = v
            for k in CHECKBOXES:
                vals[k] = "1" if f.get(k) == "1" else "0"
            color = (f.get("theme_color") or "").strip()
            if not HEX_COLOR_RE.match(color):
                raise ValueError("থিম কালার #RRGGBB ফরম্যাটে দিন।")
            vals["theme_color"] = color
            lo = to_int(f.get("min_wallet_amount"), None, lo=1)
            hi = to_int(f.get("max_wallet_amount"), None, lo=1)
            if lo is None or hi is None or lo > hi:
                raise ValueError("Add Money এর সর্বনিম্ন/সর্বোচ্চ পরিমাণ ঠিক করুন।")
            vals["min_wallet_amount"], vals["max_wallet_amount"] = str(lo), str(hi)
            key = (f.get("uddoktapay_api_key") or "").strip()
            if f.get("clear_api_key") == "1":
                vals["uddoktapay_api_key"] = ""
            elif key:
                vals["uddoktapay_api_key"] = key
            gid = (f.get("google_client_id") or "").strip()
            if gid and not gid.endswith(".apps.googleusercontent.com"):
                raise ValueError("Google Client ID সাধারণত .apps.googleusercontent.com দিয়ে শেষ হয়।")
            vals["google_client_id"] = gid
            gsecret = (f.get("google_client_secret") or "").strip()
            if f.get("clear_google_secret") == "1":
                vals["google_client_secret"] = ""
            elif gsecret:
                vals["google_client_secret"] = gsecret
            old_files = []
            for k in IMAGE_FIELDS:
                new = save_upload(request.files.get(k), k)
                if new:
                    old_files.append(g.settings.get(k))
                    vals[k] = new
                elif f.get("remove_" + k) == "1":
                    old_files.append(g.settings.get(k))
                    vals[k] = ""
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("admin.settings"))
        with tx() as db:
            save_settings(db, vals)
        for old in old_files:
            delete_upload(old)
        flash("সেটিংস সেভ হয়েছে।", "success")
        return redirect(url_for("admin.settings"))
    import google_auth
    return render_template("admin/settings.html", nav="settings", google_redirect=google_auth.redirect_uri())


# =========================================================================== pages

@bp.get("/pages")
def pages():
    rows = q("SELECT slug, title, updated_at FROM pages ORDER BY slug")
    return render_template("admin/pages.html", rows=rows, nav="pages")


@bp.route("/pages/<slug>", methods=["GET", "POST"])
def page_edit(slug):
    page = q("SELECT * FROM pages WHERE slug = ?", (slug,), one=True)
    if page is None:
        abort(404)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:80] or page["title"]
        with tx() as db:
            db.execute("UPDATE pages SET title = ?, content = ?, updated_at = datetime('now') WHERE slug = ?",
                       (title, request.form.get("content") or "", slug))
        flash("পেজ সেভ হয়েছে।", "success")
        return redirect(url_for("admin.page_edit", slug=slug))
    return render_template("admin/page_form.html", page=page, nav="pages")


# =========================================================================== gift voucher codes

@bp.get("/vouchers")
def vouchers():
    rows = q("SELECT k.id, k.name, k.price, k.in_stock, p.id AS product_id, p.name AS product_name, p.is_active, "
             "SUM(CASE WHEN v.status = 'available' THEN 1 ELSE 0 END) AS available, "
             "SUM(CASE WHEN v.status = 'reserved' THEN 1 ELSE 0 END) AS reserved, "
             "SUM(CASE WHEN v.status = 'delivered' THEN 1 ELSE 0 END) AS delivered "
             "FROM packages k JOIN products p ON p.id = k.product_id LEFT JOIN voucher_codes v ON v.package_id = k.id "
             "WHERE p.kind = 'voucher' GROUP BY k.id ORDER BY p.sort_order, p.id, k.sort_order, k.id")
    return render_template("admin/vouchers.html", rows=rows, nav="vouchers")


@bp.route("/packages/<int:kid>/codes", methods=["GET", "POST"])
def package_codes(kid):
    k = q("SELECT k.*, p.name AS product_name, p.kind FROM packages k JOIN products p ON p.id = k.product_id "
          "WHERE k.id = ?", (kid,), one=True)
    if k is None:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            lines = [ln.strip() for ln in (request.form.get("codes") or "").splitlines() if ln.strip()]
            codes = list(dict.fromkeys(lines))[:5000]   # unique, keep order
            too_long = [c for c in codes if len(c) > 500]
            if not codes:
                flash("কোড দিন (প্রতি লাইনে একটি)।", "error")
            elif too_long:
                flash("একটি কোড ৫০০ অক্ষরের বেশি হতে পারবে না।", "error")
            else:
                with tx() as db:
                    added = 0
                    for c in codes:
                        cur = db.execute("INSERT OR IGNORE INTO voucher_codes (package_id, code) VALUES (?, ?)", (kid, c))
                        added += cur.rowcount
                skipped = len(lines) - added   # repeats inside the paste + codes already in stock
                flash("%d টি কোড যোগ হয়েছে।%s" % (added, " %d টি ডুপ্লিকেট কোড বাদ দেওয়া হয়েছে।" % skipped if skipped else ""),
                      "success")
        elif action == "delete":
            with tx() as db:
                cur = db.execute("DELETE FROM voucher_codes WHERE id = ? AND package_id = ? AND status = 'available'",
                                 (to_int(request.form.get("code_id"), 0), kid))
            flash("কোড ডিলিট হয়েছে।" if cur.rowcount else "শুধু অবিক্রিত কোড ডিলিট করা যায়।",
                  "success" if cur.rowcount else "error")
        elif action == "delete_all":
            with tx() as db:
                cur = db.execute("DELETE FROM voucher_codes WHERE package_id = ? AND status = 'available'", (kid,))
            flash("%d টি অবিক্রিত কোড ডিলিট হয়েছে।" % cur.rowcount, "success")
        return redirect(url_for("admin.package_codes", kid=kid))
    status = request.args.get("status", "available")
    if status not in ("available", "reserved", "delivered"):
        status = "available"
    counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM voucher_codes WHERE package_id = ? "
                                              "GROUP BY status", (kid,))}
    codes = q("SELECT v.*, u.name AS user_name FROM voucher_codes v LEFT JOIN orders o ON o.id = v.order_id "
              "LEFT JOIN users u ON u.id = o.user_id WHERE v.package_id = ? AND v.status = ? "
              "ORDER BY v.id DESC LIMIT 300", (kid, status))
    return render_template("admin/package_codes.html", k=k, codes=codes, counts=counts, status=status, nav="vouchers")


# =========================================================================== coupons

COUPON_LIST = ("SELECT c.*, p.name AS product_name, " + coupon_lib.TIMING_SQL.replace("starts_at", "c.starts_at")
               .replace("ends_at", "c.ends_at") + " FROM coupons c LEFT JOIN products p ON p.id = c.product_id")


def _coupon_from_form(form, coupon_id=None):
    code = coupon_lib.normalize(form.get("code"))
    if not coupon_lib.CODE_RE.match(code):
        raise AdminError("কোড ৩-৩০ অক্ষরের হতে হবে (A-Z, 0-9, - _)।")
    clash = q("SELECT id FROM coupons WHERE code = ? COLLATE NOCASE AND id <> ?", (code, coupon_id or 0), one=True)
    if clash:
        raise AdminError("এই কোড আগেই আছে।")
    dtype = "fixed" if form.get("discount_type") == "fixed" else "percent"
    value = to_int(form.get("value"), None, lo=1, hi=100 if dtype == "percent" else 1000000)
    if value is None:
        raise AdminError("ছাড়ের পরিমাণ দিন (Percent হলে 1-100)।")
    product_id = to_int(form.get("product_id"), 0)
    if product_id and not q("SELECT 1 FROM products WHERE id = ?", (product_id,), one=True):
        raise AdminError("প্রোডাক্ট পাওয়া যায়নি।")
    starts = ends = None
    try:
        if (form.get("starts_at") or "").strip():
            starts = bd_input_to_utc(form.get("starts_at"))
        if (form.get("ends_at") or "").strip():
            ends = bd_input_to_utc(form.get("ends_at"))
    except ValueError:
        raise AdminError("তারিখ/সময় সঠিকভাবে দিন।")
    if starts and ends and ends <= starts:
        raise AdminError("শেষের সময় শুরুর সময়ের পরে হতে হবে।")
    return {
        "code": code,
        "description": (form.get("description") or "").strip()[:120] or None,
        "discount_type": dtype,
        "value": value,
        "max_discount": to_int(form.get("max_discount"), 0, lo=0) or 0,
        "min_order": to_int(form.get("min_order"), 0, lo=0) or 0,
        "product_id": product_id or None,
        "usage_limit": to_int(form.get("usage_limit"), 0, lo=0) or 0,
        "per_user_limit": to_int(form.get("per_user_limit"), 0, lo=0) or 0,
        "starts_at": starts,
        "ends_at": ends,
        "is_public": 1 if form.get("is_public") == "1" else 0,
        "is_active": 1 if form.get("is_active") == "1" else 0,
    }


@bp.get("/coupons")
def coupon_list():
    personal = request.args.get("personal") == "1"
    rows = q(COUPON_LIST + (" WHERE c.user_id IS NOT NULL" if personal else " WHERE c.user_id IS NULL")
             + " ORDER BY c.is_active DESC, c.id DESC LIMIT 500")
    personal_count = q("SELECT COUNT(*) n FROM coupons WHERE user_id IS NOT NULL", one=True)["n"]
    savings = {r["coupon_id"]: r["s"] for r in q("SELECT coupon_id, SUM(discount) AS s FROM orders "
                                                 "WHERE coupon_id IS NOT NULL AND status <> 'cancelled' GROUP BY coupon_id")}
    return render_template("admin/coupons.html", rows=rows, savings=savings, nav="coupons",
                           personal=personal, personal_count=personal_count)


@bp.route("/coupons/new", methods=["GET", "POST"])
def coupon_new():
    products = q("SELECT id, name FROM products ORDER BY sort_order, id")
    if request.method == "POST":
        try:
            data = _coupon_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
            return render_template("admin/coupon_form.html", c=request.form, products=products, nav="coupons",
                                   starts=request.form.get("starts_at"), ends=request.form.get("ends_at"))
        with tx() as db:
            db.execute("INSERT INTO coupons (%s) VALUES (%s)" % (", ".join(data), ",".join("?" * len(data))),
                       tuple(data.values()))
        flash("কুপন %s তৈরি হয়েছে।" % data["code"], "success")
        return redirect(url_for("admin.coupon_list"))
    return render_template("admin/coupon_form.html", products=products, nav="coupons", starts="", ends="",
                           c={"discount_type": "percent", "per_user_limit": 1, "is_active": 1, "is_public": 0})


@bp.route("/coupons/<int:cid>", methods=["GET", "POST"])
def coupon_edit(cid):
    c = q(COUPON_LIST + " WHERE c.id = ?", (cid,), one=True)
    if c is None:
        abort(404)
    products = q("SELECT id, name FROM products ORDER BY sort_order, id")
    if request.method == "POST":
        if request.form.get("delete") == "1":
            with tx() as db:
                db.execute("DELETE FROM coupons WHERE id = ?", (cid,))
            flash("কুপন ডিলিট হয়েছে (পুরনো অর্ডারে কোডের নাম থাকবে)।", "success")
            return redirect(url_for("admin.coupon_list"))
        try:
            data = _coupon_from_form(request.form, cid)
        except AdminError as e:
            flash(str(e), "error")
            return redirect(url_for("admin.coupon_edit", cid=cid))
        with tx() as db:
            db.execute("UPDATE coupons SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in data),
                       tuple(data.values()) + (cid,))
        flash("কুপন আপডেট হয়েছে।", "success")
        return redirect(url_for("admin.coupon_edit", cid=cid))
    return render_template("admin/coupon_form.html", c=c, products=products, nav="coupons",
                           starts=utc_to_bd_input(c["starts_at"]), ends=utc_to_bd_input(c["ends_at"]))


# =========================================================================== spin wheel

def _prize_from_form(form):
    label = (form.get("label") or "").strip()[:14]
    if not label:
        raise AdminError("চাকায় দেখানোর লেখা দিন (সর্বোচ্চ ১৪ অক্ষর)।")
    kind = form.get("prize_type") if form.get("prize_type") in ("wallet", "coupon", "nothing") else "nothing"
    ctype = "fixed" if form.get("coupon_type") == "fixed" else "percent"
    amount = to_int(form.get("amount"), 0, lo=0, hi=1000000) or 0
    if kind in ("wallet", "coupon") and amount < 1:
        raise AdminError("পুরস্কারের পরিমাণ দিন।")
    if kind == "coupon" and ctype == "percent" and amount > 100:
        raise AdminError("Percent কুপন ১০০ এর বেশি হতে পারবে না।")
    color = (form.get("color") or "").strip()
    if not HEX_COLOR_RE.match(color):
        color = "#7c3aed"
    weight = to_int(form.get("weight"), None, lo=0, hi=100000)
    if weight is None:
        raise AdminError("সম্ভাবনা (weight) দিন — 0 মানে কখনো পাবে না।")
    return {"label": label, "prize_type": kind, "amount": amount if kind != "nothing" else 0, "coupon_type": ctype,
            "max_discount": to_int(form.get("max_discount"), 0, lo=0) or 0,
            "valid_days": to_int(form.get("valid_days"), 7, lo=1, hi=365) or 7, "weight": weight, "color": color,
            "sort_order": to_int(form.get("sort_order"), 0), "is_active": 1 if form.get("is_active") == "1" else 0}


@bp.route("/spin", methods=["GET", "POST"])
def spin_admin():
    if request.method == "POST":
        f = request.form
        hours = to_int(f.get("spin_cooldown_hours"), None, lo=1, hi=720)
        min_orders = to_int(f.get("spin_min_orders"), None, lo=0, hi=1000)
        budget = to_int(f.get("spin_daily_budget"), None, lo=0, hi=10000000)
        turning_on = f.get("spin_enabled") == "1"
        if hours is None or min_orders is None or budget is None:
            flash("সব ঘর সঠিকভাবে পূরণ করুন।", "error")
        elif turning_on and not q("SELECT 1 FROM spin_prizes WHERE is_active = 1 AND weight > 0", one=True):
            flash("চালু করার আগে অন্তত একটি সক্রিয় পুরস্কার (weight > 0) যোগ করুন।", "error")
        else:
            with tx() as db:
                save_settings(db, {"spin_enabled": "1" if turning_on else "0", "spin_cooldown_hours": str(hours),
                                   "spin_min_orders": str(min_orders), "spin_daily_budget": str(budget)})
            flash("স্পিন সেটিংস সেভ হয়েছে।", "success")
        return redirect(url_for("admin.spin_admin"))
    import spin as spin_lib
    prizes = q("SELECT * FROM spin_prizes ORDER BY sort_order, id")
    live = [p for p in prizes if p["is_active"] and p["weight"] > 0]
    total_w = sum(p["weight"] for p in live)
    chance = {p["id"]: (p["weight"] * 100.0 / total_w if total_w and p["is_active"] else 0) for p in prizes}
    cost = (sum(p["weight"] * p["amount"] for p in live if p["prize_type"] == "wallet") / total_w) if total_w else 0
    today = spin_lib.bd_today_start_utc()
    stats = {
        "today": q("SELECT COUNT(*) n FROM spins WHERE created_at >= ?", (today,), one=True)["n"],
        "today_paid": q("SELECT COALESCE(SUM(amount),0) s FROM spins WHERE prize_type='wallet' AND created_at >= ?",
                        (today,), one=True)["s"],
        "total": q("SELECT COUNT(*) n FROM spins", one=True)["n"],
        "total_paid": q("SELECT COALESCE(SUM(amount),0) s FROM spins WHERE prize_type='wallet'", one=True)["s"],
        "coupons": q("SELECT COUNT(*) n FROM spins WHERE prize_type='coupon'", one=True)["n"],
        "coupons_used": q("SELECT COUNT(*) n FROM coupons WHERE user_id IS NOT NULL AND used > 0", one=True)["n"],
    }
    recent = q("SELECT s.*, u.name AS user_name FROM spins s JOIN users u ON u.id = s.user_id ORDER BY s.id DESC LIMIT 25")
    return render_template("admin/spin.html", prizes=prizes, chance=chance, cost=cost, stats=stats, recent=recent,
                           nav="spin")


@bp.post("/spin/prizes")
def spin_prize_add():
    try:
        data = _prize_from_form(request.form)
    except AdminError as e:
        flash(str(e), "error")
    else:
        with tx() as db:
            db.execute("INSERT INTO spin_prizes (%s) VALUES (%s)" % (", ".join(data), ",".join("?" * len(data))),
                       tuple(data.values()))
        flash("পুরস্কার যোগ হয়েছে।", "success")
    return redirect(url_for("admin.spin_admin") + "#prizes")


@bp.post("/spin/prizes/<int:sid>")
def spin_prize_update(sid):
    if not q("SELECT 1 FROM spin_prizes WHERE id = ?", (sid,), one=True):
        abort(404)
    if request.form.get("delete") == "1":
        with tx() as db:
            db.execute("DELETE FROM spin_prizes WHERE id = ?", (sid,))
        flash("পুরস্কার ডিলিট হয়েছে।", "success")
    else:
        try:
            data = _prize_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
        else:
            with tx() as db:
                db.execute("UPDATE spin_prizes SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in data),
                           tuple(data.values()) + (sid,))
            flash("পুরস্কার আপডেট হয়েছে।", "success")
    return redirect(url_for("admin.spin_admin") + "#prizes")


# =========================================================================== flash sales

FLASH_LIST = """
SELECT f.*, k.name AS package_name, k.price AS regular_price, p.name AS product_name, p.slug, p.id AS product_id,
       CASE WHEN datetime('now') < f.starts_at THEN 'upcoming'
            WHEN datetime('now') >= f.ends_at THEN 'ended'
            WHEN f.sold >= f.stock THEN 'soldout'
            ELSE 'live' END AS state,
       (SELECT COUNT(*) FROM orders o WHERE o.flash_sale_id = f.id AND o.status = 'completed') AS delivered
FROM flash_sales f JOIN packages k ON k.id = f.package_id JOIN products p ON p.id = k.product_id
"""


def _package_choices():
    return q("SELECT k.id, k.name, k.price, p.name AS product_name FROM packages k "
             "JOIN products p ON p.id = k.product_id ORDER BY p.sort_order, p.id, k.sort_order, k.id")


def _flash_from_form(form):
    pkg = q("SELECT * FROM packages WHERE id = ?", (to_int(form.get("package_id"), 0),), one=True)
    if pkg is None:
        raise AdminError("একটি প্যাকেজ সিলেক্ট করুন।")
    price = to_int(form.get("sale_price"), None, lo=1, hi=1000000)
    if price is None:
        raise AdminError("ফ্ল্যাশ সেলের দাম দিন।")
    stock = to_int(form.get("stock"), None, lo=1, hi=1000000)
    if stock is None:
        raise AdminError("কয়টি বিক্রি হবে (Stock) দিন।")
    limit = to_int(form.get("per_user_limit"), None, lo=0, hi=1000)
    if limit is None:
        raise AdminError("প্রতি ইউজার লিমিট দিন (0 = কোনো লিমিট নেই)।")
    try:
        starts = bd_input_to_utc(form.get("starts_at"))
        ends = bd_input_to_utc(form.get("ends_at"))
    except ValueError:
        raise AdminError("শুরু ও শেষের সময় দিন।")
    if ends <= starts:
        raise AdminError("শেষের সময় শুরুর সময়ের পরে হতে হবে।")
    return {"package_id": pkg["id"], "sale_price": price, "stock": stock, "per_user_limit": limit,
            "starts_at": starts, "ends_at": ends, "is_active": 1 if form.get("is_active") == "1" else 0}


@bp.get("/flash-sales")
def flash_sales():
    rows = q(FLASH_LIST + " ORDER BY CASE WHEN datetime('now') >= f.ends_at THEN 1 ELSE 0 END, f.starts_at DESC, f.id DESC")
    return render_template("admin/flash_sales.html", rows=rows, nav="flash")


@bp.route("/flash-sales/new", methods=["GET", "POST"])
def flash_new():
    if request.method == "POST":
        try:
            data = _flash_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
            return render_template("admin/flash_form.html", f=request.form, pkgs=_package_choices(), nav="flash",
                                   starts=request.form.get("starts_at"), ends=request.form.get("ends_at"))
        with tx() as db:
            db.execute("INSERT INTO flash_sales (%s) VALUES (%s)" % (", ".join(data), ",".join("?" * len(data))),
                       tuple(data.values()))
        flash("ফ্ল্যাশ সেল তৈরি হয়েছে।", "success")
        return redirect(url_for("admin.flash_sales"))
    bd_now = datetime.utcnow() + timedelta(hours=6)
    return render_template("admin/flash_form.html", f={"is_active": 1, "per_user_limit": 1, "stock": 50},
                           pkgs=_package_choices(), nav="flash",
                           starts=bd_now.strftime("%Y-%m-%dT%H:%M"),
                           ends=(bd_now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M"))


@bp.route("/flash-sales/<int:fid>", methods=["GET", "POST"])
def flash_edit(fid):
    f = q(FLASH_LIST + " WHERE f.id = ?", (fid,), one=True)
    if f is None:
        abort(404)
    if request.method == "POST":
        if request.form.get("delete") == "1":
            with tx() as db:
                db.execute("DELETE FROM flash_sales WHERE id = ?", (fid,))
            flash("ফ্ল্যাশ সেল ডিলিট হয়েছে (পুরনো অর্ডারগুলো থাকবে)।", "success")
            return redirect(url_for("admin.flash_sales"))
        try:
            data = _flash_from_form(request.form)
        except AdminError as e:
            flash(str(e), "error")
            return redirect(url_for("admin.flash_edit", fid=fid))
        with tx() as db:
            db.execute("UPDATE flash_sales SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in data),
                       tuple(data.values()) + (fid,))
        flash("ফ্ল্যাশ সেল আপডেট হয়েছে।", "success")
        return redirect(url_for("admin.flash_edit", fid=fid))
    return render_template("admin/flash_form.html", f=f, pkgs=_package_choices(), nav="flash",
                           starts=utc_to_bd_input(f["starts_at"]), ends=utc_to_bd_input(f["ends_at"]))


@bp.app_template_global()
def page_url(slug):
    return url_for("page_" + slug.replace("-", "_"))
