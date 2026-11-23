"""Database access, wallet ledger, settings and first-run seed data.

Uses SQLite (a file in instance/) by default. When DATABASE_URL points at Postgres
(e.g. Neon on Vercel), the same SQL runs there through pg.py.
"""
import hashlib
import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, g

import pg

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Catch this instead of sqlite3.IntegrityError so both databases are handled.
IntegrityError = (sqlite3.IntegrityError, pg.IntegrityError)


class InsufficientBalance(Exception):
    pass


# --------------------------------------------------------------------------- connection

def connect(path):
    """Open the database: a SQLite file path, or a postgres:// URL."""
    if pg.is_pg_url(path):
        return pg.connect(path)
    # isolation_level=None -> autocommit; multi-statement writes use tx() explicitly.
    conn = sqlite3.connect(path, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def tx():
    """Run a block of writes atomically. BEGIN IMMEDIATE takes the write lock up
    front so two requests can never spend the same wallet balance twice."""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def q(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


# --------------------------------------------------------------------------- wallet ledger

def credit(db, user_id, amount, type_, description):
    """Add money to a wallet. Must be called inside tx()."""
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    bal = db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()["balance"]
    db.execute(
        "INSERT INTO transactions (user_id, amount, type, description, balance_after) VALUES (?,?,?,?,?)",
        (user_id, amount, type_, description, bal),
    )
    return bal


def debit(db, user_id, amount, type_, description):
    """Take money from a wallet, refusing to go below zero. Must be called inside tx()."""
    if amount <= 0:
        raise ValueError("debit amount must be positive")
    cur = db.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    if cur.rowcount != 1:
        raise InsufficientBalance()
    bal = db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()["balance"]
    db.execute(
        "INSERT INTO transactions (user_id, amount, type, description, balance_after) VALUES (?,?,?,?,?)",
        (user_id, -amount, type_, description, bal),
    )
    return bal


def cancel_order(db, order, reason, refund=True):
    """Cancel an order inside tx(): refund a paid order to the wallet (if asked) and give back
    whatever it was holding — flash-sale stock, a coupon use, a reserved voucher code.
    Returns the refunded amount (0 if none)."""
    db.execute("UPDATE orders SET status = 'cancelled', admin_note = ?, updated_at = datetime('now') WHERE id = ?",
               (reason, order["id"]))
    if order["flash_sale_id"]:
        db.execute("UPDATE flash_sales SET sold = sold - 1 WHERE id = ? AND sold > 0", (order["flash_sale_id"],))
    if order["coupon_id"]:
        db.execute("UPDATE coupons SET used = used - 1 WHERE id = ? AND used > 0", (order["coupon_id"],))
    if order["is_voucher"]:
        db.execute("UPDATE voucher_codes SET status = 'available', order_id = NULL "
                   "WHERE order_id = ? AND status = 'reserved'", (order["id"],))
    if refund and order["payment_status"] == "paid":
        credit(db, order["user_id"], order["price"], "refund", "Refund for order #%d" % order["id"])
        return order["price"]
    return 0


def deliver_voucher(db, order_id):
    """Hand the reserved code to the customer and complete the order. Inside tx().
    If nothing was reserved (rare), take the next code in stock. Returns the code or None."""
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None or not order["is_voucher"]:
        return None
    vc = db.execute("SELECT * FROM voucher_codes WHERE order_id = ? AND status = 'reserved'", (order_id,)).fetchone()
    if vc is None and order["package_id"]:
        vc = db.execute("SELECT * FROM voucher_codes WHERE package_id = ? AND status = 'available' ORDER BY id LIMIT 1",
                        (order["package_id"],)).fetchone()
    if vc is None:
        return None
    db.execute("UPDATE voucher_codes SET status = 'delivered', order_id = ?, delivered_at = datetime('now') WHERE id = ?",
               (order_id, vc["id"]))
    db.execute("UPDATE orders SET status = 'completed', payment_status = 'paid', voucher_code = ?, "
               "updated_at = datetime('now') WHERE id = ?", (vc["code"], order_id))
    return vc["code"]


def expire_unpaid_gateway_orders(db, minutes=30):
    """Gateway orders that were never paid hold flash-sale stock; release them after a while.
    Must be called inside tx()."""
    rows = db.execute(
        "SELECT * FROM orders WHERE payment_status = 'awaiting' AND status IN ('pending', 'processing') "
        "AND created_at < datetime('now', ?)", ("-%d minutes" % minutes,)).fetchall()
    for o in rows:
        cancel_order(db, o, "Payment not completed in time", refund=False)
    return len(rows)


# --------------------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "site_name": "TopUp BD",
    "site_tagline": "Fast, Safe & Reliable Game Top-Up",
    "meta_description": "বাংলাদেশে সবচেয়ে কম দামে Free Fire Diamond টপআপ করুন — দ্রুত, নিরাপদ ও বিশ্বস্ত।",
    "logo": "",
    "favicon": "",
    "theme_color": "#663cbc",
    "notice_enabled": "1",
    "notice_text": "১৮ বছরের নিচে কেউ অর্ডার করবেন না! অভিভাবকের অনুমতি ছাড়া পরিবারের টাকা দিয়ে অর্ডার করা সম্পূর্ণ নিষিদ্ধ।",
    "telegram_support": "https://t.me/",
    "telegram_group": "https://t.me/",
    "helpline_title": "Telegram HelpLine",
    "helpline_hours": "সকাল ৮টা থেকে রাত ১২টা",
    "footer_text": (
        "<p><b>নোটিশ:</b> অর্ডার নিয়ে কোনো সমস্যা হলে অর্ডার নম্বর ও পেমেন্টের ট্রানজেকশন আইডিসহ "
        "আমাদের Telegram-এ মেসেজ দিন।</p><p><b>বিশেষ সতর্কতা:</b> ১৮ বছরের নিচে অভিভাবকের অনুমতি ছাড়া অর্ডার করা নিষিদ্ধ।</p>"
    ),
    "app_link": "",
    "popup_enabled": "0",
    "popup_image": "",
    "popup_text": "",
    "popup_link": "",
    "popup_button": "বিস্তারিত দেখুন",
    "show_recent_orders": "1",
    "ff_id_checker_url": "",
    "uddoktapay_base_url": "",
    "uddoktapay_api_key": "",
    "min_wallet_amount": "20",
    "max_wallet_amount": "25000",
    "google_client_id": "",
    "google_client_secret": "",
    "spin_enabled": "0",
    "spin_cooldown_hours": "24",
    "spin_min_orders": "1",          # completed orders needed before a customer can spin (stops fake accounts)
    "spin_daily_budget": "0",        # max wallet taka given away per day, 0 = no limit
}


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    for row in q("SELECT key, value FROM settings"):
        s[row["key"]] = row["value"] if row["value"] is not None else ""
    return s


def save_settings(db, values):
    for k, v in values.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, v),
        )


