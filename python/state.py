# ─────────────────────────────────────────────
#  Smart DCA Bot — state.py
#  Persist bot state to state.json.
#  state.json is gitignored (contains spend data).
# ─────────────────────────────────────────────

import json
import os
from datetime import datetime, timezone

from config import DAILY_DRIP, RESERVE_ENABLED

# state.json lives at the project root (one level above python/)
_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state.json")

_DEFAULT_STATE: dict = {
    "month_spent":  0.0,   # USD spent this calendar month
    "base_pool":    0.0,   # accumulated unspent drip budget
    "reserve_pool": 0.0,   # cumulative reserve (never resets)
    "last_run":     None,  # ISO-8601 UTC string of last execution
    "last_month":   None,  # "YYYY-MM" of last known month
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
    """Add one day's drip to base_pool, capped at POOL_CAP_MULTIPLIER × DAILY_DRIP.

    Call once per scheduled execution before calc_buy_amount().
    """
    from config import POOL_CAP_MULTIPLIER
    pool_ceiling = DAILY_DRIP * POOL_CAP_MULTIPLIER
    state["base_pool"] = min(state["base_pool"] + DAILY_DRIP, pool_ceiling)
    return state


def handle_month_rollover(state: dict) -> dict:
    """Check for a new calendar month and reset per-month counters.

    On rollover:
      - month_spent  → 0.0
      - base_pool    → 0.0  (fresh month; drips will refill it)
      - reserve_pool → topped up by the unspent base_pool remainder
                       (cumulative; never resets), only when RESERVE_ENABLED.
      - last_month   → current month string

    Returns the (potentially mutated) state dict.
    """
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    last_month    = state.get("last_month")

    if last_month is not None and last_month != current_month:
        # Optional: carry unspent pool into reserve
        if RESERVE_ENABLED:
            state["reserve_pool"] += state.get("base_pool", 0.0)

        state["base_pool"]   = 0.0
        state["month_spent"] = 0.0

    state["last_month"] = current_month
    return state


def record_execution(state: dict, amount_spent: float) -> dict:
    """Update state after an execution (real or dry-run).

    Deducts amount_spent from base_pool, adds to month_spent,
    and stamps last_run.
    """
    state["base_pool"]   = max(0.0, state["base_pool"] - amount_spent)
    state["month_spent"] = state["month_spent"] + amount_spent
    state["last_run"]    = datetime.now(timezone.utc).isoformat()
    return state
