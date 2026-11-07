"""Wallet configuration and conservative paper-trading risk controls.

Never accept recovery phrases. Live execution is intentionally unavailable.
"""
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class RiskLimits:
    order_usd: float = 2.0
    max_positions: int = 3
    daily_loss_usd: float = 3.0
    stop_loss_pct: float = 8.0
    take_profit_pct: float = 15.0
    min_liquidity_usd: float = 50000.0

def wallet_address():
    address = os.getenv("SOLANA_WALLET_PUBLIC_ADDRESS", "").strip()
    if not address:
        return None
    if len(address) < 32 or len(address) > 44 or any(c not in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in address):
        raise ValueError("Invalid Solana public address")
    return address

def can_open_trade(*, equity_usd, daily_realized_pnl_usd, open_positions, liquidity_usd, limits=RiskLimits()):
    reasons = []
    if daily_realized_pnl_usd <= -limits.daily_loss_usd:
        reasons.append("daily_loss_limit")
    if open_positions >= limits.max_positions:
        reasons.append("position_limit")
    if liquidity_usd < limits.min_liquidity_usd:
        reasons.append("liquidity_limit")
    if equity_usd < limits.order_usd + 1.0:
        reasons.append("insufficient_balance_and_fee_reserve")
    reasons.append("live_execution_not_implemented")
    return False, reasons

def paper_exit_signal(entry_price, current_price, limits=RiskLimits()):
    if entry_price <= 0 or current_price <= 0:
        return "INVALID_PRICE"
    change = (current_price / entry_price - 1) * 100
    if change <= -limits.stop_loss_pct:
        return "PAPER_STOP_LOSS"
    if change >= limits.take_profit_pct:
        return "PAPER_TAKE_PROFIT"
    return "HOLD"

def live_trade(*args, **kwargs):
    raise RuntimeError("LIVE TRADING DISABLED: on-chain security and signer integration are not approved")
