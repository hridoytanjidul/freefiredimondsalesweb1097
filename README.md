# Free Fire Diamond TopUp Store

A complete Bangladeshi game top-up website: customers buy Free Fire diamonds and memberships with their wallet balance or bKash / Nagad / Rocket, and you deliver and manage everything from an admin panel.

Built with Python (Flask) and SQLite, with no Node.js and no build step.

## Run it on your computer

```bash
cd FREEFIRE
python3 -m pip install -r requirements.txt     # Flask, requests, gunicorn
python3 app.py                                 # open http://127.0.0.1:8000
```

The database (`instance/topup.db`) is created automatically on first run, with sample Free Fire products and prices.

### Create your admin account

```bash
python3 manage.py create-admin you@example.com "Your Name"     # asks for a password
```

Then log in at `/login` and open **`/admin`**.

Other commands:

```bash
python3 manage.py set-password someone@example.com   # reset any user's password
python3 manage.py make-admin someone@example.com     # give an existing user admin access
```

## First things to do in the admin panel

1. **Settings**: set your site name, logo, theme colour, Telegram support and group links, notice text, and footer text.
2. **Payment Numbers**: replace the `01XXXXXXXXX` placeholders with your real bKash, Nagad and Rocket numbers.
3. **Products**: upload a square image for each product and adjust the package prices.
4. **Banners**: upload wide banners (about 1200×500) for the homepage slider.
5. **Pages**: edit About, Terms, Privacy, Refund, Delete Account and Tutorial.

## How orders work

| Customer pays with | What happens |
|---|---|
| **Wallet Pay** | Balance is deducted instantly. The order appears under **Orders → To deliver**. |
| **Send Money** (manual) | The customer sends money to your number and enters the sender number and TrxID. The order appears under **Orders → To verify**. Check the TrxID in your bKash/Nagad app, then complete it. |
| **Instant Pay** (optional) | Automatic payment through UddoktaPay. Once the payment is confirmed, the order goes straight to **To deliver**. |

You top up the player's ID in-game or through your supplier, then press **Complete**. If you press **Cancel** with a reason, the customer sees that reason. A cancelled order that was already paid is refunded to the customer's wallet.

**Add Money** requests (manual) appear under **Admin → Add Money**. Approve one to credit the wallet.

Every balance change is recorded in the wallet ledger, which customers see under **Transactions**.

### Automatic payments (optional)

In **Settings → Wallet & payments**, enter your UddoktaPay base URL and API key.

- **Testing:** use base URL `https://sandbox.uddoktapay.com` and API key `982d381360a69d419689740d9f2e26ce36fb7a50`.
- **Webhook:** point it at `https://yourdomain.com/payment/webhook`.

Every payment is re-checked with UddoktaPay's verify API before any credit is given. A payment counts only once, even if both the browser return and the webhook arrive.

## Flash sale

Go to **Admin → Flash Sale → New Flash Sale**. Pick a package and set:

- the sale price
- the stock (how many can be sold)
- the start and end times (Bangladesh time)
- the maximum per customer (0 means no limit)

The sale appears on the homepage in a red **⚡ Flash Sale** section with live countdowns, and on `/flashsale`. Before it starts it shows **Coming Soon**. When the stock runs out it shows **Sold Out**, and after the end time it disappears.

- **No overselling:** stock is reserved in the same database transaction as the order, so it can't be oversold even when many people buy at the same second.
- **Stock comes back:** cancelling a flash-sale order returns its stock. An Instant Pay order that isn't paid within 30 minutes is cancelled and its stock freed.
- **Late payments:** if someone pays after their order was cancelled, the money goes to their wallet.
- **Finding orders:** flash-sale orders carry a ⚡ **Flash** badge. To list all orders for one sale, open that sale in the admin and click **View orders**.

## Coupons

Go to **Admin → Coupons → New Coupon** and set:

- **Discount:** a percentage (with an optional maximum, e.g. 10% up to ৳20) or a fixed amount (e.g. ৳50 off).
- **Who and what:** a minimum package price, all products or just one, the total number of uses, and uses per customer.
- **When:** optional start and end times (Bangladesh time).
- **Public:** if ticked, the code appears as a tappable chip on product pages. Otherwise customers must know the code.

Customers type the code (or tap a chip) on the product page and see the new price before paying. It works with Wallet Pay, Send Money and Instant Pay.

- **Checked twice:** the code is checked again at payment, and uses are counted safely, so a coupon limited to 100 uses can never be used 101 times.
- **Never free:** a coupon can't make an order cost nothing; the customer always pays at least ৳1.
- **Cancellations:** a cancelled order gives the coupon use back, and the refund is the discounted price actually paid.
- **No stacking:** coupons don't apply to flash sales, which are already discounted.
- **Reporting:** the coupon list shows how many times each code was used and how much discount it has given away.

## Gift vouchers (auto-delivered codes)

A **Gift Voucher** product sells codes, such as game voucher codes or redeem codes. Customers don't enter a Player ID; they receive a code.

