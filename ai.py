"""AI help assistant (/ai and the floating chat button).

Answers customers' questions about this store (products, prices, ordering, payments, wallet,
coupons, flash sales, vouchers, spin wheel, policies, their own orders) and about Free Fire,
using the Claude API. Turned on in Admin -> Settings -> AI Assistant.
"""
import hashlib
import html
import logging
import os
import re
from datetime import timedelta

import anthropic
from flask import (Blueprint, Response, abort, current_app, g, has_request_context, jsonify, render_template, request,
                   stream_with_context, url_for)

import coupons
import payments
import spin as spin_lib
from db import get_db, q, tx
from flashsale import visible_sales
from helpers import BD_OFFSET, DB_FMT, bdtime, money, to_int, utcnow

log = logging.getLogger(__name__)
bp = Blueprint("ai", __name__)

# Models the admin can choose from (id -> label). Opus gives the best answers; the others cost less.
MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 — best answers (default)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 — about half the cost",
    "claude-haiku-5-5": "Claude Haiku 5.5 — cheapest and fastest",
}
DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output, cache read) — used only for the admin's cost estimate.
PRICES = {"claude-opus-5-5": (4.00, 20.00, 0.20), "claude-sonnet-5-5": (2.00, 10.00, 0.10),
          "claude-haiku-5-5": (0.10, 0.50, 0.01)}
# If the model's safety check declines, the API retries on a fallback model in the same call.
# Haiku 5.5 has no server-side fallback, so it is called without it.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
NO_SERVER_FALLBACK = {"claude-haiku-5-5"}
# Without an Anthropic key, Claude is reached through Vercel AI Gateway, which signs in with the
# deployment's own OIDC token (no key to create). Billed to the Vercel team's AI Gateway credits.
GATEWAY_URL = "https://ai-gateway.vercel.sh"
GATEWAY_MODELS = {"claude-opus-5-5": "anthropic/claude-opus-5.5", "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
                  "claude-haiku-5-5": "anthropic/claude-haiku-5.5"}

MAX_TOKENS = 4096        # per answer; a cost cap for a public chat box (answers are short)
MAX_TURNS = 12           # messages of history sent with each question
MAX_CHARS = 1000         # longest customer message
BURST_WINDOW_MIN, BURST_MAX = 10, 12     # per person: at most 12 questions in 10 minutes

MSG_OFF = "AI সহায়ক এখন বন্ধ আছে।"
MSG_EMPTY = "প্রশ্ন লিখুন।"
MSG_LONG = "প্রশ্নটি অনেক বড় (সর্বোচ্চ ১০০০ অক্ষর)।"
MSG_BURST = "অনেক দ্রুত প্রশ্ন করা হচ্ছে। কয়েক মিনিট পর আবার চেষ্টা করুন।"
MSG_USER_DAY = "আজকের AI প্রশ্নের সীমা শেষ। কাল আবার চেষ্টা করুন, অথবা Support এ যোগাযোগ করুন।"
MSG_SITE_DAY = "AI সহায়ক আজকের মতো ব্যস্ত। জরুরি হলে Support এ যোগাযোগ করুন।"
MSG_REFUSAL = "দুঃখিত, এই প্রশ্নের উত্তর দিতে পারছি না। অর্ডার বা পেমেন্ট নিয়ে প্রশ্ন থাকলে Support এ যোগাযোগ করুন।"
MSG_ERROR = "দুঃখিত, এখন উত্তর দেওয়া যাচ্ছে না। একটু পর আবার চেষ্টা করুন।"
MSG_BUSY = "AI সহায়ক এখন ব্যস্ত। এক মিনিট পর আবার চেষ্টা করুন।"


# --------------------------------------------------------------------------- settings

