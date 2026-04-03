# CLAUDE.md — SMART DCA BOT CONTEXT BRIDGE
> Last updated: 1 April 2026
> Purpose: Primary context file for both Claude Code (CLI) and Web Chat sessions.
> Rule: Always read this file first. Never scan Notion workspace to reconstruct state.

---

## ENVIRONMENT

| Key | Value |
|-----|-------|
| Language | Python 3.13.5 |
| Chain | Base mainnet (EVM) |
| Asset | cbBTC (Phase 1) |
| Venue | Uniswap V3 — USDC → cbBTC → cold wallet |
| VPS | srv1450062 / `100.74.164.1` / user: `stu1984` / Lithuania |
| Service | `systemd dca-bot` (separate from ezmanager) |
| Schedule | `00:20 UTC` daily (08:20 Bali WITA) |
| Status | **LIVE** — `DRY_RUN=False` |
| Repo | `https://github.com/stuartblair84-defi/dca-bot` |
| Local path | `C:\Projects\dca-bot\python\` |

**Hot wallet:** `0xd1F1a36B423Ea05e47fCB50F0b86fC5Dc3be3380` (Base)
**Cold wallet:** `0xdBBB6ed92BDc8aFDfE8295b8504A73305d0ef8C0` (Base)

---

## FILE MAP

```
config.py          — all settings, budgets, thresholds, addresses, Notion IDs
signals.py         — F&G (Alternative.me) + RSI/MA200/liq proxy (Kraken OHLCV)
dca_engine.py      — composite score, multiplier, pool/reserve logic
state.py           — state.json r/w, month rollover, cumulative reserve carryover
base_client.py     — Uniswap V3 approve → swap → transfer
portfolio.py       — purchases.json, VWAP avg entry, unrealised PnL
notion_logger.py   — auto-log buys to Notion (Purchase Ledger + Daily Buy Log)
telegram_bot.py    — short-poll, all /commands
run_bot.py         — daily scheduler, run_once(), run_daemon()
```

**Runtime files (VPS only, gitignored):**
```
~/dca-bot/state.json       — base_pool, reserve_pool, month_spent, paused
~/dca-bot/purchases.json   — full purchase history (source for /report)
~/dca-bot/python/.env      — private key, cold wallet, Telegram token, NOTION_TOKEN
```

---

## CURRENT CONFIG (`config.py`)

```python
MONTHLY_BUDGET        = 2000     # USD — increased from $500 on 31 Mar 2026
RESERVE_PCT           = 0.40     # $800/month → reserve_pool
DAILY_DRIP            = MONTHLY_BUDGET * (1 - RESERVE_PCT) / 30  # = $40/day
POOL_CAP_X            = 5.0      # base pool ceiling = $200
RESERVE_THRESHOLD     = 0.65     # score needed to release reserve
RESERVE_MAX_MONTHS    = 6        # reserve ceiling = $4,800
NO_BUY_ZONE           = True
NO_BUY_THRESHOLD      = 0.35     # skip entirely below this score
SLIPPAGE_BPS          = 50       # 0.5%
USE_RESERVE           = True
DRY_RUN               = False    # local always True; VPS always False

# — Notion Integration —
NOTION_PURCHASE_LEDGER_ID = "b95b8e65-19e0-4474-b16e-75fac7525189"
NOTION_DAILY_BUY_LOG_ID   = "330fae9c-5f32-81fc-929e-d584fa99cd38"
```

> ⚠️ `DAILY_DRIP` is derived from `MONTHLY_BUDGET` — not a standalone constant.
> To change the drip rate, change `MONTHLY_BUDGET` via `/set budget`.
> Do NOT add `/set daily_drip` to Telegram — it is unnecessary.

---

## BUDGET LOGIC

```
1. desired    = DAILY_DRIP × multiplier          e.g. $40 × 3.0 = $120
2. base_amt   = min(desired, base_pool)          capped by pool drip
3. shortfall  = desired − base_amt
4. if USE_RESERVE and score >= 0.65:
       reserve_amt = min(shortfall, reserve_pool)
