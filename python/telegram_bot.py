#!/usr/bin/env python3
# ─────────────────────────────────────────────
#  Smart DCA Bot — telegram_bot.py
#
#  Background long-polling thread + inline keyboard menu.
#  Only responds to TELEGRAM_CHAT_ID from .env.
#  No external telegram library — raw Bot API via requests.
#
#  All command handlers run in daemon threads so the
#  poll loop is never blocked by network or file I/O.
# ─────────────────────────────────────────────

import calendar
import importlib
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger("dca-bot.telegram")

# ── Config key schema ─────────────────────────
# key -> (VAR_NAME, type, min, max)
# types: "float", "int", "bool", "hhmm"
_CONFIG_SCHEMA: dict[str, tuple] = {
    "budget":             ("MONTHLY_BUDGET",    "float", 10.0,  10000.0),
    "reserve_pct":        ("RESERVE_PCT",        "float", 0.10,  0.80),
    "reserve_threshold":  ("RESERVE_THRESHOLD",  "float", 0.30,  0.95),
    "reserve_max_months": ("RESERVE_MAX_MONTHS", "int",   1,     12),
    "no_buy_threshold":   ("NO_BUY_THRESHOLD",   "float", 0.10,  0.80),
    "pool_cap_x":         ("POOL_CAP_X",         "float", 1.0,   15.0),
    "use_reserve":        ("USE_RESERVE",         "bool",  None,  None),
    "no_buy_zone":        ("NO_BUY_ZONE",         "bool",  None,  None),
    "dry_run":            ("DRY_RUN",             "bool",  None,  None),
    "execution_time":     ("EXECUTION_TIME_UTC",  "hhmm",  None,  None),
}


# ── Bot class ─────────────────────────────────

