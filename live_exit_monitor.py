"""Monitor confirmed positions and raise sell alerts; no unsigned live orders.

Uses executable Jupiter token->USDC quotes, not DexScreener chart prices.
Sales are executed by live_engine.exit_cycle; this module only evaluates/alerts.
"""
import logging, os, time
from live_trade_state import _load
from live_exit_rules import trailing_exit_signal
import live_trade_state
from jupiter_v2_executor import order, SOL_MINT, USDC_MINT

_last_alert = {}
_quote_fail_since = {}  # mint -> monotonic time quotes started failing
DEAD_AFTER_SECONDS = 10 * 60
CHECK_INTERVAL_SECONDS = 20
SNIPER_MAX_HOLD_SECONDS = 30 * 60  # sniper trade that never took off is closed

SNIPER_EXIT = {"stop_loss": "0.12", "arm": "1.25", "drop": "0.12"}
HOT_EXIT = {"stop_loss": "0.06", "arm": "1.08", "drop": "0.05"}
HOT_MAX_HOLD_SECONDS = 45 * 60  # fast mover that did not move is closed


def quote_value_usd(mint, amount):
    """Executable value of `amount` tokens: token -> SOL -> USDC (Jupiter)."""
    sol_out = int(order(mint, SOL_MINT, int(amount))["outAmount"])
    if sol_out <= 0:
        raise ValueError("No executable SOL quote")
    return int(order(SOL_MINT, USDC_MINT, sol_out)["outAmount"]) / 1_000_000


def position_signal(position, cost, proceeds):
    """Exit decision shared by real trading and the simulator."""
    if position.get("mode") in ("sniper", "pumpfun"):
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **SNIPER_EXIT)
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > SNIPER_MAX_HOLD_SECONDS and signal["reason"] != "trailing":
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "mirror":
        # Mirror: the exit is the mirrored wallet's own sale (mirror_wallet sells with it).
        # Here only the safety stop, an optional target and the max hold, all measured on that wallet.
        tp = position.get("copy_tp")
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                      stop_loss=str(position.get("copy_sl") or "0.5"),
                                      arm=str(1 + float(tp)) if tp else "1000", drop="0")
        max_hold = position.get("copy_max_hold_s")
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and max_hold and held > float(max_hold):
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") in ("copy", "clone"):
        # Copy trade: target +50% / stop -15% (selling with the copied wallet is done by copy_trader).
        # A cloned wallet carries its own measured TP / SL / max hold on the position.
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                      stop_loss=str(position.get("copy_sl") or os.getenv("COPY_STOP_LOSS", "0.15")),
                                      arm=str(1 + float(position.get("copy_tp") or os.getenv("COPY_TAKE_PROFIT", "0.50"))),
                                      drop="0")
        max_hold = position.get("copy_max_hold_s")
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and max_hold and held > float(max_hold):
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "tradeB":
        # Strategy B: fixed take profit +15% / stop loss -8% (drop=0 -> sell as soon as +15% is reached)
        return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                    stop_loss=os.getenv("SIM_STOP_LOSS", "0.08"),
                                    arm=str(1 + float(os.getenv("SIM_TAKE_PROFIT", "0.15"))), drop="0")
    if position.get("mode") == "launch":
        # Launch strategy removed; kept so any old position is still sold safely.
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0),
                                      stop_loss="0.20", arm="1.15", drop="0.08")
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > 90:
            signal.update(action="sell", reason="time_stop")
        return signal
    if position.get("mode") == "candle":
        # Closed by the candle loop; this is only the safety stop in between.
        return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **HOT_EXIT)
    if position.get("mode") == "hot":
        signal = trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0), **HOT_EXIT)
        held = time.time() - float(position.get("opened_at") or time.time())
        if signal["action"] == "hold" and held > HOT_MAX_HOLD_SECONDS and signal["reason"] != "trailing":
            signal.update(action="sell", reason="time_stop")
        return signal
    return trailing_exit_signal(cost, proceeds, position.get("peak_ratio", 0))


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
            proceeds = quote_value_usd(mint, amount)
            signal = position_signal(position, cost, proceeds)
            if signal["peak_ratio"] > float(position.get("peak_ratio", 0)):
                live_trade_state.update_peak(mint, signal["peak_ratio"])
            _quote_fail_since.pop(mint, None)
            yield mint, position, signal
        except Exception:
            logging.warning("Live position quote/monitoring unavailable for mint %s", mint[:8])
            # Dead token (rug / no liquidity): Jupiter works for SOL but has no route for it.
            if not _jupiter_healthy():
                _quote_fail_since.pop(mint, None)
                continue
            since = _quote_fail_since.setdefault(mint, time.monotonic())
            if time.monotonic() - since >= DEAD_AFTER_SECONDS:
                yield mint, position, {"action": "write_off", "reason": "dead", "change_pct": -100.0}

def _jupiter_healthy():
    try:
        return int(order(SOL_MINT, USDC_MINT, 100_000_000)["outAmount"]) > 0
    except Exception:
        return False

def sell_signals():
    """Positions to exit: TP/SL/time stop ("sell") or dead tokens ("write_off")."""
    return [(m, p, s) for m, p, s in _evaluate() if s["action"] in ("sell", "write_off")]

def monitor(send, chat):
    """Alert-only mode, used while real trading is not active."""
    if not chat:
        return
    for mint, position, signal in sell_signals():
        if signal["action"] != "sell":
            continue
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
