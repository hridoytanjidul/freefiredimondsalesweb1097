"""Postgres support (used on Vercel with Neon).

The app's SQL is written for SQLite. This module wraps a psycopg connection so the same
SQL runs on Postgres unchanged:

* ``?`` placeholders become ``%s``, ``LIKE`` becomes case-insensitive ``ILIKE`` (as in SQLite),
  and ``col = ? COLLATE NOCASE`` becomes ``lower(col) = lower(?)``.
* ``INSERT`` statements get ``RETURNING id`` so ``cursor.lastrowid`` works.
* ``BEGIN IMMEDIATE`` takes a global advisory lock, so write transactions run one at a time,
  exactly as with SQLite's write lock. The wallet, stock, coupon and spin checks rely on this.
* ``datetime('now', modifier)`` is provided as a SQL function returning SQLite-style text.
* Rows behave like ``sqlite3.Row``: ``row["col"]``, ``row[0]``, ``dict(row)``, ``row.keys()``.
"""
import re
from functools import lru_cache

import psycopg
from psycopg.adapt import Dumper, Loader
from psycopg.pq import TransactionStatus

IntegrityError = psycopg.IntegrityError

WRITE_LOCK_KEY = 72_001      # pg_advisory_xact_lock key shared by every write transaction

# Tables whose primary key is not "id" (INSERTs into them get no RETURNING clause).
NO_ID_TABLES = {"settings", "pages", "uploads"}

# SQLite helpers recreated in Postgres. Timestamps are stored as UTC text 'YYYY-MM-DD HH:MM:SS'.
FUNCTIONS_SQL = r"""
CREATE OR REPLACE FUNCTION datetime(t text) RETURNS text LANGUAGE sql VOLATILE AS $$
  SELECT to_char(CASE WHEN t = 'now' THEN timezone('UTC', clock_timestamp()) ELSE t::timestamp END,
                 'YYYY-MM-DD HH24:MI:SS')
$$;
CREATE OR REPLACE FUNCTION datetime(t text, modifier text) RETURNS text LANGUAGE sql VOLATILE AS $$
  SELECT to_char((CASE WHEN t = 'now' THEN timezone('UTC', clock_timestamp()) ELSE t::timestamp END)
                 + modifier::interval, 'YYYY-MM-DD HH24:MI:SS')
$$;
"""


def is_pg_url(target):
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


# --------------------------------------------------------------------------- rows

class Row:
    """A result row that behaves like sqlite3.Row."""
    __slots__ = ("_v",)
    _names = ()
    _index = {}

    def __init__(self, values):
        self._v = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self._index[key]
            except KeyError:
                raise IndexError("No item with that key") from None
        return self._v[key]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)

    def __eq__(self, other):
        return isinstance(other, Row) and self._names == other._names and self._v == other._v

    def __hash__(self):
        return hash((self._names, self._v))

    def __repr__(self):
        return "Row(%s)" % ", ".join("%s=%r" % kv for kv in zip(self._names, self._v))


_row_classes = {}


def _row_factory(cursor):
    desc = cursor.description
    names = tuple(d.name for d in desc) if desc else ()
    cls = _row_classes.get(names)
    if cls is None:
        index = {}
        for i, n in enumerate(names):
            index.setdefault(n, i)      # duplicate names: first column wins, like sqlite3.Row
        cls = type("Row", (Row,), {"__slots__": (), "_names": names, "_index": index})
        if len(_row_classes) < 2000:
            _row_classes[names] = cls
    return cls


# --------------------------------------------------------------------------- type adaptation

class _NumericLoader(Loader):
    """SUM()/AVG() return numeric in Postgres; give back int (or float) like SQLite does."""
    def load(self, data):
        s = bytes(data).decode()
        try:
            return int(s)
        except ValueError:
            return float(s)


class _BoolAsInt(Dumper):
    """Flags are INTEGER 0/1 columns, as in SQLite, so send Python bools as integers."""
    oid = psycopg.postgres.types["int4"].oid

    def dump(self, obj):
        return b"1" if obj else b"0"


# --------------------------------------------------------------------------- SQL translation

_NOCASE_EQ = re.compile(r"([\w.]+)\s*=\s*\?\s+COLLATE\s+NOCASE", re.I)
_INSERT = re.compile(r"^\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(\w+)", re.I)
_OR_IGNORE = re.compile(r"^(\s*INSERT)\s+OR\s+IGNORE(\s+INTO)", re.I)
_LIKE = re.compile(r"\bLIKE\b", re.I)


