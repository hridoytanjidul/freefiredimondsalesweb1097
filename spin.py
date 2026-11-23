"""Spin wheel: a logged-in customer spins once per cooldown (default 24 h) and wins wallet money,
a personal single-use coupon, or nothing. The server picks the prize; the wheel only animates to it."""
import math
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, g, jsonify, render_template

from db import credit, get_db, q, tx
from helpers import DB_FMT, to_int

bp = Blueprint("spin", __name__)

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I to avoid typos
PRIZE_TYPES = ("wallet", "coupon", "nothing")


def enabled(settings):
    return settings.get("spin_enabled") == "1"


def cooldown_hours(settings):
    return to_int(settings.get("spin_cooldown_hours"), 24, lo=1, hi=720) or 24


def active_prizes(db=None):
    return (db or get_db()).execute("SELECT * FROM spin_prizes WHERE is_active = 1 ORDER BY sort_order, id").fetchall()


def bd_today_start_utc():
    bd_now = datetime.utcnow() + timedelta(hours=6)
    return (bd_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=6)).strftime(DB_FMT)


def status(user, db=None):
    """Whether this user may spin right now: {state, message, secs}.
    state = disabled | login | ineligible | cooldown | ready"""
    s = g.settings
    if not enabled(s):
        return {"state": "disabled", "message": "স্পিন এখন বন্ধ আছে।", "secs": 0}
    if user is None:
        return {"state": "login", "message": "স্পিন করতে আগে লগইন করুন।", "secs": 0}
    db = db or get_db()
    need = to_int(s.get("spin_min_orders"), 1, lo=0) or 0
    if need:
        done = db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'completed'",
                          (user["id"],)).fetchone()[0]
        if done < need:
            return {"state": "ineligible", "secs": 0,
                    "message": "স্পিন করতে কমপক্ষে %d টি সম্পন্ন অর্ডার লাগবে (আপনার আছে %d টি)।" % (need, done)}
    last = db.execute("SELECT created_at FROM spins WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if last:
        next_at = datetime.strptime(last["created_at"][:19], DB_FMT) + timedelta(hours=cooldown_hours(s))
        secs = int((next_at - datetime.utcnow()).total_seconds())
        if secs > 0:
            return {"state": "cooldown", "message": "আপনার পরের স্পিন:", "secs": secs}
    return {"state": "ready", "message": "আপনার ফ্রি স্পিন রেডি! 🎉", "secs": 0}


def pick_prize(db, prizes):
    """Weighted random choice. Wallet prizes that would break today's budget are left out."""
    pool = [p for p in prizes if p["weight"] > 0]
    budget = to_int(g.settings.get("spin_daily_budget"), 0, lo=0) or 0
    if budget:
        paid = db.execute("SELECT COALESCE(SUM(amount), 0) FROM spins WHERE prize_type = 'wallet' AND created_at >= ?",
                          (bd_today_start_utc(),)).fetchone()[0]
        pool = [p for p in pool if p["prize_type"] != "wallet" or paid + p["amount"] <= budget]
    total = sum(p["weight"] for p in pool)
    if total <= 0:
        return None
    roll = secrets.randbelow(total)
    for p in pool:
        roll -= p["weight"]
        if roll < 0:
            return p
    return pool[-1]


def _new_coupon_code(db):
    for _ in range(25):
        code = "SPIN-" + "".join(secrets.choice(CODE_CHARS) for _ in range(6))
        if not db.execute("SELECT 1 FROM coupons WHERE code = ? COLLATE NOCASE", (code,)).fetchone():
            return code
    raise RuntimeError("could not generate a unique coupon code")


def wheel_geometry(prizes, cx=200, cy=200, r=184):
    """SVG paths for each wheel segment, measured clockwise from 12 o'clock."""
    n = len(prizes)
    segs, lights = [], []
    if n:
        step = 360.0 / n

        def pt(deg, rad):
            a = math.radians(deg)
            return cx + rad * math.sin(a), cy - rad * math.cos(a)

        for i, p in enumerate(prizes):
            a0, a1 = i * step, (i + 1) * step
            d = None
            if n > 1:
                x0, y0 = pt(a0, r)
                x1, y1 = pt(a1, r)
                d = "M%d %d L%.2f %.2f A%d %d 0 %d 1 %.2f %.2f Z" % (cx, cy, x0, y0, r, r, 1 if step > 180 else 0, x1, y1)
            segs.append({"id": p["id"], "index": i, "d": d, "color": p["color"], "label": p["label"],
                         "mid": a0 + step / 2, "type": p["prize_type"]})
    for i in range(16):
        x, y = cx + 192 * math.sin(math.radians(i * 22.5)), cy - 192 * math.cos(math.radians(i * 22.5))
        lights.append((round(x, 2), round(y, 2), i % 2))
    return {"segments": segs, "lights": lights, "label_y": cy - r * 0.66}


def prize_text(p):
    if p["prize_type"] == "wallet":
        return "৳%d ওয়ালেট ব্যালেন্স" % p["amount"]
    if p["prize_type"] == "coupon":
        if p["coupon_type"] == "percent":
            cap = " (সর্বোচ্চ ৳%d)" % p["max_discount"] if p["max_discount"] else ""
            return "%d%% ছাড়ের কুপন%s" % (p["amount"], cap)
        return "৳%d ছাড়ের কুপন" % p["amount"]
    return "আবার চেষ্টা করুন"


# --------------------------------------------------------------------------- routes

@bp.get("/spin")
def page():
    if not enabled(g.settings):
        abort(404)
    prizes = active_prizes()
    mine = []
    if g.user:
        mine = q("SELECT * FROM spins WHERE user_id = ? ORDER BY id DESC LIMIT 10", (g.user["id"],))
    winners = q("SELECT s.label, s.prize_type, s.created_at, u.name, u.avatar FROM spins s JOIN users u ON u.id = s.user_id "
                "WHERE s.prize_type <> 'nothing' ORDER BY s.id DESC LIMIT 10")
    return render_template("spin.html", wheel=wheel_geometry(prizes), prizes=prizes, st=status(g.user),
                           mine=mine, winners=winners, hours=cooldown_hours(g.settings), prize_text=prize_text)


@bp.get("/api/spin/check")
def check():
    st = status(g.user)
    return jsonify(canSpin=st["state"] == "ready", **st)


@bp.post("/api/spin")
def spin():
    if not enabled(g.settings):
        return jsonify(ok=False, error="স্পিন এখন বন্ধ আছে।"), 403
    if g.user is None:
        return jsonify(ok=False, error="স্পিন করতে আগে লগইন করুন।"), 401
    uid = g.user["id"]
    with tx() as db:   # BEGIN IMMEDIATE: two clicks/tabs can't both pass the cooldown check
        st = status(g.user, db)
        if st["state"] != "ready":
            return jsonify(ok=False, error=st["message"], state=st["state"], secs=st["secs"]), 409
        prizes = active_prizes(db)
        prize = pick_prize(db, prizes)
        if prize is None:
            return jsonify(ok=False, error="এখন স্পিন করা যাচ্ছে না। কিছুক্ষণ পর চেষ্টা করুন।"), 503
        kind, amount, code = prize["prize_type"], prize["amount"], None
        if kind == "wallet" and amount > 0:
            credit(db, uid, amount, "spin", "Spin wheel prize (%s)" % prize["label"])
            message = "অভিনন্দন! ৳%d আপনার ওয়ালেটে যোগ হয়েছে।" % amount
        elif kind == "coupon" and amount > 0:
            code = _new_coupon_code(db)
            db.execute(
                "INSERT INTO coupons (code, description, discount_type, value, max_discount, min_order, usage_limit, "
                "per_user_limit, ends_at, is_public, is_active, user_id) "
                "VALUES (?, 'Spin wheel prize', ?, ?, ?, 0, 1, 1, datetime('now', ?), 0, 1, ?)",
                (code, prize["coupon_type"], min(amount, 100) if prize["coupon_type"] == "percent" else amount,
                 prize["max_discount"], "+%d days" % prize["valid_days"], uid))
            message = "অভিনন্দন! আপনি পেয়েছেন %s। কোড: %s — %d দিনের মধ্যে চেকআউটে ব্যবহার করুন।" % (
                prize_text(prize), code, prize["valid_days"])
        else:
            kind, amount = "nothing", 0
            message = "এবার হয়নি! পরের স্পিনে আবার চেষ্টা করুন। 🍀"
        db.execute("INSERT INTO spins (user_id, prize_id, label, prize_type, amount, coupon_code) VALUES (?,?,?,?,?,?)",
                   (uid, prize["id"], prize["label"], kind, amount, code))
    return jsonify(ok=True, prize_id=prize["id"], index=[p["id"] for p in prizes].index(prize["id"]),
                   label=prize["label"], type=kind, amount=amount, coupon_code=code,
                   valid_days=prize["valid_days"] if code else None, message=message,
                   next_secs=cooldown_hours(g.settings) * 3600)
