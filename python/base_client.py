# ─────────────────────────────────────────────
#  Smart DCA Bot — base_client.py
#  cbBTC buy flow on Base mainnet:
#    USDC approval → Uniswap V3 swap → transfer to cold wallet
#
#  Requires: python/.env  with BASE_RPC_URLS (or legacy BASE_RPC_URL)
#            and EVM_PRIVATE_KEY
#  DRY_RUN = True (config.py) → prints every step, broadcasts nothing.
#
#  get_quote() strategy:
#    1. Try QuoterV2 (quoteExactInputSingle) — requires simulation-capable RPC
#    2. Fall back to pool slot0 sqrtPriceX96 spot price (works on any RPC)
#
#  RPC resilience:
#    Read calls go through _rpc_call(), which retries the current endpoint then
#    rotates through BASE_RPC_URLS. Writes never rotate silently — see
#    _sign_and_send() and _resolve_ambiguous_broadcast().
# ─────────────────────────────────────────────

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("dca-bot")

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import ProviderConnectionError, Web3RPCError
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

def _load_rpc_urls() -> list[str]:
    """Read the priority-ordered RPC endpoint list from the environment.

    BASE_RPC_URLS is the documented form: a comma-separated list, highest
    priority first. BASE_RPC_URL (singular) is still honoured as a fallback so
    a VPS running an older .env keeps working after a code-only deploy.
    Duplicates are dropped, order preserved.
    """
    raw = os.getenv("BASE_RPC_URLS", "")
    urls = [u.strip() for u in raw.split(",") if u.strip()]

    single = (os.getenv("BASE_RPC_URL") or "").strip()
    if single and single not in urls:
        # Legacy single-URL config: used as the whole list when BASE_RPC_URLS is
        # absent, appended as a last-resort fallback when both are present.
        urls.append(single)

    deduped: list[str] = []
    for u in urls:
        if u not in deduped:
            deduped.append(u)
    return deduped


def _redact(url: str) -> str:
    """Strip any API key from an RPC URL so it is safe to log or alert on.

    Keyed endpoints (Alchemy, Infura) carry the key as the final path segment.
    Logs go to journalctl and Telegram, so the key must never appear in either.
    """
    try:
        scheme, _, rest = url.partition("://")
        host, _, path = rest.partition("/")
        if not path:
            return url
        head, _, tail = path.rpartition("/")
        if len(tail) >= 12:
            tail = f"{tail[:4]}...redacted"
        return f"{scheme}://{host}/{head + '/' if head else ''}{tail}"
    except Exception:
        return "<rpc-url>"


RPC_URLS = _load_rpc_urls()
_raw_key = os.getenv("EVM_PRIVATE_KEY")

if not RPC_URLS:
    sys.exit("ERROR: neither BASE_RPC_URLS nor BASE_RPC_URL set in python/.env")

# Index of the endpoint currently bound to w3.provider. Sticky: once an
# endpoint answers, we stay on it rather than re-probing the head of the list
# (and eating a failed round trip) before every single call.
_active_idx = 0
_rpc_url = RPC_URLS[0]

w3 = Web3(Web3.HTTPProvider(_rpc_url))


# ── RPC retry / rotation ──────────────────────

# HTTP statuses that mean "this endpoint, right now" rather than "this call is
# invalid". 403 is the 29 Jul 2026 PublicNode IP block; 429 is rate limiting;
# 5xx is the endpoint failing. All are worth trying elsewhere.
_RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}

# JSON-RPC error codes providers use for rate limiting / capacity. These arrive
# as Web3RPCError rather than an HTTP error, but mean the same thing.
_RETRYABLE_RPC_CODES = {-32005, -32016, -32098, -32099}

_ATTEMPTS_PER_PROVIDER = 2     # first try + one retry before rotating
_RETRY_BACKOFF_SEC     = 1.5

# How long to hunt for a receipt after an ambiguous broadcast before
# re-broadcasting the identical signed bytes. Base blocks are ~2s, so 90s is
# many blocks of grace while still bounding the cycle.
_AMBIGUOUS_POLL_SEC      = 90
_AMBIGUOUS_POLL_INTERVAL = 5


