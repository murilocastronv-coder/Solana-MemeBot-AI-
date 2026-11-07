"""Paper trading with REAL market prices (Jupiter quotes), fake money.

Runs the same strategies as real trading (normal + sniper) with the same
filters and exit rules, so results can be compared before risking money.
Never signs or sends transactions. State lives next to the real ledger.
"""
import json
import logging
import os
import tempfile
import threading
import time

import live_engine as engine
from live_exit_monitor import quote_value_usd, position_signal, _jupiter_healthy, DEAD_AFTER_SECONDS
from jupiter_v2_executor import order, SOL_MINT
import live_order_bridge as bridge

_LOCK = threading.RLock()
START_USD = float(os.getenv("SIM_START_USD", "100"))
SLIPPAGE = 0.01      # 1% worse than the quote on each side (fees, price moving)
MAX_POSITIONS = 3
HISTORY_MAX = 300


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("sim_state.json")


def _default():
    return {"enabled": False, "balance": START_USD, "start": START_USD,
            "positions": {}, "history": [], "fail_since": {}}


def _load():
    path = _path()
    if not path.exists():
        return _default()
    return json.loads(path.read_text(encoding="utf-8"))


def _save(data):
    path = _path()
    fd, name = tempfile.mkstemp(prefix=".sim-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(name, path)


def set_enabled(enabled):
    with _LOCK:
        data = _load()
        data["enabled"] = bool(enabled)
        _save(data)


def reset():
    with _LOCK:
        data = _default()
        data["enabled"] = _load().get("enabled", False)
        _save(data)


def enabled():
    with _LOCK:
        return bool(_load().get("enabled"))


def equity(data):
    return data["balance"] + sum(p["cost_usd"] for p in data["positions"].values())


def _buy(data, p, mode, fraction, send, chat):
    mint = p["baseToken"]["address"]
    symbol = str(p["baseToken"].get("symbol", "?"))[:20]
    usd = round(equity(data) * fraction, 2)
    if usd < 1 or usd > data["balance"]:
        return False
    lamports = int(usd / float(bridge.sol_usd_price()) * 1e9)
    tokens = int(int(order(SOL_MINT, mint, lamports)["outAmount"]) * (1 - SLIPPAGE))
    if tokens <= 0:
        return False
    data["balance"] -= usd
    data["positions"][mint] = {"symbol": symbol, "mode": mode, "token_amount_raw": tokens,
                               "cost_usd": usd, "opened_at": time.time(), "peak_ratio": 0}
    send(chat, "🧪 SIM " + {"sniper": "🎯 SNIPER ", "pumpfun": "🟣 PUMP.FUN "}.get(mode, "") + "COMPROU " + symbol +
         " — US$ " + format(usd, ".2f") + " (dinheiro de mentira, preço real)\n" + mint)
    return True


def _passes_safety(mint, sniper):
    ok, _ = engine.token_safety(mint)
    if ok and sniper:
        ok, _ = engine.holders_ok(mint)
    if ok:
        ok, _ = engine.roundtrip_ok(mint)
    return ok


def buy_cycle(rows, send, chat):
    """Normal strategy on the regular scan: up to 2 buys, 10% of sim equity each."""
    if not chat or not enabled():
        return 0
    with _LOCK:
        data = _load()
        bought = 0
        for p in rows:
            if len(data["positions"]) >= MAX_POSITIONS or bought >= engine.BUYS_PER_SCAN:
                break
            if not engine.live_candidate(p):
                continue
            mint = p["baseToken"]["address"]
            if mint in data["positions"] or any(h["mint"] == mint and time.time() - h["closed_at"] < 86400
                                                for h in data["history"]):
                continue
            try:
                if _passes_safety(mint, sniper=False) and _buy(data, p, "normal", engine.ORDER_FRACTION, send, chat):
                    bought += 1
            except Exception:
                logging.warning("sim buy failed for %s", mint[:8])
        _save(data)
        return bought


def sniper_cycle(send, chat):
    """Sniper strategy (~20 s): one buy per cycle, 5% of sim equity, max 2 sniper positions."""
    if not chat or not enabled():
        return 0
    with _LOCK:
        data = _load()
        sniper_open = sum(1 for p in data["positions"].values() if p["mode"] == "sniper")
        if len(data["positions"]) >= MAX_POSITIONS or sniper_open >= engine.SNIPER_MAX_POSITIONS:
            return 0
        for p in engine.sniper_fetch():
            mint = (p.get("baseToken") or {}).get("address")
            if not mint or mint in data["positions"] or not engine.sniper_candidate(p):
                continue
            if any(h["mint"] == mint for h in data["history"][-100:]):
                continue
            try:
                if _passes_safety(mint, sniper=True) and _buy(data, p, "sniper", engine.SNIPER_FRACTION, send, chat):
                    _save(data)
                    return 1
            except Exception:
                logging.warning("sim sniper buy failed for %s", mint[:8])
        return 0


def _close(data, mint, value, reason, send, chat):
    position = data["positions"].pop(mint)
    data["fail_since"].pop(mint, None)
    pnl = value - position["cost_usd"]
    data["balance"] += value
    data["history"].append({"mint": mint, "symbol": position["symbol"], "mode": position["mode"],
                            "cost_usd": round(position["cost_usd"], 4), "pnl_usd": round(pnl, 4), "reason": reason,
                            "closed_at": time.time(),
                            "minutes": round((time.time() - position["opened_at"]) / 60, 1)})
    data["history"] = data["history"][-HISTORY_MAX:]
    pct = pnl / position["cost_usd"] * 100 if position["cost_usd"] else 0
    send(chat, "🧪 SIM " + ("💰" if pnl >= 0 else "🔻") + " VENDEU " + position["symbol"] +
         " (" + REASONS.get(reason, reason) + "): " + format(pnl, "+.2f") + " USD (" + format(pct, "+.1f") +
         "%)\nSaldo simulado: US$ " + format(equity(data), ".2f"))


REASONS = {"stop_loss": "stop", "take_profit": "stop móvel", "time_stop": "tempo", "dead": "moeda zerada"}


def exit_cycle(send, chat):
    if not chat or not enabled():
        return
    with _LOCK:
        data = _load()
        for mint, position in list(data["positions"].items()):
            try:
                value = quote_value_usd(mint, position["token_amount_raw"]) * (1 - SLIPPAGE)
                data["fail_since"].pop(mint, None)
            except Exception:
                if _jupiter_healthy():
                    since = data["fail_since"].setdefault(mint, time.time())
                    if time.time() - since >= DEAD_AFTER_SECONDS:
                        _close(data, mint, 0.0, "dead", send, chat)
                continue
            if value <= 0:
                continue
            signal = position_signal(position, position["cost_usd"], value)
            position["peak_ratio"] = max(float(position.get("peak_ratio", 0)), signal["peak_ratio"])
            if signal["action"] == "sell":
                _close(data, mint, value, signal["reason"], send, chat)
        _save(data)


def summary():
    with _LOCK:
        data = _load()
        return {"enabled": data.get("enabled", False), "equity": equity(data), "start": data["start"],
                "balance": data["balance"], "open": len(data["positions"]), "history": list(data["history"])}


def pumpfun_cycle(send, chat):
    """Pump.fun strategy (~20 s): one buy per cycle, 5% of sim equity, max 2 pump.fun positions."""
    import pumpfun
    if not chat or not enabled():
        return 0
    with _LOCK:
        data = _load()
        open_pf = sum(1 for p in data["positions"].values() if p["mode"] == "pumpfun")
        if len(data["positions"]) >= MAX_POSITIONS or open_pf >= pumpfun.MAX_POSITIONS:
            return 0
        recent = {h["mint"] for h in data["history"][-100:]}
        for t in pumpfun.candidates(exclude=set(data["positions"]) | recent):
            mint = t.get("id") or t.get("address")
            try:
                if pumpfun.safe(mint)[0] and _buy(data, pumpfun.as_pair(t), "pumpfun", pumpfun.FRACTION, send, chat):
                    _save(data)
                    return 1
            except Exception:
                logging.warning("sim pump.fun buy failed for %s", mint[:8])
        return 0
