# Operations

Day-to-day commands for running and inspecting the bot on the VPS. All read-only unless marked otherwise.

**Target:** `stu1984@100.74.164.1`, repo at `~/dca-bot`, systemd service `dca-bot`, cycle at 00:20 UTC.

> [!danger] Never run `python base_client.py` on the VPS
> With `DRY_RUN=False` its `__main__` block executes a live $1 buy. There is no confirmation prompt.

## Health

```bash
systemctl status --no-pager dca-bot
journalctl -u dca-bot --since "today" --no-pager
journalctl -u dca-bot -f                    # follow live, Ctrl-C to stop
```

Healthy startup logs `Daemon mode -- scheduled daily at 00:20 UTC DRY_RUN=False` followed by a `Next run:` line.

## Which RPC endpoint is live

```bash
cd ~/dca-bot/python && ~/dca-bot/venv/bin/python -c "import base_client as b; print('endpoint:', b.active_rpc_url()); print('USDC hot :', b.get_usdc_balance()); print('cbBTC hot:', b.get_cbbtc_balance())"
```

Read-only. Spends nothing, broadcasts nothing, starts no cycle. The endpoint prints redacted, so it is safe to paste into a chat or an issue.

Rotation warnings in the output mean the primary is unhealthy and a fallback is carrying the load. `RPCExhaustedError` means every endpoint failed, and the error names each one and its status.

## Reading the ledgers

```bash
cd ~/dca-bot

# Last 10 buys, key columns only (the CSV is 13 wide)
{ head -1 purchase_ledger.csv; tail -10 purchase_ledger.csv; } \
  | cut -d, -f1,2,4,5,6,7,8,9,12 | column -s, -t

# Full width including tx hashes, last 5
{ head -1 purchase_ledger.csv; tail -5 purchase_ledger.csv; } | column -s, -t

# Totals from the raw ledger, independent of /report and purchases.json
awk -F, 'NR>1 {n++; usd+=$4; btc+=$5} END {
  printf "buys      : %d\ninvested  : $%.2f\ncbBTC     : %.8f\nVWAP      : $%.2f\n", n, usd, btc, usd/btc}' purchase_ledger.csv

# Failed cold-wallet sweeps — should print the header and nothing else
awk -F, 'NR==1 || $12!="True"' purchase_ledger.csv | column -s, -t

# Funding, date-sorted, plus total
{ head -1 funding_ledger.csv; tail -n +2 funding_ledger.csv | sort -t, -k1; } | column -s, -t
awk -F, 'NR>1 {t+=$2} END {printf "total funded: $%.2f\n", t}' funding_ledger.csv

# Most recent buy, formatted
tail -20 daily_buy_log.md
sed -n '/^## Buy #81 /,/^---$/p' daily_buy_log.md    # one specific buy
```

The totals command is the one worth reaching for first. It recomputes from raw ledger rows rather than from `purchases.json`, so a disagreement with `/report` on Telegram is a real discrepancy worth chasing.

> [!warning] Two reading traps
> `daily_buy_log.md` rounds the composite score to 2dp, so a `0.65` next to a `1.0×` multiplier is really `0.6498` and correct. Trust the CSV for anything score-related.
>
> `funding_ledger.csv` stores rows in append order, not date order, so a later top-up can sit above an earlier one. Always sort before reading it.

## Logging a deposit

Deposits are not detected automatically. After funding the hot wallet, append a row:

```bash
cd ~/dca-bot
cp funding_ledger.csv funding_ledger.csv.bak-$(date +%Y%m%d)
echo "YYYY-MM-DD,AMOUNT,0xTXHASH,notes" >> funding_ledger.csv
```

> [!warning] Do not use `log_deposit()` for a backdated entry
> `file_logger.log_deposit()` stamps `datetime.now()` into the date column rather than the deposit's actual date, so anything logged after the day it landed is filed wrongly. Append by hand with the real on-chain date until that function takes an optional date argument.

## Copying the data files locally

They are gitignored, so git will not move them. From PowerShell in the local repo:

```powershell
scp "stu1984@100.74.164.1:~/dca-bot/*.csv" .
scp "stu1984@100.74.164.1:~/dca-bot/*.json" .
scp "stu1984@100.74.164.1:~/dca-bot/daily_buy_log.md" .
```

A snapshot, not a sync. Never copy `python/.env`: it holds the hot wallet private key, which belongs on the VPS only.

## Deploying a code change

```bash
# local: commit and push, then on the VPS
cd ~/dca-bot
git status          # STOP if any file under python/ is modified — it was patched on the box
git pull origin main
sudo systemctl restart dca-bot
```

Then re-run the endpoint check above. Runtime data files are untracked, so a pull cannot touch them; fingerprint with `md5sum` before and after if you want proof rather than reassurance.

## Known operational limits

| Thing | Detail |
|---|---|
| Buy gate | Hot wallet USDC balance, checked on-chain immediately before each swap. Not the monthly budget |
| Hot wallet runway | At the 8× tier a single day targets $320, so the hot wallet is the binding constraint, not the reserve. A low-balance Telegram alert means top up, not that something broke |
| Deposit detection | None. Funding rows are manual |
| Cold sweep failure | The swap still counts and is recorded. The cbBTC sits in the hot wallet until swept manually |
