"""Deterministic TP/SL decisions for confirmed live positions.

This module never signs, sends or retries transactions. Callers must obtain
an executable Jupiter sell quote and reconcile finalized on-chain balances.
"""
from decimal import Decimal, InvalidOperation

TAKE_PROFIT = Decimal("0.15")
STOP_LOSS = Decimal("0.08")
MAX_DAILY_LOSS_USD = Decimal("3")

def _positive(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid monetary amount")
    if not result.is_finite() or result <= 0:
        raise ValueError("Amount must be positive and finite")
    return result

def exit_signal(cost_usd, executable_proceeds_usd):
    """Compare sell-quote net proceeds against confirmed cost basis.

    Include sell network fees in proceeds before calling. Quoted prices
    are not guarantees; price can change before transaction confirmation.
    """
    cost = _positive(cost_usd)
    proceeds = _positive(executable_proceeds_usd)
    change = (proceeds - cost) / cost
    if change >= TAKE_PROFIT:
        return {"action": "sell", "reason": "take_profit", "change_pct": float(change * 100)}
    if change <= -STOP_LOSS:
        return {"action": "sell", "reason": "stop_loss", "change_pct": float(change * 100)}
    return {"action": "hold", "reason": "inside_range", "change_pct": float(change * 100)}

def daily_loss_exceeded(realized_loss_usd, additional_loss_usd=0):
    realized = Decimal(str(realized_loss_usd))
    additional = Decimal(str(additional_loss_usd))
    if not realized.is_finite() or not additional.is_finite() or realized < 0 or additional < 0:
        raise ValueError("Invalid loss")
    return realized + additional >= MAX_DAILY_LOSS_USD
