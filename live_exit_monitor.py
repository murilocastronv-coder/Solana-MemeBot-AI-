"""Monitor confirmed positions and raise sell alerts; no unsigned live orders.

Uses executable Jupiter token->USDC quotes, not DexScreener chart prices.
Sales are executed by live_engine.exit_cycle; this module only evaluates/alerts.
"""
import logging, time
from live_trade_state import _load
from live_exit_rules import trailing_exit_signal
import live_trade_state
from jupiter_v2_executor import order, SOL_MINT, USDC_MINT

_last_alert = {}
CHECK_INTERVAL_SECONDS = 30

def _evaluate():
    state = _load()
    for mint, position in state["positions"].items():
        if any(p.get("mint") == mint for p in state["pending"].values()):
            continue
        try:
            amount = int(position["token_amount_raw"])
            cost = float(position["cost_usd"])
            if amount <= 0 or cost <= 0:
                raise ValueError("Invalid position amount or cost")
            sol_quote = order(mint, SOL_MINT, amount)
            sol_out = int(sol_quote["outAmount"])
            if sol_out <= 0:
                raise ValueError("No executable SOL quote")
            usd_quote = order(SOL_MINT, USDC_MINT, sol_out)
            proceeds = int(usd_quote["outAmount"]) / 1_000_000
            signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0))
            if signal["peak_ratio"] > float(position.get("peak_ratio", 0)):
                live_trade_state.update_peak(mint, signal["peak_ratio"])
            yield mint, position, signal
        except Exception:
            logging.warning("Live position quote/monitoring unavailable for mint %s", mint[:8])

def sell_signals():
    """Positions whose executable quote hit TP or SL (used by live_engine)."""
    return [(m, p, s) for m, p, s in _evaluate() if s["action"] == "sell"]

def monitor(send, chat):
    """Alert-only mode, used while real trading is not active."""
    if not chat:
        return
    for mint, position, signal in sell_signals():
        key = (mint, signal["reason"])
        now = time.monotonic()
        if now - _last_alert.get(key, 0) < 900:
            continue
        _last_alert[key] = now
        label = "🎯 TAKE PROFIT" if signal["reason"] == "take_profit" else "🛑 STOP LOSS"
        send(chat, label + " ATINGIDO (estimativa Jupiter token → SOL → USD)\nToken: " + mint +
             "\nVariação estimada: " + format(signal["change_pct"], ".2f") +
             "%\n⚠️ Robô desligado (LIVE_TRADING_ENABLED/preflight): venda NÃO enviada. "
             "Ligue o robô ou venda pela Phantom.")
