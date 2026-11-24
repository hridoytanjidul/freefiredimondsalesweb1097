"""Shared helpers: auth guards, CSRF, uploads, validation, template filters, icons."""
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from markupsafe import Markup
from werkzeug.security import generate_password_hash

# --------------------------------------------------------------------------- passwords


def hash_password(password):
    """PBKDF2-SHA256 works on every Python build (Werkzeug's default, scrypt,
    is missing from macOS's system Python and some hosting providers)."""
    return generate_password_hash(password, method="pbkdf2:sha256:600000")

# --------------------------------------------------------------------------- auth guards


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("অর্ডার করতে বা প্রোফাইল দেখতে আগে লগইন করুন।", "info")
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    """Only allow redirects back into this site (blocks open-redirects)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# --------------------------------------------------------------------------- CSRF

CSRF_EXEMPT = {"payment_webhook"}


def csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def csrf_protect():
    if request.method != "POST" or request.endpoint in CSRF_EXEMPT:
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="ফর্মের মেয়াদ শেষ হয়ে গেছে। পেজটি রিলোড করে আবার চেষ্টা করুন।")


# --------------------------------------------------------------------------- uploads

ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif"}


def _looks_like_image(head):
    return (
        head.startswith(b"\x89PNG")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"GIF87a")
        or head.startswith(b"GIF89a")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def save_upload(file_storage, prefix="img"):
    """Store an uploaded image in the database and return its name, or None if no file was sent.
    Raises ValueError for anything that is not a real PNG/JPG/WEBP/GIF."""
    from db import get_db
    if not file_storage or not file_storage.filename:
        return None
    name = file_storage.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("শুধু PNG, JPG, WEBP বা GIF ছবি আপলোড করা যাবে।")
    data = file_storage.stream.read()
    if not _looks_like_image(data[:16]):
        raise ValueError("ফাইলটি সঠিক ছবি নয়।")
    ext = "jpg" if ext == "jpeg" else ext
    stored = "%s_%s.%s" % (re.sub(r"[^a-z_]", "", prefix.lower()) or "img", uuid.uuid4().hex[:16], ext)
    get_db().execute("INSERT INTO uploads (name, mime, data) VALUES (?, ?, ?)", (stored, IMAGE_MIME[ext], data))
    return stored


def delete_upload(filename):
    from db import get_db
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return
    get_db().execute("DELETE FROM uploads WHERE name = ?", (filename,))


def upload_url(filename):
    if not filename:
        return ""
    return url_for("media", name=filename)


# --------------------------------------------------------------------------- validation

BD_PHONE_RE = re.compile(r"^01[3-9]\d{8,9}$")   # 11 digits (12 for Rocket's check digit)
TRX_RE = re.compile(r"^[A-Za-z0-9]{6,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_digits(s):
    return re.sub(r"\D", "", s or "")


def clean_trx(s):
    return re.sub(r"\s", "", s or "").upper()


def safe_link(url):
    """Accept only http(s) links or site-relative paths (blocks javascript: links)."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("/") and not url.startswith("//"):
        return url
    if re.match(r"^https?://", url, re.I):
        return url
    raise ValueError("লিংক অবশ্যই https:// দিয়ে শুরু হতে হবে।")


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "item"


def to_int(value, default=None, lo=None, hi=None):
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        return default
    if hi is not None and v > hi:
        return default
    return v


# --------------------------------------------------------------------------- template filters

def money(v):
    try:
        return "৳%s" % format(int(v), ",")
    except (TypeError, ValueError):
        return "৳0"


def bdtime(value, fmt="%d %b %Y, %I:%M %p"):
    """Stored timestamps are UTC; Bangladesh is UTC+6 with no DST."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)
    return (dt + timedelta(hours=6)).strftime(fmt)


def initials(name):
    name = (name or "?").strip()
    return name[:1].upper() if name else "?"


def avatar_hue(seed):
    return (sum(ord(c) for c in str(seed)) * 37) % 360


def avatar_html(name, url=None, cls=""):
    """Google profile photo when we have one, otherwise a coloured initial."""
    if url and str(url).startswith("https://"):
        return Markup('<img class="avatar %s" src="%s" alt="" referrerpolicy="no-referrer" loading="lazy">') % (cls, url)
    return Markup('<span class="avatar %s" style="--h: %s">%s</span>') % (cls, avatar_hue(name), initials(name))


# --------------------------------------------------------------------------- Bangladesh time (UTC+6, no DST)

BD_OFFSET = timedelta(hours=6)
DB_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow():
    """The current time in UTC, without a timezone attached: the same form the database stores
    and compares. (Replaces datetime.utcnow(), which Python 3.12 deprecates.)"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def bd_input_to_utc(value):
    """'2026-11-23T20:00' typed in Bangladesh time (datetime-local input) -> UTC string for the DB."""
    dt = datetime.strptime((value or "").strip()[:16], "%Y-%m-%dT%H:%M")
    return (dt - BD_OFFSET).strftime(DB_FMT)


def utc_to_bd_input(value):
    if not value:
        return ""
    dt = datetime.strptime(str(value)[:19], DB_FMT)
    return (dt + BD_OFFSET).strftime("%Y-%m-%dT%H:%M")


def seconds_until(utc_value):
    dt = datetime.strptime(str(utc_value)[:19], DB_FMT)
    return max(0, int((dt - utcnow()).total_seconds()))


STATUS_LABELS = {
    "pending": ("Pending", "warn"),
    "processing": ("Processing", "info"),
    "completed": ("Done", "ok"),
    "cancelled": ("Cancelled", "bad"),
    "approved": ("Approved", "ok"),
    "rejected": ("Rejected", "bad"),
    "paid": ("Paid", "ok"),
    "unverified": ("Verifying", "warn"),
    "awaiting": ("Unpaid", "bad"),
}


def status_badge(status):
    label, kind = STATUS_LABELS.get(status, (str(status).title(), "info"))
    return Markup('<span class="badge badge-%s">%s</span>') % (kind, label)


# --------------------------------------------------------------------------- icons (Feather/Lucide style, MIT)

ICONS = {
    "home": '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    "play": '<rect x="2" y="4" width="20" height="16" rx="3"/><polygon points="10 9 15 12 10 15 10 9"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "wallet": '<path d="M20 12V8H6a2 2 0 0 1-2-2c0-1.1.9-2 2-2h12v4"/><path d="M4 6v12c0 1.1.9 2 2 2h14v-4"/><path d="M18 12a2 2 0 0 0 0 4h4v-4z"/>',
    "bag": '<path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "check-circle": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "headset": '<path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>',
    "chat": '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    "send": '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "award": '<circle cx="12" cy="8" r="7"/><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "info": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    "plus": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "gem": '<path d="M6 3h12l4 6-10 13L2 9z"/><path d="M11 3 8 9l4 13 4-13-3-6"/><path d="M2 9h20"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    "settings": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    "card": '<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>',
    "box": '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
    "chart": '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "menu": '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>',
    "arrow-right": '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "gift": '<polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"/>',
    "tag": '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
    "phone": '<rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>',
}


def icon(name, cls=""):
    return Markup(
        '<svg class="ic %s" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">%s</svg>'
        % (cls, ICONS.get(name, ""))
    )
