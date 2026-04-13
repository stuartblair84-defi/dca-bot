# ─────────────────────────────────────────────
#  Smart DCA Bot — base_client.py
#  cbBTC buy flow on Base mainnet:
#    USDC approval → Uniswap V3 swap → transfer to cold wallet
#
#  Requires: python/.env  with BASE_RPC_URL and EVM_PRIVATE_KEY
#  DRY_RUN = True (config.py) → prints every step, broadcasts nothing.
#
#  get_quote() strategy:
#    1. Try QuoterV2 (quoteExactInputSingle) — requires simulation-capable RPC
#    2. Fall back to pool slot0 sqrtPriceX96 spot price (works on any RPC)
# ─────────────────────────────────────────────

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("dca-bot")

from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import Web3RPCError
from eth_account import Account

# .env lives in the same folder as this script (python/.env)
load_dotenv(Path(__file__).parent / ".env")

# ── Import config after .env is loaded ───────
from config import (
    CBBTC_ADDRESS, USDC_ADDRESS,
    UNISWAP_V3_ROUTER, QUOTER_V2, CBBTC_USDC_POOL,
    HOT_WALLET, COLD_WALLET,
    CBBTC_DECIMALS, USDC_DECIMALS,
    POOL_FEE, CHAIN_ID, DRY_RUN,
)


# ── Minimal ABIs ──────────────────────────────

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]

# SwapRouter02: exactInputSingle (for ABI encoding) + multicall with deadline
SWAP_ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType": "address",  "name": "tokenIn",            "type": "address"},
                {"internalType": "address",  "name": "tokenOut",           "type": "address"},
                {"internalType": "uint24",   "name": "fee",                "type": "uint24"},
                {"internalType": "address",  "name": "recipient",          "type": "address"},
                {"internalType": "uint256",  "name": "amountIn",           "type": "uint256"},
                {"internalType": "uint256",  "name": "amountOutMinimum",   "type": "uint256"},
                {"internalType": "uint160",  "name": "sqrtPriceLimitX96",  "type": "uint160"},
            ],
            "internalType": "struct IV3SwapRouter.ExactInputSingleParams",
            "name": "params", "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256",  "name": "deadline", "type": "uint256"},
            {"internalType": "bytes[]",  "name": "data",     "type": "bytes[]"},
        ],
        "name": "multicall",
        "outputs": [{"internalType": "bytes[]", "name": "", "type": "bytes[]"}],
        "stateMutability": "payable", "type": "function",
    },
]

# QuoterV2: quoteExactInputSingle — requires simulation-capable RPC
QUOTER_V2_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType": "address",  "name": "tokenIn",           "type": "address"},
                {"internalType": "address",  "name": "tokenOut",          "type": "address"},
                {"internalType": "uint256",  "name": "amountIn",          "type": "uint256"},
                {"internalType": "uint24",   "name": "fee",               "type": "uint24"},
                {"internalType": "uint160",  "name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
            "name": "params", "type": "tuple",
        }],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut",               "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After",       "type": "uint160"},
            {"internalType": "uint32",  "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate",             "type": "uint256"},
        ],
        "stateMutability": "nonpayable", "type": "function",
    },
]

# Uniswap V3 pool: slot0 only (for spot-price fallback)
POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96",            "type": "uint160"},
            {"internalType": "int24",   "name": "tick",                    "type": "int24"},
            {"internalType": "uint16",  "name": "observationIndex",        "type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinality",  "type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8",   "name": "feeProtocol",             "type": "uint8"},
            {"internalType": "bool",    "name": "unlocked",                "type": "bool"},
        ],
        "stateMutability": "view", "type": "function",
    },
]


# ── Web3 + account setup ──────────────────────

_rpc_url = os.getenv("BASE_RPC_URL")
_raw_key = os.getenv("EVM_PRIVATE_KEY")

if not _rpc_url:
    sys.exit("ERROR: BASE_RPC_URL not set in python/.env")

w3 = Web3(Web3.HTTPProvider(_rpc_url))

# account is only needed for live txs
account = None
if _raw_key:
    account = Account.from_key(_raw_key)
elif not DRY_RUN:
    sys.exit("ERROR: EVM_PRIVATE_KEY not set — required when DRY_RUN=False")

# Checksummed addresses
_HOT    = Web3.to_checksum_address(HOT_WALLET)
_COLD   = Web3.to_checksum_address(COLD_WALLET)
_USDC   = Web3.to_checksum_address(USDC_ADDRESS)
_CBBTC  = Web3.to_checksum_address(CBBTC_ADDRESS)
_ROUTER = Web3.to_checksum_address(UNISWAP_V3_ROUTER)
_QUOTER = Web3.to_checksum_address(QUOTER_V2)
_POOL   = Web3.to_checksum_address(CBBTC_USDC_POOL)

