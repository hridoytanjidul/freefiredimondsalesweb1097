-- TopUp store database schema (SQLite; pg.py translates it for Postgres)
-- All money values are whole Bangladeshi Taka (INTEGER).

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    email           TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    phone           TEXT,
    password_hash   TEXT    NOT NULL,
    balance         INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    google_sub      TEXT,                          -- Google account id (Google login)
    avatar          TEXT,                          -- profile photo URL from Google
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id     INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    name            TEXT    NOT NULL,
    slug            TEXT    NOT NULL DEFAULT 'item',
    image           TEXT,
    tag_line        TEXT,
    delivery_note   TEXT,
    input_label     TEXT    NOT NULL DEFAULT 'এখানে গেমের Player ID দিন',
    numeric_id      INTEGER NOT NULL DEFAULT 1,   -- 1 = player id must be digits only
    description     TEXT,                          -- rules & conditions (HTML, admin-written)
    sort_order      INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    kind            TEXT    NOT NULL DEFAULT 'topup',  -- topup (needs player ID) | voucher (delivers a code)
    redeem_link     TEXT,                          -- where customers redeem voucher codes
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS packages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    price       INTEGER NOT NULL CHECK (price > 0),
    in_stock    INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- Time-limited discounted packages with limited stock.
-- starts_at / ends_at are stored in UTC ('YYYY-MM-DD HH:MM:SS').
CREATE TABLE IF NOT EXISTS flash_sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id      INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    sale_price      INTEGER NOT NULL CHECK (sale_price > 0),
    stock           INTEGER NOT NULL CHECK (stock > 0),
    sold            INTEGER NOT NULL DEFAULT 0 CHECK (sold >= 0),
    per_user_limit  INTEGER NOT NULL DEFAULT 1,   -- 0 = no limit
    starts_at       TEXT    NOT NULL,
    ends_at         TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_flash_time ON flash_sales(is_active, ends_at);

-- Discount codes customers type at checkout.
CREATE TABLE IF NOT EXISTS coupons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description     TEXT,
    discount_type   TEXT    NOT NULL DEFAULT 'percent',   -- percent | fixed
    value           INTEGER NOT NULL CHECK (value > 0),
    max_discount    INTEGER NOT NULL DEFAULT 0,           -- cap for percent coupons, 0 = no cap
    min_order       INTEGER NOT NULL DEFAULT 0,
    product_id      INTEGER REFERENCES products(id) ON DELETE CASCADE,  -- NULL = every product
    usage_limit     INTEGER NOT NULL DEFAULT 0,           -- total uses, 0 = unlimited
    per_user_limit  INTEGER NOT NULL DEFAULT 1,           -- 0 = unlimited
    used            INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    starts_at       TEXT,                                 -- UTC, NULL = right away
    ends_at         TEXT,                                 -- UTC, NULL = never expires
    is_public       INTEGER NOT NULL DEFAULT 0,           -- listed on the product page
    is_active       INTEGER NOT NULL DEFAULT 1,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- personal coupon (spin prize); NULL = anyone
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Spin wheel prizes (one wheel segment each) and the history of every spin.
CREATE TABLE IF NOT EXISTS spin_prizes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT    NOT NULL,                     -- text on the wheel, e.g. "৳10"
    prize_type      TEXT    NOT NULL DEFAULT 'nothing',   -- wallet | coupon | nothing
    amount          INTEGER NOT NULL DEFAULT 0,           -- wallet taka, or coupon value
    coupon_type     TEXT    NOT NULL DEFAULT 'percent',   -- percent | fixed (coupon prizes)
    max_discount    INTEGER NOT NULL DEFAULT 0,           -- cap for percent coupon prizes
    valid_days      INTEGER NOT NULL DEFAULT 7,           -- how long a won coupon lasts
    weight          INTEGER NOT NULL DEFAULT 10 CHECK (weight >= 0),  -- relative chance
    color           TEXT    NOT NULL DEFAULT '#7c3aed',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS spins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prize_id        INTEGER REFERENCES spin_prizes(id) ON DELETE SET NULL,
    label           TEXT    NOT NULL,
    prize_type      TEXT    NOT NULL,
    amount          INTEGER NOT NULL DEFAULT 0,
    coupon_code     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_spins_user ON spins(user_id, id);