# --------------------------------------------------------------------------- init + seed

DEFAULT_PAGES = {
    "about": ("About Us", """
<h2>আমাদের সম্পর্কে</h2>
<p>আমরা বাংলাদেশের গেমারদের জন্য একটি অনলাইন টপআপ প্ল্যাটফর্ম। এখানে আপনি কম দামে, দ্রুত এবং নিরাপদে Free Fire Diamond, Membership ও অন্যান্য গেম আইটেম কিনতে পারবেন।</p>
<ul><li>বিকাশ, নগদ ও রকেটে পেমেন্ট</li><li>ওয়ালেট ব্যালেন্স দিয়ে এক ক্লিকে অর্ডার</li><li>অর্ডার স্ট্যাটাস সরাসরি প্রোফাইলে দেখা যায়</li></ul>
"""),
    "terms": ("Terms & Conditions", """
<h2>শর্তাবলী</h2>
<ol>
<li>অর্ডার করার আগে Player ID ভালোভাবে যাচাই করুন। ভুল ID দিলে কর্তৃপক্ষ দায়ী নয়।</li>
<li>১৮ বছরের নিচে অভিভাবকের অনুমতি ছাড়া অর্ডার করা যাবে না।</li>
<li>ভুল নম্বরে বা পুরনো নম্বরে টাকা পাঠালে আমরা দায়ী থাকব না। প্রতিবার পেমেন্টের আগে নম্বর দেখে নিন।</li>
<li>ভুয়া ট্রানজেকশন আইডি দিলে অ্যাকাউন্ট বন্ধ করা হতে পারে।</li>
</ol>
"""),
    "privacy": ("Privacy Policy", """
<h2>গোপনীয়তা নীতি</h2>
<p>আমরা শুধুমাত্র অর্ডার সম্পন্ন করার জন্য প্রয়োজনীয় তথ্য (নাম, ইমেইল, ফোন নম্বর, Player ID ও পেমেন্ট তথ্য) সংরক্ষণ করি। আপনার তথ্য কোনো তৃতীয় পক্ষের কাছে বিক্রি করা হয় না।</p>
<p>পাসওয়ার্ড এনক্রিপ্ট করে সংরক্ষণ করা হয়।</p>
"""),
    "refund": ("Refund & Return Policy", """
<h2>রিফান্ড নীতি</h2>
<p>কোনো অর্ডার বাতিল (Cancel) হলে পরিশোধিত টাকা আপনার ওয়ালেটে ফেরত দেওয়া হয়। ওয়ালেট ব্যালেন্স দিয়ে পরবর্তীতে যেকোনো প্রোডাক্ট কিনতে পারবেন।</p>
<p>সফলভাবে সম্পন্ন (Completed) অর্ডার রিফান্ডযোগ্য নয়।</p>
"""),
    "delete-account": ("Delete Account Policy", """
<h2>অ্যাকাউন্ট ডিলিট</h2>
<p>অ্যাকাউন্ট ডিলিট করতে চাইলে আপনার রেজিস্টার্ড ইমেইলসহ আমাদের সাপোর্টে মেসেজ দিন। ওয়ালেটে ব্যালেন্স থাকলে ডিলিট করার আগে তা ব্যবহার করে নিন — ডিলিট করা অ্যাকাউন্টের ব্যালেন্স ফেরতযোগ্য নয়।</p>
"""),
    "tutorial": ("How to Order", """
<h2>কিভাবে অর্ডার করবেন?</h2>
<ol>
<li>একটি অ্যাকাউন্ট খুলুন অথবা লগইন করুন।</li>
<li>হোমপেজ থেকে প্রোডাক্ট (যেমন UID TopUp) বেছে নিন।</li>
<li>প্যাকেজ সিলেক্ট করুন এবং আপনার Free Fire Player ID দিন।</li>
<li>Wallet Pay অথবা Send Money (বিকাশ/নগদ/রকেট) বেছে নিন।</li>
<li>Send Money হলে দেখানো নম্বরে ঠিক ততো টাকা পাঠিয়ে Sender Number ও Transaction ID দিন।</li>
<li>অর্ডার সম্পন্ন হলে প্রোফাইলের <b>My Orders</b>-এ স্ট্যাটাস <b>Completed</b> দেখাবে।</li>
</ol>
"""),
}

