"""Shared setup for the test suite.

One Flask app for the whole run; every test class gets a brand-new, seeded database
(a temporary SQLite file, or the Postgres database in TEST_DATABASE_URL, which is wiped).
No network, API keys or money are involved: outside services are replaced with fakes.
"""
import html
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PG_URL = os.environ.get("TEST_DATABASE_URL") or None
_TMP = tempfile.mkdtemp(prefix="store-tests-")
os.environ["DATABASE_PATH"] = PG_URL or os.path.join(_TMP, "boot.db")
for _k in ("DATABASE_URL", "POSTGRES_URL", "ANTHROPIC_API_KEY", "VERCEL", "TRUST_PROXY", "SESSION_COOKIE_SECURE"):
    os.environ.pop(_k, None)
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import db as dbmod  # noqa: E402
from app import app  # noqa: E402
from helpers import DB_FMT, hash_password  # noqa: E402

app.config["TESTING"] = True

PASSWORD = "pass1234"
PASSWORD_HASH = hash_password(PASSWORD)     # hashing is slow on purpose; do it once
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82")
_counter = [0]


def fresh_db():
    """Point the app at a brand-new seeded database."""
    if PG_URL:
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute("DROP SCHEMA public CASCADE")
            c.execute("CREATE SCHEMA public")
        target = PG_URL
    else:
        _counter[0] += 1
        target = os.path.join(_TMP, "t%d.db" % _counter[0])
    dbmod.init_db(target)
    app.config["DATABASE"] = target
    return target


# --------------------------------------------------------------------------- database helpers

def conn():
    return dbmod.connect(app.config["DATABASE"])


def rows(sql, args=()):
    c = conn()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    c = conn()
    try:
        cur = c.execute(sql, args)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount
    finally:
        c.close()


def value(sql, args=()):
    r = one(sql, args)
    return r[0] if r else None


def settings(**values):
    c = conn()
    try:
        dbmod.save_settings(c, {k: str(v) for k, v in values.items()})
    finally:
        c.close()


def ago(**delta):
    """A UTC timestamp string in the past (or future with negative values), as stored in the DB."""
    return (datetime.utcnow() - timedelta(**delta)).strftime(DB_FMT)


def make_user(name="Customer", email=None, balance=0, admin=False, phone=None, password_hash=None):
    """Create a user; a starting balance goes through the wallet ledger, like a real deposit."""
    _counter[0] += 1
    email = email or "user%d@x.com" % _counter[0]
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        uid = c.execute("INSERT INTO users (name, email, phone, password_hash, is_admin) VALUES (?,?,?,?,?)",
                        (name, email, phone, password_hash or PASSWORD_HASH, 1 if admin else 0)).lastrowid
        if balance:
            dbmod.credit(c, uid, balance, "deposit", "test deposit")
        c.execute("COMMIT")
        return uid
    finally:
        c.close()


def balance(uid):
    return value("SELECT balance FROM users WHERE id = ?", (uid,))


def ledger_ok(uid):
    """The wallet balance always equals the sum of its ledger entries."""
    return balance(uid) == (value("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ?", (uid,)) or 0)


def pkg(product_name, package_name):
    return value("SELECT k.id FROM packages k JOIN products p ON p.id = k.product_id WHERE p.name = ? AND k.name = ?",
                 (product_name, package_name))


def product_id(name):
    return value("SELECT id FROM products WHERE name = ?", (name,))


UID_TOPUP = "UID TopUp (BD)"
VOUCHER = "Free Fire Voucher"


# --------------------------------------------------------------------------- web helpers

def client(uid=None):
    """A browser. With uid, already logged in as that user (skips the slow password check)."""
    c = app.test_client()
    if uid is not None:
        with c.session_transaction() as s:
            s["uid"] = uid
    return c


def token(c, path="/"):
    page = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


def post(c, path, data=None, follow=True, **kw):
    data = dict(data or {})
    data.setdefault("csrf_token", token(c))
    return c.post(path, data=data, follow_redirects=follow, **kw)


def messages(resp):
    """Flash messages shown on a page, as plain text."""
    page = resp.get_data(as_text=True) if hasattr(resp, "get_data") else resp
    return [html.unescape(m) for m in re.findall(r'class="flash flash-\w+"[^>]*>.*?<span>(.*?)</span>', page, re.S)]


def buy(c, pid, package_id, pay="wallet", player_id="1234567890", **extra):
    data = {"package_id": package_id, "player_id": player_id, "pay_option": pay}
    data.update(extra)
    return post(c, "/topup/%d/order" % pid, data)


def last_order(uid=None):
    if uid is None:
        return one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
    return one("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,))


def admin_order(admin_client, oid, action, note="", refund=False):
    data = {"action": action, "note": note}
    if refund:
        data["refund"] = "1"
    return post(admin_client, "/admin/orders/%d/update" % oid, data)


def in_parallel(fn, args_list):
    """Run fn(*args) for every args tuple at the same moment, each in its own thread."""
    barrier = threading.Barrier(len(args_list))
    results, errors = [None] * len(args_list), []

    def worker(i, args):
        try:
            barrier.wait()
            results[i] = fn(*args)
        except Exception as e:      # surface thread failures in the test
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(args_list)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise AssertionError("errors in parallel requests: %s" % errors[:3])
    return results


class StoreTest(unittest.TestCase):
    """Base class: a fresh database for each test class, plus an admin account."""

    @classmethod
    def setUpClass(cls):
        fresh_db()
        cls.admin_id = make_user("Boss", "boss@x.com", admin=True)
        cls.admin = client(cls.admin_id)
