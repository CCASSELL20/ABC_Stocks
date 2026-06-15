#!/usr/bin/env python3
"""
Virginia ABC restock watcher.

Watches given products at the Blacksburg and Christiansburg ABC stores and
sends a text (via email-to-SMS gateway) when a store's on-hand quantity
INCREASES versus the last time it was checked (i.e. a restock).

Designed to run headless on GitHub Actions on a cron schedule. State (last
seen quantities) persists in state.json, which the workflow commits back to
the repo after each run.

Config via environment variables (set as GitHub repo Secrets):
  WATCH_PRODUCTS   Comma-separated product names, e.g. "Eagle Rare,Blanton's"
  WATCH_STORES     Comma-separated city names to match store addresses against.
                   Default: "Blacksburg,Christiansburg"
  SMS_TO           Your SMS gateway address, e.g. "5551234567@tmomail.net"
  SMTP_USER        Gmail address you send FROM
  SMTP_PASS        Gmail app password (NOT your normal password)
  SMTP_HOST        Default smtp.gmail.com
  SMTP_PORT        Default 587

Nothing here is ABC-specific-secret; product codes and store numbers are
discovered at runtime by searching the public site.
"""

import os
import sys
import json
import time
import smtplib
from email.message import EmailMessage

import requests

STATE_FILE = "state.json"

