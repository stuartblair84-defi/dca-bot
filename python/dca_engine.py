# ─────────────────────────────────────────────
#  Smart DCA Bot — dca_engine.py
#  Pure logic: scoring, multipliers, amounts.
#  No I/O, no network calls.
# ─────────────────────────────────────────────

from config import (
    SIGNAL_WEIGHTS,
    MULTIPLIER_TIERS,
    POOL_CAP_X,
    DAILY_DRIP,
    MONTHLY_BUDGET,
    USE_RESERVE,
    RESERVE_THRESHOLD,
    NO_BUY_ZONE,
    NO_BUY_THRESHOLD,
)


def composite_score(scores: dict) -> float:
    """Weighted sum of signal scores.

    Args:
        scores: {signal_name: float 0-1}  — _meta key is ignored if present.

    Returns:
        Composite score 0-1, rounded to 4 dp.
    """
    total = 0.0
    for signal, weight in SIGNAL_WEIGHTS.items():
        total += scores.get(signal, 0.0) * weight
    return round(total, 4)


def get_multiplier(score: float) -> float:
    """Map composite score to a spend multiplier via MULTIPLIER_TIERS.

    Tiers are evaluated top-down; first matching threshold wins.
    """
    for threshold, multiplier in MULTIPLIER_TIERS:
        if score >= threshold:
            return multiplier
    return 0.5


def calc_buy_amount(score: float, state: dict) -> float:
    """Calculate how much USD to spend this execution.

    Base logic:
        target     = DAILY_DRIP x multiplier, capped at POOL_CAP_X x DAILY_DRIP
        buy        = min(target, base_pool)
        buy        = min(buy, remaining monthly budget)

    Reserve release (when USE_RESERVE and score >= RESERVE_THRESHOLD):
        shortfall  = target - base_pool contribution  (what base_pool couldn't cover)
        reserve_add = min(shortfall, reserve_pool)
        buy       += reserve_add

    Args:
        score: composite signal score 0-1.
        state: dict with 'base_pool', 'reserve_pool', and 'month_spent' keys.

    Returns:
        Dollar amount to spend, rounded to 2 dp.
    """
    multiplier = get_multiplier(score)
    pool_cap   = DAILY_DRIP * POOL_CAP_X

    # Base pool contribution
    target     = min(DAILY_DRIP * multiplier, pool_cap)
    base_avail = state.get("base_pool", 0.0)
    buy_amount = min(target, base_avail)

    # Reserve pool contribution — only released when score clears threshold
    if USE_RESERVE and score >= RESERVE_THRESHOLD:
        reserve_avail = state.get("reserve_pool", 0.0)
        shortfall     = max(0.0, target - buy_amount)
        buy_amount   += min(shortfall, reserve_avail)

    # Never exceed remaining monthly ceiling
    remaining  = MONTHLY_BUDGET - state.get("month_spent", 0.0)
    buy_amount = min(buy_amount, max(remaining, 0.0))

    return round(buy_amount, 2)


def should_buy(score: float, state: dict) -> bool:
    """Return True if conditions are met for a purchase.

    Checks:
      1. No-buy zone (if NO_BUY_ZONE): skip when score < NO_BUY_THRESHOLD.
      2. Positive buy amount is available after pool/budget checks.

    Args:
        score: composite signal score 0-1.
        state: dict with pool and spend state.
    """
    if NO_BUY_ZONE and score < NO_BUY_THRESHOLD:
        return False

    return calc_buy_amount(score, state) > 0.0