5. total_spend = base_amt + reserve_amt
6. if score < 0.35: SKIP (no-buy zone)
```

**Multiplier tiers:**

| Score | Action | Multiplier |
|-------|--------|------------|
| < 0.35 | SKIP | — |
| 0.35–0.49 | Buy | 1.0× |
| 0.50–0.64 | Buy | 2.0× |
| 0.65–0.79 | Buy + Reserve | 3.0× |
| ≥ 0.80 | Buy + Reserve | 3.0× |

---

## MONTH ROLLOVER (runs on 1st of each month)

```python
base_pool    = 0.0                                    # resets
reserve_pool = min(reserve_pool + 800, 4800)          # tops up, never resets
month_spent  = 0.0                                    # resets
```

---

## SIGNAL SOURCES (all free, no API keys)

| Signal | Source |
|--------|--------|
| Fear & Greed | `https://api.alternative.me/fng/?limit=1` |
| RSI-14, MA200 | Kraken public OHLCV API (`XBTUSD`, 1440m candles) |
| Liq proxy | Derived from same OHLCV (vol spike + price drop) |

---

## TELEGRAM BOT

| Key | Value |
|-----|-------|
| Bot name | `SmartDCAprogram_bot` |
| Chat ID | `5118874860` |
| Token | `.env` only — never in code or git |

**Commands:** `/menu` `/status` `/report` `/signals` `/history` `/config` `/set <key> <value>` `/pause` `/resume` `/help`

**Settable keys via `/set`:**
`budget`, `reserve_pct`, `reserve_threshold`, `reserve_max_months`,
`no_buy_threshold`, `pool_cap_x`, `use_reserve`, `no_buy_zone`,
`dry_run`, `execution_time`

> `daily_drip` is NOT a settable key — it derives automatically from `budget`.

---

## NOTION LOGGER (`notion_logger.py`)

Logs every completed buy to two Notion destinations after `portfolio.record_purchase()`.
Wrapped in `try/except` in `run_bot.py` — Notion failure is WARNING only, never kills bot.
**Status: LIVE from 2 April 2026.** Apr 1 buy was manually logged (notion-client missing from venv — fixed same day).

**Public interface:**
```python
from notion_logger import log_buy
log_buy(buy_record: dict) -> None
```

**buy_record schema:**
```python
{
    "buy_number":       int,
    "date":             str,        # "2026-04-01"
    "cycle_time_utc":   str,        # "00:20"
    "usdc_spent":       float,
    "cbbtc_received":   float,      # actual on-chain, not quoted
    "price_usd":        float,
    "composite_score":  float,
    "multiplier":       float,      # 1.0 / 2.0 / 3.0
    "reserve_deployed": float,      # 0.0 if none used
    "swap_tx":          str,        # hex hash
    "transfer_tx":      str | None,
    "transfer_ok":      bool,
    "transfer_error":   str | None,
    "signals": {
        "fg_raw":       float,
        "fg_score":     float,
        "rsi":          float,
        "ma200_score":  float,
        "liq_score":    float,
        "composite":    float
    }
}
```

**Destinations:**

| Destination | Notion ID |
|-------------|-----------|
| Purchase Ledger DB | `b95b8e65-19e0-4474-b16e-75fac7525189` |
| Daily Buy Log page | `330fae9c-5f32-81fc-929e-d584fa99cd38` |

