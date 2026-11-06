# AgriMarket

A farmer-focused agricultural management and trading system built with Django. Farmers keep farm records (fields, crops, harvests, livestock treatments and costs), see profit per crop and herd, plan what to plant and how to price it, and sell graded, traceable produce and livestock directly to buyers. It runs on a laptop with SQLite and needs no physical devices. Card payments go through Paystack (test mode, no real money); SMS and email are simulated in a message log.

## What it does

| For | Features |
|---|---|
| Farmers | **Farm records:** fields, plantings, a **Harvest page** (only crops whose expected harvest date has arrived: start harvesting, record each harvest, list it for sale, finish the crop), herds, treatment register with withdrawal periods, production costs. **Planting planner** ("What should I plant next?", see below). **My analytics:** line graphs of money in vs money out per month, running profit for the season, weekly sales, sales per product, my price vs the market, orders and buyers per month; profit per crop and herd, cost per unit vs the market price, who buys and where, losses, best months to sell. Farmers schedule delivery drivers from their province based on open time slots. A price guide when listing, and quote requests from buyers. |
| Agronomists | Advise the farms that add them (never see sales, buyers or bank details): field visits with photos, soil tests, recommendations the farmer marks done, spray programmes using **only products from the verified remedy catalogue**: the Act 36 registration number and the label's **pre-harvest interval for that crop are taken from the catalogue (never typed in) and block harvesting until they have passed**; off-label use is refused, and pest alerts sent to every farm growing that crop in a province. Verified by the admin first (SACNASP / SASCP number). |
| Buyers | Marketplace with search and filters, cart, reorder ("buy again"), availability alerts, favourite farms, request for quotation to several farms. |
| Everyone | Standard product list with official South African grades, orders and delivery tracking, invoices, notifications. |
| Admin | Accounts only: verifies farms and agronomists, manages users (deactivate, unlock, create staff), platform settings (fees, VAT, delivery rates, product list, card gateway), the **remedy catalogue** (verifies each pesticide against its label before it can be used) and the audit log. **No access to orders, sales, payments or reports.** |
| Distributor | Collects and delivers orders. |

## Quick start

Requires Python 3.10 or newer.

**Windows:** double-click `setup.bat` once, then `run.bat` each time.
**macOS / Linux:** run `./setup.sh` once, then `. .venv/bin/activate && python manage.py runserver`.

Manual steps:

```
python -m venv .venv
.venv\Scripts\activate            (Windows)   or   . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo         (a small, realistic season from 1 March to today)
python manage.py runserver
```

Open http://127.0.0.1:8000.

Instead of `seed_demo` you can load the ready-made sample set: `python manage.py loaddata fixtures/sample_data.json`. `fixtures/AgriMarket_sample_data.xlsx` shows the same data in readable tables.

### Demo logins (password for all: `Farm2026!`)

The demo is one season from 1 March to today: 10 farms, 10 buyers, 10 agronomists, 10 drivers and about 260 orders, every record dated on its own day. Install into a **new, empty folder** (never unzip over an older copy). Your data lives in `db.sqlite3`; to keep it when you update, copy that file into the new folder.

| Email | Role | Try this |
|---|---|---|
| farmer4@agrimarket.test | Farmer — Bokamoso Farm, Limpopo (vegetables, goats, maize) | **Analytics** (graphs, profit per crop — the frost-hit cabbage lost money), **Plan next crop**, **Advice** (agronomist visits, an overdue lime recommendation), tomatoes that can't be harvested until the spray waiting period ends, a paid order to pack |
| farmer2@agrimarket.test | Farmer — Umgeni Valley, KZN (vegetables, eggs) | Weekly egg batches, an agronomist suggestion to plant butternut (it appears in the planner) |
| farmer5@agrimarket.test | Farmer — Highveld Fields, Mpumalanga | A cabbage harvest waiting to be listed, maize just planted, fall armyworm alert |
| farmer@agrimarket.test | Farmer — Rietvlei, Free State (grain) | Silo maize with monthly prices and a bulk price; quote for the mill's yellow maize request |
| farmer3@agrimarket.test | Farmer — Kgalagadi Livestock, North West | Weaners inside a deworming withdrawal period (hidden from buyers) |
| agronomist@agrimarket.test | Agronomist | Three farms, visit due list, log a visit with a recommendation, send a pest alert |
| agronomist2@agrimarket.test | Agronomist (not verified yet) | What an agronomist sees before the admin checks the registration |
| admin@agrimarket.test | Admin | Account dashboard, verify `newfarmer` and `agronomist2`, users, audit log, Settings |
| buyer@agrimarket.test | Buyer (supermarket) | Cart and checkout (card: simulated or Paystack test mode), "Buy again", alerts |
| market@, restaurant@, mill@, butchery@agrimarket.test | Buyers | Market agent, restaurant, maize mill, butchery |
| driver.lp@agrimarket.test (also .kzn, .mp, .fs, .nw) | Distributors | Deliveries in their province; one is on the road today |
| newfarmer@agrimarket.test | Farmer (pending) | What an unverified farmer can and cannot do |

