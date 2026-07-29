#!/usr/bin/env python3
# ─────────────────────────────────────────────
#  Smart DCA Bot — run_bot.py
#
#  Usage:
#    python run_bot.py            # run one cycle, with retry, and exit
#    python run_bot.py --daemon   # run scheduler loop (systemd / production)
#    python run_bot.py --no-retry # single attempt, no re-try (debugging)
# ─────────────────────────────────────────────

import logging
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

# ── Logging setup (before any local imports) ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("dca-bot")

# ── .env (python/.env, same dir as this script) ──
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ── Local imports ──────────────────────────────
import schedule
import time

from config import (
    EXECUTION_TIME_UTC, DRY_RUN, DAILY_DRIP, POOL_CAP_X,
    NO_BUY_THRESHOLD, NO_BUY_ZONE,
    CYCLE_RETRY_ATTEMPTS, CYCLE_RETRY_DELAY_MIN,
)

import state   as state_mod
import signals as signals_mod
import dca_engine
import base_client
import portfolio
import telegram_bot


# ── Cycle stages ──────────────────────────────
# Ordered for reading, not for control flow. run_once() advances `stage` as it
# goes so a failure can be reported against the point it actually reached
# instead of the blanket "no buy was made" the alert used to assert.
STAGES = (
    "state",              # load, month rollover, drip, persist
    "signals",            # F&G + RSI/MA200 + liquidation proxy
    "engine",             # composite score, multiplier, buy amount
    "preflight_balance",  # on-chain hot wallet USDC check — the live buy gate
    "approve",            # USDC allowance to the router
    "swap",               # Uniswap V3 exactInputSingle
    "post_swap_read",     # balanceOf poll for what was actually received
    "transfer",           # sweep cbBTC to the cold wallet
    "record",             # purchases.json, CSV/MD ledgers, state
    "summary",            # portfolio summary + Telegram
)


def _outcome(ok: bool, stage: str, error: str = "") -> dict:
    """Build the cycle outcome dict from the stage plus base_client's tracker.

    base_client is the only thing that knows whether bytes reached the wire, so
    the broadcast fields always come from it rather than being inferred here.

    On failure, its per-buy sub-step wins over the coarse stage when it has one,
    so an alert can say "swap" rather than the "approve" run_once last set. On
    success the caller's stage wins, otherwise a completed cycle would report
    the last phase buy_cbbtc happened to touch rather than where it finished.
    """
    bc = base_client.get_broadcast_state()
    return {
        "ok":        ok,
        "stage":     stage if ok else (bc.get("step") or stage),
        "broadcast": bool(bc.get("attempted")),
        "confirmed": bool(bc.get("confirmed")),
        "tx_hash":   bc.get("tx_hash") or "",
        "error":     error,
    }


# ── Core execution unit ───────────────────────

