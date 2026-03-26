# ─────────────────────────────────────────────
#  Smart DCA Bot — signals.py
#  Read-only signal fetching and scoring.
#  score_all() → dict of float scores (0–1).
# ─────────────────────────────────────────────

import requests
from datetime import datetime, timezone

from config import (
    SIGNAL_WEIGHTS, KRAKEN_BTC_SYMBOL, KRAKEN_DAILY,
    DAILY_DRIP, POOL_CAP_MULTIPLIER,
)


# ── Kraken helpers (same pattern as EZManagerSDK/python/utils.py) ────────────

def _fetch_candles(symbol: str = KRAKEN_BTC_SYMBOL,
                   interval: int = KRAKEN_DAILY,
                   limit: int = 250) -> list:
    """Return the last `limit` closed daily candles from Kraken.

    Each candle: [time, open, high, low, close, vwap, volume, count]
    The final element is the still-forming candle and is excluded.
    """
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": symbol, "interval": interval}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken API error: {data['error']}")
    pair_key = list(data["result"].keys())[0]
    candles = data["result"][pair_key]
    # Drop the last (forming) candle, keep the most recent `limit` closed ones
    closed = candles[:-1]
    return closed[-limit:]


def _calculate_rsi(closes: list, period: int = 14) -> float:
    """Wilder-smoothed RSI over `period` from a list of closing prices."""
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes for RSI-{period}")
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [abs(d) if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── Individual signal scorers (all return float 0–1) ─────────────────────────

def score_fear_greed() -> tuple[float, dict]:
    """Fear & Greed index from Alternative.me.

    Extreme fear (low index) → high score (strong buy).
    Score = (100 − index) / 100
    """
    resp = requests.get(
        "https://api.alternative.me/fng/",
        params={"limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()["data"][0]
    index      = int(data["value"])
    label      = data["value_classification"]
    score      = (100 - index) / 100.0
    return round(score, 4), {"index": index, "label": label}


def score_rsi_ma200() -> tuple[float, dict]:
    """RSI-14 daily + MA200 from Kraken XBTUSD.

    RSI score: linearly maps RSI 70→0.0 down to RSI 30→1.0; clamped.
    MA200 modifier: if price < MA200, score × 1.0 (no penalty);
                    if price > MA200, score × 0.7 (less aggressive above trend).
    """
    candles = _fetch_candles(limit=250)
    closes  = [float(c[4]) for c in candles]

    rsi     = _calculate_rsi(closes, period=14)
    ma200   = sum(closes[-200:]) / 200.0
    price   = closes[-1]

    # RSI score: 0 at RSI=70, 1 at RSI=30, linear, clamped
    rsi_score = max(0.0, min(1.0, (70.0 - rsi) / 40.0))

    # MA200 modifier
    above_ma200 = price > ma200
    ma_modifier = 0.7 if above_ma200 else 1.0
    score       = round(rsi_score * ma_modifier, 4)

    return score, {
        "rsi": round(rsi, 2),
        "price": round(price, 2),
        "ma200": round(ma200, 2),
        "above_ma200": above_ma200,
    }


def score_liquidation() -> tuple[float, dict]:
    """Liquidation proxy: volume spike + price drop on the last closed candle.

    Uses the most recent 21 candles: last 20 for average volume baseline,
    candle[-1] as the signal candle.

    Score rises with: larger volume spike AND larger price drop.
    Max score 1.0 when volume ≥ 3× average AND drop ≥ 5%.
    """
    candles  = _fetch_candles(limit=22)   # extra buffer
    volumes  = [float(c[6]) for c in candles]
    closes   = [float(c[4]) for c in candles]
    opens    = [float(c[1]) for c in candles]

    baseline_vol  = sum(volumes[-21:-1]) / 20.0
    last_vol      = volumes[-1]
    last_open     = opens[-1]
    last_close    = closes[-1]

    vol_ratio    = last_vol / baseline_vol if baseline_vol > 0 else 1.0
    price_change = (last_close - last_open) / last_open if last_open > 0 else 0.0

    # Only score a bearish candle (negative return)
    if price_change >= 0:
        return 0.0, {
            "vol_ratio": round(vol_ratio, 2),
            "price_change_pct": round(price_change * 100, 2),
            "signal": "no drop",
        }

    drop_pct  = abs(price_change)           # positive fraction
    vol_score = min(1.0, (vol_ratio - 1.0) / 2.0)   # 0 at 1×, 1 at 3×
    drp_score = min(1.0, drop_pct / 0.05)            # 0 at 0%, 1 at 5%
    score     = round((vol_score + drp_score) / 2.0, 4)

    return score, {
        "vol_ratio": round(vol_ratio, 2),
        "price_change_pct": round(price_change * 100, 2),
        "signal": "liquidation-like" if score > 0.4 else "mild",
    }


# ── Public API ────────────────────────────────────────────────────────────────

def score_all() -> dict:
    """Fetch and score all signals.  Returns {signal_name: float 0–1}."""
    fg_score,  fg_meta  = score_fear_greed()
    rsi_score, rsi_meta = score_rsi_ma200()
    liq_score, liq_meta = score_liquidation()

    return {
        "fear_greed":  fg_score,
        "rsi":         rsi_score,
        "liquidation": liq_score,
        "_meta": {
            "fear_greed":  fg_meta,
            "rsi":         rsi_meta,
            "liquidation": liq_meta,
        },
    }


# ── CLI test run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    # Allow running directly: add parent dir to path so imports work
    sys.path.insert(0, os.path.dirname(__file__))

    from dca_engine import composite_score, get_multiplier, calc_buy_amount

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'=' * 50}")
    print(f"  Smart DCA Bot -- Signal Report")
    print(f"  {now}")
    print(f"{'=' * 50}")

    print("\nFetching signals...\n")

    try:
        fg_score,  fg_meta  = score_fear_greed()
        print(f"  Fear & Greed : {fg_score:.4f}  "
              f"(index={fg_meta['index']}, {fg_meta['label']})")
    except Exception as e:
        print(f"  Fear & Greed : ERROR — {e}")
        fg_score = 0.0

    try:
        rsi_score, rsi_meta = score_rsi_ma200()
        ma_label = "above MA200" if rsi_meta["above_ma200"] else "below MA200"
        print(f"  RSI-14/MA200 : {rsi_score:.4f}  "
              f"(RSI={rsi_meta['rsi']}, price=${rsi_meta['price']:,.0f}, "
              f"MA200=${rsi_meta['ma200']:,.0f}, {ma_label})")
    except Exception as e:
        print(f"  RSI-14/MA200 : ERROR — {e}")
        rsi_score = 0.0

    try:
        liq_score, liq_meta = score_liquidation()
        print(f"  Liquidation  : {liq_score:.4f}  "
              f"(vol {liq_meta['vol_ratio']:.2f}×, "
              f"Δprice {liq_meta['price_change_pct']:+.2f}%, "
              f"{liq_meta['signal']})")
    except Exception as e:
        print(f"  Liquidation  : ERROR — {e}")
        liq_score = 0.0

    scores = {
        "fear_greed":  fg_score,
        "rsi":         rsi_score,
        "liquidation": liq_score,
    }

    comp  = composite_score(scores)
    mult  = get_multiplier(comp)

    # Theoretical state: pool fully loaded to cap
    theoretical_state = {
        "base_pool":   DAILY_DRIP * POOL_CAP_MULTIPLIER,
        "month_spent": 0.0,
    }
    buy_amt = calc_buy_amount(comp, theoretical_state)

    print(f"\n{'-' * 50}")
    print(f"  Composite Score : {comp:.4f}")
    print(f"  Multiplier      : {mult:.1f}x")
    print(f"  {'-' * 44}")
    print(f"  Theoretical Buy : ${buy_amt:.2f}  [DRY RUN -- no tx]")
    print(f"  (Pool fully loaded: ${theoretical_state['base_pool']:.2f})")
    print(f"{'=' * 50}\n")