usdc_contract  = w3.eth.contract(address=_USDC,   abi=ERC20_ABI)
cbbtc_contract = w3.eth.contract(address=_CBBTC,  abi=ERC20_ABI)
router         = w3.eth.contract(address=_ROUTER, abi=SWAP_ROUTER_ABI)
quoter         = w3.eth.contract(address=_QUOTER, abi=QUOTER_V2_ABI)
pool           = w3.eth.contract(address=_POOL,   abi=POOL_ABI)


# ── Helpers ───────────────────────────────────

def _usdc_to_raw(amount_usd: float) -> int:
    return int(amount_usd * 10 ** USDC_DECIMALS)

def _cbbtc_from_raw(raw: int) -> float:
    return raw / 10 ** CBBTC_DECIMALS

def _spot_price_from_slot0() -> float:
    """Compute cbBTC-per-USDC spot price directly from pool sqrtPriceX96.

    In the cbBTC/USDC pool (USDC=token0, cbBTC=token1):
        price_raw  = sqrtPriceX96^2 / 2^192        (cbBTC_raw per USDC_raw)
        price_human = price_raw * 10^USDC_DECIMALS / 10^CBBTC_DECIMALS
    Returns cbBTC per 1 USDC (human-readable).
    """
    sqrt_price_x96 = pool.functions.slot0().call()[0]
    Q192 = 2 ** 192
    price_raw   = (sqrt_price_x96 ** 2) / Q192       # cbBTC_raw per USDC_raw
    price_human = price_raw * (10 ** USDC_DECIMALS) / (10 ** CBBTC_DECIMALS)
    return price_human

def _build_eip1559_tx(
    contract_fn,
    value_wei: int = 0,
    nonce: int | None = None,
    gas_limit: int | None = None,
) -> dict:
    """Build an EIP-1559 tx dict with 20% gas buffer.

    Pass nonce explicitly to avoid re-fetching from the node when chaining
    multiple transactions in one buy cycle (node may not have indexed prior
    txs yet, causing 'nonce too low' on the next tx).

    Pass gas_limit to bypass estimate_gas() entirely and use a fixed ceiling
    instead. Useful when the RPC is load-balanced and a different node may be
    one block behind, causing the simulation to revert on stale state.
    """
    if nonce is None:
        nonce = w3.eth.get_transaction_count(account.address, "pending")

    latest       = w3.eth.get_block("latest")
    base_fee     = latest["baseFeePerGas"]
    max_priority = w3.eth.max_priority_fee
    max_fee      = base_fee * 2 + max_priority

    tx = contract_fn.build_transaction({
        "from":                 account.address,
        "nonce":                nonce,
        "type":                 2,
        "chainId":              CHAIN_ID,
        "value":                value_wei,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": max_priority,
    })

    if gas_limit is not None:
        tx["gas"] = gas_limit
    else:
        # Estimate gas and apply 20% buffer
        gas_est  = w3.eth.estimate_gas({"from": account.address,
                                        "to":   tx["to"],
                                        "data": tx["data"],
                                        "value": value_wei})
        tx["gas"] = int(gas_est * 1.2)
    return tx

def _sign_and_send(tx: dict) -> str:
    """Sign and broadcast a transaction. Retries once on nonce-too-low."""
    signed = account.sign_transaction(tx)
    try:
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Web3RPCError as e:
        if "nonce too low" in str(e).lower():
            tx["nonce"] = w3.eth.get_transaction_count(account.address, "pending")
            signed  = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        else:
            raise
    return tx_hash.hex()


# ── Public API ────────────────────────────────

def get_usdc_balance() -> float:
    """Return hot wallet USDC balance in human-readable USD."""
    raw = usdc_contract.functions.balanceOf(_HOT).call()
    return raw / 10 ** USDC_DECIMALS


def get_cbbtc_balance() -> float:
    """Return hot wallet cbBTC balance."""
    raw = cbbtc_contract.functions.balanceOf(_HOT).call()
    return _cbbtc_from_raw(raw)