1. Create or edit a product and set **Product type → Gift Voucher**.
   - Optionally add a **Redeem link**, the page where customers use the code.
2. On each package, click **Codes** (or go to **Admin → Voucher Codes**) and paste your codes, one per line. Duplicates are skipped.
3. The stock customers see is the number of unused codes. A package with no codes shows **Stock Out**.

How each payment method delivers the code:

| Payment | Delivery |
|---|---|
| Wallet Pay | Instantly. The code appears in **Profile → Gift Vouchers** with a **Copy Code** button. |
| Instant Pay | As soon as the gateway confirms payment. |
| Send Money | A code is held for the order. Pressing **Payment Verified** delivers it; **Cancel** puts it back in stock. |

- **No double-selling:** a code can never go to two people. When several customers race for the last code, one gets it and the others are not charged.
- **Low-stock warning:** the dashboard warns you when any voucher package has fewer than 3 codes left.
- **Codes are kept:** a delivered code stays on the customer's order, even if you later delete the package.

## Spin wheel

Logged-in customers get one free spin every 24 hours at `/spin`. The wheel is linked from the homepage banner and the header. Prizes can be:

- **Wallet cash:** added to the customer's wallet straight away.
- **A discount coupon:** a personal, single-use `SPIN-XXXXXX` code, valid for the days you set. It only works for the winner and appears automatically in their coupon box at checkout.
- **Try Again:** no prize.

Manage it in **Admin → Spin Wheel**:

- **On/off and cooldown:** switch the wheel on or off, and set the hours between spins.
- **Eligibility:** completed orders needed before a customer can spin. The default is 1, which stops people opening fake accounts to farm free money.
- **Daily budget:** an optional limit on wallet cash given away per day. Once reached, only coupons and "Try Again" can be won until midnight (BD time).
- **Prizes:** each segment's label, type, amount, colour, order and **weight**. Weight is the relative chance; the page shows each prize's actual % chance and the **average wallet cost per spin**.
- **History:** recent spins and totals. Coupon prizes are listed under **Coupons → Spin prize coupons**.

The server picks the prize with secure randomness, and the wheel only animates to it, so it can't be cheated from the browser. A customer clicking SPIN many times at once still gets exactly one spin.

## Google login

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services**.
2. Open **OAuth consent screen**, fill in the app name, support email and your domain, then publish.
3. Go to **Credentials** → **Create credentials** → **OAuth client ID**, and choose type **Web application**.
4. Under **Authorized redirect URIs**, add the exact address shown in **Admin → Settings → Google Login**. It looks like:
   - `https://yourdomain.com/social-callback` for your live site
   - `http://127.0.0.1:8000/social-callback` for testing on your computer
5. Paste the **Client ID** and **Client Secret** into Admin → Settings and save.

**Continue with Google** then appears on the login and register pages.

- **Existing accounts:** a customer whose email already has an account is linked to it, not duplicated.
- **Profile photos:** the customer's Google photo appears in the header, recent orders and leaderboard.
- **Email login:** customers who signed up with Google can set a password in their profile if they also want email login.

### Free Fire ID name checker (optional)

In **Settings**, paste any player-info API URL with `{uid}` where the ID goes. A "check name" button then appears on the product page and on `/free-fire-id-checker`.

## Put it online (production)

Use any Linux VPS, or a Python host such as Render, Railway or PythonAnywhere.

```bash
pip install -r requirements.txt
export SECRET_KEY="a-long-random-string"      # keep this secret and never change it
export SESSION_COOKIE_SECURE=1                 # when the site is served over https
export TRUST_PROXY=1                           # when behind nginx / Cloudflare / Render
python3 manage.py create-admin you@example.com "Your Name"
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Put nginx or Cloudflare in front for HTTPS.

**Back up these regularly:**

- `instance/topup.db`: all users, orders and balances
- `static/uploads/`: your images

If you don't set `SECRET_KEY`, one is generated and stored in `instance/secret_key`.

## Project layout

```
app.py          customer site: pages, wallet, profile, gateway return/webhook
checkout.py     shared order + payment logic (wallet / Send Money / Instant Pay, coupons, voucher codes)
coupons.py      coupon rules + the checkout "Apply" API
spin.py         spin wheel page, prize draw and spin API (/spin)
flashsale.py    flash sale pages and ordering (/flashsale)
google_auth.py  Sign in with Google (/auth/google, /social-callback)
admin.py        admin panel (/admin)
db.py           database, wallet ledger, settings, first-run sample data
helpers.py      login guards, CSRF, image uploads, formatting, icons
payments.py     UddoktaPay client
manage.py       command-line admin tools
schema.sql      database tables
templates/      HTML pages (public, profile/, admin/)
static/         css/style.css, js/app.js, img/, uploads/
```

## Security built in

- Passwords hashed with PBKDF2.
- CSRF tokens on every form.
- The same-origin frame policy blocks clickjacking.
- Uploads are checked to be real images, and redirect targets are validated.
- Wallet spending is atomic, so balances can't go negative or be double-spent.
- TrxIDs can't be reused.
- The admin panel is restricted to admin accounts.
