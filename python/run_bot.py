#!/usr/bin/env python3
# ─────────────────────────────────────────────
#  Smart DCA Bot — run_bot.py
#
#  Usage:
#    python run_bot.py            # run once and exit  (test / manual trigger)
#    python run_bot.py --daemon   # run scheduler loop (systemd / production)
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

from config import EXECUTION_TIME_UTC, DRY_RUN

import state   as state_mod
import signals as signals_mod
import dca_engine
import base_client
import portfolio


# ── Core execution unit ───────────────────────

def run_once() -> None:
    """Execute one full DCA cycle: signals → engine → (buy) → record → summary.

    Safe to call at any time. All exceptions are caught and logged so the
    scheduler loop never crashes on a single bad run.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info("=" * 54)
    log.info(f"DCA cycle start  [{now}]  DRY_RUN={DRY_RUN}")
    log.info("=" * 54)

    try:
        # 1. State: month rollover check + drip today's budget into pool
        bot_state = state_mod.load_state()
        bot_state = state_mod.handle_month_rollover(bot_state)
        bot_state = state_mod.drip_pool(bot_state)
        log.info(f"Pool: ${bot_state['base_pool']:.2f}  "
                 f"Month spent: ${bot_state['month_spent']:.2f}")

        # 2. Signals
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
        comp       = dca_engine.composite_score(scores)
        multiplier = dca_engine.get_multiplier(comp)
        buy_amount = dca_engine.calc_buy_amount(comp, bot_state)
        buying     = dca_engine.should_buy(comp, bot_state)

        log.info(f"Composite: {comp:.4f}  Multiplier: {multiplier:.1f}x  "
                 f"Buy amount: ${buy_amount:.2f}  Buying: {buying}")

        # 4. Execute (or dry-run)
        if buying:
            log.info(f"{'[DRY RUN] ' if DRY_RUN else ''}Buying ${buy_amount:.2f} of cbBTC ...")

            result = base_client.buy_cbbtc(buy_amount)

            # Derive qty and price from the quote used in buy_cbbtc
            quoted_cbbtc, _ = base_client.get_quote(buy_amount)
            btc_price       = buy_amount / quoted_cbbtc if quoted_cbbtc > 0 else 0.0

            portfolio.record_purchase(
                asset      = "cbBTC",
                qty        = quoted_cbbtc,
                price_usd  = btc_price,
                usdc_spent = buy_amount,
                tx_hash    = result.get("swap_tx", ""),
                signals    = scores_full,
            )
            log.info(f"Recorded: {quoted_cbbtc:.8f} cbBTC @ ${btc_price:,.2f}")

            # Update state
            bot_state = state_mod.record_execution(bot_state, buy_amount)
        else:
            log.info("No buy this cycle (pool empty or budget exhausted).")

        # Save state regardless
        state_mod.save_state(bot_state)

        # 5. Portfolio summary
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
        log.error(f"Cycle error (continuing): {exc}", exc_info=True)

    log.info("Cycle complete.")


# ── Scheduler loop ────────────────────────────

def run_daemon() -> None:
    """Schedule run_once() daily at EXECUTION_TIME_UTC and loop forever."""
    log.info(f"Daemon mode — scheduled daily at {EXECUTION_TIME_UTC} UTC  DRY_RUN={DRY_RUN}")

    schedule.every().day.at(EXECUTION_TIME_UTC).do(run_once)

    log.info("Waiting for next scheduled run ...")
    log.info(f"  Next run: {schedule.next_run()}")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Entry point ───────────────────────────────

if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        # Default: run once and exit (for testing / manual triggers)
        run_once()