SEED_CATEGORIES = [("Regular Game TopUp", 1), ("Special Offer", 2), ("Gift Voucher", 3)]

FF_RULES = """
<p>⦿ শুধুমাত্র Bangladesh সার্ভারের Player ID দিয়ে টপআপ হবে।</p>
<p>⦿ ভুল Player ID দিলে Diamond না পেলে কর্তৃপক্ষ দায়ী নয়।</p>
<p>⦿ অর্ডার Cancel হলে কারণ অর্ডার হিস্টোরিতে লেখা থাকবে — দেখে সঠিক তথ্য দিয়ে আবার অর্ডার করুন।</p>
<p>⦿ সাধারণত কয়েক মিনিটের মধ্যে অর্ডার সম্পন্ন হয়।</p>
"""

# (category index, name, tag line, delivery note, [(package, price), ...])
SEED_PRODUCTS = [
    (0, "UID TopUp (BD)", "Best Price", "⚡ ২ মিনিটে টপআপ", [
        ("25 Diamond", 22), ("50 Diamond", 40), ("115 Diamond", 79), ("240 Diamond", 158),
        ("355 Diamond", 237), ("480 Diamond", 315), ("610 Diamond", 410), ("850 Diamond", 565),
        ("1240 Diamond", 800), ("2530 Diamond", 1590), ("5060 Diamond", 3200), ("10120 Diamond", 6400),
    ]),
    (0, "Weekly / Monthly", "Membership", "⚡ ২ মিনিটে টপআপ", [
        ("Weekly", 158), ("Monthly", 790), ("2x Weekly", 315), ("2x Monthly", 1580), ("1x Weekly + 1x Monthly", 950),
    ]),
    (0, "Weekly Lite", None, "⚡ ২ মিনিটে টপআপ", [
        ("1x Weekly Lite", 40), ("2x Weekly Lite", 80), ("3x Weekly Lite", 120),
    ]),
    (0, "Level Up Pass", None, "⚡ ৫ মিনিটে টপআপ", [
        ("Level 6", 40), ("Level 10", 75), ("Level 15", 75), ("Level 20", 75), ("Level 25", 75), ("Level 30", 110), ("Full Pass", 420),
    ]),
    (0, "Indonesia Server", None, "⚡ ১০ মিনিটে টপআপ", [
        ("5 Diamond", 10), ("12 Diamond", 22), ("50 Diamond", 75), ("70 Diamond", 100), ("140 Diamond", 195),
    ]),
    (1, "Evo Access", "Hot", "⚡ ৫ মিনিটে টপআপ", [
        ("3 Days", 70), ("7 Days", 105), ("30 Days", 300),
    ]),
]

