# ─────────────────────────────────────────────
#  Smart DCA Bot — dca_engine.py
#  Pure logic: scoring, multipliers, amounts.
#  No I/O, no network calls.
# ─────────────────────────────────────────────

from config import (
    SIGNAL_WEIGHTS,
    MULTIPLIER_TIERS,
    POOL_CAP_MULTIPLIER,
    DAILY_DRIP,
    MONTHLY_BUDGET,
    NO_BUY_ZONE_ENABLED,
)


def composite_score(scores: dict) -> float:
    """Weighted sum of signal scores.

    Args:
        scores: {signal_name: float 0–1}  — _meta key is ignored if present.

    Returns:
        Composite score 0–1, rounded to 4 dp.
    """
    total = 0.0
    for signal, weight in SIGNAL_WEIGHTS.items():
        total += scores.get(signal, 0.0) * weight
    return round(total, 4)


def get_multiplier(score: float) -> float:
    """Map composite score to a spend multiplier via MULTIPLIER_TIERS.

    Tiers are evaluated top-down; first matching threshold wins.
    Returns the multiplier for the highest tier the score qualifies for.
    """
    for threshold, multiplier in MULTIPLIER_TIERS:
        if score >= threshold:
            return multiplier
    return 0.5   # fallback (should never be reached given tier floor at 0.00)


def calc_buy_amount(score: float, state: dict) -> float:
    """Calculate how much USD to spend this execution.

    Logic:
        target   = DAILY_DRIP × multiplier
        target   = min(target, pool_cap)       ← single-shot cap
        buy      = min(target, base_pool)       ← limited by available pool
        buy      = min(buy, remaining_budget)   ← never blow monthly ceiling

    Args:
        score: composite signal score 0–1.
        state: dict with 'base_pool' and 'month_spent' keys.

    Returns:
        Dollar amount to spend, rounded to 2 dp.
    """
    multiplier  = get_multiplier(score)
    pool_cap    = DAILY_DRIP * POOL_CAP_MULTIPLIER

    target      = min(DAILY_DRIP * multiplier, pool_cap)
    available   = state.get("base_pool", 0.0)
    buy_amount  = min(target, available)

    remaining   = MONTHLY_BUDGET - state.get("month_spent", 0.0)
    buy_amount  = min(buy_amount, max(remaining, 0.0))

    return round(buy_amount, 2)


def should_buy(score: float, state: dict) -> bool:
    """Return True if conditions are met for a purchase.

    Checks:
      1. No-buy zone (if enabled): skips when score is very low.
      2. Positive buy amount available.

    Args:
        score: composite signal score 0–1.
        state: dict with pool and spend state.
    """
    if NO_BUY_ZONE_ENABLED:
        # No-buy zone: don't buy when score is below a weak threshold
        NO_BUY_THRESHOLD = 0.10
        if score < NO_BUY_THRESHOLD:
            return False

    return calc_buy_amount(score, state) > 0.0
