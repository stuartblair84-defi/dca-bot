#!/usr/bin/env python3
# ─────────────────────────────────────────────
#  Smart DCA Bot — telegram_bot.py
#
#  Background long-polling thread + inline keyboard menu.
#  Only responds to TELEGRAM_CHAT_ID from .env.
#  No external telegram library — raw Bot API via requests.
# ─────────────────────────────────────────────

import calendar
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger("dca-bot.telegram")


# ── Bot class ─────────────────────────────────

class TelegramBot:
    def __init__(self) -> None:
        self.token   = os.getenv("TELEGRAM_TOKEN", "")
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID", ""))
        self._offset  = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock    = threading.Lock()
        # Pending pause confirm — set when /pause is issued
        self._pending_pause: dict | None = None  # {'expires_at': float}

    # ── Telegram API ──────────────────────────

    def _api(self, method: str, **params) -> dict:
        url  = f"https://api.telegram.org/bot{self.token}/{method}"
        resp = requests.post(url, json=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def send(self, text: str, reply_markup: dict | None = None) -> dict:
        """Send HTML message to the configured chat. Silently swallows errors."""
        params: dict = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            return self._api("sendMessage", **params)
        except Exception as exc:
            log.warning(f"Telegram send failed: {exc}")
            return {}

    # ── Lifecycle ─────────────────────────────

    def start(self) -> None:
        """Launch background polling thread. Safe to call multiple times."""
        if not self.token or not self.chat_id:
            log.warning("Telegram token/chat_id not configured — bot disabled")
            return
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, daemon=True, name="telegram-poll"
        )
        self._thread.start()
        log.info("Telegram bot polling thread started")

    def stop(self) -> None:
        self._running = False

    # ── Polling loop ──────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                data = self._api("getUpdates", offset=self._offset, timeout=20)
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    self._dispatch(update)
            except Exception as exc:
                log.debug(f"Telegram poll error (retry in 5s): {exc}")
                time.sleep(5)

    def _dispatch(self, update: dict) -> None:
        """Route an update to the correct handler, enforcing chat_id guard."""
        msg = update.get("message")
        cb  = update.get("callback_query")

        if msg:
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                return
            self._handle_message(msg)

        elif cb:
            if str(cb.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                self._api("answerCallbackQuery", callback_query_id=cb["id"])
                return
            self._handle_callback(cb)

    # ── Message routing ───────────────────────

    def _handle_message(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        if not text.startswith("/"):
            return
        cmd = text.split()[0].lower().split("@")[0]
        _ROUTES = {
            "/start":   self._cmd_menu,
            "/menu":    self._cmd_menu,
            "/status":  self._cmd_status,
            "/report":  self._cmd_report,
            "/signals": self._cmd_signals,
            "/history": self._cmd_history,
            "/pause":   self._cmd_pause,
            "/resume":  self._cmd_resume,
            "/help":    self._cmd_help,
        }
        handler = _ROUTES.get(cmd)
        if handler:
            try:
                handler()
            except Exception as exc:
                log.error(f"Command {cmd} error: {exc}", exc_info=True)
                self.send(f"Error: {exc}")

    # ── Callback routing ──────────────────────

    def _handle_callback(self, cb: dict) -> None:
        query_id = cb["id"]
        data     = cb.get("data", "")
        self._api("answerCallbackQuery", callback_query_id=query_id)

        _ROUTES = {
            "cmd_status":  self._cmd_status,
            "cmd_report":  self._cmd_report,
            "cmd_signals": self._cmd_signals,
            "cmd_history": self._cmd_history,
            "cmd_pause":   self._cmd_pause,
            "cmd_resume":  self._cmd_resume,
        }
        if data in _ROUTES:
            try:
                _ROUTES[data]()
            except Exception as exc:
                log.error(f"Callback {data} error: {exc}", exc_info=True)
                self.send(f"Error: {exc}")
        elif data == "confirm_pause":
            self._confirm_pause()
        elif data == "cancel_action":
            self.send("Cancelled.")

    # ── /menu ─────────────────────────────────

    def _cmd_menu(self) -> None:
        kb = {"inline_keyboard": [
            [{"text": "Status",  "callback_data": "cmd_status"},
             {"text": "Report",  "callback_data": "cmd_report"}],
            [{"text": "Signals", "callback_data": "cmd_signals"},
             {"text": "History", "callback_data": "cmd_history"}],
            [{"text": "Pause",   "callback_data": "cmd_pause"},
             {"text": "Resume",  "callback_data": "cmd_resume"}],
        ]}
        self.send("<b>Smart DCA Bot</b>\nChoose a command:", reply_markup=kb)

    # ── /status ───────────────────────────────

    def _cmd_status(self) -> None:
        from config import MONTHLY_BUDGET, EXECUTION_TIME_UTC
        s = _load_state()

        now        = datetime.now(timezone.utc)
        days_in_mo = calendar.monthrange(now.year, now.month)[1]
        days_left  = days_in_mo - now.day
        spent      = s.get("month_spent", 0.0)
        pool       = s.get("base_pool",   0.0)
        paused     = s.get("paused",      False)

        h, m   = EXECUTION_TIME_UTC.split(":")
        run_dt = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if run_dt <= now:
            run_dt += timedelta(days=1)
        next_run = run_dt.strftime("%Y-%m-%d %H:%M UTC")

        icon = "PAUSED" if paused else "RUNNING"
        self.send(
            f"<b>Status [{icon}]</b>\n"
            f"\n"
            f"Monthly budget  : <b>${MONTHLY_BUDGET:,.2f}</b>\n"
            f"Spent this month: <b>${spent:,.2f}</b>\n"
            f"Remaining       : <b>${MONTHLY_BUDGET - spent:,.2f}</b>\n"
            f"Pool balance    : <b>${pool:,.2f}</b>\n"
            f"Days left       : <b>{days_left}</b>\n"
            f"Next run        : <b>{next_run}</b>"
        )

    # ── /report ───────────────────────────────

    def _cmd_report(self) -> None:
        import portfolio
        s    = portfolio.get_summary()
        sign = "+" if s["unrealised_pnl"] >= 0 else ""
        self.send(
            f"<b>Portfolio Report</b>\n"
            f"\n"
            f"Purchases : <b>{s['purchase_count']}</b>\n"
            f"Total qty : <b>{s['total_qty']:.8f} cbBTC</b>\n"
            f"Invested  : <b>${s['total_invested']:,.2f}</b>\n"
            f"Avg entry : <b>${s['avg_entry_price']:,.2f}</b>\n"
            f"Now       : <b>${s['current_price']:,.2f}</b>\n"
            f"Value     : <b>${s['current_value']:,.2f}</b>\n"
            f"P&amp;L   : <b>{sign}${s['unrealised_pnl']:,.2f} "
            f"({sign}{s['unrealised_pnl_pct']:.2f}%)</b>"
        )

    # ── /signals ──────────────────────────────

    def _cmd_signals(self) -> None:
        import signals as sig_mod
        import dca_engine
        from config import DAILY_DRIP, POOL_CAP_X

        self.send("Fetching live signals...")
        try:
            scores_full = sig_mod.score_all()
            meta        = scores_full.get("_meta", {})
            scores      = {k: v for k, v in scores_full.items() if k != "_meta"}

            comp   = dca_engine.composite_score(scores)
            mult   = dca_engine.get_multiplier(comp)
            theory = {"base_pool": DAILY_DRIP * POOL_CAP_X, "month_spent": 0.0}
            buy    = dca_engine.calc_buy_amount(comp, theory)

            fg  = meta.get("fear_greed",  {})
            rsi = meta.get("rsi",         {})
            liq = meta.get("liquidation", {})
            ma  = "above MA200" if rsi.get("above_ma200") else "below MA200"

            self.send(
                f"<b>Signal Scores</b>\n"
                f"\n"
                f"Fear &amp; Greed : <b>{scores['fear_greed']:.4f}</b>"
                f"  (index={fg.get('index','?')}, {fg.get('label','?')})\n"
                f"RSI-14 / MA200 : <b>{scores['rsi']:.4f}</b>"
                f"  (RSI={rsi.get('rsi','?')}, {ma})\n"
                f"Liquidation    : <b>{scores['liquidation']:.4f}</b>"
                f"  (vol {liq.get('vol_ratio','?')}x,"
                f" dprice {liq.get('price_change_pct','?')}%)\n"
                f"\n"
                f"Composite : <b>{comp:.4f}</b>\n"
                f"Multiplier: <b>{mult:.1f}x</b>\n"
                f"Theo. buy : <b>${buy:.2f}</b>"
            )
        except Exception as exc:
            self.send(f"Signal fetch error: {exc}")

    # ── /history ──────────────────────────────

    def _cmd_history(self) -> None:
        import portfolio
        purchases = portfolio.load_purchases()
        recent    = list(reversed(purchases[-10:]))  # newest first

        if not recent:
            self.send("No purchases recorded yet.")
            return

        lines = ["<b>Last 10 Purchases</b>", ""]
        for p in recent:
            date     = p.get("timestamp", "")[:10]
            spent    = p.get("usdc_spent", 0.0)
            qty      = p.get("qty",        0.0)
            price    = p.get("price_usd",  0.0)
            tx       = p.get("tx_hash",    "")
            tx_short = f"{tx[:6]}...{tx[-4:]}" if len(tx) > 12 else tx or "n/a"
            lines.append(
                f"{date}  ${spent:.2f} -> {qty:.8f} cbBTC"
                f" @ ${price:,.0f}  <code>{tx_short}</code>"
            )
        self.send("\n".join(lines))

    # ── /pause ────────────────────────────────

    def _cmd_pause(self) -> None:
        if _load_state().get("paused", False):
            self.send("Already paused. Use /resume to re-enable buying.")
            return

        with self._lock:
            self._pending_pause = {"expires_at": time.time() + 60}

        kb = {"inline_keyboard": [[
            {"text": "Confirm Pause", "callback_data": "confirm_pause"},
            {"text": "Cancel",        "callback_data": "cancel_action"},
        ]]}
        self.send(
            "Pause buying?\n"
            "All DCA buys will be skipped until resumed.\n"
            "<i>Confirm within 60 seconds.</i>",
            reply_markup=kb,
        )

    def _confirm_pause(self) -> None:
        with self._lock:
            pending               = self._pending_pause
            self._pending_pause   = None

        if pending is None or time.time() > pending["expires_at"]:
            self.send("Confirm expired (>60s). Send /pause again.")
            return

        s = _load_state()
        s["paused"] = True
        _save_state(s)
        self.send("Bot paused. Send /resume to re-enable buying.")

    # ── /resume ───────────────────────────────

    def _cmd_resume(self) -> None:
        s = _load_state()
        if not s.get("paused", False):
            self.send("Bot is not currently paused.")
            return
        s["paused"] = False
        _save_state(s)
        self.send("Bot resumed. Buying will continue at next scheduled run.")

    # ── /help ─────────────────────────────────

    def _cmd_help(self) -> None:
        self.send(
            "<b>Smart DCA Bot — Commands</b>\n"
            "\n"
            "/menu    — inline keyboard\n"
            "/status  — budget, pool, next run\n"
            "/report  — portfolio P&amp;L\n"
            "/signals — live signal scores\n"
            "/history — last 10 purchases\n"
            "/pause   — pause buying (requires confirm)\n"
            "/resume  — resume buying\n"
            "/help    — this message"
        )


# ── Module helpers (lazy imports to avoid circular deps) ──────────

def _load_state() -> dict:
    import state
    return state.load_state()

def _save_state(s: dict) -> None:
    import state
    state.save_state(s)


# ── Module-level singleton + public API ───────────────────────────

_bot: TelegramBot | None = None


def start_background_bot() -> TelegramBot:
    """Instantiate and start the background polling bot.

    Called once from run_bot.run_daemon(). Returns the bot instance.
    """
    global _bot
    _bot = TelegramBot()
    _bot.start()
    return _bot


def send_buy_alert(
    qty:        float,
    price_usd:  float,
    usdc_spent: float,
    comp_score: float,
    multiplier: float,
    tx_hash:    str,
    summary:    dict,
) -> None:
    """Send a DCA buy alert to the configured Telegram chat.

    Standalone — works whether or not the polling bot is running.
    """
    token   = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    tx_short = f"{tx_hash[:6]}...{tx_hash[-4:]}" if len(tx_hash) > 12 else tx_hash or "n/a"
    sign     = "+" if summary.get("unrealised_pnl", 0) >= 0 else ""

    text = (
        f"DCA Buy Executed\n"
        f"\n"
        f"cbBTC: <b>{qty:.8f}</b>\n"
        f"Price: <b>${price_usd:,.0f}</b>\n"
        f"Spent: <b>${usdc_spent:.2f} USDC</b>\n"
        f"Score: <b>{comp_score:.2f} ({multiplier:.1f}x)</b>\n"
        f"Tx: <code>{tx_short}</code>\n"
        f"\n"
        f"Portfolio: <b>{summary.get('total_qty', 0):.8f} cbBTC</b>"
        f" | avg ${summary.get('avg_entry_price', 0):,.0f}"
        f" | P&amp;L {sign}{summary.get('unrealised_pnl_pct', 0):.2f}%"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        log.warning(f"Buy alert failed: {exc}")