def _placeholders(sql):
    """Escape % and turn ? (outside quoted strings) into %s."""
    out, quote = [], None
    for ch in sql:
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "?":
            out.append("%s")
            continue
        out.append("%%" if ch == "%" else ch)
    return "".join(out)


@lru_cache(maxsize=1024)
def translate(sql):
    """Return (postgres_sql, returns_id)."""
    s = _NOCASE_EQ.sub(r"lower(\1) = lower(?)", sql)
    s = re.sub(r"\s+COLLATE\s+NOCASE", "", s, flags=re.I)
    s = _LIKE.sub("ILIKE", s)
    returns_id = False
    m = _INSERT.match(s)
    if m:
        body = s.rstrip().rstrip(";")
        if _OR_IGNORE.match(body):
            body = _OR_IGNORE.sub(r"\1\2", body) + " ON CONFLICT DO NOTHING"
        if m.group(1).lower() not in NO_ID_TABLES and not re.search(r"\bRETURNING\b", body, re.I):
            body += " RETURNING id"
            returns_id = True
        s = body
    return _placeholders(s), returns_id


def translate_schema(sql):
    """Turn the SQLite schema file into Postgres DDL."""
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", sql, flags=re.I)
    s = re.sub(r"\(\s*(\w+)\s+COLLATE\s+NOCASE\s*\)", r"(lower(\1))", s, flags=re.I)   # index on (col COLLATE NOCASE)
    s = re.sub(r"\s+COLLATE\s+NOCASE", "", s, flags=re.I)
    s = re.sub(r"\bBLOB\b", "BYTEA", s)
    # case-insensitive uniqueness that SQLite got from COLLATE NOCASE columns
    s += ("\nCREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_ci ON users (lower(email));"
          "\nCREATE UNIQUE INDEX IF NOT EXISTS uq_coupons_code_ci ON coupons (lower(code));\n")
    return s


# --------------------------------------------------------------------------- connection

class Cursor:
    def __init__(self, cur, returns_id):
        self._cur = cur
        self._returns_id = returns_id
        self._lastrowid = None
        self._id_read = False

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        if self._returns_id and not self._id_read:
            self._id_read = True
            row = self._cur.fetchone()
            self._lastrowid = row[0] if row else None
        return self._lastrowid

    @property
    def description(self):
        return self._cur.description

    def fetchone(self):
        if self._cur.description is None:
            return None
        return self._cur.fetchone()

    def fetchall(self):
        if self._cur.description is None:
            return []
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self.fetchall())


class _Empty(Cursor):
    def __init__(self):
        self._returns_id, self._lastrowid, self._id_read = False, None, True

    rowcount = -1
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class Connection:
    """Just enough of the sqlite3.Connection API for this app."""

    def __init__(self, url):
        self._c = psycopg.connect(url, autocommit=True, prepare_threshold=None, row_factory=_row_factory,
                                  connect_timeout=15)
        self._c.adapters.register_loader("numeric", _NumericLoader)
        self._c.adapters.register_dumper(bool, _BoolAsInt)

    def execute(self, sql, params=()):
        word = sql.lstrip()[:8].upper()
        if word.startswith("BEGIN"):
            # SQLite's BEGIN IMMEDIATE = one writer at a time; a transaction-scoped advisory lock does the same.
            self._c.execute("BEGIN")
            self._c.execute("SELECT pg_advisory_xact_lock(%d)" % WRITE_LOCK_KEY)
            return _Empty()
        if word.startswith("PRAGMA"):
            return _Empty()
        pg_sql, returns_id = translate(sql)
        cur = self._c.cursor()
        cur.execute(pg_sql, tuple(params) if params is not None else ())
        return Cursor(cur, returns_id)

    def executemany(self, sql, seq):
        pg_sql, _ = translate(sql)
        cur = self._c.cursor()
        cur.executemany(pg_sql, [tuple(p) for p in seq])
        return Cursor(cur, False)

    def executescript(self, script):
        self._c.execute(script)

    @property
    def in_transaction(self):
        return self._c.info.transaction_status != TransactionStatus.IDLE

    def close(self):
        try:
            if self.in_transaction:
                self._c.execute("ROLLBACK")
        except psycopg.Error:
            pass
        self._c.close()


def connect(url):
    return Connection(url)
