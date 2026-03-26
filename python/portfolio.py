# ─────────────────────────────────────────────
#  Smart DCA Bot — portfolio.py
#  Tracks cbBTC purchases and reports P&L.
#  purchases.json is gitignored (project root).
# ─────────────────────────────────────────────

import json
import os
from datetime import datetime, timezone

import requests

from config import KRAKEN_BTC_SYMBOL

_PURCHASES_FILE = os.path.join(os.path.dirname(__file__), "..", "purchases.json")


# ── Persistence ───────────────────────────────

def load_purchases() -> list:
    """Load purchase history from purchases.json. Returns [] if absent."""
    if os.path.exists(_PURCHASES_FILE):
        with open(_PURCHASES_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return []


def save_purchases(purchases: list) -> None:
    """Write purchase list to purchases.json."""
    path = os.path.abspath(_PURCHASES_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(purchases, fh, indent=2)


# ── Recording ─────────────────────────────────

def record_purchase(
    asset: str,
    qty: float,
    price_usd: float,
    usdc_spent: float,
    tx_hash: str,
    signals: dict,
) -> dict:
    """Append one purchase record to purchases.json and return the record.

    Args:
        asset:      Token symbol, e.g. "cbBTC".
        qty:        Amount of asset received.
        price_usd:  Execution price in USD per unit.
        usdc_spent: USDC actually spent (may differ slightly from qty*price).
        tx_hash:    Swap tx hash (or dry-run placeholder).
        signals:    Dict of signal scores at time of purchase.

    Returns the newly appended record dict.
    """
    purchases = load_purchases()

    record = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "asset":      asset,
        "qty":        qty,
        "price_usd":  price_usd,
        "usdc_spent": usdc_spent,
        "tx_hash":    tx_hash,
        "signals":    {k: v for k, v in signals.items() if k != "_meta"},
    }

    purchases.append(record)
    save_purchases(purchases)
    return record


# ── Live price ────────────────────────────────

def _fetch_live_btc_price() -> float:
    """Fetch latest BTC/USD trade price from Kraken public Ticker API."""
    url    = "https://api.kraken.com/0/public/Ticker"
    params = {"pair": KRAKEN_BTC_SYMBOL}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data   = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken Ticker error: {data['error']}")
    pair_key    = list(data["result"].keys())[0]
    last_price  = float(data["result"][pair_key]["c"][0])  # "c" = last trade closed
    return last_price


# ── Summary ───────────────────────────────────

def get_summary(live_price: float | None = None) -> dict:
    """Return portfolio summary for cbBTC holdings.

    Calculates:
      - total_qty        : sum of all cbBTC purchased
      - total_invested   : sum of USDC spent
      - avg_entry_price  : VWAP = sum(price * qty) / sum(qty)
      - current_price    : live BTC price (fetched or supplied)
      - current_value    : total_qty * current_price
      - unrealised_pnl   : current_value - total_invested  (USD)
      - unrealised_pnl_pct

    Args:
        live_price: supply to skip the Kraken fetch (useful in tests / dry-runs).

    Returns a dict; all monetary values are float USD.
    """
    purchases = load_purchases()
    cbbtc     = [p for p in purchases if p.get("asset") == "cbBTC"]

    total_qty      = sum(p["qty"]        for p in cbbtc)
    total_invested = sum(p["usdc_spent"] for p in cbbtc)

    if total_qty > 0:
        vwap = sum(p["price_usd"] * p["qty"] for p in cbbtc) / total_qty
    else:
        vwap = 0.0

    if live_price is None:
        try:
            live_price = _fetch_live_btc_price()
        except Exception:
            live_price = vwap  # fall back to entry price if fetch fails

    current_value       = total_qty * live_price
    unrealised_pnl      = current_value - total_invested
    unrealised_pnl_pct  = (unrealised_pnl / total_invested * 100) if total_invested > 0 else 0.0

    return {
        "purchase_count":     len(cbbtc),
        "total_qty":          total_qty,
        "total_invested":     round(total_invested, 2),
        "avg_entry_price":    round(vwap, 2),
        "current_price":      round(live_price, 2),
        "current_value":      round(current_value, 2),
        "unrealised_pnl":     round(unrealised_pnl, 2),
        "unrealised_pnl_pct": round(unrealised_pnl_pct, 4),
    }


def print_summary() -> None:
    """Fetch and print a formatted portfolio summary."""
    s = get_summary()
    sign = "+" if s["unrealised_pnl"] >= 0 else ""
    print(f"\n  Portfolio  cbBTC")
    print(f"  Purchases     : {s['purchase_count']}")
    print(f"  Total qty     : {s['total_qty']:.8f} cbBTC")
    print(f"  Total invested: ${s['total_invested']:,.2f}")
    print(f"  Avg entry     : ${s['avg_entry_price']:,.2f}")
    print(f"  Current price : ${s['current_price']:,.2f}")
    print(f"  Current value : ${s['current_value']:,.2f}")
    print(f"  Unrealised P&L: {sign}${s['unrealised_pnl']:,.2f}  "
          f"({sign}{s['unrealised_pnl_pct']:.2f}%)")
