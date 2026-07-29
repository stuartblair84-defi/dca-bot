# CLAUDE.md — SMART DCA BOT CONTEXT BRIDGE
> Last updated: 29 July 2026
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
| Local path | `C:\Users\stuar\StuartOS\50-personal\Personal Projects\dca-bot\` (moved into the vault 28 Jul 2026; the old `C:\Projects\dca-bot\` no longer exists) |

**Hot wallet:** `0xd1F1a36B423Ea05e47fCB50F0b86fC5Dc3be3380` (Base)
**Cold wallet:** `0xdBBB6ed92BDc8aFDfE8295b8504A73305d0ef8C0` (Base)

---

## FILE MAP

```
config.py          — all settings, budgets, thresholds, addresses
signals.py         — F&G (Alternative.me) + RSI/MA200/liq proxy (Kraken OHLCV)
dca_engine.py      — composite score, multiplier, pool/reserve logic
state.py           — state.json r/w, month rollover, cumulative reserve carryover
base_client.py     — RPC rotation/retry, Uniswap V3 approve → swap → transfer
portfolio.py       — purchases.json, VWAP avg entry, unrealised PnL
file_logger.py     — local CSV + MD logging (replaced Notion logger)
telegram_bot.py    — short-poll, all /commands
run_bot.py         — daily scheduler, run_once(), run_with_retry(), run_daemon()
```

**Notes and docs:**
```
README.md                        — what the bot is, how the decision works. Start here
notes/operations.md              — day-to-day VPS commands: health, ledgers, deploys, deposits
notes/smart-dca-logic-brief.md   — strategy thinking behind the scoring model
```

> [!note] One project, one folder (29 Jul 2026)
> An empty `Personal Projects/Smart DCA Bot/` used to sit alongside this repo, and the
> vault's routing table listed both, so half the time a session hunting for the DCA work
> landed in an empty directory. It is gone. Everything lives here, notes included, per
> `50-personal/CLAUDE.md`: code in the repo, thinking in `notes/`, not copied out.

**Runtime files (VPS only, gitignored):**
```
~/dca-bot/state.json            — base_pool, reserve_pool, month_spent, paused
~/dca-bot/purchases.json        — full purchase history (source for /report)
~/dca-bot/purchase_ledger.csv   — CSV log of all buys
~/dca-bot/daily_buy_log.md      — markdown log of all buys
~/dca-bot/funding_ledger.csv    — deposit history
~/dca-bot/python/.env           — BASE_RPC_URLS, EVM_PRIVATE_KEY, COLD_WALLET, Telegram token
```

---

## CURRENT CONFIG (`config.py`) — updated 18 June 2026

```python
MONTHLY_BUDGET        = 2000.0    # descriptive/reporting only — NOT enforced as a buy gate
RESERVE_PCT           = 0.40
DAILY_DRIP            = MONTHLY_BUDGET * (1 - RESERVE_PCT) / 30  # ~$40/day
POOL_CAP_X            = 8.0       # base pool ceiling = $320 — ALSO caps any single buy at $320
USE_RESERVE           = True
RESERVE_THRESHOLD     = 0.65
RESERVE_MAX_MONTHS    = 6         # reserve ceiling = $4,800
NO_BUY_ZONE           = False     # always buys, even on low scores
NO_BUY_THRESHOLD      = 0.35     # dead config while NO_BUY_ZONE = False
DRY_RUN               = False
CYCLE_RETRY_ATTEMPTS  = 3         # cycle re-attempts, only when nothing was broadcast
CYCLE_RETRY_DELAY_MIN = 15        # minutes between cycle attempts
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
6. PRE-FLIGHT: fetch hot wallet USDC balance on-chain
       if balance < total_spend: skip cycle entirely (drip carries forward)
       if balance >= total_spend: execute at full intended size
