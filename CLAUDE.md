# CLAUDE.md — SMART DCA BOT CONTEXT BRIDGE
> Last updated: 31 March 2026
> Source: SMART_DCA_31st_March.txt snapshot
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
config.py        — all settings, budgets, thresholds, addresses
signals.py       — F&G (Alternative.me) + RSI/MA200/liq proxy (Kraken OHLCV)
dca_engine.py    — composite score, multiplier, pool/reserve logic
state.py         — state.json r/w, month rollover, cumulative reserve carryover
base_client.py   — Uniswap V3 approve → swap → transfer
portfolio.py     — purchases.json, VWAP avg entry, unrealised PnL
telegram_bot.py  — short-poll, all /commands
run_bot.py       — daily scheduler, run_once(), run_daemon()
```

**Runtime files (VPS only, gitignored):**
```
~/dca-bot/state.json       — base_pool, reserve_pool, month_spent, paused
~/dca-bot/purchases.json   — full purchase history (source for /report)
~/dca-bot/python/.env      — private key, cold wallet, Telegram token
```

---

## CURRENT CONFIG (`config.py`)

```python
MONTHLY_BUDGET        = 500      # USD
RESERVE_PCT           = 0.40     # $200/month → reserve_pool
BASE_DAILY_DRIP       = 10       # $10/day (60% ÷ 30)
POOL_CAP_X            = 5.0      # base pool ceiling = $50
RESERVE_THRESHOLD     = 0.65     # score needed to release reserve
RESERVE_MAX_MONTHS    = 6        # reserve ceiling = $1,200
NO_BUY_ZONE           = True
NO_BUY_THRESHOLD      = 0.35     # skip entirely below this score
SLIPPAGE_BPS          = 50       # 0.5%
USE_RESERVE           = True
DRY_RUN               = False
```

---

## BUDGET LOGIC

```
1. desired    = base_daily × multiplier          e.g. $10 × 3.0 = $30
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
reserve_pool = min(reserve_pool + 200, 1200)          # tops up, never resets
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
`budget`, `reserve_pct`, `reserve_threshold`, `reserve_max_months`, `no_buy_threshold`, `pool_cap_x`, `use_reserve`, `no_buy_zone`, `dry_run`, `execution_time`

---

## PORTFOLIO STATE (as of 30 Mar 2026)

| # | Date | Spent | cbBTC | Price | Score | Notes |
|---|------|-------|-------|-------|-------|-------|
| 1 | 28 Mar | $10.00 | 0.00015067 | $66,370 | 0.71 (3×) | Manual transfer (nonce bug) |
| 2 | 29 Mar | $10.00 | 0.00015053 | $66,432 | 0.58 (2×) | Clean ✅ |
| 3 | 30 Mar | $10.00 | 0.00014923 | $67,011 | 0.61 (2×) | Manual transfer (balance bug) |

**Summary:** 3 purchases | 0.00045043 cbBTC | $30.00 invested | VWAP $66,603 | P&L +0.51%

---

## BUG HISTORY (`base_client.py` — all fixed as of 30 Mar)

| Date | Bug | Fix |
|------|-----|-----|
| 28 Mar | Nonce too low | Fetch nonce once with `pending` and pass through all txs |
| 29 Mar | STF revert | Wait for approve receipt with status check before firing swap |
| 30 Mar | Transfer exceeds balance | Read actual on-chain cbBTC balance after swap; log quoted vs actual delta |

---

## NOTION PAGE IDs

> Use ONLY for logging completed tasks or looking up old architectural decisions. Never scan/search workspace.

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

---

## LAST 3 COMPLETED TASKS

1. **STF transfer fix (30 Mar)** — `base_client.py` reads actual on-chain cbBTC balance post-swap instead of quoted amount. Delta logged on every buy.
2. **Full Telegram config management (27–28 Mar)** — `/set <key> <value>` changes any config live with inline confirm + 60s expiry. Writes to `config.py` via regex, reloads via `importlib.reload`. `/set execution_time` reschedules without restart.
3. **Reserve + no-buy zone wired (27 Mar)** — All parameters live in `config.py` and correctly consumed by `dca_engine.py` and `state.py`. Cumulative reserve carryover confirmed in `handle_month_rollover()`.

---

## IMMEDIATE NEXT ACTIONS

- [ ] **31 Mar** — Final March buy (all three `base_client.py` fixes in place, should be fully clean)
- [ ] **1 Apr** — Month rollover: reserve tops up to ~$250, base pool resets to 0
- [ ] **April** — First reserve deployments on score ≥ 0.65 days
- [ ] **Future** — `notion_logger.py` — auto-log to Notion after each buy
- [ ] **Future** — Phase 2: add WETH (same chain, ~20-min config change)

---

## SESSION NOTES
> Append timestamped notes here during each session. Clear when stale.

- 31 Mar 2026: CLAUDE.md initialised from snapshot. Bot live, no outstanding bugs.