**.env entry required (VPS only):**
```
NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**VPS venv install:**
```bash
cd ~/dca-bot/python && source ../venv/bin/activate
pip install notion-client
```

**Notion integration setup:**
Go to Smart DCA Bot page → `...` → Connections → add integration.
One parent-level connection cascades to all sub-pages and databases.

---

## STATE FILE KEYS (`state.json`)

Confirmed key names (important for notion_logger reserve_deployed calc):
```json
{
    "month":        "2026-04",
    "month_spent":  40.0,
    "base_pool":    0.0,
    "reserve_pool": 860.0,
    "paused":       false,
    "last_run":     "2026-04-01T00:20:11.000000+00:00",
    "last_month":   "2026-04"
}
```

> `reserve_pool` = $60 carried from March + $800 April top-up = ~$860

---

## PORTFOLIO STATE (as of 1 Apr 2026)

| # | Date | Spent | cbBTC | Price | Score | Notes |
|---|------|-------|-------|-------|-------|-------|
| 1 | 28 Mar | $10.00 | 0.00015067 | $66,370 | 0.71 (3×) | Nonce bug — manual transfer |
| 2 | 29 Mar | $10.00 | 0.00015053 | $66,432 | 0.58 (2×) | Clean ✅ |
| 3 | 30 Mar | $10.00 | 0.00014923 | $67,011 | 0.61 (2×) | Balance bug — manual transfer |
| 4 | 31 Mar | $10.00 | 0.00014923 | $67,011 | 0.59 (2×) | Clean ✅ |
| 5 | 1 Apr | $40.00 | 0.00058695 | $68,149 | 0.55 (2×) | Clean ✅ First $40 drip |

**Summary:** 5 purchases | 0.00118661 cbBTC | $80.00 invested | VWAP $67,419 | P&L +0.98%

---

## BUG HISTORY (`base_client.py` — all fixed)

| Date | Bug | Fix |
|------|-----|-----|
| 28 Mar | Nonce too low | Fetch nonce once with `pending`, pass through all txs |
| 29 Mar | STF revert | Wait for approve receipt + status check before swap |
| 30 Mar | Transfer exceeds balance | Read actual on-chain cbBTC balance post-swap; log delta |
| 1 Apr | notion-client missing from venv | `pip install notion-client` inside venv; added to requirements.txt |

---

## NOTION PAGE IDs

> Use ONLY for logging completed tasks or looking up old architectural decisions.

| Page | ID |
|------|----|
| Smart DCA Bot (main) | `32ffae9c-5f32-8041-adfc-d3b308521f9e` |
| Session Log | `32ffae9c-5f32-81b9-99ed-c32af4b2e8b7` |
| Signal Output Log | `32ffae9c-5f32-811c-9dde-d20aca5b6582` |
| Config & Decisions | `32ffae9c-5f32-81e5-aec8-d855468e5aa8` |
| Project Reference | `32ffae9c-5f32-8100-85a7-c1518dff0d8a` |
| Purchase Ledger DB | `b95b8e65-19e0-4474-b16e-75fac7525189` |
| Known Issues | `330fae9c-5f32-8128-bffafc5288ba6db7` |
| Daily Buy Log | `330fae9c-5f32-81fc-929e-d584fa99cd38` |
| Useful Prompts & CLI | `334fae9c-5f32-8109-84ce-d4005073881d` |

---

## LAST 3 COMPLETED TASKS

1. **notion-client venv fix (1 Apr)** — `notion_logger.py` failed on first live run with `No module named 'notion_client'`. Fixed by installing inside the correct venv: `pip install notion-client`. `requirements.txt` updated and pushed. Auto-logging live from Apr 2.
2. **notion_logger.py built (31 Mar)** — auto-logs every buy to Purchase Ledger DB and Daily Buy Log. Two private functions `_add_to_purchase_ledger` and `_add_daily_buy_log`. Transfer Tx omitted when None. Wrapped in try/except — Notion failure is WARNING only.
3. **Budget increased to $2,000/month (31 Mar)** — `MONTHLY_BUDGET = 2000`, `DAILY_DRIP` auto-derives to $40/day. Reserve ceiling $4,800. Pool cap $200. Confirmed live via Telegram `/config`.

---

## IMMEDIATE NEXT ACTIONS

- [ ] **2 Apr 00:20 UTC** — First fully automated buy with Notion auto-logging. Verify both Purchase Ledger row and Daily Buy Log page created automatically.
- [ ] **Future** — Phase 2: add WETH (same chain, ~20-min config change)
- [ ] **Future** — Monthly review automation

---

## SESSION NOTES
> Append timestamped notes here during each session. Clear when stale.

- 1 Apr 2026: Apr 1 buy clean ✅ — $40 drip, rollover confirmed, zero-approval handled. notion_logger failed (missing notion-client in venv) — fixed. All 5 buys manually logged in Notion. Auto-logging live from Apr 2.