```

> **Buy gate (as of 18 Jun 2026):** Hot wallet USDC balance, not monthly budget.
> MONTHLY_BUDGET is no longer enforced anywhere in the buy decision path — it is kept
> purely for reserve top-up math (state.py) and display (/status, /config).
> The bot will spend as long as the hot wallet has funds, regardless of month-to-date total.

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

> ⚠️ **Resolved 29 Jul 2026 — read before changing POOL_CAP_X again.**
> `calc_buy_amount()` computes `target = min(DAILY_DRIP × multiplier, DAILY_DRIP × POOL_CAP_X)`,
> so `POOL_CAP_X` silently caps every single buy as well as the pool ceiling. At `5.0` it
> clipped the top tier to $200 and the 8× multiplier behaved as 5×, which this table claimed
> wrongly for months. Raised to `8.0` the same day, so $320 above is now reachable and true.
> The 4× tier was never affected ($160 < either cap).
>
> The number that actually moved is reserve burn. `NO_BUY_ZONE` is False, so the bot buys
> daily and `base_pool` rarely accumulates past one day's drip. A top-tier day therefore
> draws ~$280 from reserve rather than ~$160. At a $4,800 reserve ceiling that is roughly
> 15 sustained extreme-fear days of ammunition instead of 24. Deliberate, not incidental.
>
> Verified against config.py + dca_engine.py + state.py, not assumed.

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

> ⚠️ **`funding_ledger.csv` on the VPS is the source of truth, not this table.**
> This table drifted two deposits behind between May and July 2026 because it is
> hand-maintained and nothing reconciles it. Read it for orientation, never for a number.
> `/funding` on Telegram computes from the CSV and is always current.

| Date | Amount | Notes |
|------|--------|-------|
| 2026-03-26 | $10.00 | Initial seed |
| 2026-03-27 | $91.00 | Top-up |
| 2026-04-01 | $2,000.00 | April funding |
| 2026-05-01 | $1,000.00 | May top-up |
| 2026-06-01 | $1,000.00 | June top-up |
| 2026-07-03 | $2,000.00 | July top-up — logged late on 29 Jul, verified on-chain |
| **Total** | **$6,101.00** | as at 29 Jul 2026 |

> [!warning] `log_deposit()` stamps today's date, not the deposit's
> `file_logger.log_deposit()` writes `datetime.now()` into the `date` column, so a
> deposit logged any day after it landed is filed under the wrong date. The July row
> above was appended by hand with the real on-chain date for that reason. Worth fixing
> by giving `log_deposit()` an optional date argument.

---

## KNOWN FIXES & KEY LEARNINGS

- **RPC stale node (publicnode.com):** `estimate_gas()` hits stale load-balanced nodes after swap. Fix: hardcode gas limits — approve 100k, swap 300k, transfer 100k. Pass `"gas"` key *inside* `build_transaction()` dict, not after. Also: 3s sleep + balanceOf retry loop + 3× transfer retry in `buy_cbbtc()`.
- **State persistence:** Save state before buy execution so failed cycles carry drip forward correctly. Also applies to wallet-balance skip: drip is persisted before the balance check, so skipped days carry forward automatically.
- **Notion logging retired:** Replaced with `file_logger.py` writing to local CSV + MD files on VPS.
- **DAILY_DRIP:** Never set directly — auto-derives from `MONTHLY_BUDGET`. No `/set daily_drip`.
- **Monthly budget gate removed (Jun 2026):** MONTHLY_BUDGET was silently blocking buys after aggressive 8× spending stretch exhausted the $2,000 cap mid-month. Fixed: `dca_engine.calc_buy_amount()` no longer caps against remaining monthly budget. New live gate is hot wallet USDC balance, checked on-chain immediately before each swap. If balance < intended buy: cycle skips (drip carries), Telegram alert fires. Log messages now state the precise skip reason.
- **No-buy log messages:** Each skip reason now logs distinctly — "No buy: insufficient hot wallet balance ($X available, $Y needed)", "No buy: pool empty — base $X, reserve $Y, score Z", "No buy: composite score below NO_BUY_THRESHOLD (if NO_BUY_ZONE enabled)".
- **RPC provider rotation (Jul 2026):** A single hardcoded `BASE_RPC_URL` meant any endpoint-level fault killed the day's buy. Now `BASE_RPC_URLS` holds a comma-separated priority list; `base_client._rpc_call()` retries the current endpoint once, then rotates, exhausting the list before raising an error naming every endpoint and its status. Selection is **sticky** — once one answers the bot stays on it, so a known-blocked primary is not re-probed before every call. Rotation swaps `w3.provider` on the live `Web3` instance (web3 7.14.1 exposes `provider` as a property whose setter reassigns `manager.provider`), so the module-level contract objects follow automatically. Only transport faults rotate — HTTP {403,429,5xx}, connection errors, timeouts, `ProviderConnectionError`, and JSON-RPC rate-limit codes. Contract reverts and insufficient-funds errors propagate immediately, because retrying those on four endpoints just fails four times.
- **Writes never rotate silently — the double-spend trap:** If `send_raw_transaction` fails at the transport level the tx may already be in a mempool. Re-signing or re-fetching the nonce there would create a SECOND valid transaction and could buy twice. `_resolve_ambiguous_broadcast()` instead uses the fact that the signed tx hash is known *before* broadcast: rotate to a healthy endpoint, poll `eth_getTransactionReceipt` for that exact hash for 90s, and only then re-broadcast the **identical signed bytes** (idempotent — same nonce, same hash). If still unresolved it raises `AmbiguousBroadcastError` carrying the hash so the alert says "verify on Basescan" rather than the bot guessing. The pre-existing nonce-too-low retry is untouched and is safe, because a JSON-RPC rejection means the node received and rejected it.
- **Cycle retry gated on broadcast (Jul 2026):** `run_with_retry()` re-attempts up to `CYCLE_RETRY_ATTEMPTS` times, `CYCLE_RETRY_DELAY_MIN` apart, **only when nothing was broadcast**. If anything hit the wire it stops immediately regardless of attempts left. Only the final attempt alerts; intermediate ones log at WARNING, because three alerts for one bad morning trains you to ignore the channel.
- **Drip is idempotent per UTC day:** `state.drip_pool()` was unconditional, so the new cycle retry would have dripped the pool 3× on a bad morning. Now guarded by a `last_drip_date` field in state.json. Side effect, and the correct behaviour: a manual `python run_bot.py` on a day the scheduler already ran no longer adds a second drip.
- **RPC URLs are redacted in logs and alerts:** the Alchemy primary carries an API key in its path. `base_client._redact()` strips it before anything reaches journalctl or Telegram.
- **POOL_CAP_X caps single buys, not just the pool:** `calc_buy_amount()` clips every buy at `DAILY_DRIP × POOL_CAP_X`, so this one constant governs both the `base_pool` ceiling in `state.py` and the maximum any single purchase can reach. At `5.0` it silently clipped the 8× tier to $200 and this file documented $320 wrongly for months. Raised to `8.0` on 29 Jul 2026 so the top tier is real. If it is ever changed again, the multiplier tiers change with it whether or not that was the intent, and the reserve burn rate on high-conviction days moves with it too.
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
- 29 Jul 2026: **Cycle failed, no buy.** `403 Client Error: Forbidden for url: https://base-rpc.publicnode.com/` raised by web3's HTTPProvider, caught by the blanket `except Exception` in `run_once()`, alerted via `send_cycle_error_alert()`. Not rate limiting — 403 is an IP-level block from PublicNode's edge and will recur until `BASE_RPC_URL` changes. Endpoint confirmed healthy from a different network the same morning, so it is the VPS source IP, not an outage. Drip persisted correctly (state saved before buy attempt), so the day carries forward. **FIXED same day** — all three open issues closed:
  - (a) `BASE_RPC_URLS` priority list + sticky retry/rotate in `base_client._rpc_call()`, wrapping every read (`balanceOf`, `allowance`, `slot0`, quoter, `get_block`, `max_priority_fee`, `getTransactionCount`, `estimateGas`, receipts). Writes deliberately excluded — see the double-spend note in KNOWN FIXES.
  - (b) `run_bot.run_with_retry()` — 3 attempts, 15 min apart, gated on nothing having been broadcast. Scheduler and the default CLI entry point now call it; `--no-retry` gives the old single-attempt behaviour.
  - (c) `send_cycle_error_alert()` now takes `stage`, `broadcast`, `confirmed` and `tx_hash`, and reports three distinct outcomes (nothing broadcast / broadcast+confirmed / broadcast+unknown). `run_once()` tracks a stage through `state → signals → engine → preflight_balance → approve → swap → post_swap_read → transfer → record → summary` and returns an outcome dict.
  - Endpoints verified live by `eth_chainId == 0x2105`: `mainnet.base.org`, `base.drpc.org`, `base.meowrpc.com`, `1rpc.io/base`, `base-mainnet.public.blastapi.io` all PASS. **`base.llamarpc.com` returns HTTP 521 and was dropped** from the proposed rotation list. `base-rpc.publicnode.com` answered fine from a home network, confirming the 403 is specific to the VPS source IP.
  - Tested with `DRY_RUN=True`: full cycle; rotation past a 403 + dead-DNS head endpoint; total exhaustion (error names all four endpoints and statuses, alert reports `preflight_balance` with no broadcast); 3-attempt retry with exactly one drip and one alert; and two ambiguous-broadcast cases proving the tx is signed exactly once. `DRY_RUN` returned to `False`.
  - Also fixed: an `UnboundLocalError` in `_rpc_call` (missing `global _active_idx`), and the config comment claiming a ~$50 pool ceiling when it is $200.
- 18 Jun 2026: Replaced monthly-budget buy gate with hot-wallet USDC balance check. Root cause: 8× spending Jun 2–6 exhausted $2k cap on Jun 13; bot sat out Jun 14–17 despite scores 0.53–0.62. Fix: removed MONTHLY_BUDGET cap from dca_engine.calc_buy_amount(); added on-chain balance preflight in run_bot.run_once() before each swap; added send_low_balance_alert() Telegram alert; fixed all no-buy log messages to state exact reason.