class TelegramBot:
    def __init__(self) -> None:
        self.token   = os.getenv("TELEGRAM_TOKEN", "")
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID", ""))
        self._offset  = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock    = threading.Lock()
        self._pending_pause: dict | None = None  # {'expires_at': float}
        self._pending_set:   dict | None = None  # {'var', 'new_val', 'new_str', 'key', 'expires_at'}

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

    def _get_updates(self) -> list:
        """Short-poll getUpdates — returns immediately with pending updates or []."""
        url  = f"https://api.telegram.org/bot{self.token}/getUpdates"
        resp = requests.get(
            url,
            params={
                "offset":          self._offset,
                "timeout":         0,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=(5, 5),   # (connect timeout, read timeout)
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    def _poll_loop(self) -> None:
        while self._running:
            try:
                for update in self._get_updates():
                    log.info(f"Update received: {list(update.keys())}")
                    self._offset = update["update_id"] + 1
                    try:
                        self._dispatch(update)
                    except Exception as exc:
                        log.error(
                            f"Dispatch error (update_id={update.get('update_id')}): {exc}",
                            exc_info=True,
                        )
            except Exception as exc:
                log.warning(f"Telegram poll error: {exc}")
                time.sleep(1)
            time.sleep(0.5)   # 500ms between polls — fast enough to feel instant

    def _ack_callback(self, callback_id: str) -> None:
        """Fire-and-forget answerCallbackQuery. Never raises."""
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=3,
            )
        except Exception:
            pass

    def _dispatch(self, update: dict) -> None:
        """Route an update to the correct handler, enforcing chat_id guard."""
        msg = update.get("message")
        cb  = update.get("callback_query")

        if msg:
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                return
            self._handle_message(msg)

        if cb:
            # Fire-and-forget ack — clears the spinner, never blocks dispatch.
            cb_id = cb["id"]
            threading.Thread(target=self._ack_callback, args=(cb_id,), daemon=True).start()
            if str(cb.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                return
            self._handle_callback(cb)

    # ── Message routing (non-blocking) ────────

    def _handle_message(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd   = parts[0].lower().split("@")[0]
        args  = parts[1:]

        _ROUTES = {
            "/start":   self._cmd_menu,
            "/menu":    self._cmd_menu,
            "/status":  self._cmd_status,
            "/report":  self._cmd_report,
            "/signals": self._cmd_signals,
            "/history": self._cmd_history,
            "/config":  self._cmd_config,
            "/pause":   self._cmd_pause,
            "/resume":  self._cmd_resume,
            "/help":    self._cmd_help,
        }

        def _run() -> None:
            try:
                if cmd == "/set":
                    self._cmd_set(args)
                elif cmd in _ROUTES:
                    _ROUTES[cmd]()
                # Unknown commands silently ignored
            except Exception as exc:
                log.error(f"Command {cmd} error: {exc}", exc_info=True)
                self.send(f"Error running {cmd}: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    # ── Callback routing (non-blocking) ───────

    def _handle_callback(self, cb: dict) -> None:
        data = cb.get("data", "")
        log.info(f"Callback received: {data}")

        _ROUTES = {
            "status":  self._cmd_status,
            "report":  self._cmd_report,
            "signals": self._cmd_signals,
            "history": self._cmd_history,
            "config":  self._cmd_config,
            "pause":   self._cmd_pause,
            "resume":  self._cmd_resume,
        }

        def _run() -> None:
            if data in _ROUTES:
                try:
                    _ROUTES[data]()
                except Exception as exc:
                    log.error(f"Callback {data} error: {exc}", exc_info=True)
                    self.send(f"Error in {data}: {exc}")
            elif data == "confirm_pause":
                try:
                    self._confirm_pause()
                except Exception as exc:
                    log.error(f"Callback confirm_pause error: {exc}", exc_info=True)
                    self.send(f"Error confirming pause: {exc}")
            elif data == "confirm_set":
                try:
                    self._confirm_set()
                except Exception as exc:
                    log.error(f"Callback confirm_set error: {exc}", exc_info=True)
                    self.send(f"Error applying setting: {exc}")
            elif data == "cancel_action":
                try:
                    self.send("Cancelled.")
                except Exception as exc:
                    log.error(f"Callback cancel_action error: {exc}", exc_info=True)
            else:
                log.warning(f"Unrecognised callback_data: {data!r}")

        threading.Thread(target=_run, daemon=True).start()

    # ── /menu ─────────────────────────────────

    def _cmd_menu(self) -> None:
        kb = {"inline_keyboard": [
            [{"text": "Status",  "callback_data": "status"},
             {"text": "Report",  "callback_data": "report"}],
            [{"text": "Signals", "callback_data": "signals"},
             {"text": "History", "callback_data": "history"}],
            [{"text": "Config",  "callback_data": "config"},
             {"text": "Pause",   "callback_data": "pause"}],
            [{"text": "Resume",  "callback_data": "resume"}],
        ]}
        self.send("<b>Smart DCA Bot</b>\nChoose a command:", reply_markup=kb)

    # ── /status ───────────────────────────────

    def _cmd_status(self) -> None:
        import config as cfg
        s = _load_state()

        now        = datetime.now(timezone.utc)
        days_in_mo = calendar.monthrange(now.year, now.month)[1]
        days_left  = days_in_mo - now.day
        spent      = s.get("month_spent", 0.0)
        pool       = s.get("base_pool",   0.0)
        reserve    = s.get("reserve_pool", 0.0)
        paused     = s.get("paused",      False)

        h, m   = cfg.EXECUTION_TIME_UTC.split(":")
        run_dt = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if run_dt <= now:
            run_dt += timedelta(days=1)
        next_run = run_dt.strftime("%Y-%m-%d %H:%M UTC")

        icon = "PAUSED" if paused else "RUNNING"
        self.send(
            f"<b>Status [{icon}]</b>\n"
            f"\n"
            f"Monthly budget  : <b>${cfg.MONTHLY_BUDGET:,.2f}</b>\n"
            f"Spent this month: <b>${spent:,.2f}</b>\n"
            f"Remaining       : <b>${cfg.MONTHLY_BUDGET - spent:,.2f}</b>\n"
            f"Pool balance    : <b>${pool:,.2f}</b>\n"
            f"Reserve pool    : <b>${reserve:,.2f}</b>\n"
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
        import config as cfg

        self.send("Fetching live signals...")
        try:
            scores_full = sig_mod.score_all()
            meta        = scores_full.get("_meta", {})
            scores      = {k: v for k, v in scores_full.items() if k != "_meta"}

            comp   = dca_engine.composite_score(scores)
            mult   = dca_engine.get_multiplier(comp)
            theory = {
                "base_pool":    cfg.DAILY_DRIP * cfg.POOL_CAP_X,
                "reserve_pool": cfg.MONTHLY_BUDGET * cfg.RESERVE_PCT,
                "month_spent":  0.0,
            }
            buy  = dca_engine.calc_buy_amount(comp, theory)

            fg   = meta.get("fear_greed",  {})
            rsi  = meta.get("rsi",         {})
            liq  = meta.get("liquidation", {})
            ma   = "above MA200" if rsi.get("above_ma200") else "below MA200"

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
        recent    = list(reversed(purchases[-10:]))

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

    # ── /config ───────────────────────────────

    def _cmd_config(self) -> None:
        import config as cfg

        base_pct    = int(round((1 - cfg.RESERVE_PCT) * 100))
        pool_ceil   = cfg.DAILY_DRIP * cfg.POOL_CAP_X
        res_portion = cfg.MONTHLY_BUDGET * cfg.RESERVE_PCT
        res_ceiling = res_portion * cfg.RESERVE_MAX_MONTHS
        h, m        = cfg.EXECUTION_TIME_UTC.split(":")
        bali_h      = (int(h) + 8) % 24
        bali_str    = f"{bali_h:02d}:{m} Bali"

        self.send(
            f"<b>Bot Configuration</b>\n"
            f"\n"
            f"<b>Budget</b>\n"
            f"  Monthly budget    : <b>${cfg.MONTHLY_BUDGET:,.2f}</b>\n"
            f"  Base daily drip   : <b>${cfg.DAILY_DRIP:.2f}</b>"
            f"  ({base_pct}% of budget / 30)\n"
            f"  Pool cap          : <b>${pool_ceil:.2f}</b>"
            f"  ({cfg.POOL_CAP_X:.0f}x base daily)\n"
            f"\n"
            f"<b>Reserve Mode : {'ON' if cfg.USE_RESERVE else 'OFF'}</b>\n"
            f"  Reserve %         : {cfg.RESERVE_PCT * 100:.0f}%\n"
            f"  Reserve threshold : {cfg.RESERVE_THRESHOLD}\n"
            f"  Reserve max months: {cfg.RESERVE_MAX_MONTHS}\n"
            f"  Reserve ceiling   : ${res_ceiling:,.0f}\n"
            f"\n"
            f"<b>No-Buy Zone : {'ON' if cfg.NO_BUY_ZONE else 'OFF'}</b>\n"
            f"  No-buy threshold  : {cfg.NO_BUY_THRESHOLD}\n"
            f"\n"
            f"Schedule : <b>{cfg.EXECUTION_TIME_UTC} UTC</b>  ({bali_str})\n"
            f"DRY RUN  : <b>{cfg.DRY_RUN}</b>"
        )

    # ── /set <key> <value> ────────────────────

    def _cmd_set(self, args: list) -> None:
        if len(args) < 2:
            self.send(
                "Usage: <code>/set &lt;key&gt; &lt;value&gt;</code>\n"
                "Example: <code>/set budget 600</code>\n\n"
                "Send /help for the full key list."
            )
            return

        key   = args[0].lower()
        value = " ".join(args[1:]).strip()

        if key not in _CONFIG_SCHEMA:
            valid = "\n".join(f"  {k}" for k in sorted(_CONFIG_SCHEMA))
            self.send(f"Unknown key <b>{key}</b>.\n\nValid keys:\n{valid}")
            return

        var_name, typ, min_val, max_val = _CONFIG_SCHEMA[key]

        try:
            new_val = _parse_config_value(typ, value, min_val, max_val)
        except ValueError as exc:
            self.send(f"Invalid value for <b>{key}</b>: {exc}")
            return

        import config as cfg
        old_val = getattr(cfg, var_name)
        old_str = _format_config_val(old_val, typ)
        new_str = _format_config_val(new_val, typ)

        confirm_msg = (
            f"Change <b>{var_name}</b>\n"
            f"  from: <b>{old_str}</b>\n"
            f"  to:   <b>{new_str}</b>\n\n"
            f"This takes effect immediately."
        )
        if key == "dry_run" and new_val is False:
            confirm_msg += (
                "\n\n<b>WARNING: This enables LIVE trading."
                " Real USDC will be spent.</b>"
            )
        confirm_msg += "\n\n<i>Confirm within 60 seconds.</i>"

        with self._lock:
            self._pending_set = {
                "key":        key,
                "var":        var_name,
                "typ":        typ,
                "new_val":    new_val,
                "new_str":    new_str,
                "expires_at": time.time() + 60,
            }

        kb = {"inline_keyboard": [[
            {"text": "Yes, apply", "callback_data": "confirm_set"},
            {"text": "Cancel",     "callback_data": "cancel_action"},
        ]]}
        self.send(confirm_msg, reply_markup=kb)

    def _confirm_set(self) -> None:
        with self._lock:
            pending          = self._pending_set
            self._pending_set = None

        if pending is None or time.time() > pending["expires_at"]:
            self.send("Confirm expired (>60s). Send /set again.")
            return

        var_name = pending["var"]
        new_val  = pending["new_val"]
        key      = pending["key"]
        new_str  = pending["new_str"]

        try:
            _update_config_file(var_name, new_val)
            _reload_all_config()

            # Reschedule the daily job if execution_time changed
            if key == "execution_time" and _reschedule_fn is not None:
                _reschedule_fn(new_val)

            # Build success message with derived values for budget/pct changes
            import config as cfg
            msg = f"{var_name} updated to <b>{new_str}</b>."
            if key in ("budget", "reserve_pct"):
                msg += (
                    f"\nBase daily drip is now <b>${cfg.DAILY_DRIP:.2f}</b>."
                    f"\nPool cap is now <b>${cfg.DAILY_DRIP * cfg.POOL_CAP_X:.2f}</b>."
                )
            elif key == "execution_time":
                h, m     = new_val.split(":")
                bali_h   = (int(h) + 8) % 24
                msg     += f"  ({bali_h:02d}:{m} Bali)  Job rescheduled."

            self.send(msg)
            log.info(f"Config updated via Telegram: {var_name} = {new_str}")

        except Exception as exc:
            self.send(f"Failed to update {var_name}: {exc}")
            log.error(f"Config update failed: {exc}", exc_info=True)

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
            pending             = self._pending_pause
            self._pending_pause = None

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
        keys = "  ".join(sorted(_CONFIG_SCHEMA.keys()))
        self.send(
            "<b>Smart DCA Bot — Commands</b>\n"
            "\n"
            "/menu              — inline keyboard\n"
            "/status            — budget, pool, next run\n"
            "/report            — portfolio P&amp;L\n"
            "/signals           — live signal scores\n"
            "/history           — last 10 buys\n"
            "/config            — view all settings\n"
            "/set &lt;key&gt; &lt;value&gt;  — change a setting\n"
            "/pause             — pause buying\n"
            "/resume            — resume buying\n"
            "/help              — this list\n"
            "\n"
            "<b>Settings keys:</b>\n"
            f"<code>{keys}</code>"
        )


# ── Module helpers ────────────────────────────

def _load_state() -> dict:
    import state
    return state.load_state()

def _save_state(s: dict) -> None:
    import state
    state.save_state(s)

def _parse_config_value(typ: str, raw: str, min_val, max_val):
    """Parse and validate a user-supplied config value string."""
    if typ == "float":
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"Expected a number, got '{raw}'")
        if not (min_val <= v <= max_val):
            raise ValueError(f"Must be between {min_val} and {max_val}")
        return v
    elif typ == "int":
        try:
            v = int(raw)
        except ValueError:
            raise ValueError(f"Expected an integer, got '{raw}'")
        if not (min_val <= v <= max_val):
            raise ValueError(f"Must be between {min_val} and {max_val}")
        return v
    elif typ == "bool":
        if raw.lower() in ("true", "on", "1", "yes"):
            return True
        if raw.lower() in ("false", "off", "0", "no"):
            return False
        raise ValueError("Must be true/false/on/off/1/0")
    elif typ == "hhmm":
        if not re.match(r"^\d{2}:\d{2}$", raw):
            raise ValueError("Must be HH:MM format (e.g. 09:00)")
        h, m = raw.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError("Invalid time — hours 00-23, minutes 00-59")
        return raw
    raise ValueError(f"Unknown type '{typ}'")

def _format_config_val(val, typ: str) -> str:
    """Format a config value for display in confirmation messages."""
    if typ == "bool":
        return "True" if val else "False"
    if typ == "float":
        # Show up to 4 significant digits, no trailing zeros
        return f"{val:.10g}"
    return str(val)

def _update_config_file(var_name: str, new_val) -> None:
    """Regex-replace the assignment line for var_name in config.py."""
    config_path = Path(__file__).parent / "config.py"
    content     = config_path.read_text(encoding="utf-8")

    if isinstance(new_val, bool):
        val_str = "True" if new_val else "False"
    elif isinstance(new_val, str):
        val_str = f'"{new_val}"'
    elif isinstance(new_val, float):
        val_str = f"{new_val:.10g}"
    elif isinstance(new_val, int):
        val_str = str(new_val)
    else:
        val_str = repr(new_val)

    pattern     = rf"^{re.escape(var_name)}\s*=.*$"
    new_content = re.sub(pattern, f"{var_name} = {val_str}", content, flags=re.MULTILINE)

    if new_content == content:
        raise ValueError(f"Pattern for {var_name} not found in config.py")

    config_path.write_text(new_content, encoding="utf-8")

def _reload_all_config() -> None:
    """Reload config and all modules that import from it at module level."""
    import config, dca_engine, state, signals, base_client
    importlib.reload(config)
    # Dependent modules — re-executes their top-level 'from config import ...'
    importlib.reload(state)
    importlib.reload(dca_engine)
    importlib.reload(signals)
    importlib.reload(base_client)
    log.info("Config reloaded in-process.")


# ── Module-level singleton + public API ───────────────────────────

_bot:          TelegramBot | None           = None
_reschedule_fn: "callable[[str], None] | None" = None


def start_background_bot() -> TelegramBot:
    """Instantiate and start the background polling bot.

    Called once from run_bot.run_daemon(). Returns the bot instance.
    """
    global _bot
    _bot = TelegramBot()
    _bot.start()
    return _bot


def register_reschedule_fn(fn) -> None:
    """Register a callback that run_bot exposes to reschedule the daily job.

    Signature: fn(new_time_utc: str) -> None
    Called by _confirm_set when execution_time is changed.
    """
    global _reschedule_fn
    _reschedule_fn = fn


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
