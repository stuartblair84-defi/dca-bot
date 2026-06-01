# CLAUDE.md — SMART DCA BOT CONTEXT BRIDGE
> Last updated: 1 June 2026
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
| Service | `systemd dca-bot` |
| Schedule | `00:20 UTC` daily |
| Status | **LIVE** — `DRY_RUN=False` |
| Repo | `https://github.com/stuartblair84-defi/dca-bot` |
| Local path | `C:\Projects\dca-bot\` |

**Hot wallet:** `0xd1F1a36B423Ea05e47fCB50F0b86fC5Dc3be3380` (Base)
**Cold wallet:** `0xdBBB6ed92BDc8aFDfE8295b8504A73305d0ef8C0` (Base)

---

## FILE MAP

```
config.py          — all settings, budgets, thresholds, addresses
signals.py         — F&G (Alternative.me) + RSI/MA200/liq proxy (Kraken OHLCV)
dca_engine.py      — composite score, multiplier, pool/reserve logic
state.py           — state.json r/w, month rollover, cumulative reserve carryover
base_client.py     — Uniswap V3 approve → swap → transfer
portfolio.py       — purchases.json, VWAP avg entry, unrealised PnL
file_logger.py     — local CSV + MD logging (replaced Notion logger)
telegram_bot.py    — short-poll, all /commands
run_bot.py         — daily scheduler, run_once(), run_daemon()
```

**Runtime files (VPS only, gitignored):**
```
~/dca-bot/state.json            — base_pool, reserve_pool, month_spent, paused
~/dca-bot/purchases.json        — full purchase history (source for /report)
~/dca-bot/purchase_ledger.csv   — CSV log of all buys
~/dca-bot/daily_buy_log.md      — markdown log of all buys
~/dca-bot/funding_ledger.csv    — deposit history
~/dca-bot/python/.env           — EVM_PRIVATE_KEY, COLD_WALLET, Telegram token
```

---

## CURRENT CONFIG (`config.py`) — updated 1 June 2026

```python
MONTHLY_BUDGET        = 2000.0
RESERVE_PCT           = 0.40
DAILY_DRIP            = MONTHLY_BUDGET * (1 - RESERVE_PCT) / 30  # ~$40/day
POOL_CAP_X            = 5.0       # base pool ceiling = ~$200
USE_RESERVE           = True
RESERVE_THRESHOLD     = 0.65
RESERVE_MAX_MONTHS    = 6         # reserve ceiling = $4,800
NO_BUY_ZONE           = False     # always buys, even on low scores
NO_BUY_THRESHOLD      = 0.35     # dead config while NO_BUY_ZONE = False
DRY_RUN               = False
```

---

## BUDGET LOGIC

```
1. desired    = DAILY_DRIP × multiplier
2. base_amt   = min(desired, base_pool)
3. shortfall  = desired − base_amt
4. if USE_RESERVE and score >= 0.65:
       reserve_amt = min(shortfall, reserve_pool)