VOUCHER_RULES = """
<p>⦿ পেমেন্টের পর কোডটি সাথে সাথে আপনার প্রোফাইলের <b>Gift Vouchers</b> সেকশনে দেখাবে।</p>
<p>⦿ কোডটি কপি করে রিডিম পেজে গিয়ে ব্যবহার করুন।</p>
<p>⦿ কোড ডেলিভারির পর রিফান্ড বা পরিবর্তন সম্ভব নয়।</p>
"""

# Example gift-voucher product for new installs. Codes are not included: upload yours in Admin.
SEED_VOUCHERS = [
    ("Free Fire Voucher", "Instant Code", "⚡ সাথে সাথে কোড", [
        ("25 Diamond Voucher", 22), ("50 Diamond Voucher", 40), ("115 Diamond Voucher", 79), ("240 Diamond Voucher", 158),
    ]),
]

# Default wheel: 8 segments. Weights are relative chances (they don't need to add up to 100).
SEED_SPIN_PRIZES = [
    # label, type, amount, coupon_type, max_discount, valid_days, weight, color
    ("৳5", "wallet", 5, "percent", 0, 7, 18, "#7c3aed"),
    ("Try Again", "nothing", 0, "percent", 0, 7, 30, "#0ea5e9"),
    ("5% OFF", "coupon", 5, "percent", 20, 7, 20, "#f59e0b"),
    ("৳10", "wallet", 10, "percent", 0, 7, 6, "#ec4899"),
    ("Try Again", "nothing", 0, "percent", 0, 7, 30, "#14b8a6"),
    ("10% OFF", "coupon", 10, "percent", 30, 7, 8, "#22c55e"),
    ("৳2", "wallet", 2, "percent", 0, 7, 25, "#6366f1"),
    ("৳50", "wallet", 50, "percent", 0, 7, 1, "#ef4444"),
]

SEED_METHODS = [
    ("bKash", "01XXXXXXXXX", "Personal", "#e2136e", "bKash অ্যাপ থেকে Send Money করুন।", 1),
    ("Nagad", "01XXXXXXXXX", "Personal", "#f6921e", "Nagad অ্যাপ থেকে Send Money করুন।", 2),
    ("Rocket", "01XXXXXXXXXX", "Personal", "#8c3494", "Rocket থেকে Send Money করুন।", 3),
]


def _slugify(text):
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "item"


def _schema_sql():
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return f.read()


def schema_version():
    """Changes whenever the schema or migrations change, so Postgres re-runs setup only then."""
    text = _schema_sql() + repr(COLUMN_MIGRATIONS) + repr(INDEX_MIGRATIONS) + pg.FUNCTIONS_SQL
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def init_db(path):
    if pg.is_pg_url(path):
        return _init_pg(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_schema_sql())
        _migrate(conn)
        seeded = conn.execute("SELECT value FROM settings WHERE key = '_seeded'").fetchone()
        if not seeded:
            _seed(conn)
    finally:
        conn.close()