**Card payments:** as admin open *Settings → Card payments*, choose *Paystack test mode* and paste your Paystack test secret key (`sk_test_…`). Paystack test card: `4084 0840 8408 4081`, CVV `408`, any future expiry. Past sales recorded with an earlier order date use a built-in card check (`4111 1111 1111 1111` accepted, `4000 0000 0000 0002` declined).

## How the database keeps stock correct

The main rule: **when only one item is left, only one buyer can get it.** This is enforced in three layers.

1. **One atomic statement takes stock.** Every sale runs a single conditional update:
   `UPDATE listing SET quantity = quantity - n WHERE id = ? AND quantity >= n`.
   The check and the change happen together inside the database. If two buyers press "Place order" at the same moment, the first update succeeds and the second finds the condition false and changes nothing, so that buyer gets "Only 0 head available". See `marketplace/services.py`.
2. **The whole checkout is one transaction.** Order, sub-orders, order lines, payment and stock changes are saved together or not at all. If any line fails (out of stock, declined card), everything is rolled back, so you never get half an order or stock taken without an order. See `orders/services.py: place_order`.
3. **The database itself refuses bad data.** CHECK constraints stop stock going below zero even if application code has a bug.

`tests/test_stock_integrity.py` proves this, including a test that starts 12 real threads racing to buy the last 5 lambs. Exactly 5 succeed and stock ends at 0.

Other rules that work the same way:

- **Status changes are race-safe.** A sub-order only moves if it still has the status that was read, so a double click cannot cancel an order twice or return stock twice. Refunds use the same pattern, so they cannot be sent twice.
- **Livestock is whole head only.** Quantities like 2.5 head are rejected in forms, at checkout and in stock adjustments.
- **Stock changes always have a reason.** A farmer cannot type over the stock figure; changes go through "Adjust stock" with a reason (restock, spoilage, death, theft, recount), which feeds the wastage report.

## Data standards applied

| Standard | Where |
|---|---|
| Money as `DECIMAL(12,2)`, quantities as `DECIMAL(12,3)`, never floating point | All models |
| VAT and commission rounded half-up to cents | `marketplace.models.money` |
| Prices, names and VAT rate copied onto each order line at purchase, so later edits never change past orders | `OrderItem` |
| Unique keys: email, batch number, order number, invoice number, one cart line per listing, one sub-order per farmer per order, one review per purchase | Model `UniqueConstraint`s |
| CHECK constraints: no negative stock or money, prices above zero, rating 1–5, livestock sold per head only, expiry after harvest, delivered orders must have a delivery note, rejected farms and resolved disputes must have a reason, refund parts must add up; paid payouts must have a reference | Model `CheckConstraint`s |
| Normalised design: livestock details in their own table, bulk price tiers in their own table, status history in its own table | `LivestockDetail`, `PriceTier`, `StatusChange` |
| Financial records are never deleted (`PROTECT` foreign keys, no delete in the admin) | Orders, payments, refunds |
| Append-only audit log that cannot be edited or deleted | `core.models.AuditLog` |
| POPIA: consent stored at registration, buyer contact details shown only to parties to that order, "remove my personal information" anonymises the account but keeps tax records | `accounts` |
| Passwords hashed (PBKDF2-SHA256), 8+ characters with a letter and a digit, lockout for 15 minutes after 5 failures, 30-minute idle timeout | `settings.py`, `accounts/backends.py` |