def api_key(settings):
    return (settings.get("ai_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def gateway_token():
    """Vercel AI Gateway credential: an AI_GATEWAY_API_KEY if one is set, otherwise the OIDC token
    Vercel attaches to every request of a deployed site (it rotates, so it's read per request).
    The header is only trusted when actually running on Vercel."""
    key = (os.environ.get("AI_GATEWAY_API_KEY") or "").strip()
    if key:
        return key
    if os.environ.get("VERCEL") and has_request_context():
        return (request.headers.get("x-vercel-oidc-token") or "").strip()
    return ""


def credentials(settings):
    """How to reach Claude: ("anthropic", key) with the owner's Anthropic key, else ("gateway", token)
    through Vercel AI Gateway, else (None, None)."""
    key = api_key(settings)
    if key:
        return "anthropic", key
    token = gateway_token()
    if token:
        return "gateway", token
    return None, None


def enabled(settings):
    return settings.get("ai_enabled") == "1" and credentials(settings)[0] is not None


def model_for(settings):
    m = settings.get("ai_model") or DEFAULT_MODEL
    return m if m in MODELS else DEFAULT_MODEL


_clients = {}


def client_for(key, base_url=None):
    """One client per credential (reused across requests on a warm server)."""
    c = _clients.get((key, base_url))
    if c is None:
        _clients.clear()
        c = _clients[(key, base_url)] = anthropic.Anthropic(api_key=key, base_url=base_url, timeout=60.0, max_retries=2)
    return c


# --------------------------------------------------------------------------- what the assistant knows

def _plain(markup, limit):
    """Admin-written HTML (page content, product rules) -> short plain text."""
    t = re.sub(r"<\s*(br|/p|/li|/h\d|/div|/tr)\s*/?>", "\n", markup or "", flags=re.I)
    t = re.sub(r"<\s*li[^>]*>", "• ", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t\r]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + " …"


INSTRUCTIONS = """You are the AI help assistant on {site}, a Bangladeshi website where gamers buy Free Fire diamonds, memberships and gift vouchers.

What you help with:
1. Questions about this website: products and prices, how to order, Player ID, payment (wallet, bKash/Nagad/Rocket Send Money, Instant Pay), Add Money, order status, cancellations and refunds, coupons, flash sales, gift vouchers, the spin wheel, the account, and the site's policies.
2. Questions about the game Free Fire: gameplay, characters, pets, weapons, ranks and rank push, modes, settings and sensitivity, events, diamonds, memberships, level-up pass, servers and regions, UID, and account safety.
For anything unrelated, say in one short sentence that you only help with this website and Free Fire, and offer to help with those.

How to answer:
- Reply in the customer's language: Bangla if they write Bangla, English if English, and natural Banglish if they mix. Be friendly and brief: a few sentences, or short numbered steps for how-to questions. No long essays.
- For facts about this website, use only the STORE INFORMATION below and the live context you are given with each question. Never invent prices, packages, payment numbers, delivery times, offers or coupon codes. If something isn't there, say you're not sure and point the customer to support ({support}).
- Link to pages of this site with Markdown links using the paths below, for example [Add Money](/profile/addwallet). Don't make up page paths.
- Free Fire changes often (events, patch notes, prices in the game, redeem codes). Say when something may have changed and suggest checking in the game or Garena's official channels. Never invent redeem codes.
- You cannot see or change payments, place or cancel orders, or give refunds, diamonds or discounts yourself. Explain how the customer can do it, or that the admin handles it, and give the support link when a person is needed.
- Order status: if the live context includes the customer's orders, answer from it. If they aren't logged in, ask them to log in at [Login](/login) and check [My Orders](/profile/order). Only talk about the logged-in customer's own account.
- Safety: never ask for passwords, OTPs, PINs or bKash/Nagad PINs. Warn that "free diamond" generators, hacks, mod apps and account sellers are scams or get accounts banned, and don't help with cheating, hacking or selling accounts. Customers should send money only to the numbers shown on the payment page.
- If a customer says they sent money but the order isn't updated, tell them manual payments are checked by the admin, to wait a little, and to contact support with the order number and Transaction ID if it takes long.

STORE INFORMATION
{info}"""


def store_info(settings):
    """Facts about the store for the system prompt. Kept stable between requests (no times,
    no stock counts) so the prompt can be cached."""
    s = settings
    L = ["Store: %s — %s" % (s.get("site_name", ""), s.get("site_tagline", "")),
         "Website: %s" % request.url_root.rstrip("/")]
    support = s.get("telegram_support") or ""
    if support and support.rstrip("/") not in ("https://t.me", "http://t.me"):
        L.append("Support (Telegram): %s" % support)
    if (s.get("telegram_group") or "").rstrip("/") not in ("", "https://t.me", "http://t.me"):
        L.append("Telegram group: %s" % s["telegram_group"])
    if s.get("helpline_hours"):
        L.append("Support hours: %s" % s["helpline_hours"])
    L.append("Contact page: /contactus")

    L.append("\nPAGES")
    pages = [("Home", "/"), ("All products", "/shop"), ("Login", "/login"), ("Register", "/register"),
             ("My account", "/profile"), ("Add Money to wallet", "/profile/addwallet"), ("My Orders", "/profile/order"),
             ("My Gift Vouchers", "/profile/giftvoucher"), ("Wallet transactions", "/profile/transaction"),
             ("Flash Sale", "/flashsale"), ("Leaderboard", "/leaderboard"), ("How to order (tutorial)", "/tutorial"),
             ("About", "/about-us"), ("Terms", "/terms-and-conditions"), ("Privacy", "/privacy-policy"),
             ("Refund policy", "/refund-return-policy"), ("Delete account policy", "/delete-account-policy")]
    if spin_lib.enabled(s):
        pages.append(("Daily Spin Wheel", "/spin"))
    if (s.get("ff_id_checker_url") or "").strip():
        pages.append(("Free Fire ID name checker", "/free-fire-id-checker"))
    L += ["- %s: %s" % p for p in pages]

    L.append("\nPAYMENT")
    L.append("- Wallet Pay: pay instantly from the wallet balance. Add Money between %s and %s."
             % (money(s.get("min_wallet_amount", 20)), money(s.get("max_wallet_amount", 25000))))
    methods = q("SELECT name, number, account_type, instructions FROM payment_methods WHERE is_active = 1 ORDER BY sort_order, id")
    if methods:
        L.append("- Send Money (manual): send the exact amount to one of these numbers, then enter the sender number "
                 "and Transaction ID (TrxID). The admin checks it, usually within minutes during support hours. A TrxID can be used only once.")
        for m in methods:
            L.append("  • %s (%s): %s%s" % (m["name"], m["account_type"], m["number"],
                                            (" — " + _plain(m["instructions"], 200)) if m["instructions"] else ""))
    if payments.enabled(s):
        L.append("- Instant Pay: automatic online payment; the order is paid as soon as the payment goes through.")

    L.append("\nPRODUCTS AND PRICES (Bangladeshi Taka)")
    cats = q("SELECT id, name FROM categories WHERE is_active = 1 ORDER BY sort_order, id")
    rules_seen = {}
    for c in cats:
        prods = q("SELECT * FROM products WHERE is_active = 1 AND category_id = ? ORDER BY sort_order, id", (c["id"],))
        if not prods:
            continue
        L.append("[%s]" % c["name"])
        for p in prods:
            path = url_for("voucher_product" if p["kind"] == "voucher" else "product", pid=p["id"], slug=p["slug"])
            kind = ("gift voucher: the code is delivered on the website after payment" if p["kind"] == "voucher"
                    else "top-up: enter the Free Fire Player ID (UID)")
            L.append("%s (%s) — %s%s" % (p["name"], path, kind, ("; " + p["delivery_note"]) if p["delivery_note"] else ""))
            pk = q("SELECT k.name, k.price, k.in_stock, (SELECT COUNT(*) FROM voucher_codes v WHERE v.package_id = k.id "
                   "AND v.status = 'available') AS codes FROM packages k WHERE k.product_id = ? ORDER BY k.sort_order, k.id",
                   (p["id"],))
            items = []
            for k in pk:
                out = not k["in_stock"] or (p["kind"] == "voucher" and k["codes"] == 0)
                items.append("%s %s%s" % (k["name"], money(k["price"]), " (stock out)" if out else ""))
            if items:
                L.append("  " + "; ".join(items))
            rules = _plain(p["description"], 600)
            if rules:
                if rules in rules_seen:
                    L.append("  Rules: same as %s" % rules_seen[rules])
                else:
                    rules_seen[rules] = p["name"]
                    L.append("  Rules: " + rules.replace("\n", " "))

    L.append("\nFEATURES")
    L.append("- Coupons: type the code (or tap a coupon chip) on the product page before paying. Coupons don't work on flash-sale items. "
             "A cancelled order gives the coupon use back.")
    L.append("- Flash sales: limited-stock discounted packages with a timer, shown on the homepage and /flashsale.")
    L.append("- Cancelled orders: if the order was already paid, the money goes back to the customer's wallet, and the reason is shown in My Orders.")
    if spin_lib.enabled(s):
        prizes = ", ".join(p["label"] for p in spin_lib.active_prizes())
        L.append("- Spin wheel: one free spin every %s hours for customers with at least %s completed order(s). Prizes: %s."
                 % (spin_lib.cooldown_hours(s), s.get("spin_min_orders", "1"), prizes))

    L.append("\nPOLICIES")
    for slug, path in (("tutorial", "/tutorial"), ("terms", "/terms-and-conditions"), ("refund", "/refund-return-policy"),
                       ("privacy", "/privacy-policy"), ("delete-account", "/delete-account-policy"), ("about", "/about-us")):
        pg = q("SELECT title, content FROM pages WHERE slug = ?", (slug,), one=True)
        if pg:
            L.append("[%s — %s]\n%s" % (pg["title"], path, _plain(pg["content"], 1200)))

    extra = (s.get("ai_extra_info") or "").strip()
    if extra:
        L.append("\nNOTES FROM THE STORE OWNER\n" + extra[:4000])
    return "\n".join(L)


def system_prompt(settings):
    support = settings.get("telegram_support") or ""
    support = support if support.rstrip("/") not in ("", "https://t.me", "http://t.me") else "the Contact page (/contactus)"
    return INSTRUCTIONS.format(site=settings.get("site_name", "this website"), support=support, info=store_info(settings))


def live_context(user):
    """Changes from request to request (time, flash sales, the customer's own account), so it goes in
    a system message after the question instead of the cached system prompt."""
    now_bd = utcnow() + BD_OFFSET
    L = ["Live context — current time in Bangladesh: %s" % now_bd.strftime("%d %b %Y, %I:%M %p")]
    sales = [f for f in visible_sales(limit=6) if f["state"] in ("live", "upcoming")]
    if sales:
        L.append("Flash sales:")
        for f in sales:
            when = ("ends %s" % bdtime(f["ends_at"])) if f["state"] == "live" else ("starts %s" % bdtime(f["starts_at"]))
            L.append("- %s %s: %s instead of %s, %s left, %s (/flashsale/%d/%s)"
                     % (f["product_name"], f["package_name"], money(f["sale_price"]), money(f["regular_price"]),
                        f["left"], when, f["id"], f["slug"]))
    else:
        L.append("Flash sales: none running right now.")
    chips = coupons.public_coupons(None, user["id"] if user else None)
    if chips:
        L.append("Coupon codes the customer can use now: " + "; ".join(
            "%s (%s%s)" % (c["code"], coupons.label(c), ", only for you" if c["user_id"] else "") for c in chips))
    if not user:
        L.append("The customer is not logged in.")
        return "\n".join(L)
    L.append("Logged-in customer: %s, wallet balance %s." % (user["name"], money(user["balance"])))
    orders = q("SELECT id, product_name, package_name, price, player_id, payment_method, payment_status, status, admin_note, "
               "created_at, is_voucher FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user["id"],))
    if orders:
        L.append("Their latest orders (newest first):")
        for o in orders:
            note = (" Admin note: %s." % o["admin_note"]) if o["admin_note"] else ""
            pid = "" if o["is_voucher"] else " Player ID %s," % o["player_id"]
            L.append("- #%d %s — %s, %s,%s paid by %s, payment %s, order %s, placed %s.%s"
                     % (o["id"], o["package_name"], o["product_name"], money(o["price"]), pid, o["payment_method"],
                        o["payment_status"], o["status"], bdtime(o["created_at"]), note))
    else:
        L.append("They have no orders yet.")
    reqs = q("SELECT id, amount, method, status, admin_note, created_at FROM wallet_requests WHERE user_id = ? "
             "ORDER BY id DESC LIMIT 3", (user["id"],))
    if reqs:
        L.append("Their latest Add Money requests: " + "; ".join(
            "%s via %s, %s, %s%s" % (money(r["amount"]), r["method"], r["status"], bdtime(r["created_at"]),
                                     (" (" + r["admin_note"] + ")") if r["admin_note"] else "") for r in reqs))
    return "\n".join(L)


# --------------------------------------------------------------------------- limits

def client_key():
    if g.get("user"):
        return "u:%d" % g.user["id"]
    raw = (current_app.config["SECRET_KEY"] + "|" + (request.remote_addr or "?")).encode()
    return "ip:" + hashlib.sha256(raw).hexdigest()[:20]


def _utc(dt):
    return dt.strftime(DB_FMT)


def check_and_count(db, settings, key, user_id, model):
    """Inside tx(): enforce the limits, then record this question. Returns an error message or None."""
    since_burst = _utc(utcnow() - timedelta(minutes=BURST_WINDOW_MIN))
    day_start = spin_lib.bd_today_start_utc()
    if db.execute("SELECT COUNT(*) FROM ai_usage WHERE client_key = ? AND created_at >= ?",
                  (key, since_burst)).fetchone()[0] >= BURST_MAX:
        return MSG_BURST
    if db.execute("SELECT COUNT(*) FROM ai_usage WHERE client_key = ? AND created_at >= ?",
                  (key, day_start)).fetchone()[0] >= to_int(settings.get("ai_user_limit"), 40, lo=1):
        return MSG_USER_DAY
    if db.execute("SELECT COUNT(*) FROM ai_usage WHERE created_at >= ?",
                  (day_start,)).fetchone()[0] >= to_int(settings.get("ai_daily_limit"), 300, lo=1):
        return MSG_SITE_DAY
    g.ai_usage_id = db.execute("INSERT INTO ai_usage (client_key, user_id, model) VALUES (?, ?, ?)",
                               (key, user_id, model)).lastrowid
    return None


def usage_summary(settings):
    """Questions and estimated cost for the admin settings page."""
    day_start = spin_lib.bd_today_start_utc()
    month_start = _utc(utcnow() - timedelta(days=30))
    out = {}
    for label, since in (("today", day_start), ("month", month_start)):
        rows = q("SELECT model, COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o, "
                 "COALESCE(SUM(cache_read_tokens), 0) AS c FROM ai_usage WHERE created_at >= ? GROUP BY model", (since,))
        n = sum(r["n"] for r in rows)
        usd = 0.0
        for r in rows:
            pi, po, pc = PRICES.get(r["model"], PRICES[DEFAULT_MODEL])
            usd += (r["i"] * pi + r["o"] * po + r["c"] * pc) / 1_000_000
        out[label] = {"questions": n, "usd": usd}
    out["limit"] = to_int(settings.get("ai_daily_limit"), 300, lo=1)
    return out


# --------------------------------------------------------------------------- routes

def _clean_history(raw):
    """Validate the chat history sent by the browser: alternating user/assistant text, ending with
    the customer's new question. Returns (messages, error)."""
    if not isinstance(raw, list) or not raw:
        return None, MSG_EMPTY
    msgs = []
    for m in raw[-MAX_TURNS:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            return None, MSG_EMPTY
        text = m["content"].strip()
        if not text:
            continue
        if m["role"] == "user" and len(text) > MAX_CHARS:
            return None, MSG_LONG
        text = text[:4000]
        if msgs and msgs[-1]["role"] == m["role"]:
            msgs[-1]["content"] += "\n\n" + text      # keep turns alternating
        else:
            msgs.append({"role": m["role"], "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        return None, MSG_EMPTY
    return msgs, None


@bp.get("/ai")
def page():
    if not enabled(g.settings):
        abort(404)
    return render_template("ai.html")


@bp.post("/api/ai/chat")
def chat():
    settings = g.settings
    if not enabled(settings):
        return jsonify(error=MSG_OFF), 404
    msgs, err = _clean_history((request.get_json(silent=True) or {}).get("messages"))
    if err:
        return jsonify(error=err), 400
    model = model_for(settings)
    mode, credential = credentials(settings)
    user = g.get("user")
    with tx() as db:
        err = check_and_count(db, settings, client_key(), user["id"] if user else None, model)
    if err:
        return jsonify(error=err), 429

    params = dict(
        model=GATEWAY_MODELS[model] if mode == "gateway" else model,
        max_tokens=MAX_TOKENS,
        # Store facts first and unchanged between requests, so they're served from the prompt cache;
        # this request's live facts (time, flash sales, the customer's account) come after the cache point.
        system=[{"type": "text", "text": system_prompt(settings), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": live_context(user)}],
        messages=msgs,
        output_config={"effort": "low"},   # chat: quick, short answers
    )
    client = client_for(credential, GATEWAY_URL if mode == "gateway" else None)
    # Server-side refusal fallbacks are a Claude API beta; AI Gateway gets the plain request.
    use_fallback = mode == "anthropic" and model not in NO_SERVER_FALLBACK
    usage_id = g.get("ai_usage_id")

    def generate():
        sent = False
        try:
            if use_fallback:
                stream_cm = client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **params)
            else:
                stream_cm = client.messages.stream(**params)
            with stream_cm as stream:
                for text in stream.text_stream:
                    sent = True
                    yield text
                final = stream.get_final_message()
            if final.stop_reason == "refusal":
                yield ("\n\n" if sent else "") + MSG_REFUSAL
            elif final.stop_reason == "max_tokens":
                yield " …"
            u = final.usage
            if usage_id:
                get_db().execute("UPDATE ai_usage SET model = ?, input_tokens = ?, output_tokens = ?, cache_read_tokens = ? WHERE id = ?",
                                 (model,
                                  (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                                  u.output_tokens or 0, getattr(u, "cache_read_input_tokens", 0) or 0, usage_id))
        except anthropic.AuthenticationError:
            log.error("AI: the %s credential was rejected", "Anthropic API key" if mode == "anthropic" else "AI Gateway")
            yield ("\n\n" if sent else "") + MSG_ERROR
        except (anthropic.RateLimitError, anthropic.InternalServerError):
            yield ("\n\n" if sent else "") + MSG_BUSY
        except anthropic.APIConnectionError:
            yield ("\n\n" if sent else "") + MSG_ERROR
        except anthropic.APIStatusError as e:
            log.error("AI: API error %s: %s", e.status_code, getattr(e, "message", e))
            yield ("\n\n" if sent else "") + MSG_ERROR

    resp = Response(stream_with_context(generate()), mimetype="text/plain")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
