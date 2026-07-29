# Smart DCA Bot

A dollar-cost-averaging bot that buys cbBTC on Base mainnet once a day, sizing each
purchase from a weighted composite of three market signals rather than buying a flat
amount. Every purchase is swept straight from the hot wallet to a cold wallet, so the
key the bot holds only ever guards working capital.

Deep operational context — VPS details, incident history, known fixes — lives in
[CLAUDE.md](CLAUDE.md), which is the source of truth. This file explains how the thing
works.

## How the decision works

Each cycle scores three signals from 0 to 1, where 1 means "maximum fear, buy more".

| Signal | Weight | Source |
|---|---|---|
| `fear_greed` | 0.35 | Alternative.me Fear & Greed index |
| `rsi` | 0.40 | Kraken daily OHLCV — RSI-14 with an MA200 modifier |
| `liquidation` | 0.25 | Derived from the same OHLCV — volume spike + price drop proxy |

The weighted sum is the **composite score**. That score maps to a spend multiplier
applied to the daily drip (`DAILY_DRIP`, currently $40/day: 60% of a $2,000 monthly
budget spread over 30 days).

| Composite score | Multiplier | Target spend | Reserve released? | Max spend |
|---|---|---|---|---|
| 0.00 – 0.34 | 0.5× | $20 | No | $20 |
| 0.35 – 0.64 | 1.0× | $40 | No | $40 |
| 0.65 – 0.79 | 4.0× | $160 | Yes | $160 |
| ≥ 0.80 | 8.0× | $320 | Yes | $320 |

One non-obvious thing about that table: `POOL_CAP_X` caps any single purchase at
`POOL_CAP_X × DAILY_DRIP`, not just the pool it is named after. It is set to `8.0` so
the top tier is reachable. At its previous value of `5.0` the 8× multiplier silently
behaved as 5×, because the cap wins over the multiplier and nothing warned about it.
Change that constant and the tier table changes with it.

The intent is a flat daily DCA most of the time, with a step change on high conviction.

**Pools.** Each cycle drips `DAILY_DRIP` into a `base_pool` that accumulates on days the
bot does not spend it, capped at $320. The remaining 40% of the monthly budget accrues
into a `reserve_pool` that is topped up on each month rollover, never resets, and is
capped at six months' worth. The reserve only unlocks when the composite score clears
`RESERVE_THRESHOLD` (0.65) — deliberately the same threshold as the 4× tier.

Spending draws `base_pool` first, then covers any shortfall from `reserve_pool`.

## The buy gate

**The live gate is the hot wallet's on-chain USDC balance**, read immediately before
each swap. If the balance is below the intended purchase, the cycle skips entirely, the
drip carries forward, and a Telegram alert fires.

`MONTHLY_BUDGET` is descriptive only. It is **not** a buy gate. It feeds the reserve
top-up maths and the `/status` and `/config` displays, and nothing else. It used to gate
buys, and in June 2026 that silently blocked four days of purchases after an aggressive
run exhausted the monthly cap mid-month. Do not reintroduce it as a gate without
understanding that.

## Architecture

| Module | Responsibility |
|---|---|
| [config.py](python/config.py) | Every setting: budgets, thresholds, tiers, addresses, retry knobs. No secrets. |
| [signals.py](python/signals.py) | Fetches and normalises the three signals to 0–1 scores. |
| [dca_engine.py](python/dca_engine.py) | Pure logic — composite score, multiplier, buy amount. No I/O. |
| [state.py](python/state.py) | `state.json` read/write, month rollover, pool drip, reserve carryover. |
| [base_client.py](python/base_client.py) | All chain interaction: RPC rotation, approve, swap, cold-wallet sweep. |
| [portfolio.py](python/portfolio.py) | `purchases.json`, VWAP average entry, unrealised P&L. |
| [file_logger.py](python/file_logger.py) | Appends each buy to a local CSV and markdown ledger. |
| [telegram_bot.py](python/telegram_bot.py) | Command polling and all outbound alerts. |
| [run_bot.py](python/run_bot.py) | Cycle orchestration, stage tracking, cycle retry, daily scheduler. |

### RPC resilience

`BASE_RPC_URLS` is a comma-separated, priority-ordered endpoint list. Read calls retry
the current endpoint once, then rotate to the next, exhausting the whole list before
raising an error that names every endpoint tried and what each returned. Selection is
sticky — once an endpoint answers, the bot stays on it rather than re-probing a known
bad primary before every call.

Writes never rotate silently. If `send_raw_transaction` fails at the transport level the
outcome is genuinely unknown, so the bot does **not** re-sign or re-fetch the nonce —
that would risk a second valid transaction. It polls for the already-known transaction
hash on a healthy endpoint, and only re-broadcasts the identical signed bytes, which is
idempotent because the nonce is unchanged. If it still cannot tell, it raises and says to
check Basescan rather than guessing.

A failed cycle is retried up to `CYCLE_RETRY_ATTEMPTS` times, `CYCLE_RETRY_DELAY_MIN`
minutes apart, **only** when nothing was broadcast. If anything reached the wire the
cycle stops immediately regardless of attempts remaining. The pool drip is idempotent
per UTC day, so retries cannot inflate it.

## Setup

Python 3.13.

```
python -m venv venv
venv/Scripts/activate          # source venv/bin/activate on Linux
pip install -r requirements.txt
```

Copy [.env.example](.env.example) to `python/.env` and fill in:

| Key | Purpose |
|---|---|
| `BASE_RPC_URLS` | Comma-separated, priority-ordered Base RPC endpoints. Put a keyed endpoint first. |
| `BASE_RPC_URL` | Legacy single endpoint. Still honoured as a fallback. |
| `EVM_PRIVATE_KEY` | Hot wallet key. |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Alerting and commands. |

Run:

```
python run_bot.py             # one cycle, with retry, then exit
python run_bot.py --no-retry  # one cycle, single attempt (debugging)
python run_bot.py --daemon    # scheduler loop (production)
```

## Deployment

Runs on the VPS under systemd as `dca-bot`, in daemon mode, firing daily at `00:20 UTC`
(`EXECUTION_TIME_UTC`).

```
sudo systemctl restart dca-bot
systemctl status dca-bot
journalctl -u dca-bot --since "today" --no-pager
```

Runtime files (`state.json`, `purchases.json`, the CSV and markdown ledgers, and
`python/.env`) live on the VPS and are gitignored. The VPS copies are authoritative.

## Telegram commands

| Command | Description |
|---|---|
| `/status` | Pool, reserve, month spent, next run |
| `/config` | Live config values |
| `/signals` | Current signal scores and composite |
| `/report` | Portfolio summary — VWAP, P&L, all buys |
| `/balance` | Live on-chain USDC (hot) and cbBTC (hot + cold) |
| `/funding` | Deposit history, total in, total spent, implied balance |
| `/pause` | Pause buying cycles |
| `/resume` | Resume buying cycles |
| `/help` | All commands |

## Safety notes

`DRY_RUN` in [config.py](python/config.py) prints every step and broadcasts nothing. Set
it to `True` before testing anything that touches the buy path, and check it is back to
`False` before deploying.

The hot and cold wallets are deliberately split. `EVM_PRIVATE_KEY` is a **hot wallet
key**: it holds only enough USDC to fund upcoming buys plus gas, and every cbBTC
purchase is transferred out to the cold wallet in the same cycle. Compromising the key
should cost working capital, not the position. Fund the hot wallet in deliberate top-ups
rather than parking months of budget in it — the buy gate is that balance, so whatever
sits there is what the bot is willing to spend.
