"""Free Fire diamond top-up store — customer-facing site.

Run locally:   python3 app.py          (http://127.0.0.1:8000)
Production:    gunicorn -w 2 -b 0.0.0.0:8000 app:app
"""
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import coupons
import google_auth
import payments
from checkout import (CheckoutError, active_methods, place_order as checkout_order, settle_gateway_payment,
                      start_gateway_payment, validate_manual_payment)
from db import IntegrityError, close_db, get_settings, init_db, q, tx
from flashsale import has_visible_sales, visible_sales
from helpers import (BD_PHONE_RE, EMAIL_RE, avatar_html, avatar_hue, bdtime, clean_digits, csrf_protect,
                     csrf_token, hash_password, icon, initials, login_required, money, safe_next,
                     status_badge, to_int, upload_url)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_VERSION = "10"


def database_target(instance_path):
    """Postgres when DATABASE_URL is set (Vercel + Neon), otherwise a SQLite file in instance/."""
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
    if url:
        return url
    if os.environ.get("VERCEL"):
        # Vercel's disk is read-only and wiped between requests, so a SQLite file would lose every order.
        raise RuntimeError("Set DATABASE_URL in Vercel to a Postgres database (for example Neon).")
    return os.environ.get("DATABASE_PATH", os.path.join(instance_path, "topup.db"))


def _secret_key(instance_path):
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if os.environ.get("VERCEL"):
        raise RuntimeError("Set the SECRET_KEY environment variable in Vercel (Settings → Environment Variables).")
    os.makedirs(instance_path, exist_ok=True)
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


