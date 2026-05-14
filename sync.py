"""
Sales Sync — Shopify + Square → Supabase
=========================================
First run : backfills every order/payment from BACKFILL_START (Sept 1 2025) to today.
Daily run : fetches only today's data (smart — skips backfill if data already exists).

Usage:
    python sync.py            # auto mode (backfill if needed, then today)
    python sync.py --backfill # force full re-backfill
    python sync.py --today    # force today only
"""

import os
import sys
import logging
import argparse
import requests
from datetime import date, datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
BACKFILL_START = date(2025, 9, 1)          # fill history from this date
SHOPIFY_STORE  = os.getenv("SHOPIFY_STORE")          # e.g. mystore.myshopify.com
SHOPIFY_TOKEN  = os.getenv("SHOPIFY_ACCESS_TOKEN")
SQUARE_TOKEN   = os.getenv("SQUARE_ACCESS_TOKEN")
SQUARE_LOC     = os.getenv("SQUARE_LOCATION_ID", "")  # blank = all locations
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY")    # service key for writes
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

def _require(*keys):
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)

_require("SHOPIFY_STORE", "SHOPIFY_ACCESS_TOKEN",
         "SQUARE_ACCESS_TOKEN",
         "SUPABASE_URL", "SUPABASE_SERVICE_KEY")

# ── Date helpers ─────────────────────────────────────────────────────────────
def _iso(d: date, end=False) -> str:
    t = "23:59:59" if end else "00:00:00"
    return f"{d.isoformat()}T{t}Z"

def date_range(start: date, end: date):
    """Yield each date from start to end inclusive."""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)

# ── Shopify ──────────────────────────────────────────────────────────────────
def _shopify_orders_for_day(day: date) -> list[dict]:
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/orders.json"
    params = {
        "created_at_min": _iso(day),
        "created_at_max": _iso(day, end=True),
        "financial_status": "paid",
        "status": "any",
        "limit": 250,
        "fields": "id,total_price,currency,created_at",
    }
    orders = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        orders += r.json().get("orders", [])
        link = r.headers.get("Link", "")
        url = next(
            (p.split(";")[0].strip().strip("<>")
             for p in link.split(",") if 'rel="next"' in p),
            None,
        )
        params = {}
    return orders

def shopify_records(day: date) -> tuple[list[dict], float]:
    rows, total = [], 0.0
    for o in _shopify_orders_for_day(day):
        amt = float(o["total_price"])
        total += amt
        rows.append({
            "order_id":  str(o["id"]),
            "platform":  "shopify",
            "amount":    amt,
            "currency":  o.get("currency", "USD"),
            "sale_date": day.isoformat(),
        })
    return rows, total

# ── Square ───────────────────────────────────────────────────────────────────
def _square_payments_for_day(day: date) -> list[dict]:
    headers = {
        "Authorization":  f"Bearer {SQUARE_TOKEN}",
        "Square-Version": "2024-01-18",
    }
    params: dict = {"begin_time": _iso(day), "end_time": _iso(day, end=True), "sort_order": "ASC"}
    if SQUARE_LOC:
        params["location_id"] = SQUARE_LOC
    payments, cursor = [], None
    while True:
        if cursor:
            params["cursor"] = cursor
        r = requests.get("https://connect.squareup.com/v2/payments",
                         headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"Square error: {data['errors']}")
        payments += [p for p in data.get("payments", []) if p.get("status") == "COMPLETED"]
        cursor = data.get("cursor")
        if not cursor:
            break
    return payments

def square_records(day: date) -> tuple[list[dict], float]:
    rows, total = [], 0.0
    for p in _square_payments_for_day(day):
        amt = p["amount_money"]["amount"] / 100   # cents → dollars
        total += amt
        rows.append({
            "order_id":  p["id"],
            "platform":  "square",
            "amount":    amt,
            "currency":  p["amount_money"].get("currency", "USD"),
            "sale_date": day.isoformat(),
        })
    return rows, total

# ── Supabase ─────────────────────────────────────────────────────────────────
_SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

def upsert(records: list[dict]):
    if not records:
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/sales",
                      headers=_SB_HEADERS, json=records, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Supabase error {r.status_code}: {r.text}")

def has_any_data() -> bool:
    """Returns True if the sales table already has rows (backfill done)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/sales",
        headers={**_SB_HEADERS, "Range": "0-0"},
        params={"limit": 1},
        timeout=10,
    )
    return r.ok and r.json() != []

# ── Telegram ─────────────────────────────────────────────────────────────────
def telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
        timeout=10,
    )

# ── Core logic ───────────────────────────────────────────────────────────────
def sync_day(day: date) -> dict:
    """Fetch Shopify + Square for one day, upsert to Supabase. Returns summary."""
    sh_rows, sh_total = shopify_records(day)
    sq_rows, sq_total = square_records(day)
    all_rows = sh_rows + sq_rows
    upsert(all_rows)
    return {
        "date":           day.isoformat(),
        "shopify_total":  sh_total,
        "shopify_orders": len(sh_rows),
        "square_total":   sq_total,
        "square_orders":  len(sq_rows),
        "grand_total":    sh_total + sq_total,
    }

def run_backfill():
    today = date.today()
    days = list(date_range(BACKFILL_START, today))
    log.info("Backfilling %d days (%s → %s)…", len(days), BACKFILL_START, today)
    grand = 0.0
    for i, day in enumerate(days, 1):
        try:
            s = sync_day(day)
            grand += s["grand_total"]
            log.info(
                "[%d/%d] %s  Shopify=$%.2f (%d)  Square=$%.2f (%d)  Day=$%.2f",
                i, len(days), day,
                s["shopify_total"], s["shopify_orders"],
                s["square_total"],  s["square_orders"],
                s["grand_total"],
            )
        except Exception as e:
            log.warning("Skipped %s: %s", day, e)
    log.info("Backfill complete. Total revenue synced: $%.2f", grand)

def run_today():
    today = date.today()
    log.info("Syncing today: %s", today)
    s = sync_day(today)
    log.info(
        "Done. Shopify=$%.2f (%d)  Square=$%.2f (%d)  Total=$%.2f",
        s["shopify_total"], s["shopify_orders"],
        s["square_total"],  s["square_orders"],
        s["grand_total"],
    )
    # Send Telegram daily report
    msg = (
        f"📊 <b>Daily Sales — {today.strftime('%B %d, %Y')}</b>\n\n"
        f"🛍 Shopify: <b>${s['shopify_total']:,.2f}</b> ({s['shopify_orders']} orders)\n"
        f"⬛ Square:  <b>${s['square_total']:,.2f}</b> ({s['square_orders']} payments)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Total: <b>${s['grand_total']:,.2f}</b>"
    )
    telegram(msg)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--backfill", action="store_true", help="Force full backfill from Sept 1 2025")
    grp.add_argument("--today",    action="store_true", help="Force today-only sync")
    args = parser.parse_args()

    if args.backfill:
        run_backfill()
    elif args.today:
        run_today()
    else:
        # Auto mode: backfill first if DB is empty, then sync today
        if not has_any_data():
            log.info("Database is empty — running first-time backfill…")
            run_backfill()
        run_today()
