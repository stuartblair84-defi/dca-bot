# ─────────────────────────────────────────────
#  Smart DCA Bot — notion_logger.py
#  Logs each completed buy cycle to Notion.
#  Public API: log_buy(buy_record: dict) -> None
# ─────────────────────────────────────────────

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger("dca-bot")


# ── Helpers ───────────────────────────────────

def _tx_url(tx_hash: str | None) -> str | None:
    """Return a Basescan URL for tx_hash, or None if hash is empty/None."""
    if not tx_hash:
        return None
    h = tx_hash.lstrip("0x")
    return f"https://basescan.org/tx/0x{h}"


def _rich_text(content: str) -> list:
    return [{"type": "text", "text": {"content": str(content)}}]


def _table_cell(content: str, url: str | None = None) -> list:
    text: dict = {"content": str(content)}
    if url:
        text["link"] = {"url": url}
    return [{"type": "text", "text": text}]


# ── Destination 1: Purchase Ledger (database row) ─────────────────────────────

def _add_to_purchase_ledger(client, buy: dict) -> None:
    from config import NOTION_PURCHASE_LEDGER_ID

    n            = buy["buy_number"]
    date_str     = buy["date"]
    month_str    = date_str[:7]          # "YYYY-MM"
    swap_url     = _tx_url(buy.get("swap_tx"))
    transfer_url = _tx_url(buy.get("transfer_tx"))

    if buy["transfer_ok"]:
        notes = "Clean ✅"
    else:
        reason = buy.get("transfer_error") or "unknown"
        notes  = f"Swap ✅ Transfer ❌ — {reason}"

    properties = {
        "Name": {
            "title": _rich_text(f"Buy #{n}")
        },
        "Asset": {
            "select": {"name": "cbBTC"}
        },
        "Date": {
            "date": {"start": date_str}
        },
        "Month": {
            "rich_text": _rich_text(month_str)
        },
        "USDC Spent": {
            "number": buy["usdc_spent"]
        },
        "Quantity": {
            "number": buy["cbbtc_received"]
        },
        "Price USD": {
            "number": buy["price_usd"]
        },
        "Cost Basis USD": {
            "number": buy["usdc_spent"]
        },
        "Composite Score": {
            "number": buy["composite_score"]
        },
        "Multiplier": {
            "rich_text": _rich_text(f"{buy['multiplier']:.1f}x")
        },
        "Swap Tx": {
            "url": swap_url or ""
        },
        "Notes": {
            "rich_text": _rich_text(notes)
        },
    }

    if transfer_url:
        properties["Transfer Tx"] = {"url": transfer_url}

    client.pages.create(
        parent={"database_id": NOTION_PURCHASE_LEDGER_ID},
        properties=properties,
    )


# ── Destination 2: Daily Buy Log (child page) ─────────────────────────────────

def _build_summary_table(buy: dict) -> dict:
    swap_url     = _tx_url(buy.get("swap_tx"))
    swap_display = (buy.get("swap_tx") or "")[:10] + "…" if swap_url else "—"

    transfer_url     = _tx_url(buy.get("transfer_tx"))
    transfer_display = (buy.get("transfer_tx") or "")[:10] + "…" if transfer_url else "FAILED"

    header = {
        "type": "table_row",
        "table_row": {
            "cells": [
                _table_cell("Date"),
                _table_cell("Cycle Time UTC"),
                _table_cell("USDC Spent"),
                _table_cell("cbBTC Received"),
                _table_cell("BTC Price USD"),
                _table_cell("Composite Score"),
                _table_cell("Multiplier"),
                _table_cell("Reserve Deployed"),
                _table_cell("Swap Tx"),
                _table_cell("Transfer Tx"),
            ]
        },
    }
    data = {
        "type": "table_row",
        "table_row": {
            "cells": [
                _table_cell(buy["date"]),
                _table_cell(buy["cycle_time_utc"]),
                _table_cell(f"${buy['usdc_spent']:.2f}"),
                _table_cell(f"{buy['cbbtc_received']:.8f}"),
                _table_cell(f"${buy['price_usd']:,.2f}"),
                _table_cell(f"{buy['composite_score']:.4f}"),
                _table_cell(f"{buy['multiplier']:.1f}x"),
                _table_cell(f"${buy['reserve_deployed']:.2f}"),
                _table_cell(swap_display, swap_url),
                _table_cell(transfer_display, transfer_url),
            ]
        },
    }

    return {
        "type": "table",
        "table": {
            "table_width": 10,
            "has_column_header": True,
            "has_row_header": False,
            "children": [header, data],
        },
    }


def _build_signals_table(sig: dict) -> dict:
    fg_raw  = int(sig.get("fg_raw", 0))
    rsi_val = sig.get("rsi", 0.0)

    header = {
        "type": "table_row",
        "table_row": {
            "cells": [
                _table_cell("Signal"),
                _table_cell("Raw"),
                _table_cell("Score"),
            ]
        },
    }

    rows_data = [
        ("Fear & Greed", str(fg_raw),           f"{sig.get('fg_score', 0):.4f}"),
        ("RSI / MA200",  f"RSI {rsi_val:.1f}",  f"{sig.get('ma200_score', 0):.4f}"),
        ("Liquidation",  "—",                   f"{sig.get('liq_score', 0):.4f}"),
        ("Composite",    "—",                   f"{sig.get('composite', 0):.4f}"),
    ]

    rows = [
        {
            "type": "table_row",
            "table_row": {
                "cells": [
                    _table_cell(label),
                    _table_cell(raw),
                    _table_cell(score),
                ]
            },
        }
        for label, raw, score in rows_data
    ]

    return {
        "type": "table",
        "table": {
            "table_width": 3,
            "has_column_header": True,
            "has_row_header": False,
            "children": [header] + rows,
        },
    }


def _add_daily_buy_log(client, buy: dict) -> None:
    from config import NOTION_DAILY_BUY_LOG_ID

    n          = buy["buy_number"]
    date_str   = buy["date"]
    title      = f"Buy #{n} — {date_str}"
    icon_emoji = "✅" if buy["transfer_ok"] else "⚠️"

    children = [
        {
            "type": "heading_2",
            "heading_2": {
                "rich_text":      _rich_text("Summary"),
                "is_toggleable":  False,
            },
        },
        _build_summary_table(buy),
        {
            "type": "heading_2",
            "heading_2": {
                "rich_text":      _rich_text("Signals"),
                "is_toggleable":  False,
            },
        },
        _build_signals_table(buy["signals"]),
    ]

    client.pages.create(
        parent={"page_id": NOTION_DAILY_BUY_LOG_ID},
        icon={"type": "emoji", "emoji": icon_emoji},
        properties={"title": {"title": _rich_text(title)}},
        children=children,
    )


# ── Public API ────────────────────────────────

def log_buy(buy_record: dict) -> None:
    """Log a completed buy cycle to Notion.

    Writes one database row to the Purchase Ledger and one child page
    to the Daily Buy Log.  Silently returns (WARNING logged) if
    NOTION_TOKEN is absent from the environment.

    All other exceptions propagate to the caller, which wraps this
    call in a try/except and logs a WARNING — so Notion failure never
    stops the bot.

    Args:
        buy_record: dict matching the schema defined in run_bot.py.
    """
    token = os.getenv("NOTION_TOKEN")
    if not token:
        log.warning("Notion logging skipped: NOTION_TOKEN not set in .env")
        return

    from notion_client import Client
    client = Client(auth=token)

    _add_to_purchase_ledger(client, buy_record)
    _add_daily_buy_log(client, buy_record)