class RPCExhaustedError(Exception):
    """Every endpoint in BASE_RPC_URLS failed at the transport level.

    Carries the per-endpoint failure detail so the log and the Telegram alert
    say which endpoints were tried and what each returned.
    """

    def __init__(self, label: str, failures: list[tuple[str, str]]):
        self.label    = label
        self.failures = failures
        detail = "; ".join(f"{_redact(u)} -> {why}" for u, why in failures)
        super().__init__(f"All {len(failures)} RPC endpoints failed on {label}: {detail}")


class AmbiguousBroadcastError(Exception):
    """A raw transaction may or may not have reached a mempool.

    Raised only when the broadcast failed at the transport level AND the
    receipt could not be found AND re-broadcasting the identical signed bytes
    also failed. The caller must not re-sign or retry the cycle — it must tell
    the operator to check Basescan.
    """

    def __init__(self, tx_hash: str, reason: str):
        self.tx_hash = tx_hash
        super().__init__(
            f"Ambiguous broadcast for tx {tx_hash}: {reason}. "
            f"Verify on Basescan before re-running."
        )


def _describe_transport_error(exc: Exception) -> str:
    """One-line description of a transport failure, for the exhaustion report."""
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None:
            return f"HTTP {resp.status_code}"
        return "HTTP error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection error"
    if isinstance(exc, Web3RPCError):
        return f"JSON-RPC {getattr(exc, 'rpc_response', {}).get('error', {}).get('code', '?')}"
    return type(exc).__name__