def _init_pg(url):
    """Create tables and seed data on Postgres. Runs on every cold start but only does work when
    the schema changed. Everything happens in one transaction holding a lock, so two cold starts
    can't race and a failure leaves nothing half-created (works with Neon's pooled URL too)."""
    version = schema_version()
    conn = connect(url)
    try:
        if _pg_version(conn) == version:
            return
        conn.execute("BEGIN")       # also takes the write lock (see pg.Connection.execute)
        try:
            if _pg_version(conn) != version:
                conn.executescript(pg.FUNCTIONS_SQL + pg.translate_schema(_schema_sql()))
                _migrate(conn)
                if not conn.execute("SELECT value FROM settings WHERE key = '_seeded'").fetchone():
                    _seed(conn, own_tx=False)
                save_settings(conn, {"_schema_version": version})
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _pg_version(conn):
    if conn.execute("SELECT to_regclass('public.settings') AS t").fetchone()["t"] is None:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key = '_schema_version'").fetchone()
    return row["value"] if row else None


# Columns added after the first release. Existing databases get them automatically.
COLUMN_MIGRATIONS = [
    ("users", "google_sub", "TEXT"),
    ("users", "avatar", "TEXT"),
    ("orders", "flash_sale_id", "INTEGER REFERENCES flash_sales(id) ON DELETE SET NULL"),
    ("products", "kind", "TEXT NOT NULL DEFAULT 'topup'"),
    ("products", "redeem_link", "TEXT"),
    ("orders", "coupon_id", "INTEGER REFERENCES coupons(id) ON DELETE SET NULL"),
    ("orders", "coupon_code", "TEXT"),
    ("orders", "discount", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "is_voucher", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "voucher_code", "TEXT"),
    ("coupons", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
]

INDEX_MIGRATIONS = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google ON users(google_sub) WHERE google_sub IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_flash ON orders(flash_sale_id, user_id) WHERE flash_sale_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_coupon ON orders(coupon_id, user_id) WHERE coupon_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_voucher ON orders(user_id, is_voucher)",
]


def _table_columns(conn, table):
    if isinstance(conn, pg.Connection):
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?",
            (table,))}
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _migrate(conn):
    for table, column, decl in COLUMN_MIGRATIONS:
        cols = _table_columns(conn, table)
        if column not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
    for sql in INDEX_MIGRATIONS:
        conn.execute(sql)


def _seed(conn, own_tx=True):
    if own_tx:
        conn.execute("BEGIN")
    try:
        cat_ids = []
        for name, order in SEED_CATEGORIES:
            cat_ids.append(conn.execute(
                "INSERT INTO categories (name, sort_order) VALUES (?, ?)", (name, order)).lastrowid)
        for i, (ci, name, tag, note, pkgs) in enumerate(SEED_PRODUCTS):
            pid = conn.execute(
                "INSERT INTO products (category_id, name, slug, tag_line, delivery_note, description, sort_order) "
                "VALUES (?,?,?,?,?,?,?)",
                (cat_ids[ci], name, _slugify(name), tag, note, FF_RULES, i),
            ).lastrowid
            for j, (pname, price) in enumerate(pkgs):
                conn.execute(
                    "INSERT INTO packages (product_id, name, price, sort_order) VALUES (?,?,?,?)",
                    (pid, pname, price, j),
                )
        for i, (name, tag, note, pkgs) in enumerate(SEED_VOUCHERS):
            pid = conn.execute(
                "INSERT INTO products (category_id, name, slug, tag_line, delivery_note, description, sort_order, "
                "kind, numeric_id) VALUES (?,?,?,?,?,?,?, 'voucher', 0)",
                (cat_ids[2], name, _slugify(name), tag, note, VOUCHER_RULES, i),
            ).lastrowid
            for j, (pname, price) in enumerate(pkgs):
                conn.execute("INSERT INTO packages (product_id, name, price, sort_order) VALUES (?,?,?,?)",
                             (pid, pname, price, j))
        for i, prize in enumerate(SEED_SPIN_PRIZES):
            conn.execute("INSERT INTO spin_prizes (label, prize_type, amount, coupon_type, max_discount, valid_days, "
                         "weight, color, sort_order) VALUES (?,?,?,?,?,?,?,?,?)", prize + (i,))
        for m in SEED_METHODS:
            conn.execute(
                "INSERT INTO payment_methods (name, number, account_type, color, instructions, sort_order) "
                "VALUES (?,?,?,?,?,?)", m)
        for slug, (title, content) in DEFAULT_PAGES.items():
            conn.execute("INSERT INTO pages (slug, title, content) VALUES (?,?,?)", (slug, title, content.strip()))
        conn.execute("INSERT INTO settings (key, value) VALUES ('_seeded', '1')")
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