5. total_spend = base_amt + reserve_amt
```

---

## MULTIPLIER TIERS — updated 1 June 2026

| Score | Multiplier | Base Spend | Reserve? | Max Total |
|-------|------------|------------|----------|-----------|
| 0.00–0.19 | 0.5× | $20 | No | $20 |
| 0.20–0.34 | 0.5× | $20 | No | $20 |
| 0.35–0.64 | 1.0× | $40 | No | $40 |
| 0.65–0.79 | 4.0× | $40* | Yes | $160 |
| ≥ 0.80 | 8.0× | $40* | Yes | $320 |

*base_amt capped by pool; reserve covers shortfall up to reserve_pool balance.

> Strategy intent: flat daily DCA at low scores, step-change on high conviction (≥0.65).
> Reserve unlocks at same threshold as 4× tier — designed alignment.
> Note: (0.50, 1.0) and (0.35, 1.0) are redundant in config — same outcome, cosmetic only.

---

## SIGNAL SOURCES

| Signal | Weight | Source |
|--------|--------|--------|
| fear_greed | 0.35 | Alternative.me FNG API |
| rsi | 0.40 | Kraken OHLCV — RSI-14 daily + MA200 modifier |
| liquidation | 0.25 | Derived from OHLCV — vol spike + price drop proxy |

---

## TELEGRAM COMMANDS

| Command | Description |
|---------|-------------|
| `/status` | Pool, reserve, month spent, next run |
| `/config` | Live config values |
| `/signals` | Current signal scores and composite |
| `/report` | Portfolio summary — VWAP, P&L, all buys |
| `/balance` | Live on-chain USDC (hot) + cbBTC (hot + cold) |
| `/funding` | Deposit history, total in, total spent, implied balance |
| `/pause` | Pause buying cycles |
| `/resume` | Resume buying cycles |
| `/help` | All commands |

---

## CURRENT STATE (as of 1 May 2026 — update after each session)

```
Monthly budget    : $2,000
Daily drip        : $40
Reserve pool      : $1,660  ($860 Apr carry + $800 May top-up)
Pool balance      : ~$100   (carry from April)
Month spent       : $0      (May just started at last update)
```

> ⚠️ Run `/status` on Telegram for live figures — state above may be stale.

---

## PORTFOLIO SUMMARY (through 30 Apr 2026 — update after each session)

| Metric | Value |
|--------|-------|
| Total buys | 20 |
| Total invested | ~$780 |
| cbBTC acquired | ~0.01089 |
| Avg buy price (VWAP) | ~$71,630 |

---

## FUNDING HISTORY

| Date | Amount | Notes |
|------|--------|-------|
| 2026-03-26 | $10.00 | Initial seed |
| 2026-03-27 | $91.00 | Top-up |
| 2026-04-01 | $2,000.00 | April funding |
| 2026-05-01 | $1,000.00 | May top-up |
| **Total** | **$3,101.00** | |

---

## KNOWN FIXES & KEY LEARNINGS

- **RPC stale node (publicnode.com):** `estimate_gas()` hits stale load-balanced nodes after swap. Fix: hardcode gas limits — approve 100k, swap 300k, transfer 100k. Pass `"gas"` key *inside* `build_transaction()` dict, not after. Also: 3s sleep + balanceOf retry loop + 3× transfer retry in `buy_cbbtc()`.
- **State persistence:** Save state before buy execution so failed cycles carry drip forward correctly.
- **Notion logging retired:** Replaced with `file_logger.py` writing to local CSV + MD files on VPS.
- **DAILY_DRIP:** Never set directly — auto-derives from `MONTHLY_BUDGET`. No `/set daily_drip`.
- **Claude Code git discipline:** Always include "Commit all changes and push to GitHub origin main" in prompts. Omitting this leaves changes local only.
- **CVE-2026-31431 "Copy Fail" (May 2026):** Local privilege escalation in kernel `algif_aead` module. Mitigation applied: `algif_aead` blacklisted via `/etc/modprobe.d/disable-algif.conf`. Safe — does not affect dca-bot. Once Debian releases patched kernel: `sudo apt update && sudo apt upgrade -y`, reboot, then remove `/etc/modprobe.d/disable-algif.conf`. Track: https://security-tracker.debian.org/tracker/CVE-2026-31431

---

## NOTION IDs (documentation only — never scan/search workspace)

| Page | ID |
|------|----|
| Smart DCA Bot main | `32ffae9c-5f32-8041-adfc-d3b308521f9e` |
| Purchase Ledger DB | `b95b8e65-19e0-4474-b16e-75fac7525189` |
| Daily Buy Log | `330fae9c-5f32-81fc-929e-d584fa99cd38` |
| Useful Prompts | `334fae9c-5f32-8109-84ce-d4005073881d` |

---

## SESSION NOTES
> Append timestamped notes during each session. Clear when stale.

- 1 Jun 2026: Updated multiplier tiers — new config: 0.5× (<0.35), 1.0× (0.35–0.64), 4.0× (0.65–0.79), 8.0× (≥0.80). NO_BUY_ZONE set to False. Strategy: flat daily DCA + heavy reserve deployment on high-conviction signals. Deployed to VPS via git push/pull.