# The Virginia ABC store/product data is served by a backend JSON API that the
# website's front-end calls. These endpoints are public (no auth) and are what
# the site itself hits. If ABC changes their site, update these two constants.
BASE = "https://www.abc.virginia.gov"
# Product search: returns products matching a text query, including the
# product code/number used for inventory lookups.
PRODUCT_SEARCH_URL = BASE + "/webapi/api/commerce/products/search"
# Store inventory: given a product code, returns per-store on-hand quantities
# for the nearest stores (or all stores, depending on params).
STORE_INVENTORY_URL = BASE + "/webapi/api/commerce/products/{code}/inventory"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/products/all-products",
}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# ABC API access
#
# NOTE ON ROBUSTNESS: ABC has changed their API shape over time and the JSON
# field names are not guaranteed stable. Rather than hard-code one schema, the
# helpers below search the returned JSON flexibly for the fields we need. If a
# call ever returns an unexpected shape, the script logs the raw payload so you
# can adjust the field lookups quickly.
# ---------------------------------------------------------------------------
def _get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _find_first(obj, keys):
    """Depth-first search for the first value whose key matches any in `keys`
    (case-insensitive). Returns None if not found."""
    keys_l = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys_l and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = _find_first(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first(item, keys)
            if found is not None:
                return found
    return None


def _iter_records(obj):
    """Yield dicts from a response that look like product/store records
    (i.e. dicts that contain identifying fields)."""
    if isinstance(obj, dict):
        # Common envelope keys
        for key in ("products", "items", "results", "data", "inventory",
                    "stores", "value"):
            if key in obj and isinstance(obj[key], list):
                for item in obj[key]:
                    yield from _iter_records(item)
                return
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_records(item)


def search_product_code(name):
    """Resolve a product name to its ABC product code. Returns (code, label)
    for the best match, or (None, None)."""
    data = _get(PRODUCT_SEARCH_URL, params={"q": name, "page": 1, "size": 20})
    best = None
    for rec in _iter_records(data):
        code = _find_first(rec, ["code", "productCode", "productNumber",
                                 "itemNumber", "no", "id"])
        label = _find_first(rec, ["name", "productName", "description",
                                  "title"])
        if code is None or label is None:
            continue
        label_l = str(label).lower()
        # Prefer an exact-ish name match; fall back to first result.
        if name.lower() in label_l:
            return str(code), str(label)
        if best is None:
            best = (str(code), str(label))
    if best:
        return best
    print(f"  [warn] no product match for {name!r}. Raw response head:")
    print("  " + json.dumps(data)[:800])
    return None, None


def fetch_inventory(code):
    """Return list of (store_label, quantity) for a product code."""
    out = []
    data = _get(STORE_INVENTORY_URL.format(code=code))
    for rec in _iter_records(data):
        qty = _find_first(rec, ["quantity", "onHand", "qty", "available",
                                "stock", "inventory"])
        store = _find_first(rec, ["storeName", "store", "address1", "address",
                                  "city", "location", "storeNumber"])
        if qty is None:
            continue
        try:
            qty = int(float(qty))
        except (TypeError, ValueError):
            continue
        out.append((str(store), qty))
    if not out:
        print(f"  [warn] no inventory rows for code {code}. Raw head:")
        print("  " + json.dumps(data)[:800])
    return out


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
def send_text(subject, body):
    to = os.environ.get("SMS_TO")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not all([to, user, pw]):
        print("  [warn] SMS not configured (SMS_TO/SMTP_USER/SMTP_PASS); "
              "would have sent:")
        print(f"  {subject} :: {body}")
        return
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    print(f"  [sent] {body}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # --- config diagnostics ---------------------------------------------
    # Print whether each config value arrived. Secrets are masked by GitHub
    # in logs, so showing presence/length is safe and tells us if a secret
    # is missing or misnamed.
    def _diag(label, val, secret=False):
        if not val:
            print(f"[config] {label}: <MISSING/EMPTY>")
        elif secret:
            print(f"[config] {label}: set (length {len(val)})")
        else:
            print(f"[config] {label}: {val!r}")

    _diag("WATCH_PRODUCTS", os.environ.get("WATCH_PRODUCTS"))
    _diag("WATCH_STORES", os.environ.get("WATCH_STORES"))
    _diag("SMS_TO", os.environ.get("SMS_TO"), secret=True)
    _diag("SMTP_USER", os.environ.get("SMTP_USER"), secret=True)
    _diag("SMTP_PASS", os.environ.get("SMTP_PASS"), secret=True)
    # --------------------------------------------------------------------

    products = [p.strip() for p in
                os.environ.get("WATCH_PRODUCTS", "Eagle Rare").split(",")
                if p.strip()]
    store_filters = [s.strip().lower() for s in
                     os.environ.get("WATCH_STORES",
                                    "Blacksburg,Christiansburg").split(",")
                     if s.strip()]

    print(f"[config] resolved products to watch: {products}")
    print(f"[config] resolved store filters: {store_filters}")

    state = load_state()
    state.setdefault("codes", {})       # product name -> code
    state.setdefault("qty", {})         # "code|store" -> last qty
    alerts = []

    for name in products:
        print(f"[checking] {name}")
        code = state["codes"].get(name)
        if not code:
            try:
                code, label = search_product_code(name)
            except Exception as e:
                print(f"[error] product search failed for {name}: "
                      f"{type(e).__name__}: {e}")
                continue
            if not code:
                print(f"[skip] could not resolve product: {name}")
                continue
            state["codes"][name] = code
            print(f"[resolved] {name} -> code {code} ({label})")

        try:
            rows = fetch_inventory(code)
        except Exception as e:
            print(f"[error] inventory fetch failed for {name}: "
                  f"{type(e).__name__}: {e}")
            continue

        print(f"[info] {name}: got {len(rows)} store rows before filtering")

        for store, qty in rows:
            # Only stores matching our city filters
            if store_filters and not any(f in store.lower()
                                         for f in store_filters):
                continue
            key = f"{code}|{store}"
            prev = state["qty"].get(key)
            state["qty"][key] = qty
            if prev is None:
                print(f"  [baseline] {name} @ {store}: {qty}")
                continue
            if qty > prev:
                msg = f"RESTOCK: {name} @ {store} {prev}->{qty}"
                print(f"  [ALERT] {msg}")
                alerts.append(msg)
            else:
                print(f"  [ok] {name} @ {store}: {qty} (was {prev})")
        time.sleep(1)  # be polite to the server

    if alerts:
        body = "\n".join(alerts)
        send_text("ABC Restock", body)

    save_state(state)
    print("done.")


if __name__ == "__main__":
    main()