def create_app():
    # Static files live in public/static: Vercel serves that folder from its CDN, Flask serves it locally.
    app = Flask(__name__, instance_path=os.path.join(BASE_DIR, "instance"),
                static_folder=os.path.join(BASE_DIR, "public", "static"), static_url_path="/static")
    app.config.update(
        DATABASE=database_target(app.instance_path),
        SECRET_KEY=_secret_key(app.instance_path),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,   # Vercel accepts request bodies up to 4.5 MB
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1" or bool(os.environ.get("VERCEL")),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if os.environ.get("TRUST_PROXY") == "1" or os.environ.get("VERCEL"):
        # behind nginx / Cloudflare / Render: trust X-Forwarded-* so https links are built correctly
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    app.jinja_env.filters.update(money=money, bdtime=bdtime, initials=initials, hue=avatar_hue)
    app.jinja_env.globals.update(icon=icon, csrf_token=csrf_token, upload_url=upload_url,
                                 status_badge=status_badge, avatar=avatar_html, asset_v=ASSET_VERSION)

    from admin import bp as admin_bp
    from flashsale import bp as flash_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(flash_bp)
    app.register_blueprint(google_auth.bp)
    app.register_blueprint(coupons.bp)
    from spin import bp as spin_bp
    app.register_blueprint(spin_bp)
    app.jinja_env.globals.update(product_url=product_url, coupon_label=coupons.label)
    register_routes(app)
    return app


# =========================================================================== request lifecycle

def load_request_state():
    if request.endpoint in ("static", "media"):   # files: no settings or login needed
        return
    g.settings = get_settings()
    g.user = None
    uid = session.get("uid")
    if uid:
        user = q("SELECT * FROM users WHERE id = ?", (uid,), one=True)
        if user is None or user["is_banned"]:
            session.pop("uid", None)
        else:
            g.user = user


def template_context():
    s = g.get("settings", {})
    return {"S": s, "U": g.get("user"), "now_year": datetime.utcnow().year,
            "google_on": google_auth.enabled(s), "has_flash": has_visible_sales()}


def _find_nickname(obj, depth=0):
    keys = ("nickname", "nickName", "player_name", "playerName", "PlayerNickname", "name", "username")
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:60]
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _find_nickname(v, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for v in obj[:3]:
            found = _find_nickname(v, depth + 1)
            if found:
                return found
    return None


def product_url(p, **kw):
    """Top-up products live at /topup/..., gift vouchers at /giftvoucher/... (like the original site)."""
    endpoint = "voucher_product" if p["kind"] == "voucher" else "product"
    return url_for(endpoint, pid=p["id"], slug=p["slug"], **kw)


def voucher_stock(product_id):
    """{package_id: codes available} for a voucher product."""
    rows = q("SELECT k.id, (SELECT COUNT(*) FROM voucher_codes v WHERE v.package_id = k.id AND v.status = 'available') AS n "
             "FROM packages k WHERE k.product_id = ?", (product_id,))
    return {r["id"]: r["n"] for r in rows}


def recent_orders_rows():
    return q("SELECT o.id, u.name, u.avatar, o.package_name, o.price, o.status FROM orders o "
             "JOIN users u ON u.id = o.user_id WHERE o.payment_status = 'paid' "
             "ORDER BY o.id DESC LIMIT 10")


# =========================================================================== routes

def register_routes(app):
    app.before_request(load_request_state)
    app.before_request(csrf_protect)
    app.context_processor(template_context)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp

    @app.get("/media/<name>")
    def media(name):
        """Uploaded images are stored in the database (see helpers.save_upload)."""
        row = q("SELECT mime, data FROM uploads WHERE name = ?", (name,), one=True)
        if row is None:
            abort(404)
        resp = Response(bytes(row["data"]), mimetype=row["mime"])
        # names are random and never reused, so browsers and the CDN can keep them for a year
        resp.headers["Cache-Control"] = "public, max-age=31536000, s-maxage=31536000, immutable"
        return resp

    # ------------------------------------------------------------------ home & catalogue

    @app.get("/")
    def home():
        cats = q("SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order, id")
        products = q("SELECT * FROM products WHERE is_active = 1 ORDER BY sort_order, id")
        sections = []
        for c in cats:
            items = [p for p in products if p["category_id"] == c["id"]]
            if items:
                sections.append((c, items))
        banners = q("SELECT * FROM banners WHERE is_active = 1 ORDER BY sort_order, id")
        recent = recent_orders_rows() if g.settings.get("show_recent_orders") == "1" else []
        return render_template("index.html", sections=sections, banners=banners, recent=recent,
                               flash_sales=visible_sales(limit=8))

    @app.get("/topup")
    @app.get("/shop")
    def shop():
        products = q("SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c ON c.id = p.category_id "
                     "WHERE p.is_active = 1 AND (c.is_active = 1 OR c.id IS NULL) ORDER BY c.sort_order, p.sort_order, p.id")
        return render_template("shop.html", products=products)

    def render_product(pid, want_kind):
        p = q("SELECT * FROM products WHERE id = ? AND is_active = 1", (pid,), one=True)
        if p is None:
            abort(404)
        if p["kind"] != want_kind:                       # e.g. /topup/ link to a voucher product
            return redirect(product_url(p), code=301)
        pkgs = q("SELECT * FROM packages WHERE product_id = ? ORDER BY sort_order, id", (pid,))
        return render_template("product.html", p=p, pkgs=pkgs, methods=active_methods(),
                               gateway=payments.enabled(g.settings),
                               id_checker=bool(g.settings.get("ff_id_checker_url", "").strip()),
                               stock=voucher_stock(pid) if p["kind"] == "voucher" else {},
                               coupons=coupons.public_coupons(pid, g.user["id"] if g.user else None))

    @app.get("/topup/<int:pid>")
    @app.get("/topup/<int:pid>/<slug>")
    def product(pid, slug=None):
        return render_product(pid, "topup")

    @app.get("/giftvoucher/<int:pid>")
    @app.get("/giftvoucher/<int:pid>/<slug>")
    def voucher_product(pid, slug=None):
        return render_product(pid, "voucher")

    @app.post("/topup/<int:pid>/order")
    @login_required
    def place_order(pid):
        p = q("SELECT * FROM products WHERE id = ? AND is_active = 1", (pid,), one=True)
        if p is None:
            abort(404)
        back = product_url(p)
        pkg = q("SELECT * FROM packages WHERE id = ? AND product_id = ?",
                (to_int(request.form.get("package_id"), 0), pid), one=True)
        if pkg is None:
            flash("একটি প্যাকেজ সিলেক্ট করুন।", "error")
            return redirect(back)
        if not pkg["in_stock"] or (p["kind"] == "voucher" and not voucher_stock(pid).get(pkg["id"])):
            flash("এই প্যাকেজটি এখন স্টকে নেই।", "error")
            return redirect(back)
        return checkout_order(p, pkg["id"], pkg["name"], pkg["price"], back)

    # ------------------------------------------------------------------ json api

    @app.get("/api/recent-orders")
    def api_recent_orders():
        if g.settings.get("show_recent_orders") != "1":
            return jsonify([])
        return jsonify([{"name": r["name"], "item": r["package_name"], "price": r["price"], "status": r["status"],
                         "initial": initials(r["name"]), "hue": avatar_hue(r["name"]),
                         "avatar": r["avatar"] if (r["avatar"] or "").startswith("https://") else None}
                        for r in recent_orders_rows()])

    @app.get("/api/check-id")
    def api_check_id():
        template = g.settings.get("ff_id_checker_url", "").strip()
        if not template:
            return jsonify(ok=False, error="ID checker চালু নেই।"), 503
        uid = clean_digits(request.args.get("uid"))
        if not 5 <= len(uid) <= 15:
            return jsonify(ok=False, error="সঠিক Player ID দিন।"), 400
        url = template.replace("{uid}", quote(uid)) if "{uid}" in template else template.rstrip("/") + "/" + quote(uid)
        try:
            r = requests.get(url, timeout=8, headers={"Accept": "application/json", "User-Agent": "TopUpSite/1.0"})
            data = r.json()
        except (requests.RequestException, ValueError):
            return jsonify(ok=False, error="এখন আইডি চেক করা যাচ্ছে না।"), 502
        name = _find_nickname(data)
        if not name:
            return jsonify(ok=False, error="এই আইডিতে কোনো প্লেয়ার পাওয়া যায়নি।"), 404
        return jsonify(ok=True, name=name)

    # ------------------------------------------------------------------ auth

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("profile"))
        nxt = safe_next(request.values.get("next"))
        if request.method == "POST":
            ident = (request.form.get("identity") or "").strip()
            password = request.form.get("password") or ""
            if "@" in ident:
                user = q("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (ident,), one=True)
            else:
                user = q("SELECT * FROM users WHERE phone = ?", (clean_digits(ident),), one=True)
            if user and check_password_hash(user["password_hash"], password):
                if user["is_banned"]:
                    flash("আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে। সাপোর্টে যোগাযোগ করুন।", "error")
                else:
                    session.clear()
                    session.permanent = True
                    session["uid"] = user["id"]
                    flash("স্বাগতম, %s!" % user["name"], "success")
                    return redirect(nxt or url_for("home"))
            elif user and user["google_sub"] and user["password_hash"].startswith("!"):
                flash("এই অ্যাকাউন্টটি Google দিয়ে খোলা — \"Continue with Google\" দিয়ে লগইন করুন।", "error")
            else:
                flash("ইমেইল/ফোন অথবা পাসওয়ার্ড ভুল।", "error")
        return render_template("login.html", next=nxt)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("profile"))
        nxt = safe_next(request.values.get("next"))
        form = {}
        if request.method == "POST":
            form = request.form
            name = (form.get("name") or "").strip()[:60]
            email = (form.get("email") or "").strip().lower()[:120]
            phone = clean_digits(form.get("phone"))
            pw = form.get("password") or ""
            error = None
            if len(name) < 2:
                error = "আপনার নাম দিন।"
            elif not EMAIL_RE.match(email):
                error = "সঠিক ইমেইল দিন।"
            elif phone and not BD_PHONE_RE.match(phone):
                error = "সঠিক মোবাইল নম্বর দিন (01XXXXXXXXX)।"
            elif len(pw) < 6:
                error = "পাসওয়ার্ড কমপক্ষে ৬ অক্ষরের হতে হবে।"
            elif pw != form.get("password2"):
                error = "দুইটি পাসওয়ার্ড মিলছে না।"
            elif q("SELECT 1 FROM users WHERE email = ? COLLATE NOCASE", (email,), one=True):
                error = "এই ইমেইল দিয়ে আগেই অ্যাকাউন্ট খোলা হয়েছে।"
            elif phone and q("SELECT 1 FROM users WHERE phone = ?", (phone,), one=True):
                error = "এই নম্বর দিয়ে আগেই অ্যাকাউন্ট খোলা হয়েছে।"
            if error:
                flash(error, "error")
            else:
                with tx() as db:
                    uid = db.execute("INSERT INTO users (name, email, phone, password_hash) VALUES (?,?,?,?)",
                                     (name, email, phone or None, hash_password(pw))).lastrowid
                session.clear()
                session.permanent = True
                session["uid"] = uid
                flash("অ্যাকাউন্ট তৈরি হয়েছে! এখন টপআপ করতে পারবেন।", "success")
                return redirect(nxt or url_for("home"))
        return render_template("register.html", form=form, next=nxt)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("লগআউট হয়েছে।", "info")
        return redirect(url_for("home"))

    # ------------------------------------------------------------------ profile

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        uid = g.user["id"]
        has_password = not g.user["password_hash"].startswith("!")
        if request.method == "POST":
            action = request.form.get("action")
            if action == "info":
                name = (request.form.get("name") or "").strip()[:60]
                phone = clean_digits(request.form.get("phone"))
                if len(name) < 2:
                    flash("আপনার নাম দিন।", "error")
                elif phone and not BD_PHONE_RE.match(phone):
                    flash("সঠিক মোবাইল নম্বর দিন।", "error")
                elif phone and q("SELECT 1 FROM users WHERE phone = ? AND id <> ?", (phone, uid), one=True):
                    flash("এই নম্বর অন্য অ্যাকাউন্টে ব্যবহার হচ্ছে।", "error")
                else:
                    with tx() as db:
                        db.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name, phone or None, uid))
                    flash("প্রোফাইল আপডেট হয়েছে।", "success")
            elif action == "password":
                if has_password and not check_password_hash(g.user["password_hash"], request.form.get("current") or ""):
                    flash("বর্তমান পাসওয়ার্ড ভুল।", "error")
                elif len(request.form.get("new") or "") < 6:
                    flash("নতুন পাসওয়ার্ড কমপক্ষে ৬ অক্ষরের হতে হবে।", "error")
                elif request.form.get("new") != request.form.get("new2"):
                    flash("দুইটি পাসওয়ার্ড মিলছে না।", "error")
                else:
                    with tx() as db:
                        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                                   (hash_password(request.form["new"]), uid))
                    flash("পাসওয়ার্ড সেট হয়েছে।" if not has_password else "পাসওয়ার্ড পরিবর্তন হয়েছে।", "success")
            return redirect(url_for("profile"))
        stats = q("SELECT COUNT(*) AS total, "
                  "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS done, "
                  "SUM(CASE WHEN status IN ('pending','processing') AND payment_status <> 'awaiting' THEN 1 ELSE 0 END) AS waiting, "
                  "COALESCE(SUM(CASE WHEN status='completed' THEN price ELSE 0 END),0) AS spent "
                  "FROM orders WHERE user_id = ?", (uid,), one=True)
        recent = q("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 5", (uid,))
        return render_template("profile/dashboard.html", stats=stats, recent=recent, tab="dashboard",
                               has_password=has_password)

    @app.route("/profile/addwallet", methods=["GET", "POST"])
    @login_required
    def add_wallet():
        s = g.settings
        lo = to_int(s.get("min_wallet_amount"), 20)
        hi = to_int(s.get("max_wallet_amount"), 25000)
        gateway = payments.enabled(s)
        if request.method == "POST":
            amount = to_int(request.form.get("amount"))
            option = request.form.get("pay_option")
            if amount is None or not lo <= amount <= hi:
                flash("টাকার পরিমাণ %s থেকে %s এর মধ্যে হতে হবে।" % (money(lo), money(hi)), "error")
                return redirect(url_for("add_wallet"))
            if option == "gateway" and gateway:
                try:
                    return redirect(start_gateway_payment("wallet", None, amount))
                except payments.GatewayError as e:
                    flash(str(e), "error")
                    return redirect(url_for("add_wallet"))
            if option == "manual":
                pending = q("SELECT COUNT(*) AS n FROM wallet_requests WHERE user_id = ? AND status = 'pending'",
                            (g.user["id"],), one=True)["n"]
                if pending >= 3:
                    flash("আপনার ৩টি রিকোয়েস্ট এখনও যাচাই বাকি। সেগুলো যাচাই হওয়া পর্যন্ত অপেক্ষা করুন।", "error")
                    return redirect(url_for("add_wallet"))
                try:
                    method, sender, trx = validate_manual_payment(request.form)
                    with tx() as db:
                        db.execute("INSERT INTO wallet_requests (user_id, amount, method, sender_number, trx_id) "
                                   "VALUES (?,?,?,?,?)", (g.user["id"], amount, method["name"], sender, trx))
                except CheckoutError as e:
                    flash(str(e), "error")
                    return redirect(url_for("add_wallet"))
                except IntegrityError:
                    flash("এই Transaction ID আগেই ব্যবহার করা হয়েছে।", "error")
                    return redirect(url_for("add_wallet"))
                flash("Add Money রিকোয়েস্ট জমা হয়েছে। যাচাইয়ের পর ব্যালেন্স যোগ হবে।", "success")
                return redirect(url_for("add_wallet"))
            flash("পেমেন্ট অপশন সিলেক্ট করুন।", "error")
            return redirect(url_for("add_wallet"))
        reqs = q("SELECT * FROM wallet_requests WHERE user_id = ? ORDER BY id DESC LIMIT 20", (g.user["id"],))
        return render_template("profile/addwallet.html", methods=active_methods(), gateway=gateway,
                               reqs=reqs, lo=lo, hi=hi, tab="wallet")

    @app.get("/profile/order")
    @login_required
    def my_orders():
        page = max(to_int(request.args.get("page"), 1) or 1, 1)
        per = 15
        rows = q("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                 (g.user["id"], per + 1, (page - 1) * per))
        return render_template("profile/orders.html", orders=rows[:per], page=page,
                               has_next=len(rows) > per, tab="orders")

    @app.get("/profile/giftvoucher")
    @login_required
    def my_vouchers():
        rows = q("SELECT o.*, p.redeem_link, p.image, p.slug FROM orders o LEFT JOIN products p ON p.id = o.product_id "
                 "WHERE o.user_id = ? AND o.is_voucher = 1 AND o.status <> 'cancelled' ORDER BY o.id DESC LIMIT 100",
                 (g.user["id"],))
        return render_template("profile/vouchers.html", rows=rows, tab="vouchers")

    @app.get("/profile/transaction")
    @login_required
    def my_transactions():
        page = max(to_int(request.args.get("page"), 1) or 1, 1)
        per = 20
        rows = q("SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                 (g.user["id"], per + 1, (page - 1) * per))
        return render_template("profile/transactions.html", txs=rows[:per], page=page,
                               has_next=len(rows) > per, tab="transactions")

    # ------------------------------------------------------------------ payment gateway return

    @app.get("/payment/return")
    def payment_return():
        gp_id = to_int(request.args.get("gp"), 0)
        invoice_id = (request.args.get("invoice_id") or "").strip()
        if not payments.enabled(g.settings):
            return redirect(url_for("payment_failed"))
        try:
            ok, purpose = settle_gateway_payment(gp_id, invoice_id)
        except payments.GatewayError:
            ok, purpose = False, None
        if ok:
            return redirect(url_for("payment_success", t=purpose))
        return redirect(url_for("payment_failed"))

    @app.post("/payment/webhook")
    def payment_webhook():
        if not payments.enabled(g.settings):
            return jsonify(ok=False), 404
        key = request.headers.get("RT-UDDOKTAPAY-API-KEY", "")
        if not secrets.compare_digest(key, g.settings["uddoktapay_api_key"].strip()):
            return jsonify(ok=False), 401
        body = request.get_json(silent=True) or {}
        meta = body.get("metadata") or {}
        try:
            ok, _ = settle_gateway_payment(to_int(meta.get("gp_id"), 0), str(body.get("invoice_id") or ""))
        except payments.GatewayError:
            return jsonify(ok=False), 502
        return jsonify(ok=ok)

    @app.get("/paymentsuccess")
    def payment_success():
        return render_template("payment_result.html", ok=True, purpose=request.args.get("t"))

    @app.get("/paymentfailed")
    def payment_failed():
        return render_template("payment_result.html", ok=False, purpose=None)

    # ------------------------------------------------------------------ misc pages

    @app.get("/leaderboard")
    def leaderboard():
        rows = q("SELECT u.name, u.avatar, SUM(o.price) AS total, COUNT(*) AS orders FROM orders o "
                 "JOIN users u ON u.id = o.user_id WHERE o.status = 'completed' "
                 "GROUP BY u.id, u.name, u.avatar ORDER BY total DESC LIMIT 20")
        return render_template("leaderboard.html", rows=rows)

    @app.get("/free-fire-id-checker")
    def id_checker():
        return render_template("idchecker.html", enabled=bool(g.settings.get("ff_id_checker_url", "").strip()))

    @app.get("/contactus")
    def contact():
        return render_template("contact.html")

    page_aliases = {
        "/about-us": "about", "/terms-and-conditions": "terms", "/privacy-policy": "privacy",
        "/refund-return-policy": "refund", "/delete-account-policy": "delete-account", "/tutorial": "tutorial",
    }

    def make_page_view(slug):
        def view():
            page = q("SELECT * FROM pages WHERE slug = ?", (slug,), one=True)
            if page is None:
                abort(404)
            return render_template("page.html", page=page)
        return view

    for path, slug in page_aliases.items():
        app.add_url_rule(path, "page_" + slug.replace("-", "_"), make_page_view(slug))

    @app.get("/robots.txt")
    def robots():
        body = "User-agent: *\nDisallow: /admin\nDisallow: /profile\nAllow: /\n"
        return body, 200, {"Content-Type": "text/plain; charset=utf-8"}

    # ------------------------------------------------------------------ errors

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def error_page(e):
        code = getattr(e, "code", 500) or 500
        messages = {
            400: getattr(e, "description", None) or "অনুরোধটি সঠিক নয়।",
            403: "এই পেজ দেখার অনুমতি আপনার নেই।",
            404: "দুঃখিত, পেজটি খুঁজে পাওয়া যায়নি।",
            413: "ফাইলটি অনেক বড় (সর্বোচ্চ 4MB)।",
            500: "সার্ভারে সমস্যা হয়েছে। কিছুক্ষণ পর আবার চেষ্টা করুন।",
        }
        if request.endpoint in ("static", "media"):   # a missing image/file: no need for a full page
            return Response(messages.get(code, "Error"), status=code, mimetype="text/plain")
        if "settings" not in g:
            try:
                load_request_state()
            except Exception:  # never fail while rendering an error page
                from db import DEFAULT_SETTINGS
                g.settings, g.user = dict(DEFAULT_SETTINGS), None
        return render_template("error.html", code=code, message=messages.get(code, "Error")), code


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
