# AgriMarket

A Django web system where South African farmers keep farm records, see profit per crop and herd, plan what to plant, and sell graded produce and livestock directly to buyers. Buyers pay by card through Paystack (test mode, no real money), and each farm's share is paid straight into its own bank account. Verified agronomists advise the farms that add them, and the administrator verifies farms, agronomists and spray products.

Built with Django 5.2, SQLite, Chart.js, ReportLab and the Paystack API. Runs on a laptop.

## Roles

| Role | Main features |
|---|---|
| Farmer | Fields, plantings, harvests, herds, treatments, costs; analytics and profit per crop; planting planner; listings; packing orders and booking drivers; complaints and refunds; earnings |
| Buyer | Marketplace search, cart and checkout from several farms, card payment, reorder, alerts, quote requests |
| Agronomist | Visits, soil tests, recommendations and sprays for linked farms (sprays only from the verified remedy catalogue), pest alerts |
| Distributor | Collects and delivers orders in their province |
| Admin | Verifies farms, agronomists and spray products; users; settings; audit log. No access to sales or payments |

## Setup

Requires Python 3.10 or newer.

**Windows:** double-click `setup.bat` once, then `run.bat` each time.
**macOS / Linux:** run `./setup.sh` once, then `. .venv/bin/activate && python manage.py runserver`.

Manual steps:

```
python -m venv .venv
.venv\Scripts\activate            (macOS/Linux: . .venv/bin/activate)
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo         (sample season from 1 March to today)
python manage.py runserver
```

Open http://127.0.0.1:8000.

## Demo logins

Password for all accounts: `Farm2026!`

| Email | Role |
|---|---|
| farmer4@agrimarket.test | Farmer with the most data (analytics, planner, advice, spray waiting period) |
| buyer@agrimarket.test | Buyer (supermarket) |
| agronomist@agrimarket.test | Verified agronomist |
| driver.lp@agrimarket.test | Distributor (Limpopo) |
| admin@agrimarket.test | Administrator |
| newfarmer@agrimarket.test, agronomist2@agrimarket.test | Not yet verified |

More accounts (all `@agrimarket.test`): farmers `farmer@`, `farmer2@` to `farmer10@`; buyers such as `market@`, `restaurant@`, `mill@`, `butchery@`, `spar@`; agronomists `agronomist3@` to `agronomist10@`; one driver per province, e.g. `driver.kzn@`, `driver.gp@`, `driver.wc@`.

## Card payments (Paystack test mode)

As admin, open **Settings → Card payments**, choose *Paystack test mode* and paste your `sk_test_…` key.
Test card: `4084 0840 8408 4081`, CVV `408`, PIN `408408`, OTP `123456`, any future expiry date.

Without a key, the built-in simulated card works offline (`4111 1111 1111 1111`).

## Optional settings

Copy `email_settings.example.bat` to `email_settings.bat` and fill it in; `run.bat` loads it. This file is in `.gitignore` and must never be uploaded.

- `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`: send real password-reset e-mails (otherwise they are printed in the terminal).
- `PAYSTACK_SECRET_KEY`: Paystack key used when none is saved in Settings.
- `DB_ENGINE=postgres`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`: use PostgreSQL instead of SQLite (`pip install "psycopg[binary]"`).

## Scheduled jobs

Run every hour (Windows Task Scheduler or cron); `run.bat` also runs it once at start-up:

```
python manage.py run_daily_jobs
```

It cancels unpaid orders after 60 minutes, completes delivered orders after 3 days, and sends expiry, harvest and withdrawal-period alerts.

## Tests

```
python manage.py test tests
```

136 tests covering stock integrity, checkout, payments and refunds, the order lifecycle, permissions, farm records, spray and withdrawal rules, and analytics.

## Project layout

```
accounts/     users, roles, verification, login lockout, POPIA
marketplace/  products and grades, listings, cart, alerts
farm/         fields, plantings, harvests, herds, treatments, costs
orders/       checkout, payments (Paystack), delivery, refunds, invoices, quotes
advisory/     agronomists, remedy catalogue, sprays, pest alerts
insights/     market prices, analytics, planting planner
finance/      farm earnings, AgriMarket income, reports
core/         dashboards, notifications, audit log, settings, scheduled jobs, demo data
tests/        automated tests
```