## Standards applied

**Product standards.** Every listing, planting, herd and quote request uses a product from the **standard product list** (`marketplace/standards.py`). Each product has a code, allowed units and its official grade table under the Agricultural Product Standards Act, 1990 (Act No. 119 of 1990):

| Product | Official grades |
|---|---|
| White / yellow maize | WM1, WM2, WM3 / YM1, YM2, YM3 |
| Bread wheat | Grade 1–4, Utility |
| Sunflower seed / soya beans | FH1, FS1 / SB1 |
| Fresh vegetables, tomatoes, potatoes | Class 1, Class 2, Class 3, Lowest Class (or Unclassified) |
| Fresh fruit | Class 1, Class 2, Lowest Class |
| Eggs | Grade 1–3, sold by size (large, extra large, medium) |
| Seed | Certified / uncertified |
| Livestock | Sold per head, identified by brand mark and tags |

A listing can't use a grade or unit that isn't in its product's table; both the form and the database model check this. The admin can add products or hide them under **Settings → Standard product list**.

**Food safety: sprays.** A spray can only be recorded with a product from the **verified remedy catalogue** (Admin → *Remedies*). Each entry holds the Act 36 registration number and, for every crop on the label, that crop's pre-harvest interval (PHI). The agronomist or farmer only picks the crop and the product; the backend copies the registration number, active ingredient and PHI from the catalogue and calculates the safe-to-harvest date. A product that isn't verified, has been withdrawn, or isn't registered for that crop (off-label) is refused, in the form and again in the model. A missing product can be sent for verification with a photo of the label (*Spray products* page); the admin checks it and verifies it. The full register can be loaded from a CSV file with `python manage.py import_remedies file.csv` (see the command's help for the columns). The sample products in the demo data use made-up registration numbers and are marked "Sample data for the demo - not a real label".

**Food safety: livestock.** Livestock treatments are recorded with the remedy's Act 36 registration number and its withdrawal period. While any treatment on a herd is within its withdrawal period, that herd's listings are hidden from buyers and checkout refuses them. They come back automatically when the period ends, and buyers who set alerts are notified.

