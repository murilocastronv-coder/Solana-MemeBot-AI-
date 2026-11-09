"""All strategy parameters in one place.

Freeze these BEFORE looking at the out-of-sample period. The backtest prints a
fingerprint of this config so you can prove the OOS run used the same numbers.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field

# This package never trades real money. The flag is checked anyway, so a wrong
# environment variable cannot silently turn anything on.
LIVE_TRADING_ENABLED = False


def assert_paper_only():
    env = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower()
    if env != "false" or LIVE_TRADING_ENABLED:
        raise SystemExit("LIVE_TRADING_ENABLED precisa ser false. Este módulo é só backtest/paper trading.")


@dataclass(frozen=True)
class ExchangeRules:
    """Order limits. Verify against the exchange's exchangeInfo before trusting results."""
    name: str
    min_qty: float          # smallest order in BTC
    qty_step: float         # quantity increment in BTC
    min_notional: float     # smallest order value in USDT
    taker_fee: float        # per side
    max_leverage: float     # hard cap (never above 2x here)
    has_funding: bool


# Binance USDT-M perpetual BTCUSDT (isolated margin). Values as published by the
# exchange at the time of writing; check /fapi/v1/exchangeInfo before use.
FUTURES = ExchangeRules("binance_usdm_btcusdt", min_qty=0.001, qty_step=0.001, min_notional=100.0,
                        taker_fee=0.0005, max_leverage=2.0, has_funding=True)
# Binance spot BTCUSDT (no leverage, no funding). Useful to see how the same
# rules behave when the minimum order is small enough for a $50 account.
SPOT = ExchangeRules("binance_spot_btcusdt", min_qty=0.00001, qty_step=0.00001, min_notional=5.0,
                     taker_fee=0.001, max_leverage=1.0, has_funding=False)
PROFILES = {"futures": FUTURES, "spot": SPOT}


@dataclass(frozen=True)
class StrategyConfig:
    # 1h trend filter
    ema_trend: int = 200
    ema_slope_lookback_h: int = 10        # EMA200 now must be >= EMA200 10 hours ago
    # 5m entry
    ema_fast: int = 20
    ema_slow: int = 50
    pullback_lookback: int = 6            # pullback must have happened in the last 6 bars
    near_ema_pct: float = 0.0015          # "near" = low within 0.15% above EMA20 ...
    max_below_slow_pct: float = 0.003     # ... and not more than 0.3% below EMA50
    volume_len: int = 20
    rsi_len: int = 14
    rsi_max: float = 70.0
    atr_len: int = 14
    # exits
    stop_atr_mult: float = 1.5
    reward_risk: float = 1.5
    # risk
    risk_per_trade: float = 0.005         # 0.5% of equity, fees included
    daily_loss_limit: float = 0.02        # 2% of the day's starting equity
    max_leverage: float = 2.0             # never above 2x
    slippage: float = 0.0002              # 0.02% per side (backtest + paper)
    default_funding_8h: float = 0.0001    # used only when funding history is missing
    max_entries_per_day: int = 0          # 0 = no daily cap
    kill_switch_drawdown: float = 0.0     # 0 = off; 0.20 = stop for good at -20% of start_equity
    # extra entry filters (off in the conservative preset, on in the aggressive one)
    block_fast_drop: bool = False         # skip if price fell more than fast_drop_atr x ATR in the last bars
    fast_drop_atr: float = 3.0
    fast_drop_bars: int = 6
    block_extended: bool = False          # skip if close is more than extended_atr x ATR above EMA50
    extended_atr: float = 3.0
    # pass/fail rules of the evaluation
    max_dd_limit: float = 0.10
    require_oos_profit: bool = False
    name: str = "conservador"
    start_equity: float = 50.0
    profile: str = "futures"
    rules_override: dict = field(default_factory=dict)

    @property
    def rules(self) -> ExchangeRules:
        base = PROFILES[self.profile]
        if not self.rules_override:
            return base
        return ExchangeRules(**{**asdict(base), **self.rules_override})

    @property
    def leverage(self) -> float:
        return min(self.max_leverage, self.rules.max_leverage, 2.0)

    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    @property
    def warmup_5m(self) -> int:
        # enough 5m bars for the 1h EMA200 + slope lookback to be meaningful
        return (self.ema_trend * 3 + self.ema_slope_lookback_h) * 12


def preset(name, profile="futures"):
    """conservador: 0.5% risk, 1.5R, 2%/day. agressivo: 2% risk, 2R, 6%/day, 3 entries/day, stop at -20%.

    The aggressive one has a simulation goal of 50 -> 500 USD in 30 days. That is only a
    reference number printed in the report: no rule is changed to chase it.
    """
    if name == "conservador":
        return StrategyConfig(profile=profile)
    if name == "agressivo":
        return StrategyConfig(name="agressivo", profile=profile, risk_per_trade=0.02, reward_risk=2.0,
                              daily_loss_limit=0.06, max_entries_per_day=3, kill_switch_drawdown=0.20,
                              block_fast_drop=True, block_extended=True, max_dd_limit=0.20,
                              require_oos_profit=True)
    raise ValueError(name)


PRESETS = ("conservador", "agressivo")
AGGRESSIVE_GOAL_USD = 500.0
