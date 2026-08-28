"""
sheets.py - mirror the SQLite database into a Google Sheet.

Auth uses a **service account**, not OAuth. A headless Pi cannot complete
the OAuth consent flow in a browser, and refresh tokens expire; a service
account key is a file that just works and never prompts.

Setup, once:

  1. console.cloud.google.com -> new project
  2. APIs & Services -> Enable APIs -> enable "Google Sheets API"
  3. Credentials -> Create credentials -> Service account -> create,
     then Keys -> Add key -> JSON. Save it on the Pi as
     /etc/mplabel-sheets.json  (chmod 600)
  4. Open that JSON, copy the "client_email" value
  5. Create the Google Sheet, hit Share, paste that address, give Editor

Step 5 is the one people miss. The service account is a separate identity
with its own inbox-less address; until the sheet is shared with it, every
write returns 403 no matter how correct the key is.

Writes are whole-tab replacements in a single batch call, which keeps us
far under the Sheets quota (60 writes/min/user) no matter how often the
poller runs.
"""

import logging
from datetime import datetime

log = logging.getLogger("mplabel.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# tab name -> (SQL, column headers)
TABS = {
    "Sales": (
        """SELECT received_at, item, buyer, price, ship_by, tracking,
                  ship_to, weight, service, status, printed_at
             FROM sales ORDER BY received_at DESC""",
        ["Sold", "Item", "Buyer", "Price", "Ship by", "Tracking",
         "Ship to", "Weight", "Service", "Status", "Printed"]),

    "Listings": (
        """SELECT listing_id, title, category, price, state, listed_at,
                  sold_at, days_to_sell, days_listed, inquiries,
                  renewed_count, price_band
             FROM v_listing_perf
            ORDER BY COALESCE(sold_at, listed_at) DESC""",
        ["Listing ID", "Title", "Category", "Price", "State", "Listed",
         "Sold", "Days to sell", "Days listed", "Inquiries", "Renewals",
         "Price band"]),

    "By price band": (
        "SELECT price_band, listed, sold, sell_through_pct, avg_days_to_sell, avg_price "
        "FROM v_price_band ORDER BY avg_price",
        ["Price band", "Listed", "Sold", "Sell-through %",
         "Avg days to sell", "Avg price"]),

    "Monthly": (
        "SELECT month, orders, gross, avg_order, avg_days_to_sell FROM v_monthly",
        ["Month", "Orders", "Gross", "Avg order", "Avg days to sell"]),

    "Aging": (
        "SELECT listing_id, title, price, days_listed, inquiries, renewed_count "
        "FROM v_aging",
        ["Listing ID", "Title", "Price", "Days listed", "Inquiries",
         "Renewals"]),
}


def _client(key_path):
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(key_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_sheet(client, sheet_id=None, sheet_name=None):
    import gspread
    if sheet_id:
        return client.open_by_key(sheet_id)
    if sheet_name:
        try:
            return client.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            raise SystemExit(
                f"No sheet named {sheet_name!r} visible to the service "
                f"account. Either the name is wrong, or the sheet has not "
                f"been shared with the service account's client_email "
                f"(Share -> paste address -> Editor).")
    raise SystemExit("set sheet_id or sheet_name in the config")


def _rows(conn, sql):
    try:
        cur = conn.execute(sql)
    except Exception as exc:
        log.warning("query failed (%s) - is the listings table built? "
                    "run `mplabel.py backfill`", exc)
        return []
    out = []
    for row in cur:
        out.append(["" if v is None else v for v in row])
    return out


def sync(conn, key_path, sheet_id=None, sheet_name=None, tabs=None, dry_run=False):
    """Replace each tab's contents with the current database state.

    Returns {tab: row_count}. Whole-tab replacement rather than appending
    means the sheet is always a true mirror - re-running never doubles
    rows, and a corrected record in SQLite corrects the sheet."""
    wanted = tabs or list(TABS)
    payload = {}
    for name in wanted:
        sql, headers = TABS[name]
        payload[name] = [headers] + _rows(conn, sql)

    if dry_run:
        for name, values in payload.items():
            print(f"\n--- {name} ({len(values)-1} rows) ---")
            for row in values[:6]:
                print("   " + " | ".join(str(c)[:22] for c in row))
            if len(values) > 6:
                print(f"   ... {len(values)-6} more")
        return {k: len(v) - 1 for k, v in payload.items()}

    client = _client(key_path)
    book = _open_sheet(client, sheet_id, sheet_name)
    existing = {ws.title: ws for ws in book.worksheets()}
    counts = {}

    for name, values in payload.items():
        n_rows = max(len(values), 2)
        n_cols = max(len(values[0]), 1)
        ws = existing.get(name)
        if ws is None:
            ws = book.add_worksheet(title=name, rows=n_rows + 50, cols=n_cols)
        else:
            ws.clear()
            if ws.row_count < n_rows or ws.col_count < n_cols:
                ws.resize(rows=n_rows + 50, cols=max(n_cols, ws.col_count))
        # One batched call per tab; RAW stops Sheets reinterpreting
        # tracking numbers as huge integers and mangling them.
        ws.update(values=values, range_name="A1", value_input_option="RAW")
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
        counts[name] = len(values) - 1
        log.info("synced %s (%d rows)", name, counts[name])

    # A stamp so it is obvious at a glance whether the Pi is still alive.
    try:
        meta = existing.get("Sales") or book.worksheet("Sales")
        meta.update_acell(
            f"A{len(payload['Sales']) + 2}",
            f"last synced {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    except Exception:
        pass

    return counts
