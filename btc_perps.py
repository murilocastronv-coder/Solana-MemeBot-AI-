"""BTC leveraged trend-following (long + short) — simulated with real BTC prices.

Strategy (classic trend following, "Turtle"/Donchian breakout + chandelier exit):
- 1h candles. Trend filter: EMA 200.
- LONG when the hourly close breaks above the previous 20-hour high and is above EMA 200.
- SHORT when the hourly close breaks below the previous 20-hour low and is below EMA 200.
- Initial stop 2 x ATR(14); trailing stop 3 x ATR from the best price since entry.
- An opposite signal closes the trade and reverses.
- Stops are always placed well before liquidation (at most 70% of the liquidation distance).
Sizing: 20% of the account as collateral per trade; position = collateral x leverage.
Costs modelled on Jupiter Perps: 0.06% open + 0.06% close of position size,
~0.01%/hour borrow fee. Liquidation when losses reach 90% of the collateral.

No real orders are sent: real execution on Jupiter Perps is not wired yet.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

_LOCK = threading.RLock()
START_USD = float(os.getenv("BTC_SIM_START_USD", "100"))
ORDER_FRACTION = 0.20
LEVERAGES = (2, 3, 5, 10, 20)
DEFAULT_LEVERAGE = 3
FEE_RATE = 0.0006          # per side, on position size
BORROW_RATE_HOUR = 0.0001  # on position size
LIQ_LOSS = 0.90            # liquidated when loss >= 90% of collateral
EMA_LEN, CHANNEL, ATR_LEN = 200, 20, 14
STOP_ATR, TRAIL_ATR = 2.0, 3.0
MAX_STOP_OF_LIQ = 0.70
_http = requests.Session()


# ------------------------------------------------------------------ market data
def _get(url):
    r = _http.get(url, timeout=12, headers={"User-Agent": "BTCPerpsBot/1.0"})
    r.raise_for_status()
    return r.json()


def fetch_candles():
    """Closed 1h BTC/USD candles, oldest first (Binance, Coinbase fallback)."""
    try:
        rows = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=300")
        candles = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                   for r in rows]
    except Exception:
        rows = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600")
        candles = sorted(({"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4])}
                          for r in rows), key=lambda c: c["t"])
    now = time.time()
    return [c for c in candles if c["t"] + 3600 <= now]  # drop the candle still forming


def fetch_price():
    try:
        return float(_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
    except Exception:
        return float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])


# ------------------------------------------------------------------ indicators
def ema(values, length):
    k = 2 / (length + 1)
    out = sum(values[:length]) / length
    for v in values[length:]:
        out = v * k + out * (1 - k)
    return out


def atr(candles, length=ATR_LEN):
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(candles, candles[1:])]
    value = sum(trs[:length]) / length
    for tr in trs[length:]:
        value = (value * (length - 1) + tr) / length
    return value


def signal(candles):
    """('long'|'short'|None, atr, close) on the last closed candle."""
    if len(candles) < EMA_LEN + 2:
        return None, 0.0, 0.0
    closes = [c["c"] for c in candles]
    last = candles[-1]
    prior = candles[-CHANNEL - 1:-1]
    trend = ema(closes, EMA_LEN)
    a = atr(candles)
    if last["c"] > max(c["h"] for c in prior) and last["c"] > trend:
        return "long", a, last["c"]
    if last["c"] < min(c["l"] for c in prior) and last["c"] < trend:
        return "short", a, last["c"]
    return None, a, last["c"]


# ------------------------------------------------------------------ simulated account
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_state.json")


def _default():
    return {"enabled": False, "leverage": DEFAULT_LEVERAGE, "balance": START_USD, "start": START_USD,
            "position": None, "history": [], "last_candle": 0}


def _load():
    path = _path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else _default()


def _save(data):
    path = _path()
    fd, name = tempfile.mkstemp(prefix=".btc-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(name, path)


def set_enabled(value):
    with _LOCK:
        data = _load(); data["enabled"] = bool(value); _save(data)


def set_leverage(value):
    value = int(value)
    if value not in LEVERAGES:
        raise ValueError("Alavancagem inválida")
    with _LOCK:
        data = _load(); data["leverage"] = value; _save(data)
    return value


def reset():
    with _LOCK:
        old = _load()
        data = _default(); data.update(enabled=old.get("enabled", False), leverage=old.get("leverage", DEFAULT_LEVERAGE))
        _save(data)


def _pnl(pos, price):
    move = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "long" else -1)
    hours = (time.time() - pos["opened_at"]) / 3600
    return pos["size_usd"] * move - pos["size_usd"] * (FEE_RATE * 2 + BORROW_RATE_HOUR * hours)


def _liq_price(pos):
    """Approximate price where losses reach LIQ_LOSS of collateral."""
    move = LIQ_LOSS * pos["collateral"] / pos["size_usd"]
    return pos["entry"] * (1 - move if pos["side"] == "long" else 1 + move)


def _open(data, side, price, a, send, chat):
    lev = int(data["leverage"])
    collateral = round(data["balance"] * ORDER_FRACTION, 2)
    if collateral < 1:
        send(chat, "💤 BTC: saldo simulado baixo demais para abrir posição.")
        return
    size = collateral * lev
    max_dist = MAX_STOP_OF_LIQ * LIQ_LOSS / lev   # keep the stop well before liquidation
    dist = min(STOP_ATR * a / price, max_dist)
    stop = price * (1 - dist if side == "long" else 1 + dist)
    data["balance"] -= collateral
    data["position"] = {"side": side, "entry": price, "collateral": collateral, "size_usd": size, "leverage": lev,
                        "stop": stop, "best": price, "atr": a, "opened_at": time.time()}
    pos = data["position"]
    send(chat, ("🟢 LONG" if side == "long" else "🔴 SHORT") + " BTC " + str(lev) + "x (SIMULADO, preço real)\n"
         "Entrada: $" + format(price, ",.0f") + "\nGarantia: $" + format(collateral, ".2f") + " (20% da conta) → posição $" +
         format(size, ",.2f") + "\n🛑 Stop: $" + format(stop, ",.0f") + " (" + format(dist * 100, ".2f") + "% no preço = −" +
         format(dist * lev * 100, ".1f") + "% da garantia)\n💀 Liquidação ≈ $" + format(_liq_price(pos), ",.0f") +
         "\n📈 Stop móvel: 3×ATR do melhor preço")


def _close(data, price, reason, send, chat):
    pos = data["position"]
    pnl = max(_pnl(pos, price), -pos["collateral"])
    data["balance"] += pos["collateral"] + pnl
    data["position"] = None
    data["history"].append({"symbol": "BTC " + pos["side"].upper() + " " + str(pos["leverage"]) + "x", "mode": "btc",
                            "cost_usd": pos["collateral"], "pnl_usd": round(pnl, 4), "reason": reason,
                            "closed_at": time.time(), "minutes": round((time.time() - pos["opened_at"]) / 60, 1),
                            "mint": "BTC"})
    data["history"] = data["history"][-300:]
    names = {"stop_loss": "stop", "trailing": "stop móvel", "reversal": "virada de tendência", "liquidated": "LIQUIDADO"}
    send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — FECHOU " + pos["side"].upper() + " BTC " +
         str(pos["leverage"]) + "x (" + names.get(reason, reason) + ")\nEntrada $" + format(pos["entry"], ",.0f") +
         " → saída $" + format(price, ",.0f") + "\nP/L: " + format(pnl, "+.2f") + " USD (" +
         format(pnl / pos["collateral"] * 100, "+.1f") + "% da garantia)\nSaldo simulado: $" + format(data["balance"], ".2f"))


def manage(data, price, send, chat):
    """Trailing stop, stop loss and liquidation check on the live price."""
    pos = data["position"]
    if not pos:
        return
    if _pnl(pos, price) <= -LIQ_LOSS * pos["collateral"]:
        _close(data, price, "liquidated", send, chat)
        return
    if pos["side"] == "long":
        pos["best"] = max(pos["best"], price)
        pos["stop"] = max(pos["stop"], pos["best"] - TRAIL_ATR * pos["atr"])
        hit = price <= pos["stop"]
    else:
        pos["best"] = min(pos["best"], price)
        pos["stop"] = min(pos["stop"], pos["best"] + TRAIL_ATR * pos["atr"])
        hit = price >= pos["stop"]
    if hit:
        trailed = (pos["side"] == "long" and pos["stop"] > pos["entry"]) or (pos["side"] == "short" and pos["stop"] < pos["entry"])
        _close(data, price, "trailing" if trailed else "stop_loss", send, chat)


def cycle(send, chat, candles=None, price=None):
    """Run every ~20 s: manage the open trade; on each new closed hour, act on the signal."""
    if not chat:
        return
    with _LOCK:
        data = _load()
        if not data.get("enabled"):
            return
        price = price if price is not None else fetch_price()
        manage(data, price, send, chat)
        candles = candles if candles is not None else fetch_candles()
        if candles and candles[-1]["t"] != data.get("last_candle"):
            data["last_candle"] = candles[-1]["t"]
            side, a, _ = signal(candles)
            pos = data["position"]
            if side and pos and pos["side"] != side:
                _close(data, price, "reversal", send, chat)
                pos = None
            if side and not pos and a > 0:
                _open(data, side, price, a, send, chat)
        _save(data)


def summary():
    with _LOCK:
        data = _load()
        pos = data["position"]
        equity = data["balance"] + (pos["collateral"] if pos else 0)
        return {"enabled": data.get("enabled", False), "leverage": data.get("leverage", DEFAULT_LEVERAGE),
                "balance": data["balance"], "equity": equity, "start": data["start"], "position": pos,
                "history": list(data["history"])}


def status_text(price=None):
    s = summary()
    lines = ["₿ ROBÔ BTC ALAVANCADO — " + ("🟢 LIGADO" if s["enabled"] else "🔴 DESLIGADO"),
             "Modo: SIMULAÇÃO com preço real (execução real na Jupiter Perps ainda não ligada)",
             "Alavancagem: " + str(s["leverage"]) + "x | 20% da conta por ordem",
             "Saldo simulado: $" + format(s["equity"], ".2f") + " (início $" + format(s["start"], ".2f") + ", " +
             format((s["equity"] / s["start"] - 1) * 100 if s["start"] else 0, "+.1f") + "%)"]
    pos = s["position"]
    if pos:
        line = ("Posição: " + pos["side"].upper() + " " + str(pos["leverage"]) + "x desde $" + format(pos["entry"], ",.0f") +
                " | stop $" + format(pos["stop"], ",.0f"))
        if price:
            pnl = _pnl(pos, price)
            line += "\nAgora $" + format(price, ",.0f") + " → P/L " + format(pnl, "+.2f") + " USD (" + \
                    format(pnl / pos["collateral"] * 100, "+.1f") + "%)"
        lines.append(line)
    else:
        lines.append("Posição: nenhuma (esperando rompimento de 20h a favor da média de 200)")
    closed = [t for t in s["history"] if t.get("pnl_usd") is not None]
    if closed:
        wins = sum(1 for t in closed if t["pnl_usd"] > 0)
        lines.append("Operações: " + str(len(closed)) + " | acerto " + format(wins / len(closed) * 100, ".0f") +
                     "% | P/L " + format(sum(t["pnl_usd"] for t in closed), "+.2f") + " USD")
    lines.append("⚠️ Alavancagem multiplica ganhos E perdas. Nenhuma estratégia garante lucro.")
    return "\n".join(lines)