def check_and_approve_usdc(
    amount_raw: int,
    nonce: int | None = None,
) -> tuple[str | None, int | None]:
    """Check current allowance; approve only if insufficient.

    Returns (approve_tx_hash | None, next_nonce).
    next_nonce is the nonce the caller should use for its next transaction.
    Handles the zero-first reset pattern if a stale non-zero allowance exists.
    Does nothing on-chain in DRY_RUN mode.
    """
    allowance     = usdc_contract.functions.allowance(_HOT, _ROUTER).call()
    approve_amount = amount_raw + 1   # +1 raw unit buffer for fee-math rounding

    log.info(f"[approve] spender        : {_ROUTER}")
    log.info(f"[approve] current allowance: {allowance} raw = ${allowance / 10**USDC_DECIMALS:.6f} USDC")
    log.info(f"[approve] required       : {approve_amount} raw = ${approve_amount / 10**USDC_DECIMALS:.6f} USDC")

    if allowance >= amount_raw:
        log.info("[approve] allowance sufficient -- skipping")
        return None, nonce

    if DRY_RUN:
        log.info(f"[approve] DRY RUN -- would approve ${approve_amount / 10**USDC_DECIMALS:.6f} USDC to {_ROUTER}")
        return None, nonce

    next_nonce = nonce

    # Some ERC-20 implementations require zeroing a non-zero allowance before
    # setting a new value. Send a zero-approval first when that's the case.
    if allowance > 0:
        log.info(f"[approve] non-zero stale allowance ({allowance}) -- zeroing first")
        zero_tx   = _build_eip1559_tx(usdc_contract.functions.approve(_ROUTER, 0), nonce=next_nonce, gas_limit=100_000)
        zero_hash = _sign_and_send(zero_tx)
        log.info(f"[approve] zero-approval tx: {zero_hash}")
        zero_receipt = w3.eth.wait_for_transaction_receipt(zero_hash, timeout=60)
        if zero_receipt.status != 1:
            raise Exception(f"Zero-approval transaction reverted: {zero_hash}")
        log.info(f"[approve] zero-approval confirmed (block {zero_receipt.blockNumber})")
        if next_nonce is not None:
            next_nonce += 1

    tx      = _build_eip1559_tx(usdc_contract.functions.approve(_ROUTER, approve_amount), nonce=next_nonce, gas_limit=100_000)
    tx_hash = _sign_and_send(tx)
    log.info(f"[approve] tx: {tx_hash}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise Exception(f"Approval transaction reverted: {tx_hash}")

    actual = usdc_contract.functions.allowance(_HOT, _ROUTER).call()
    log.info(
        f"[approve] confirmed (block {receipt.blockNumber}), "
        f"on-chain allowance: {actual} raw = ${actual / 10**USDC_DECIMALS:.6f} USDC"
    )

    if next_nonce is not None:
        next_nonce += 1
    return tx_hash, next_nonce


def get_quote(usdc_amount_usd: float) -> tuple[float, str]:
    """Get expected cbBTC out for a given USDC input.

    Strategy:
      1. QuoterV2.quoteExactInputSingle  (exact, accounts for price impact)
      2. Pool slot0 spot price fallback  (spot price, fine for small DCA amounts)

    Returns (cbbtc_amount_float, source_label).
    """
    amount_raw = _usdc_to_raw(usdc_amount_usd)

    # -- Attempt 1: QuoterV2 --
    try:
        result = quoter.functions.quoteExactInputSingle({
            "tokenIn":           _USDC,
            "tokenOut":          _CBBTC,
            "amountIn":          amount_raw,
            "fee":               POOL_FEE,
            "sqrtPriceLimitX96": 0,
        }).call()
        cbbtc_out = _cbbtc_from_raw(result[0])
        if cbbtc_out > 0:
            return cbbtc_out, "QuoterV2"
    except Exception:
        pass

    # -- Attempt 2: pool slot0 spot price --
    price_per_usdc = _spot_price_from_slot0()   # cbBTC per 1 USDC
    cbbtc_out      = usdc_amount_usd * price_per_usdc
    return cbbtc_out, "slot0-spot"


def swap_usdc_to_cbbtc(usdc_amount_usd: float, slippage_bps: int = 50, nonce: int | None = None) -> str:
    """Build and broadcast exactInputSingle on SwapRouter02.

    Wraps the call in multicall(deadline, [data]) so the tx reverts
    if not mined within 5 minutes.
    Returns tx hash.
    """
    usdc_raw           = _usdc_to_raw(usdc_amount_usd)
    quoted_out, source = get_quote(usdc_amount_usd)
    quoted_raw         = int(quoted_out * 10 ** CBBTC_DECIMALS)
    min_out_raw        = int(quoted_raw * (1 - slippage_bps / 10_000))
    deadline           = w3.eth.get_block("latest")["timestamp"] + 300

    print(f"  [swap] {usdc_amount_usd:.2f} USDC -> ~{quoted_out:.8f} cbBTC "
          f"(min {_cbbtc_from_raw(min_out_raw):.8f}, slippage {slippage_bps}bps, src={source})")

    if DRY_RUN:
        print(f"  [swap] DRY RUN -- would call SwapRouter02.multicall(deadline+300s, [exactInputSingle])")
        return "0x" + "0" * 64

    swap_params = {
        "tokenIn":           _USDC,
        "tokenOut":          _CBBTC,
        "fee":               POOL_FEE,
        "recipient":         account.address,
        "amountIn":          usdc_raw,
        "amountOutMinimum":  min_out_raw,
        "sqrtPriceLimitX96": 0,
    }
    inner_calldata = router.encode_abi("exactInputSingle", args=[swap_params])
    # Bypass estimate_gas to avoid stale RPC state on load-balanced publicnode.com
    # nodes — same fix as approve and transfer. Uniswap V3 exactInputSingle via
    # multicall typically costs 150k-200k gas; 300k is a safe fixed ceiling.
    tx = _build_eip1559_tx(router.functions.multicall(deadline, [inner_calldata]), nonce=nonce, gas_limit=300_000)
    tx_hash = _sign_and_send(tx)
    print(f"  [swap] tx: {tx_hash}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise Exception(f"Swap transaction reverted: {tx_hash}")
    return tx_hash


def transfer_cbbtc_to_cold(amount_raw: int, nonce: int | None = None) -> str:
    """ERC-20 transfer of cbBTC from hot wallet to COLD_WALLET.

    Returns tx hash.
    """
    amount_human = _cbbtc_from_raw(amount_raw)
    print(f"  [transfer] {amount_human:.8f} cbBTC  {_HOT} -> {_COLD}")

    if DRY_RUN:
        print(f"  [transfer] DRY RUN -- would call cbBTC.transfer(cold_wallet, {amount_raw})")
        return "0x" + "0" * 64

    # Use a hardcoded gas limit to bypass estimate_gas(). The publicnode.com RPC
    # is load-balanced — estimate_gas() can hit a node that is one block behind,
    # sees zero cbBTC balance, and reverts the simulation before broadcast.
    # ERC-20 transfers cost ~65k gas; 100k is a safe fixed ceiling.
    tx = _build_eip1559_tx(
        cbbtc_contract.functions.transfer(_COLD, amount_raw),
        nonce=nonce,
        gas_limit=100_000,
    )
    tx_hash = _sign_and_send(tx)
    print(f"  [transfer] tx: {tx_hash}")
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return tx_hash


def buy_cbbtc(usdc_amount_usd: float) -> dict:
    """Full flow: balance check -> approve -> swap -> transfer to cold wallet.

    In DRY_RUN mode every step is printed but nothing is broadcast.
    Returns dict with tx hashes (or dry-run placeholders).
    """
    print(f"\n{'=' * 54}")
    print(f"  buy_cbbtc(${usdc_amount_usd:.2f})  "
          f"[{'DRY RUN' if DRY_RUN else 'LIVE'}]")
    print(f"{'=' * 54}")

    # 1. Balance check
    usdc_bal = get_usdc_balance()
    print(f"  [balance] USDC  : ${usdc_bal:.6f}")
    print(f"  [balance] cbBTC : {get_cbbtc_balance():.8f}")

    if usdc_bal < usdc_amount_usd and not DRY_RUN:
        raise ValueError(f"Insufficient USDC: have ${usdc_bal:.2f}, need ${usdc_amount_usd:.2f}")

    # 2. Quote
    quoted, source = get_quote(usdc_amount_usd)
    print(f"  [quote]  ${usdc_amount_usd:.2f} USDC = ~{quoted:.8f} cbBTC  (src={source})")

    # 3. Approve + Swap + Transfer — fetch nonce once with 'pending' so each
    #    successive tx in this cycle gets the correct sequential nonce even
    #    before the node has indexed the prior tx.
    usdc_raw = _usdc_to_raw(usdc_amount_usd)
    nonce    = w3.eth.get_transaction_count(account.address, "pending") if not DRY_RUN else 0

    # check_and_approve_usdc returns (hash | None, next_nonce) — next_nonce
    # accounts for 0, 1, or 2 txs (zero-reset + approve) so the nonce
    # sequence fed to swap and transfer is always correct.
    approve_hash, nonce = check_and_approve_usdc(usdc_raw, nonce=nonce)

    # 4. Swap
    swap_hash = swap_usdc_to_cbbtc(usdc_amount_usd, nonce=nonce)
    nonce += 1

    # 5. Determine quantity received.
    #    Always read actual on-chain balance after the swap is confirmed —
    #    never use the pre-swap quote, which may be higher than what was
    #    actually received due to slippage and fees.
    if DRY_RUN:
        cbbtc_raw = int(quoted * 10 ** CBBTC_DECIMALS)
    else:
        # Wait 3 seconds for the RPC cluster to propagate swap state before
        # reading balance or broadcasting the transfer.
        time.sleep(3)
        # Poll balanceOf up to 5 times with 2-second intervals until > 0.
        cbbtc_raw = 0
        for _poll in range(5):
            cbbtc_raw = cbbtc_contract.functions.balanceOf(account.address).call()
            if cbbtc_raw > 0:
                break
            if _poll < 4:
                log.info(f"[swap] balanceOf returned 0, retrying in 2s ({_poll + 1}/5) ...")
                time.sleep(2)

    qty   = _cbbtc_from_raw(cbbtc_raw)
    price = usdc_amount_usd / qty if qty > 0 else 0.0
    log.info(
        f"[swap] quoted {quoted:.8f} cbBTC, actual balance {qty:.8f} cbBTC "
        f"(delta {qty - quoted:+.8f})"
    )

    # 6. Transfer to cold wallet — caught here so a nonce or RPC failure
    #    after a successful swap does not prevent the caller from recording
    #    the purchase and updating state.
    #    Retry up to 3 times on "exceeds balance" (stale RPC pre-flight);
    #    any other error fails immediately.
    transfer_hash  = None
    transfer_error = None
    try:
        for attempt in range(1, 4):
            try:
                transfer_hash = transfer_cbbtc_to_cold(cbbtc_raw, nonce=nonce)
                break
            except Exception as exc:
                if "exceeds balance" in str(exc).lower() and attempt < 3:
                    log.warning(f"[transfer] retry {attempt}/3 after exceeds-balance — waiting 3s")
                    time.sleep(3)
                else:
                    raise
    except Exception as exc:
        transfer_error = str(exc)
        print(f"  [transfer] FAILED: {exc}")

    print(f"\n  {'DRY RUN complete -- no transactions broadcast' if DRY_RUN else 'Done.'}")
    print(f"{'=' * 54}\n")

    return {
        "approve_tx":     approve_hash,
        "swap_tx":        swap_hash,
        "transfer_tx":    transfer_hash,
        "transfer_error": transfer_error,
        "qty":            qty,
        "price":          price,
    }


# ── CLI test run ──────────────────────────────

if __name__ == "__main__":
    print(f"\nBase client -- RPC: {_rpc_url}")

    if not w3.is_connected():
        sys.exit("ERROR: cannot connect to Base RPC")

    chain_id = w3.eth.chain_id
    if chain_id != CHAIN_ID:
        sys.exit(f"ERROR: connected to chain {chain_id}, expected {CHAIN_ID} (Base mainnet)")

    print(f"Connected to Base mainnet (chain {chain_id})\n")

    # 1. Balances
    usdc_bal  = get_usdc_balance()
    cbbtc_bal = get_cbbtc_balance()
    print(f"Hot wallet USDC balance : ${usdc_bal:.6f}")
    print(f"Hot wallet cbBTC balance: {cbbtc_bal:.8f}")

    # 2. Quote for $10
    print(f"\nFetching quote: $10.00 USDC -> cbBTC ...")
    quote_10, source = get_quote(10.0)
    btc_price_implied = 10.0 / quote_10 if quote_10 > 0 else 0
    print(f"  $10.00 USDC  =>  {quote_10:.8f} cbBTC  (source: {source})")
    print(f"  Implied BTC price: ${btc_price_implied:,.2f}")

    # 3. Dry-run summary / live execution
    if DRY_RUN:
        q1        = quote_10 / 10.0
        min_out   = q1 * (1 - 50 / 10_000)
        print(f"\n{'=' * 54}")
        print(f"  DRY RUN SUMMARY  (DRY_RUN=True in config.py)")
        print(f"{'=' * 54}")
        print(f"  Target buy       : $1.00 USDC -> cbBTC")
        print(f"  Expected out     : ~{q1:.8f} cbBTC")
        print(f"  Min out (50bps)  : ~{min_out:.8f} cbBTC")
        print(f"  Step 1  approve  : exact {1.0:.6f} USDC to SwapRouter02")
        print(f"  Step 2  swap     : exactInputSingle via multicall(deadline+300s)")
        print(f"  Step 3  transfer : cbBTC -> cold wallet {_COLD}")
        print(f"  No transactions broadcast.")
        print(f"{'=' * 54}\n")
    else:
        result = buy_cbbtc(1.0)
        print(f"approve_tx : {result['approve_tx']}")
        print(f"swap_tx    : {result['swap_tx']}")
        print(f"transfer_tx: {result['transfer_tx']}")