**Market data privacy.** Market prices (used in the price guide, a farm's "my price vs the market" and the planting planner) only count a product when at least **3 different farms** sold it in the period, so no one can work out another farm's sales. Farmers never see each other's quotes, and buyer names are never shown.

## Scheduled jobs

Run this every hour (Windows Task Scheduler or cron). `run.bat` also runs it once at start-up.

```
python manage.py run_daily_jobs
```

It sends near-expiry alerts, writes off expired stock as wastage, releases livestock whose withdrawal period has ended (and notifies waiting buyers), reminds farmers of harvests due in 7 days and again on the day a crop is due, cancels card orders not paid within 60 minutes (after checking with Paystack), returning their stock, and completes orders 3 days after delivery if no dispute was raised. Each automatic change is dated when its deadline passed, not when the job ran. The time limits are editable under **Settings**.

## Running the tests

```
python manage.py test tests
```

There are 136 tests. They cover the agronomist role (verification, access only to linked farms, visits, alerts), spray pre-harvest intervals, the verified remedy catalogue (label PHI used, off-label and unverified products refused, admin verification, CSV import), the planting planner, the sales graphs, orders confirming without farmer approval, past sales dated on their own day, and overselling, rollback, negative stock, VAT rounding, the order lifecycle, refunds, time-outs, permissions, lockout and the audit log. They also cover past-dated orders and the date window, duplicate refusal, double-submit protection, official grades and units, the Harvest page (only due crops, start, record, finish) and listing from a harvest, saved bank details and Paystack key, profit per crop, withdrawal blocking, alerts, favourites, reorder, the quote flow (only one quote can be accepted), and the 3-farm privacy rule.

## Using PostgreSQL

SQLite is fine for a laptop demo; it queues writers one after another. For real multi-user use, switch to PostgreSQL:

```
pip install "psycopg[binary]"
set DB_ENGINE=postgres            (Windows; use export on macOS/Linux)
set DB_NAME=agrimarket
set DB_USER=postgres
set DB_PASSWORD=yourpassword
python manage.py migrate
```

No code changes are needed; the same constraints and atomic updates apply.

## Project layout

```
accounts/      users, roles, farmer verification, login lockout, POPIA deletion
marketplace/   standard product list, listings (produce + livestock), photos, price tiers, stock adjustments,
               cart, catalog, availability alerts, favourite farms, search log
farm/          fields, plantings, harvests, herds, treatment register, production costs, profit per crop/herd
insights/      market price rules (3-farm rule), price guide, seasonality, unmet demand, farmer analytics,
               sales trends (trends.py) and the planting planner (planner.py)
advisory/      agronomists: verification, farm links, soil tests, field visits, recommendations,
               verified remedy catalogue (Act 36) and spray records with label pre-harvest intervals, pest alerts
orders/        checkout, sub-orders per farmer, status flow, delivery, payments, refunds, disputes, reviews,
               PDF invoices, reorder, quote requests and quotes
finance/       farm earnings, AgriMarket income, farm reports (sales, best sellers, wastage, CSV export)
core/          dashboards, notifications, simulated SMS/email log, audit log, settings, scheduled jobs, demo data
tests/         automated tests
templates/     HTML pages      static/css/app.css   styling
```

## Dates, past sales and duplicates

- **Past sales:** at checkout the buyer can set an **order date** anywhere from 1 March of this year to today. The sale is then recorded as if it happened that day: the order time, payment, invoice, history and notifications all carry that date, and nothing on screen says it was entered later. For the later steps (packing, collection, delivery) a **Date** box appears, so each step can be dated after the previous one and never in the future. Only the admin's audit log keeps the real time each record was typed in. The order date can't be earlier than the stock's harvest date. Admin can switch this off, or change the start date, under Settings.
- **Records dated on their own day:** a planting, harvest, cost, treatment, visit or listing entered for an earlier date is stamped on that date (core/clock.py), with a believable time of day.
- **One date window everywhere:** planting, harvest, treatment, cost and herd dates must also fall between the start date (1 March by default) and today. Date pickers only offer those dates.
- **No duplicates:** the same planting, harvest, treatment, cost, listing (same product, grade and harvest date), quote request, phone number, VAT number or farm (same name and town) can't be entered twice. Each form also carries a one-time token, so a double click, refresh or Back + resubmit never saves anything twice.
- **Fewer pages:** forms open inside the page they belong to, for example "Add produce" on My stock, "New quote request" on Quotes, password on Profile, staff accounts on Users, and approvals on Farmer verification. Cart and checkout are one page. After saving you stay on the same page and the new row is highlighted.

## Order records

Every order shows exactly who ordered what, from whom and when:

- **Order lists** (farmer, distributor, buyer): order number, date and time placed, buyer (name, business type, province), selling farm, each item (product, grade, quantity × price), delivery, payment and status.
- **Order page:** buyer and contact details, delivery address, payment details (method, card ending or Paystack reference, and when it was paid), and totals. For each farm it shows the seller and VAT number, every line with grade and batch number, the distributor and delivery date, and a history of who changed the status and when.
- **Order register** (menu: Order register / Sales register): one row per order line, with filters for date range, buyer, buyer type, farm, product, order status and payment status, and a 30-column CSV export. Farmers see only their own sales; buyers use My orders.

Each order line stores the product name, code, grade, batch number, unit price and VAT rate as they were at the time of purchase, so later changes to a listing never change a past order.

## Order flow

Awaiting payment → Confirmed → Packed → In transit → Delivered → Completed. Collection orders skip "In transit".

- **Card only.** Buyers pay by card on Paystack's page (or the simulated card offline). The order is confirmed as soon as the payment is verified; the farmer is told to pack and the invoice is issued.
- **Money goes straight to the farms (Paystack split payments).** Each verified farm with bank details gets a Paystack subaccount (set up automatically when the farm is verified or saves its bank details, or with *Settings → Set up farm payout accounts*). Paystack pays each farm's share (goods + VAT − 5% commission) straight into its bank account; the commission and the delivery fee stay in AgriMarket's account. A farm's stock is only shown in the marketplace once its payout account is ready. Bank details are entered once and kept; when the admin saves the Paystack key, farms that already saved bank details are set up automatically.
- **Refunds and complaints are handled by the farmer.** If a farm can't supply (frost, hail) it cancels before packing with a reason; a buyer can also cancel before packing. Buyers can complain within 3 days of delivery and the farm answers: full refund, partial refund or rejected with a reason.
- **Each side returns its own part of a refund.** Paystack sends the whole refund back to the buyer's card at once. AgriMarket returns its 5% on the refunded goods (and the delivery fee if the goods were never delivered); the farm's part is kept back automatically from its next card sales. If Paystack refuses a refund, the hourly job tries again and the admin sees it under *Income*.
- **Farm earnings** (farmer: *More → Earnings*): per month, sales, AgriMarket's 5%, what Paystack paid into the bank, and the farm's part of refunds. Nothing is settled by hand.
- **AgriMarket income** (admin: *Income*): commission and delivery fees per month, totals only.

## Planting planner

Farm records → **Plan next crop** answers "what should I plant next on each field?" for a chosen planting date. For every crop that suits the field it shows the expected profit on the free area, when it would be ready, and where each figure comes from: the farm's **own** past yields and costs first, then the average of at least 3 farms, then a crop guide (`farm/crop_guide.py`). It warns about:

- harvest gluts (several farms expecting the same crop in the same weeks),
- crop rotation (same family as the last crop on that field),
- planting outside the usual months, soil pH from the latest soil test, dryland fields for vegetables;

and highlights crops buyers are searching for or requesting quotes on that nobody is selling, plus any planting advice from the farm's agronomist. "Plan this crop" opens the planting form already filled in.

## Card payments with Paystack (test mode = no real money)

Admin → **Settings → Card payments**:

| Choice | What happens |
|---|---|
| Simulated test card | Card checked inside AgriMarket (test card 4111 1111 1111 1111). Works offline; the sample data uses it. |
| **Paystack test mode** | Paste your `sk_test_` secret key (or put it in `email_settings.bat` as `PAYSTACK_SECRET_KEY`). The buyer pays on Paystack's real page; no real money moves. Test card 4084 0840 8408 4081, CVV 408, PIN 408408, OTP 123456. Real cards don't work in test mode. |
| Paystack live | Real money, with your own live key. |

How a card order works with Paystack:

1. Place order → the order is **Awaiting payment** and the stock is held (default 60 minutes, under Settings).
2. The buyer pays on Paystack's page; AgriMarket never sees the card details. The payment carries a split: each farm's share goes to its subaccount.
3. Paystack sends the buyer back and AgriMarket asks Paystack directly whether the payment succeeded and the amount matches.
4. The payment becomes **Paid**, the order **Confirmed**, the invoice is issued and the farm is told to pack.

Past-dated card sales (order date before today) were already paid, so they are recorded with the built-in card.

## Simulated or not included

- **Payments:** card only, through Paystack (test or live) with split payments; a built-in card is used offline and to record past sales. Full card numbers are never stored.
- **SMS and email:** order notifications are shown in the system and written to the message log (Admin → SMS / email log).
- **Forgot password:** the login page has a "Forgot your password?" link. The reset e-mail holds a link valid for 30 minutes that works once. By default the e-mail is printed in the terminal running the server (copy the link from there). To send real e-mails, copy `email_settings.example.bat` to `email_settings.bat` and fill it in (`run.bat` loads it; it is in `.gitignore`, so it is never uploaded). The settings, for example for Gmail with an app password:

  ```
  set EMAIL_HOST=smtp.gmail.com
  set EMAIL_PORT=587
  set EMAIL_HOST_USER=youraddress@gmail.com
  set EMAIL_HOST_PASSWORD=your-16-letter-app-password
  ```
  (on macOS/Linux use `export` instead of `set`).
- **Not included:** distributor costs in the platform profit and loss report, and split deliveries.
