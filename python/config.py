# ─────────────────────────────────────────────
#  Smart DCA Bot — config.py
#  All settings in one place. No secrets here.
# ─────────────────────────────────────────────

# ── Mode ─────────────────────────────────────
DRY_RUN = True              # True = simulate only, never execute

# ── Budget ───────────────────────────────────
MONTHLY_BUDGET = 500.0       # USD per month, for cbBTC
DAILY_DRIP = MONTHLY_BUDGET / 30  # ~$16.67 dripped per execution day

# ── Pool ─────────────────────────────────────
POOL_CAP_MULTIPLIER = 5      # base_pool ceiling = 5 × DAILY_DRIP (~$83.33)
RESERVE_ENABLED     = False  # reserve_pool accumulation off
NO_BUY_ZONE_ENABLED = False  # no-buy zone off

# ── Signal Weights (must sum to 1.0) ─────────
SIGNAL_WEIGHTS = {
    "fear_greed":  0.35,   # Alternative.me Fear & Greed index
    "rsi":         0.40,   # Kraken RSI-14 daily + MA200 modifier
    "liquidation": 0.25,   # volume-spike + price-drop proxy
}

# ── Multiplier Tiers ─────────────────────────
# (min_score_threshold, multiplier)  — evaluated top-down
MULTIPLIER_TIERS = [
    (0.80, 5.0),
    (0.65, 3.0),
    (0.50, 2.0),
    (0.35, 1.5),
    (0.20, 1.0),
    (0.00, 0.5),
]

# ── Base Mainnet Addresses ───────────────────
CBBTC_ADDRESS      = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"  # Coinbase Wrapped BTC
USDC_ADDRESS       = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base
UNISWAP_V3_ROUTER  = "0x2626664c2603336E57B271c5C0b26F421741e481"  # SwapRouter02
UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"  # UniV3 Factory
QUOTER_V2          = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"  # QuoterV2
QUOTER_V1          = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"  # QuoterV1 (fallback)
CBBTC_USDC_POOL    = "0xfBB6Eed8e7aa03B138556eeDaF5D271A5E1e43ef"  # cbBTC/USDC 0.05% pool
HOT_WALLET         = "0xd1F1a36B423Ea05e47fCB50F0b86fC5Dc3be3380"  # execution wallet
COLD_WALLET        = "0xdbbb6ed92bdc8afdfe8295b8504a73305d0ef8c0"  # destination wallet

# ── Token metadata ───────────────────────────
USDC_DECIMALS  = 6
CBBTC_DECIMALS = 8
POOL_FEE       = 500    # 0.05% — primary cbBTC/USDC pool on Base

# ── Chain ────────────────────────────────────
CHAIN_ID = 8453         # Base mainnet

# ── Execution ────────────────────────────────
EXECUTION_TIME_UTC = "09:00"

# ── Kraken symbol for BTC (proxy for cbBTC) ──
KRAKEN_BTC_SYMBOL  = "XBTUSD"
KRAKEN_DAILY       = 1440   # minutes — daily candles
