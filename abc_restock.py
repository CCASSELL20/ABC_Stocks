#!/usr/bin/env python3
"""
Virginia ABC restock watcher.

Watches given products at specific ABC stores and sends a text (via
email-to-SMS gateway) when a store's on-hand quantity INCREASES versus the
last check (i.e. a restock).

Confirmed API (from the live site):
  https://www.abc.virginia.gov/webapi/inventory/mystore
      ?storeNumbers=327,...&productCodes=017766,...

Confirmed response shape:
  {
    "products": [
      {
        "productId": "017766",
        "storeInfo":   { "storeId": 327, "quantity": 0, "city": "Blacksburg", ... },
        "nearbyStores": [ { "storeId": 269, "quantity": 1, "city": "Roanoke", ... }, ... ]
      }
    ]
  }

Eagle Rare product code = 017766. Store 327 = Blacksburg (S. Main St).

Runs headless on GitHub Actions; state persists in state.json which the
workflow commits back each run.

Config via environment variables (GitHub repo Secrets):
  WATCH_PRODUCTS  "Name=Code" pairs, e.g. "Eagle Rare=017766,Blanton's=000378"
  STORE_NUMBERS   Comma-separated store numbers, e.g. "327,414,67,195,356"
  SMS_TO          SMS gateway address, e.g. "5401234567@tmomail.net"
  SMTP_USER       Gmail address to send FROM
  SMTP_PASS       Gmail app password (16 chars, no spaces)
  SMTP_HOST       default smtp.gmail.com
  SMTP_PORT       default 587
  ONLY_WATCHED    if "1" (default), ignore the API's extra "nearbyStores" and
                  only track the store numbers you listed. Set "0" to also
                  track whatever nearby stores the API volunteers.
  DEBUG_JSON      if "1", print raw API response (use on first run)
"""

import os
import json
import smtplib
from email.message import EmailMessage

import requests

STATE_FILE = "state.json"
MYSTORE_URL = "https://www.abc.virginia.gov/webapi/inventory/mystore"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.abc.virginia.gov/products/all-products",
    "X-Requested-With": "XMLHttpRequest",
}

DEBUG = os.environ.get("DEBUG_JSON") == "1"
ONLY_WATCHED = os.environ.get("ONLY_WATCHED", "1") != "0"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _make_session():
    """Create a session and prime it by visiting a normal page first, so we
    pick up any cookies the API endpoints expect. The browser had a session;
    a bare server-side request does not, which can cause 400/403."""
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.abc.virginia.gov/products/all-products",
              timeout=30)
    except requests.RequestException as e:
        print(f"[warn] priming request failed (continuing anyway): {e}")
    return s


SESSION = None


def _get(url, params):
    global SESSION
    if SESSION is None:
        SESSION = _make_session()
    r = SESSION.get(url, params=params, timeout=30)
    if r.status_code >= 400:
        # Servers usually explain a 400 in the body — surface it.
        print(f"[http {r.status_code}] {r.url}")
        print(f"[http body] {r.text[:1000]}")
    r.raise_for_status()
    return r.json()


def _store_row(product_code, store_obj):
    """Turn one storeInfo/nearbyStore object into our normalized row."""
    return {
        "product_code": str(product_code),
        "store_number": str(store_obj.get("storeId", "?")),
        "city": store_obj.get("city") or "",
        "address": store_obj.get("address") or "",
        "qty": int(store_obj.get("quantity", 0)),
    }


def _parse_products_response(data):
    """Pull normalized rows out of one mystore response."""
    rows = []
    products = data.get("products", []) if isinstance(data, dict) else []
    for p in products:
        code = p.get("productId")
        info = p.get("storeInfo")
        if isinstance(info, dict):
            rows.append(_store_row(code, info))
        for nb in p.get("nearbyStores", []) or []:
            if isinstance(nb, dict):
                rows.append(_store_row(code, nb))
    return rows


