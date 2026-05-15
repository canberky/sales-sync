"""
Sales Sync — Shopify + Square → Supabase
=========================================
  - Shopify: ALL orders (no financial_status filter), uses total_price
  - Square:  /v2/orders/search with net_amounts (handles refunds correctly)
  - Timezone: America/New_York (Orlando EDT) for all date boundaries

Modes:
    python sync.py --live     # sync today + yesterday silently (run every 10 min)
    python sync.py --report   # sync yesterday + send Telegram report (run once daily)
    python sync.py --backfill # full re-backfill from Sept 1 2025
    python sync.py            # auto: backfill if DB empty, then --live
"""

import os, sys, json, logging, argparse, requests, pytz
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SHOPIFY_STORE  = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN  = os.getenv("SHOPIFY_ACCESS_TOKEN")
SQUARE_TOKEN   = os.getenv("SQUARE_ACCESS_TOKEN")
SQUARE_LOC     = os.getenv("SQUARE_LOCATION_ID", "")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

BACKFILL_START = date(2025, 9, 1)
TZ_ORLANDO     = pytz.timezone("America/New_York")


def _require(*keys):
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)

_require(
    "SHOPIFY_STORE", "SHOPIFY_ACCESS_TOKEN",
    "SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID",
    "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
)


# ── Date helpers ──────────────────────────────────────────────────────────────

def orlando_today() -> date:
    """Current date in Orlando time (not UTC)."""
    return datetime.now(TZ_ORLANDO).date()


def day_to_utc_iso(d: date, end=False) -> str:
    """
    Convert an Orlando calendar day to a UTC ISO string.
    start → midnight EDT   end → 23:59:59 EDT
    Both Shopify and Square use this so 'May 9' always means May 9 in Orlando.
    """
    h, m, s = (23, 59, 59) if end else (0, 0, 0)
    local_dt = TZ_ORLANDO.localize(datetime(d.year, d.month, d.day, h, m, s))
    return local_dt.astimezone(pytz.utc).isoformat().replace("+00:00", "Z")


def date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


# ── Shopify ───────────────────────────────────────────────────────────────────

def shopify_records(day: date):
    headers  = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    base_url = f"https://{SHOPIFY_STORE}/admin/api/2023-10/orders.json"
    params   = {
        "created_at_min": day_to_utc_iso(day, end=False),
        "created_at_max": day_to_utc_iso(day, end=True),
        "status": "any",  # no financial_status filter — matches original repo
        "limit": 250,
        "fields": "id,total_price,currency,created_at",
    }
    orders, url = [], base_url
    while url:
        r = requests.get(
            url, headers=headers,
            params=(params if url == base_url else {}),
            timeout=30,
        )
        r.raise_for_status()
        orders += r.json().get("orders", [])
        link = r.headers.get("Link", "")
        url  = next(
            (p.split(";")[0].strip().strip("<>")
             for p in link.split(",") if 'rel="next"' in p),
            None,
        )

    rows, total = [], 0.0
    for o in orders:
        amt = float(o.get("total_price", 0.0))
        total += amt
        rows.append({
            "order_id":  str(o["id"]),
            "platform":  "shopify",
            "amount":    round(amt, 2),
            "currency":  o.get("currency", "USD"),
            "sale_date": day.isoformat(),
        })
    return rows, round(total, 2)


# ── Square ────────────────────────────────────────────────────────────────────

def square_records(day: date):
    if not SQUARE_LOC:
        log.warning("SQUARE_LOCATION_ID not set — skipping Square")
        return [], 0.0

    headers = {
        "Square-Version": "2023-10-20",
        "Authorization":  f"Bearer {SQUARE_TOKEN}",
        "Content-Type":   "application/json",
    }
    # Orlando timezone boundaries (same as Shopify).
    # Raw UTC midnight = 8 PM EDT the day before, which shifts early-morning
    # transactions to the wrong date.
    payload = {
        "location_ids": [SQUARE_LOC],
        "query": {
            "filter": {
                "date_time_filter": {
                    "created_at": {
                        "start_at": day_to_utc_iso(day, end=False),
                        "end_at":   day_to_utc_iso(day, end=True),
                    }
                },
                "state_filter": {"states": ["COMPLETED"]},
            }
        },
    }
    r = requests.post(
        "https://connect.squareup.com/v2/orders/search",
        headers=headers, data=json.dumps(payload), timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Square error: {data['errors']}")

    rows, total = [], 0.0
    for o in data.get("orders", []):
        if "total_money" not in o:
            continue
        # net_amounts subtracts refunds; fall back to total_money if absent
        if "net_amounts" in o and "total_money" in o.get("net_amounts", {}):
            amt = float(o["net_amounts"]["total_money"]["amount"]) / 100
        else:
            amt = float(o["total_money"]["amount"]) / 100
        total += amt
        rows.append({
            "order_id":  o["id"],
            "platform":  "square",
            "amount":    round(amt, 2),
            "currency":  o["total_money"].get("currency", "USD"),
            "sale_date": day.isoformat(),
        })
    return rows, round(total, 2)


# ── Supabase ──────────────────────────────────────────────────────────────────

_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}