CREATE INDEX IF NOT EXISTS idx_spins_time ON spins(created_at);

-- Manual "Send Money" numbers (bKash / Nagad / Rocket ...)
CREATE TABLE IF NOT EXISTS payment_methods (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    number        TEXT    NOT NULL,
    account_type  TEXT    NOT NULL DEFAULT 'Personal',
    color         TEXT    NOT NULL DEFAULT '#e2136e',
    instructions  TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id      INTEGER REFERENCES products(id) ON DELETE SET NULL,
    package_id      INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    product_name    TEXT    NOT NULL,
    package_name    TEXT    NOT NULL,
    price           INTEGER NOT NULL,
    player_id       TEXT    NOT NULL,
    payment_method  TEXT    NOT NULL,             -- wallet | gateway | <manual method name>
    payment_status  TEXT    NOT NULL,             -- paid | unverified | awaiting
    sender_number   TEXT,
    trx_id          TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending | processing | completed | cancelled
    admin_note      TEXT,
    flash_sale_id   INTEGER REFERENCES flash_sales(id) ON DELETE SET NULL,
    coupon_id       INTEGER REFERENCES coupons(id) ON DELETE SET NULL,
    coupon_code     TEXT,
    discount        INTEGER NOT NULL DEFAULT 0,    -- coupon discount; price is what the customer paid
    is_voucher      INTEGER NOT NULL DEFAULT 0,
    voucher_code    TEXT,                          -- copy of the delivered code (kept even if stock is deleted)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders(user_id, id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_trx ON orders(trx_id COLLATE NOCASE) WHERE trx_id IS NOT NULL;

-- Gift voucher codes in stock. available -> reserved (payment pending) -> delivered.
CREATE TABLE IF NOT EXISTS voucher_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id      INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    code            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'available',
    order_id        INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    delivered_at    TEXT,
    UNIQUE (package_id, code)
);
CREATE INDEX IF NOT EXISTS idx_vc_stock ON voucher_codes(package_id, status, id);
CREATE INDEX IF NOT EXISTS idx_vc_order ON voucher_codes(order_id);

-- Manual add-money requests that an admin approves
CREATE TABLE IF NOT EXISTS wallet_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount          INTEGER NOT NULL CHECK (amount > 0),
    method          TEXT    NOT NULL,
    sender_number   TEXT    NOT NULL,
    trx_id          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    admin_note      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_trx ON wallet_requests(trx_id COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_wallet_status ON wallet_requests(status, id);

-- Automatic payment gateway (UddoktaPay) sessions
CREATE TABLE IF NOT EXISTS gateway_payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose         TEXT    NOT NULL,              -- order | wallet
    ref_id          INTEGER,                       -- order id when purpose = order
    amount          INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'initiated',  -- initiated | paid
    invoice_id      TEXT    UNIQUE,
    method          TEXT,
    sender_number   TEXT,
    trx_id          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Wallet ledger: every balance change is recorded here
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount          INTEGER NOT NULL,              -- + credit, - debit
    type            TEXT    NOT NULL,              -- deposit | purchase | refund | adjustment
    description     TEXT,
    balance_after   INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id);

CREATE TABLE IF NOT EXISTS banners (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    image       TEXT    NOT NULL,
    link        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    slug        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Uploaded images (logo, banners, product pictures), kept in the database so they
-- survive on hosts with no permanent disk (Vercel). Served at /media/<name>.
CREATE TABLE IF NOT EXISTS uploads (
    name        TEXT PRIMARY KEY,
    mime        TEXT NOT NULL,
    data        BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- AI assistant: one row per question, for rate limits and the admin's cost estimate.
-- The conversation text is not stored.
CREATE TABLE IF NOT EXISTS ai_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key          TEXT    NOT NULL,              -- u:<user id>, or ip:<hash> for visitors
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    model               TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_client ON ai_usage(client_key, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_time ON ai_usage(created_at);
