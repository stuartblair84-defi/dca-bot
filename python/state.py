# ─────────────────────────────────────────────
#  Smart DCA Bot — state.py
#  Persist bot state to state.json.
#  state.json is gitignored (contains spend data).
# ─────────────────────────────────────────────

import json
import os
from datetime import datetime, timezone

from config import (
    DAILY_DRIP,
    MONTHLY_BUDGET,
    POOL_CAP_X,
    USE_RESERVE,
    RESERVE_PCT,
    RESERVE_MAX_MONTHS,
)

# state.json lives at the project root (one level above python/)
_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state.json")

_DEFAULT_STATE: dict = {
    "month_spent":  0.0,   # USD spent this calendar month
    "base_pool":    0.0,   # accumulated unspent drip budget (non-reserve)
    "reserve_pool": 0.0,   # cumulative reserve — grows monthly, never resets
    "last_run":     None,  # ISO-8601 UTC string of last execution
    "last_month":   None,  # "YYYY-MM" of last known month
    "paused":       False, # set True via /pause Telegram command
}


def load_state() -> dict:
    """Load state from state.json.  Returns default state if file absent."""
    if os.path.exists(_STATE_FILE):
        with open(_STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Back-fill any keys added to DEFAULT that are missing from the file
        for key, default in _DEFAULT_STATE.items():
            data.setdefault(key, default)
        return data
    return _DEFAULT_STATE.copy()


def save_state(state: dict) -> None:
    """Persist state dict to state.json."""
    path = os.path.abspath(_STATE_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def drip_pool(state: dict) -> dict:
    """Add one day's base drip to base_pool, capped at POOL_CAP_X × DAILY_DRIP.

    Call once per scheduled execution before calc_buy_amount().
    The cap prevents runaway accumulation during skipped or paused days.
    """
    pool_ceiling       = DAILY_DRIP * POOL_CAP_X
    state["base_pool"] = min(state["base_pool"] + DAILY_DRIP, pool_ceiling)
    return state


def handle_month_rollover(state: dict) -> dict:
    """Check for a new calendar month and reset/top-up per-month counters.

    On rollover:
      - base_pool    -> 0.0          (fresh month; daily drips will refill it)
      - month_spent  -> 0.0
      - reserve_pool -> min(reserve_pool + reserve_portion, reserve_ceiling)
                        where reserve_portion = MONTHLY_BUDGET * RESERVE_PCT
                        and   reserve_ceiling  = reserve_portion * RESERVE_MAX_MONTHS
                        Reserve accumulates cumulatively and is NEVER reset to zero.
      - last_month   -> current month string

    Returns the (potentially mutated) state dict.
    """
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    last_month    = state.get("last_month")

    if last_month is not None and last_month != current_month:
        if USE_RESERVE:
            reserve_portion = MONTHLY_BUDGET * RESERVE_PCT
            reserve_ceiling = reserve_portion * RESERVE_MAX_MONTHS
            state["reserve_pool"] = min(
                state.get("reserve_pool", 0.0) + reserve_portion,
                reserve_ceiling,
            )

        state["base_pool"]   = 0.0
        state["month_spent"] = 0.0

    state["last_month"] = current_month
    return state


def record_execution(state: dict, amount_spent: float) -> dict:
    """Update state after an execution (real or dry-run).

    Deduction order: base_pool first, then reserve_pool for any remainder.
    This matches calc_buy_amount() which draws base first, then reserve.
    """
    base_draw    = min(amount_spent, state.get("base_pool", 0.0))
    reserve_draw = max(0.0, amount_spent - base_draw)

    state["base_pool"]    = max(0.0, state["base_pool"] - base_draw)
    state["reserve_pool"] = max(0.0, state.get("reserve_pool", 0.0) - reserve_draw)
    state["month_spent"]  = state["month_spent"] + amount_spent
    state["last_run"]     = datetime.now(timezone.utc).isoformat()
    return state