def upsert(records):
    if not records:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/sales?on_conflict=order_id,platform",
        headers=_SB, json=records, timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Supabase {r.status_code}: {r.text}")


def has_any_data() -> bool:
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/sales",
            headers={**_SB, "Range": "0-0"},
            params={"limit": 1},
            timeout=10,
        )
        return r.ok and len(r.json()) > 0
    except Exception:
        return False


# ── Telegram ──────────────────────────────────────────────────────────────────

def telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
        timeout=10,
    )


# ── Core ──────────────────────────────────────────────────────────────────────

def sync_day(day: date) -> dict:
    sh_rows, sh_total = shopify_records(day)
    sq_rows, sq_total = square_records(day)
    upsert(sh_rows + sq_rows)
    return {
        "date":           day.isoformat(),
        "shopify_total":  sh_total,
        "shopify_orders": len(sh_rows),
        "square_total":   sq_total,
        "square_orders":  len(sq_rows),
        "grand_total":    round(sh_total + sq_total, 2),
    }


# ── Run modes ─────────────────────────────────────────────────────────────────

def run_live():
    """
    Sync today AND yesterday in Orlando time.
    Runs every 10 minutes — upsert deduplicates so re-runs are always safe.
    New orders are inserted; changed amounts (refunds) are updated.
    No Telegram message — silent data sync only.
    """
    today     = orlando_today()
    yesterday = today - timedelta(days=1)

    log.info("── Live sync  today=%s  yesterday=%s ──", today, yesterday)
    for day in [yesterday, today]:
        try:
            s = sync_day(day)
            log.info(
                "%s  Shopify=$%.2f (%d orders)  Square=$%.2f (%d orders)  Total=$%.2f",
                day,
                s["shopify_total"], s["shopify_orders"],
                s["square_total"],  s["square_orders"],
                s["grand_total"],
            )
        except Exception as e:
            log.warning("Failed to sync %s: %s", day, e)

    log.info("── Live sync complete ──")


def run_report():
    """
    Sync yesterday (full day, Orlando time) and send the Telegram daily report.
    Runs once a day at 8 AM EDT via cron.
    """
    yesterday = orlando_today() - timedelta(days=1)
    log.info("Daily report sync: %s", yesterday)
    s = sync_day(yesterday)
    log.info(
        "Done. Shopify=$%.2f (%d)  Square=$%.2f (%d)  Total=$%.2f",
        s["shopify_total"], s["shopify_orders"],
        s["square_total"],  s["square_orders"],
        s["grand_total"],
    )
    telegram(
        f"📊 <b>Daily Sales — {yesterday.strftime('%B %d, %Y')}</b>\n\n"
        f"🛍 Shopify: <b>${s['shopify_total']:,.2f}</b> ({s['shopify_orders']} orders)\n"
        f"⬛ Square:  <b>${s['square_total']:,.2f}</b> ({s['square_orders']} orders)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Total: <b>${s['grand_total']:,.2f}</b>"
    )


def run_backfill():
    """Full historical sync from BACKFILL_START to today (Orlando date)."""
    today = orlando_today()
    days  = list(date_range(BACKFILL_START, today))
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
    log.info("Backfill complete. Total synced: $%.2f", grand)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--live",     action="store_true", help="Sync today + yesterday (run every 10 min)")
    grp.add_argument("--report",   action="store_true", help="Sync yesterday + send Telegram (run daily)")
    grp.add_argument("--backfill", action="store_true", help="Full re-backfill from Sept 1 2025")
    args = parser.parse_args()

    if args.live:
        run_live()
    elif args.report:
        run_report()
    elif args.backfill:
        run_backfill()
    else:
        # Auto mode: backfill first if DB is empty, then live sync
        if not has_any_data():
            log.info("DB is empty — running first-time backfill…")
            run_backfill()
        run_live()
