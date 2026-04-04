# ─────────────────────────────────────────────
#  Smart DCA Bot — file_logger.py
#
#  Appends every completed buy to two local files on the VPS:
#    ~/dca-bot/purchase_ledger.csv   — one row per buy
#    ~/dca-bot/daily_buy_log.md      — one markdown section per buy
#
#  Both files live one directory above python/ (Path(__file__).parent.parent).
#  All writes are wrapped in try/except — logging failures never kill the bot.
# ─────────────────────────────────────────────

import csv
import logging
from pathlib import Path

log = logging.getLogger("dca-bot")

_BASE_DIR = Path(__file__).parent.parent
_CSV_PATH = _BASE_DIR / "purchase_ledger.csv"
_MD_PATH  = _BASE_DIR / "daily_buy_log.md"

_CSV_COLUMNS = [
    "buy_number", "date", "cycle_time_utc", "usdc_spent", "cbbtc_received",
    "price_usd", "composite_score", "multiplier", "reserve_deployed",
    "swap_tx", "transfer_tx", "transfer_ok", "transfer_error",
]


def _append_csv(buy_record: dict) -> None:
    write_header = not _CSV_PATH.exists()
    with _CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow({col: buy_record.get(col, "") for col in _CSV_COLUMNS})


def _append_md(buy_record: dict) -> None:
    n            = buy_record.get("buy_number", "?")
    date         = buy_record.get("date", "")
    time_utc     = buy_record.get("cycle_time_utc", "")
    usdc_spent   = buy_record.get("usdc_spent", 0.0)
    cbbtc        = buy_record.get("cbbtc_received", 0.0)
    price        = buy_record.get("price_usd", 0.0)
    score        = buy_record.get("composite_score", 0.0)
    multiplier   = buy_record.get("multiplier", 0.0)
    reserve      = buy_record.get("reserve_deployed", 0.0)
    transfer_ok  = buy_record.get("transfer_ok", False)
    swap_tx      = buy_record.get("swap_tx") or "—"
    transfer_tx  = buy_record.get("transfer_tx") or "—"
    transfer_err = buy_record.get("transfer_error")

    transfer_icon = "✅" if transfer_ok else "❌"

    lines = [
        f"## Buy #{n} — {date}",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Date | {date} |",
        f"| Time (UTC) | {time_utc} |",
        f"| USDC Spent | ${usdc_spent:.2f} |",
        f"| cbBTC Received | {cbbtc:.8f} |",
        f"| Price | ${price:,.2f} |",
        f"| Composite Score | {score:.2f} |",
        f"| Multiplier | {multiplier:.1f}x |",
        f"| Reserve Deployed | ${reserve:.2f} |",
        f"| Transfer | {transfer_icon} |",
        f"| Swap Tx | {swap_tx} |",
        f"| Transfer Tx | {transfer_tx} |",
        "",
    ]

    if not transfer_ok and transfer_err:
        lines.append(f"> ⚠️ Transfer failed: {transfer_err}")
        lines.append("")

    lines.append("---")
    lines.append("")

    with _MD_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def log_buy(buy_record: dict) -> None:
    """Append buy_record to purchase_ledger.csv and daily_buy_log.md."""
    try:
        _append_csv(buy_record)
        log.info(f"[file_logger] CSV row written to {_CSV_PATH}")
    except Exception as exc:
        log.warning(f"[file_logger] CSV write failed: {exc}")

    try:
        _append_md(buy_record)
        log.info(f"[file_logger] MD section written to {_MD_PATH}")
    except Exception as exc:
        log.warning(f"[file_logger] MD write failed: {exc}")