def run_once(*, alert_on_error: bool = True) -> dict:
    """Execute one full DCA cycle: signals → engine → (buy) → record → summary.

    Safe to call at any time. All exceptions are caught and logged so the
    scheduler loop never crashes on a single bad run.

    Keyword-only `alert_on_error` exists solely so run_with_retry() can suppress
    the Telegram alert on intermediate attempts. Three alerts for one bad
    morning trains you to ignore the alert channel. Manual invocation as
    `run_once()` is unchanged.

    Returns an outcome dict:
        ok         — the cycle completed without an unhandled exception
        stage      — the stage reached (see STAGES); the failure point on error
        broadcast  — whether any transaction hit the wire this attempt
        confirmed  — whether a broadcast transaction was seen confirmed
        tx_hash    — the hash involved, when one is known
        error      — the exception text, empty on success
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info("=" * 54)
    log.info(f"DCA cycle start  [{now}]  DRY_RUN={DRY_RUN}")
    log.info("=" * 54)

    # Broadcast tracking is per-attempt. Reset before anything can set it.
    base_client.reset_broadcast_tracker()
    stage = "state"

    try:
        # 1. State: month rollover check + drip today's budget into pool
        bot_state = state_mod.load_state()
        bot_state = state_mod.handle_month_rollover(bot_state)
        bot_state = state_mod.drip_pool(bot_state)
        log.info(f"Pool: ${bot_state['base_pool']:.2f}  "
                 f"Month spent: ${bot_state['month_spent']:.2f}")
        state_mod.save_state(bot_state)  # persist drip before buy attempt

        # 2. Signals
        stage = "signals"
        log.info("Fetching signals ...")
        scores_full = signals_mod.score_all()
        scores = {k: v for k, v in scores_full.items() if k != "_meta"}
        meta   = scores_full.get("_meta", {})

        log.info(f"  Fear & Greed : {scores['fear_greed']:.4f}  "
                 f"(index={meta.get('fear_greed', {}).get('index', '?')}, "
                 f"{meta.get('fear_greed', {}).get('label', '?')})")
        log.info(f"  RSI / MA200  : {scores['rsi']:.4f}  "
                 f"(RSI={meta.get('rsi', {}).get('rsi', '?')}, "
                 f"{'above' if meta.get('rsi', {}).get('above_ma200') else 'below'} MA200)")
        log.info(f"  Liquidation  : {scores['liquidation']:.4f}  "
                 f"(vol {meta.get('liquidation', {}).get('vol_ratio', '?')}x, "
                 f"dprice {meta.get('liquidation', {}).get('price_change_pct', '?')}%)")

        # 3. Engine
        stage      = "engine"
        comp       = dca_engine.composite_score(scores)
        multiplier = dca_engine.get_multiplier(comp)
        buy_amount = dca_engine.calc_buy_amount(comp, bot_state)
        buying     = dca_engine.should_buy(comp, bot_state)
        paused     = bot_state.get("paused", False)

        log.info(f"Composite: {comp:.4f}  Multiplier: {multiplier:.1f}x  "
                 f"Buy amount: ${buy_amount:.2f}  Buying: {buying}  Paused: {paused}")

        # 4. Execute (or dry-run)
        if buying and not paused:
            # Pre-flight: check actual hot wallet USDC balance before committing.
            # If insufficient, skip entirely (state already saved; drip carries forward).
            stage = "preflight_balance"
            usdc_balance = base_client.get_usdc_balance()
            if usdc_balance < buy_amount:
                log.info(
                    f"No buy: insufficient hot wallet balance "
                    f"(${usdc_balance:.2f} available, ${buy_amount:.2f} needed)."
                )
                telegram_bot.send_low_balance_alert(usdc_balance, buy_amount)
                state_mod.save_state(bot_state)
                log.info("Cycle complete.")
                return _outcome(True, "preflight_balance")

            log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}Buying ${buy_amount:.2f} of cbBTC ...")

            stage     = "approve"          # buy_cbbtc reports its own sub-step
            result    = base_client.buy_cbbtc(buy_amount)
            qty       = result["qty"]
            btc_price = result["price"]

            # Record purchase and update state immediately after the swap,
            # regardless of whether the cold-wallet transfer succeeded.
            stage = "record"
            portfolio.record_purchase(
                asset      = "cbBTC",
                qty        = qty,
                price_usd  = btc_price,
                usdc_spent = buy_amount,
                tx_hash    = result.get("swap_tx", ""),
                signals    = scores_full,
            )
            log.info(f"Recorded: {qty:.8f} cbBTC @ ${btc_price:,.2f}")

            # File logging — fire and forget, never raises
            _pool_cap     = DAILY_DRIP * POOL_CAP_X
            _target       = min(DAILY_DRIP * multiplier, _pool_cap)
            _base_contrib = min(_target, bot_state.get("base_pool", 0.0))
            buy_record = {
                "buy_number":       len(portfolio.load_purchases()),
                "date":             datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "cycle_time_utc":   EXECUTION_TIME_UTC,
                "usdc_spent":       buy_amount,
                "cbbtc_received":   qty,
                "price_usd":        btc_price,
                "composite_score":  comp,
                "multiplier":       multiplier,
                "reserve_deployed": max(0.0, round(buy_amount - _base_contrib, 2)),
                "swap_tx":          result.get("swap_tx", ""),
                "transfer_tx":      result.get("transfer_tx"),
                "transfer_ok":      not bool(result.get("transfer_error")),
                "transfer_error":   result.get("transfer_error"),
                "signals": {
                    "fg_raw":      float(meta.get("fear_greed", {}).get("index", 0)),
                    "fg_score":    scores.get("fear_greed", 0.0),
                    "rsi":         meta.get("rsi", {}).get("rsi", 0.0),
                    "ma200_score": scores.get("rsi", 0.0),
                    "liq_score":   scores.get("liquidation", 0.0),
                    "composite":   comp,
                },
            }
            try:
                from file_logger import log_buy
                log_buy(buy_record)
            except Exception as exc:
                log.warning(f"File logging failed: {exc}")

            bot_state = state_mod.record_execution(bot_state, buy_amount)

            # Alert if transfer to cold wallet failed
            if result.get("transfer_error"):
                log.error(f"Transfer to cold wallet FAILED: {result['transfer_error']}")
                telegram_bot.send_transfer_failed_alert(
                    qty          = qty,
                    swap_tx_hash = result.get("swap_tx", ""),
                    error        = result["transfer_error"],
                )

            # Telegram buy alert
            summary = portfolio.get_summary()
            telegram_bot.send_buy_alert(
                qty            = qty,
                price_usd      = btc_price,
                usdc_spent     = buy_amount,
                comp_score     = comp,
                multiplier     = multiplier,
                tx_hash        = result.get("swap_tx", ""),
                summary        = summary,
                transfer_ok    = not bool(result.get("transfer_error")),
                transfer_error = result.get("transfer_error") or "",
            )
        elif paused:
            log.info("Buy skipped — bot is paused (/resume to re-enable).")
            telegram_bot.send_paused_alert()
        else:
            if NO_BUY_ZONE and comp < NO_BUY_THRESHOLD:
                log.info(
                    f"No buy: composite score {comp:.4f} below NO_BUY_THRESHOLD "
                    f"{NO_BUY_THRESHOLD} (NO_BUY_ZONE enabled)."
                )
                telegram_bot.send_no_buy_alert(comp, NO_BUY_THRESHOLD, scores)
            else:
                pool_bal    = bot_state.get("base_pool", 0.0)
                reserve_bal = bot_state.get("reserve_pool", 0.0)
                log.info(
                    f"No buy: pool empty — base ${pool_bal:.2f}, "
                    f"reserve ${reserve_bal:.2f}, score {comp:.4f}."
                )
                telegram_bot.send_no_buy_alert(
                    comp, NO_BUY_THRESHOLD, scores,
                    title="🟡 DCA Cycle — No Buy",
                )

        # Save state regardless
        state_mod.save_state(bot_state)

        # 5. Portfolio summary
        stage = "summary"
        log.info("Portfolio summary:")
        summary = portfolio.get_summary()
        sign    = "+" if summary["unrealised_pnl"] >= 0 else ""
        log.info(f"  {summary['purchase_count']} purchases | "
                 f"{summary['total_qty']:.8f} cbBTC | "
                 f"invested ${summary['total_invested']:,.2f} | "
                 f"VWAP ${summary['avg_entry_price']:,.2f} | "
                 f"now ${summary['current_price']:,.2f} | "
                 f"P&L {sign}${summary['unrealised_pnl']:,.2f} "
                 f"({sign}{summary['unrealised_pnl_pct']:.2f}%)")

    except Exception as exc:
        outcome = _outcome(False, stage, str(exc))
        log.error(
            f"Cycle error at stage '{outcome['stage']}' "
            f"(broadcast={outcome['broadcast']}, confirmed={outcome['confirmed']}): {exc}",
            exc_info=True,
        )
        if alert_on_error:
            telegram_bot.send_cycle_error_alert(
                error     = str(exc),
                stage     = outcome["stage"],
                broadcast = outcome["broadcast"],
                confirmed = outcome["confirmed"],
                tx_hash   = outcome["tx_hash"],
            )
        log.info("Cycle complete.")
        return outcome

    log.info("Cycle complete.")
    return _outcome(True, "summary")


# ── Cycle-level retry ─────────────────────────

def run_with_retry() -> dict:
    """Run a cycle, re-attempting only when nothing was broadcast.

    The scheduler fires once a day, so before this existed a single 403 cost a
    full 24 hours of DCA. Retrying is safe precisely and only when no bytes
    reached the wire — a retry after a partial buy would buy twice in one day,
    which is a far worse outcome than a missed day.

    Only the final failed attempt alerts. Intermediate attempts log at WARNING.

    The pool cannot be double-dripped across attempts: state.drip_pool() is
    idempotent per UTC day (see its `last_drip_date` guard), so attempts two and
    three re-read the same pool the first attempt persisted.
    """
    delay_sec = CYCLE_RETRY_DELAY_MIN * 60
    outcome   = _outcome(False, "state", "no attempt ran")

    for attempt in range(1, CYCLE_RETRY_ATTEMPTS + 1):
        is_last = attempt == CYCLE_RETRY_ATTEMPTS
        if attempt > 1:
            log.info(f"Cycle attempt {attempt}/{CYCLE_RETRY_ATTEMPTS}")

        # Suppress the alert until the last attempt, so one bad morning
        # produces one message rather than three.
        outcome = run_once(alert_on_error=is_last)

        if outcome["ok"]:
            return outcome

        if outcome["broadcast"]:
            # Something is on chain, or may be. Stop unconditionally, whatever
            # attempts remain, and make sure the operator hears about it even
            # though this was not the final attempt.
            log.error(
                f"Cycle failed at '{outcome['stage']}' AFTER a broadcast "
                f"(tx {outcome['tx_hash'] or 'unknown'}) — not retrying."
            )
            if not is_last:
                telegram_bot.send_cycle_error_alert(
                    error     = outcome["error"],
                    stage     = outcome["stage"],
                    broadcast = True,
                    confirmed = outcome["confirmed"],
                    tx_hash   = outcome["tx_hash"],
                )
            return outcome

        if not is_last:
            log.warning(
                f"Cycle attempt {attempt}/{CYCLE_RETRY_ATTEMPTS} failed at "
                f"'{outcome['stage']}' with nothing broadcast — retrying in "
                f"{CYCLE_RETRY_DELAY_MIN} min. ({outcome['error']})"
            )
            time.sleep(delay_sec)

    log.error(f"Cycle failed after {CYCLE_RETRY_ATTEMPTS} attempts. Alert sent.")
    return outcome


# ── Scheduler loop ────────────────────────────

def run_daemon() -> None:
    """Schedule run_with_retry() daily at EXECUTION_TIME_UTC and loop forever."""
    log.info(f"Daemon mode -- scheduled daily at {EXECUTION_TIME_UTC} UTC  DRY_RUN={DRY_RUN}")

    def _reschedule(new_time: str) -> None:
        schedule.clear()
        schedule.every().day.at(new_time).do(run_with_retry)
        log.info(f"Job rescheduled to {new_time} UTC")
        log.info(f"  Next run: {schedule.next_run()}")

    telegram_bot.start_background_bot()
    telegram_bot.register_reschedule_fn(_reschedule)
    schedule.every().day.at(EXECUTION_TIME_UTC).do(run_with_retry)

    log.info("Waiting for next scheduled run ...")
    log.info(f"  Next run: {schedule.next_run()}")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Entry point ───────────────────────────────

if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    elif "--no-retry" in sys.argv:
        # Single attempt, no re-try, no waiting. For interactive debugging.
        run_once()
    else:
        # Default: run once and exit (for testing / manual triggers).
        # Uses the retry wrapper so a manual trigger has the same resilience
        # as the scheduled run.
        run_with_retry()