def _is_transport_error(exc: Exception) -> bool:
    """True only for faults that another endpoint might not have.

    Deliberately narrow. Contract reverts, insufficient-funds and bad-argument
    errors mean the chain rejected the call on its merits — retrying those on
    four endpoints just fails four times instead of once, and costs four round
    trips of latency inside a time-sensitive buy cycle.
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        return resp is not None and resp.status_code in _RETRYABLE_STATUSES
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(exc, ProviderConnectionError):
        return True
    if isinstance(exc, Web3RPCError):
        code = (getattr(exc, "rpc_response", None) or {}).get("error", {})
        code = code.get("code") if isinstance(code, dict) else None
        return code in _RETRYABLE_RPC_CODES
    return False


def _bind_provider(idx: int) -> None:
    """Point the existing Web3 instance at RPC_URLS[idx].

    web3 7.14.1 exposes `Web3.provider` as a property whose setter reassigns
    `self.manager.provider`. Swapping the provider on the live Web3 instance
    (rather than constructing a new Web3) means every contract object bound at
    import time — usdc_contract, cbbtc_contract, router, quoter, pool — follows
    automatically, because each holds a reference to this same Web3 object.
    """
    global _active_idx, _rpc_url
    _active_idx = idx
    _rpc_url    = RPC_URLS[idx]
    w3.provider = Web3.HTTPProvider(_rpc_url)


def active_rpc_url() -> str:
    """The endpoint currently in use, redacted. For logs and /status."""
    return _redact(RPC_URLS[_active_idx])


def _rpc_call(label: str, fn, *args, **kwargs):
    """Run a read-only RPC call with retry-then-rotate across BASE_RPC_URLS.

    Tries the sticky endpoint first, then each remaining endpoint in priority
    order, wrapping around so the whole list is exhausted regardless of where
    the sticky pointer happened to be. Each endpoint gets a fresh attempt
    budget. Non-transport exceptions propagate immediately and untouched.

    Never use this for anything that broadcasts — see _sign_and_send().
    """
    global _active_idx

    n        = len(RPC_URLS)
    start    = _active_idx
    order    = [(start + i) % n for i in range(n)]
    failures: list[tuple[str, str]] = []

    for idx in order:
        if idx != _active_idx:
            _bind_provider(idx)

        last_exc: Exception | None = None
        for attempt in range(1, _ATTEMPTS_PER_PROVIDER + 1):
            try:
                result = fn(*args, **kwargs)
                _active_idx = idx          # sticky: stay here for the next call
                return result
            except Exception as exc:
                if not _is_transport_error(exc):
                    raise
                last_exc = exc
                if attempt < _ATTEMPTS_PER_PROVIDER:
                    log.warning(
                        f"[rpc] {label}: {_describe_transport_error(exc)} from "
                        f"{_redact(RPC_URLS[idx])} — retrying in {_RETRY_BACKOFF_SEC}s "
                        f"({attempt}/{_ATTEMPTS_PER_PROVIDER})"
                    )
                    time.sleep(_RETRY_BACKOFF_SEC)

        reason = _describe_transport_error(last_exc) if last_exc else "unknown"
        failures.append((RPC_URLS[idx], reason))
        log.warning(
            f"[rpc] {label}: abandoning {_redact(RPC_URLS[idx])} after "
            f"{_ATTEMPTS_PER_PROVIDER} attempts — {reason}"
        )

    raise RPCExhaustedError(label, failures)

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
    sqrt_price_x96 = _rpc_call("slot0", pool.functions.slot0().call)[0]
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
        nonce = _rpc_call(
            "eth_getTransactionCount",
            w3.eth.get_transaction_count, account.address, "pending",
        )

    latest       = _rpc_call("eth_getBlockByNumber", w3.eth.get_block, "latest")
    base_fee     = latest["baseFeePerGas"]
    max_priority = _rpc_call("eth_maxPriorityFeePerGas", lambda: w3.eth.max_priority_fee)
    max_fee      = base_fee * 2 + max_priority

    # web3.py's fill_transaction_defaults() calls estimate_gas() internally
    # inside build_transaction() when "gas" is absent from the tx dict. Setting
    # tx["gas"] after the call is too late — the estimate already happened.
    # Including "gas" in the dict passed to build_transaction() prevents
    # fill_transaction_defaults from ever invoking estimate_gas().
    tx_fields = {
        "from":                 account.address,
        "nonce":                nonce,
        "type":                 2,
        "chainId":              CHAIN_ID,
        "value":                value_wei,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": max_priority,
    }
    if gas_limit is not None:
        tx_fields["gas"] = gas_limit
    tx = contract_fn.build_transaction(tx_fields)
    if gas_limit is None:
        # Estimate gas and apply 20% buffer
        gas_est = _rpc_call("eth_estimateGas", w3.eth.estimate_gas, {
            "from":  account.address,
            "to":    tx["to"],
            "data":  tx["data"],
            "value": value_wei,
        })
        tx["gas"] = int(gas_est * 1.2)
    return tx


# ── Broadcast tracking ────────────────────────
# run_bot.run_once() needs to know whether this cycle put a transaction on the
# wire, because a cycle-level retry after a partial buy is how you get two buys
# in one day. This is the single source of truth for that question.

_broadcast = {"attempted": False, "confirmed": False, "tx_hash": None, "step": None}


def reset_broadcast_tracker() -> None:
    """Clear broadcast state. Call at the start of every cycle attempt."""
    _broadcast.update({"attempted": False, "confirmed": False, "tx_hash": None, "step": None})


def get_broadcast_state() -> dict:
    """Snapshot of whether anything was broadcast, and whether it confirmed."""
    return dict(_broadcast)


def _set_step(name: str) -> None:
    """Record which phase of the buy flow we are in.

    buy_cbbtc() spans approve, swap, post_swap_read and transfer. When an
    exception escapes it, run_bot needs to name the phase in the alert, and
    only this module knows which one it reached.
    """
    _broadcast["step"] = name


def _wait_for_receipt(tx_hash: str, timeout: int = 120):
    """Wait for a receipt, rotating endpoints if the current one goes down.

    Read-only and idempotent, so a rotation mid-wait simply restarts the poll
    against a healthy endpoint. Marks the broadcast confirmed on a status-1
    receipt so the error alert can distinguish "confirmed then failed" from
    "outcome unknown".
    """
    receipt = _rpc_call(
        "eth_getTransactionReceipt",
        w3.eth.wait_for_transaction_receipt, tx_hash, timeout=timeout,
    )
    if receipt is not None and receipt.get("status") == 1:
        _broadcast["confirmed"] = True
    return receipt


def _receipt_if_present(tx_hash: str):
    """Return the receipt for tx_hash if the chain has one, else None."""
    try:
        return _rpc_call(
            "eth_getTransactionReceipt",
            w3.eth.get_transaction_receipt, tx_hash,
        )
    except RPCExhaustedError:
        raise
    except Exception:
        # web3 raises TransactionNotFound when the hash is unknown — that is a
        # legitimate "not landed yet", not a fault.
        return None


def _resolve_ambiguous_broadcast(signed, tx_hash: str, cause: Exception) -> str:
    """Recover from a transport failure during send_raw_transaction.

    A 403 / timeout / reset on send means the request may have reached the node
    before the response was lost, so the transaction may already be sitting in a
    mempool. Re-signing or re-fetching the nonce here would produce a SECOND
    valid transaction and could buy twice. So:

      1. Rotate to a healthy endpoint using a cheap read.
      2. Poll for a receipt for this exact hash. If it lands, it was sent.
      3. If it never appears, re-broadcast the IDENTICAL signed bytes. That is
         idempotent: same nonce, same signature, same hash. A node that already
         has it answers "already known", which is success, not a duplicate.
      4. If even that fails, raise AmbiguousBroadcastError so the operator is
         told to check Basescan rather than the bot quietly retrying.
    """
    log.warning(
        f"[send] ambiguous broadcast failure ({_describe_transport_error(cause)}) "
        f"for tx {tx_hash} — NOT re-signing. Polling for receipt."
    )

    # Force a rotation off the endpoint that just failed, if we have another.
    if len(RPC_URLS) > 1:
        _bind_provider((_active_idx + 1) % len(RPC_URLS))
    try:
        _rpc_call("eth_blockNumber", lambda: w3.eth.block_number)
    except RPCExhaustedError as exc:
        raise AmbiguousBroadcastError(tx_hash, f"no healthy RPC to verify against: {exc}") from exc

    deadline = time.time() + _AMBIGUOUS_POLL_SEC
    while time.time() < deadline:
        receipt = _receipt_if_present(tx_hash)
        if receipt is not None:
            log.warning(f"[send] tx {tx_hash} DID land despite the send error — treating as sent")
            if receipt.get("status") == 1:
                _broadcast["confirmed"] = True
            return tx_hash
        time.sleep(_AMBIGUOUS_POLL_INTERVAL)

    # Not on chain after the poll window. Re-broadcast the same signed bytes — same nonce,
    # so this can only ever result in one transaction.
    log.warning(
        f"[send] tx {tx_hash} not found after {_AMBIGUOUS_POLL_SEC}s — "
        f"re-broadcasting identical signed bytes"
    )
    try:
        w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash
    except Exception as exc:
        msg = str(exc).lower()
        if "already known" in msg or "already imported" in msg or "nonce too low" in msg:
            # The node has it (or the nonce is already spent by this same tx).
            log.warning(f"[send] node reports tx already known — treating as sent: {tx_hash}")
            return tx_hash
        raise AmbiguousBroadcastError(tx_hash, f"re-broadcast failed: {exc}") from exc


def _sign_and_send(tx: dict) -> str:
    """Sign and broadcast a transaction.

    Two distinct failure modes, handled differently on purpose:

      * "nonce too low" — the chain rejected it on its merits. The transaction
        definitively did not enter a mempool under this nonce, so re-fetching
        the nonce and re-signing is safe. Pre-existing behaviour, unchanged.
      * transport failure — outcome unknown. Never re-sign. See
        _resolve_ambiguous_broadcast().
    """
    signed = account.sign_transaction(tx)
    # The hash is fixed by the signature, so it is known before broadcast. That
    # is what makes recovery from an ambiguous send possible at all.
    tx_hash = signed.hash.hex()

    # Flagged BEFORE the send, not after: if the send fails at the transport
    # level we cannot know whether the bytes reached a node, and the safe
    # assumption is that they did.
    _broadcast["attempted"] = True
    _broadcast["tx_hash"]   = tx_hash

    try:
        sent = w3.eth.send_raw_transaction(signed.raw_transaction)
        return sent.hex()
    except Web3RPCError as e:
        if _is_transport_error(e):
            return _resolve_ambiguous_broadcast(signed, tx_hash, e)
        if "nonce too low" not in str(e).lower():
            # A JSON-RPC error response means the node received the transaction
            # and rejected it outright (bad gas, insufficient funds, malformed).
            # It is definitively not in any mempool, so the cycle is safe to
            # retry — clear the flag rather than blocking on a false positive.
            _broadcast["attempted"] = False
            _broadcast["tx_hash"]   = None
            raise
        if "nonce too low" in str(e).lower():
            tx["nonce"] = _rpc_call(
                "eth_getTransactionCount",
                w3.eth.get_transaction_count, account.address, "pending",
            )
            signed  = account.sign_transaction(tx)
            resent  = w3.eth.send_raw_transaction(signed.raw_transaction)
            _broadcast["tx_hash"] = resent.hex()
            return resent.hex()
        raise
    except Exception as e:
        if _is_transport_error(e):
            return _resolve_ambiguous_broadcast(signed, tx_hash, e)
        raise


# ── Public API ────────────────────────────────

def get_usdc_balance() -> float:
    """Return hot wallet USDC balance in human-readable USD."""
    raw = _rpc_call("balanceOf(USDC)", usdc_contract.functions.balanceOf(_HOT).call)
    return raw / 10 ** USDC_DECIMALS


def get_cbbtc_balance() -> float:
    """Return hot wallet cbBTC balance."""
    raw = _rpc_call("balanceOf(cbBTC)", cbbtc_contract.functions.balanceOf(_HOT).call)
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
    allowance     = _rpc_call("allowance", usdc_contract.functions.allowance(_HOT, _ROUTER).call)
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
        zero_receipt = _wait_for_receipt(zero_hash, timeout=60)
        if zero_receipt.status != 1:
            raise Exception(f"Zero-approval transaction reverted: {zero_hash}")
        log.info(f"[approve] zero-approval confirmed (block {zero_receipt.blockNumber})")
        if next_nonce is not None:
            next_nonce += 1

    tx      = _build_eip1559_tx(usdc_contract.functions.approve(_ROUTER, approve_amount), nonce=next_nonce, gas_limit=100_000)
    tx_hash = _sign_and_send(tx)
    log.info(f"[approve] tx: {tx_hash}")
    receipt = _wait_for_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise Exception(f"Approval transaction reverted: {tx_hash}")

    actual = _rpc_call("allowance", usdc_contract.functions.allowance(_HOT, _ROUTER).call)
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
    deadline           = _rpc_call("eth_getBlockByNumber", w3.eth.get_block, "latest")["timestamp"] + 300

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
    receipt = _wait_for_receipt(tx_hash, timeout=120)
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
    _wait_for_receipt(tx_hash, timeout=120)
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

    _set_step("approve")

    # 3. Approve + Swap + Transfer — fetch nonce once with 'pending' so each
    #    successive tx in this cycle gets the correct sequential nonce even
    #    before the node has indexed the prior tx.
    usdc_raw = _usdc_to_raw(usdc_amount_usd)
    nonce    = _rpc_call(
        "eth_getTransactionCount",
        w3.eth.get_transaction_count, account.address, "pending",
    ) if not DRY_RUN else 0

    # check_and_approve_usdc returns (hash | None, next_nonce) — next_nonce
    # accounts for 0, 1, or 2 txs (zero-reset + approve) so the nonce
    # sequence fed to swap and transfer is always correct.
    approve_hash, nonce = check_and_approve_usdc(usdc_raw, nonce=nonce)

    # 4. Swap
    _set_step("swap")
    swap_hash = swap_usdc_to_cbbtc(usdc_amount_usd, nonce=nonce)
    nonce += 1

    # 5. Determine quantity received.
    #    Always read actual on-chain balance after the swap is confirmed —
    #    never use the pre-swap quote, which may be higher than what was
    #    actually received due to slippage and fees.
    _set_step("post_swap_read")
    if DRY_RUN:
        cbbtc_raw = int(quoted * 10 ** CBBTC_DECIMALS)
    else:
        # Wait 3 seconds for the RPC cluster to propagate swap state before
        # reading balance or broadcasting the transfer.
        time.sleep(3)
        # Poll balanceOf up to 5 times with 2-second intervals until > 0.
        cbbtc_raw = 0
        for _poll in range(5):
            cbbtc_raw = _rpc_call(
                "balanceOf(cbBTC)",
                cbbtc_contract.functions.balanceOf(account.address).call,
            )
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
    _set_step("transfer")
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

    chain_id = _rpc_call("eth_chainId", lambda: w3.eth.chain_id)
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