def fetch_inventory(store_numbers, product_codes):
    """Query mystore once PER STORE so a single invalid store number doesn't
    400 the whole batch. Returns normalized rows (one per store per product).

    Note: the API also volunteers nearbyStores for each queried store; those
    get filtered out later unless ONLY_WATCHED is disabled."""
    all_rows = []
    seen = set()  # dedupe (code, store) since nearby lists overlap
    codes_param = ",".join(product_codes)

    for store in store_numbers:
        params = {"storeNumbers": store, "productCodes": codes_param}
        try:
            data = _get(MYSTORE_URL, params)
        except requests.HTTPError as e:
            print(f"[skip] store {store}: rejected by API ({e})")
            continue
        except requests.RequestException as e:
            print(f"[skip] store {store}: request failed ({e})")
            continue

        if DEBUG:
            print(f"[debug] store {store} raw response:")
            print(json.dumps(data, indent=2)[:1500])

        for row in _parse_products_response(data):
            key = (row["product_code"], row["store_number"])
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(row)

    if not all_rows:
        print("[warn] no rows parsed from any store.")
    return all_rows


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------
def send_text(body):
    to = os.environ.get("SMS_TO")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not all([to, user, pw]):
        print(f"[warn] SMS not configured; would have sent:\n  {body}")
        return
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = "ABC Restock"
    msg.set_content(body)
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    print(f"[sent]\n  {body}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def parse_products():
    raw = os.environ.get("WATCH_PRODUCTS", "Eagle Rare=017766")
    out = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, code = chunk.rsplit("=", 1)
            out[code.strip()] = name.strip()
        else:
            out[chunk] = chunk
    return out


def parse_stores():
    return [s.strip() for s in
            os.environ.get("STORE_NUMBERS", "327").split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # TEST_SMS=1 sends a single test text and exits — use this once to verify
    # your email-to-SMS setup works, then unset it.
    if os.environ.get("TEST_SMS") == "1":
        print("[test] TEST_SMS=1 -> sending a test message")
        send_text("ABC watcher test: texting works. Reply not needed.")
        print("[test] done — unset TEST_SMS to resume normal watching.")
        return

    products = parse_products()            # code -> name
    watched = parse_stores()               # list of store-number strings
    watched_set = set(watched)

    print(f"[config] DEBUG_JSON={DEBUG}  ONLY_WATCHED={ONLY_WATCHED}")
    print(f"[config] products (code->name): {products}")
    print(f"[config] store numbers: {watched}")
    for label, val in [("SMS_TO", os.environ.get("SMS_TO")),
                       ("SMTP_USER", os.environ.get("SMTP_USER")),
                       ("SMTP_PASS", os.environ.get("SMTP_PASS"))]:
        print(f"[config] {label}: {'set' if val else '<MISSING>'}")

    state = load_state()
    state.setdefault("qty", {})            # "code|storeNumber" -> last qty
    alerts = []

    try:
        rows = fetch_inventory(watched, list(products.keys()))
    except Exception as e:
        print(f"[error] inventory fetch failed: {type(e).__name__}: {e}")
        save_state(state)
        return

    print(f"[info] parsed {len(rows)} rows from API")

    for row in rows:
        code = row["product_code"]
        store_no = row["store_number"]
        if ONLY_WATCHED and store_no not in watched_set:
            continue
        qty = row["qty"]
        pname = products.get(code, code)
        where = row["city"] or store_no
        key = f"{code}|{store_no}"
        prev = state["qty"].get(key)
        state["qty"][key] = qty

        if prev is None:
            print(f"  [baseline] {pname} @ {where} (store {store_no}): {qty}")
        elif qty > prev:
            msg = f"RESTOCK: {pname} @ {where} (store {store_no}) {prev}->{qty}"
            print(f"  [ALERT] {msg}")
            alerts.append(msg)
        else:
            print(f"  [ok] {pname} @ {where} (store {store_no}): {qty} (was {prev})")

    if alerts:
        send_text("\n".join(alerts))

    save_state(state)
    print("done.")


if __name__ == "__main__":
    main()
