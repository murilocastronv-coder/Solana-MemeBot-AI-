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

Two timeframes: "1h" (trend) and "5m" (intraday momentum breakouts all day;
tighter ATR stops and a fee filter that skips trades whose stop is too small
compared with Jupiter's fees).
Two modes: "sim" (paper money, real prices) and "real" (bot wallet on Jupiter
Perps through jupiter_perps.py, USDC collateral).
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


TIMEFRAMES = {"1h": {"seconds": 3600, "stop_atr": 2.0, "trail_atr": 3.0},
              "5m": {"seconds": 300, "stop_atr": 1.5, "trail_atr": 2.5}}
MIN_STOP_PCT = {"1h": 0.0, "5m": 0.0035}  # 5m: skip trades where fees would eat > ~1/3 of the risk


def fetch_candles(tf="1h"):
    """Closed BTC/USD candles, oldest first (Binance, Coinbase fallback)."""
    secs = TIMEFRAMES[tf]["seconds"]
    try:
        rows = _get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=" + tf + "&limit=300")
        candles = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                        "v": float(r[5])}
                   for r in rows]
    except Exception:
        rows = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=" + str(secs))
        candles = sorted(({"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4]),
                          "v": float(r[5])}
                          for r in rows), key=lambda c: c["t"])
    now = time.time()
    return [c for c in candles if c["t"] + secs <= now]  # drop the candle still forming


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


# ------------------------------------------------------------------ "Compra no fundo" (dip buying with up to 3 buys)
# Long only. On 5m candles: after a big drop, buy US$ 25 at 5x; if it keeps falling a good distance,
# buy US$ 25 more (max 3 buys). One buy: close at +US$ 2.50 net of fees. 2-3 buys: close as soon as the
# whole position reaches DIP_TP_MULTI net. Emergency stop before liquidation.
DIP_USD = 25.0
DIP_LEV = 5
DIP_MAX_BUYS = 3
DIP_DROP = 0.015          # price >= 1.5% below the highest high of the last 2 hours (24 candles)
DIP_LOOKBACK = 24
DIP_RSI = 30              # RSI 14 below 30 (oversold) ...
DIP_GAP = 0.02            # re-buy when price is 2% below the last buy
DIP_TP_FIRST = 2.50       # net profit target (US$) with a single buy
DIP_TP_MULTI = 3.00       # net profit target (US$) with 2-3 buys
DIP_EMERGENCY = 0.07      # close all if price falls 7% below the average price (5x liquidates ~18%)


def rsi(closes, length=14):
    gains = losses = 0.0
    for a, b in zip(closes[-length - 1:-1], closes[-length:]):
        d = b - a
        gains += max(d, 0); losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / length) / (losses / length)
    return 100 - 100 / (1 + rs)


def dip_signal(candles, drop=None, rsi_max=None):
    """(buy?, info) on the last closed 5m candle: big drop from the 2-hour high, oversold RSI, and the
    first green candle (sign of a bottom)."""
    drop = DIP_DROP if drop is None else drop
    rsi_max = DIP_RSI if rsi_max is None else rsi_max
    if len(candles) < DIP_LOOKBACK + 16:
        return False, {"drop": 0.0, "rsi": 50.0}
    closes = [c["c"] for c in candles]
    last = candles[-1]
    high = max(c["h"] for c in candles[-DIP_LOOKBACK - 1:-1])
    fall = 1 - last["c"] / high
    r = min(rsi(closes), rsi(closes[:-1]))
    ok = fall >= drop and r < rsi_max and last["c"] > last["o"]
    return ok, {"drop": fall, "rsi": r}


# ------------------------------------------------------------------ simulated account
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_state.json")


def _default():
    return {"enabled": False, "leverage": DEFAULT_LEVERAGE, "balance": START_USD, "start": START_USD,
            "position": None, "history": [], "last_candle": 0, "mode": "sim", "timeframe": "1h",
            "real_position": None, "real_history": [], "real_start": None,
            "strategy": "trend"}


def _load():
    path = _path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = _default()
    merged.update(data)
    if merged.get("strategy") == "scalp":       # old M5 scalping was replaced by the dip strategy
        merged["strategy"] = "dip"
    return merged


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


def set_option(name, value):
    allowed = {"mode": ("sim", "real"), "timeframe": tuple(TIMEFRAMES), "strategy": ("trend", "dip")}
    if value not in allowed[name]:
        raise ValueError("Opção inválida")
    with _LOCK:
        data = _load()
        if name == "mode" and data.get("position" if data["mode"] == "sim" else "real_position"):
            raise ValueError("Feche a posição aberta antes de trocar de modo")
        if name == "mode" and value == "real" and not data.get("real_test_ok"):
            raise ValueError("Faça primeiro o 🧪 Teste real $5 (precisa passar uma vez)")
        if name == "mode" and value == "real" and not data.get("real_start"):
            try:
                data["real_start"] = real_equity()
            except Exception:
                pass
        data[name] = value
        if name in ("timeframe", "strategy"):
            data["last_candle"] = 0
        _save(data)
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


# ------------------------------------------------------------------ books (sim / real)
def _keys(data):
    return ("position", "history") if data["mode"] == "sim" else ("real_position", "real_history")


def _tf(data):
    return TIMEFRAMES[data.get("timeframe", "1h")]


SOL_RESERVE_LAMPORTS = 50_000_000  # keep 0.05 SOL for fees and account rent


def real_balances():
    """(sol, usdc, sol_price) of the bot wallet."""
    import jupiter_perps as jp
    import live_order_bridge as bridge
    from bot_wallet_service import get_address
    owner = get_address()
    if not owner:
        raise RuntimeError("Carteira do bot não encontrada")
    return bridge.get_sol_balance(owner) / 1e9, jp.usdc_balance(owner), float(bridge.sol_usd_price())


def real_equity():
    sol, usdc, sol_price = real_balances()
    return sol * sol_price + usdc


def ensure_usdc(amount):
    """Swap SOL -> USDC through Jupiter if the wallet lacks USDC for the collateral."""
    import jupiter_perps as jp
    import live_order_bridge as bridge
    from bot_wallet_service import get_address
    from jupiter_v2_executor import order, sign_order, execute_signed, SOL_MINT, USDC_MINT
    owner = get_address()
    have = jp.usdc_balance(owner)
    if have >= amount:
        return have
    need_usd = (amount - have) * 1.02
    lamports = int(need_usd / float(bridge.sol_usd_price()) * 1e9)
    if bridge.get_sol_balance(owner) < lamports + SOL_RESERVE_LAMPORTS:
        raise RuntimeError("Saldo insuficiente: deposite mais SOL na carteira do bot")
    quote = order(SOL_MINT, USDC_MINT, lamports, taker=owner)
    execute_signed(sign_order(quote, os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"]), quote["requestId"])
    for _ in range(20):
        time.sleep(3)
        have = jp.usdc_balance(owner)
        if have >= amount:
            return have
    raise RuntimeError("Troca SOL→USDC não confirmou a tempo")


def _open(data, side, price, a, send, chat):
    lev = int(data["leverage"])
    tf = data.get("timeframe", "1h")
    real = data["mode"] == "real"
    pos_key, _ = _keys(data)
    try:
        equity = real_equity() if real else data["balance"]
    except Exception as exc:
        send(chat, "⛔ BTC real: não consegui ler o saldo da carteira (" + str(exc)[:150] + ").")
        return
    collateral = round(equity * ORDER_FRACTION, 2)
    if collateral < (5 if real else 1):
        send(chat, "💤 BTC: saldo baixo demais para abrir posição (20% = $" + format(collateral, ".2f") +
             (", mínimo $5 no real)." if real else ")."))
        return
    size = collateral * lev
    max_dist = MAX_STOP_OF_LIQ * LIQ_LOSS / lev   # keep the stop well before liquidation
    dist = min(_tf(data)["stop_atr"] * a / price, max_dist)
    if dist < MIN_STOP_PCT[tf]:
        logging.info("BTC %s signal skipped: stop %.3f%% too small vs fees", tf, dist * 100)
        return
    entry, signature = price, None
    if real:
        import jupiter_perps as jp
        try:
            ensure_usdc(collateral)
            onchain = jp.open_position(side, collateral, lev, price)
        except Exception as exc:
            send(chat, "⛔ BTC REAL: ordem " + side.upper() + " não aberta: " + str(exc)[:300])
            return
        entry, signature = onchain["price"] or price, onchain.get("signature")
        size, collateral = onchain["size_usd"] or size, onchain["collateral_usd"] or collateral
    else:
        data["balance"] -= collateral
    stop = entry * (1 - dist if side == "long" else 1 + dist)
    data[pos_key] = {"side": side, "entry": entry, "collateral": collateral, "size_usd": size, "leverage": lev,
                     "stop": stop, "best": entry, "atr": a, "opened_at": time.time(), "timeframe": tf,
                     "strategy": "trend"}
    pos = data[pos_key]
    send(chat, ("🟢 LONG" if side == "long" else "🔴 SHORT") + " BTC " + str(lev) + "x " +
         ("💵 DINHEIRO REAL" if real else "(SIMULADO, preço real)") + " · gráfico " + tf + "\n"
         "Entrada: $" + format(entry, ",.0f") + "\nGarantia: $" + format(collateral, ".2f") + " (20% da conta) → posição $" +
         format(size, ",.2f") + "\n🛑 Stop: $" + format(stop, ",.0f") + " (" + format(dist * 100, ".2f") + "% no preço = −" +
         format(dist * lev * 100, ".1f") + "% da garantia)\n💀 Liquidação ≈ $" + format(_liq_price(pos), ",.0f") +
         "\n📈 Stop móvel por ATR" + ("\n🔗 https://solscan.io/tx/" + signature if signature else ""))


def _close(data, price, reason, send, chat):
    real = data["mode"] == "real"
    pos_key, hist_key = _keys(data)
    pos = data[pos_key]
    signature = None
    if real:
        import jupiter_perps as jp
        try:
            before = jp.usdc_balance(jp.Pubkey.from_string(_owner()))
            result = jp.close_position(pos["side"], price)
            after = jp.usdc_balance(jp.Pubkey.from_string(_owner()))
            signature = result.get("signature")
        except Exception as exc:
            send(chat, "⛔ BTC REAL: não consegui FECHAR a posição (" + str(exc)[:250] + "). Tento de novo em ~20 s. "
                 "Se continuar, feche no site da Jupiter (jup.ag/perps) com a carteira do bot.")
            return False
        pnl = (after - before) - pos["collateral"] if not result.get("already_closed") else _pnl(pos, price)
    else:
        pnl = max(_pnl(pos, price), -pos["collateral"])
        data["balance"] += pos["collateral"] + pnl
    data[pos_key] = None
    data[hist_key].append({"symbol": "BTC " + pos["side"].upper() + " " + str(pos["leverage"]) + "x" +
                           (" (" + str(len(pos["entries"])) + " compras)" if pos.get("entries") else ""), "mode": "btc",
                           "cost_usd": pos["collateral"], "pnl_usd": round(pnl, 4), "reason": reason,
                           "closed_at": time.time(), "minutes": round((time.time() - pos["opened_at"]) / 60, 1),
                           "mint": "BTC", "timeframe": pos.get("timeframe", "1h")})
    data[hist_key] = data[hist_key][-300:]
    names = {"stop_loss": "stop", "trailing": "stop móvel", "reversal": "virada de tendência", "liquidated": "LIQUIDADO",
             "take_profit": "lucro alvo", "emergency": "stop de emergência"}
    send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — FECHOU " + pos["side"].upper() + " BTC " +
         str(pos["leverage"]) + "x " + ("💵 REAL" if real else "(simulado)") + " (" + names.get(reason, reason) + ")\nEntrada $" +
         format(pos["entry"], ",.0f") + " → saída $" + format(price, ",.0f") + "\nP/L: " + format(pnl, "+.2f") + " USD (" +
         format(pnl / pos["collateral"] * 100, "+.1f") + "% da garantia)" +
         ("" if real else "\nSaldo simulado: $" + format(data["balance"], ".2f")) +
         ("\n🔗 https://solscan.io/tx/" + signature if signature else ""))
    return True


def _owner():
    from bot_wallet_service import get_address
    return get_address()


def manage(data, price, send, chat):
    """Trailing stop, stop loss and liquidation check on the live price."""
    pos_key, _ = _keys(data)
    pos = data[pos_key]
    if not pos:
        return
    if _pnl(pos, price) <= -LIQ_LOSS * pos["collateral"]:
        _close(data, price, "liquidated", send, chat)
        return
    if pos.get("strategy") == "dip":
        return                      # exits handled by _dip_step
    trail = TIMEFRAMES[pos.get("timeframe", "1h")]["trail_atr"]
    if pos["side"] == "long":
        pos["best"] = max(pos["best"], price)
        if trail:
            pos["stop"] = max(pos["stop"], pos["best"] - trail * pos["atr"])
        hit = price <= pos["stop"]
    else:
        pos["best"] = min(pos["best"], price)
        if trail:
            pos["stop"] = min(pos["stop"], pos["best"] + trail * pos["atr"])
        hit = price >= pos["stop"]
    if hit:
        trailed = (pos["side"] == "long" and pos["stop"] > pos["entry"]) or (pos["side"] == "short" and pos["stop"] < pos["entry"])
        _close(data, price, "trailing" if trailed else "stop_loss", send, chat)


def cycle(send, chat, candles=None, price=None):
    """Run every ~20 s: manage the open trade; on each new closed candle, act on the signal."""
    if not chat:
        return
    with _LOCK:
        data = _load()
        if not data.get("enabled"):
            return
        pos_key, _ = _keys(data)
        price = price if price is not None else fetch_price()
        manage(data, price, send, chat)
        if data.get("strategy") == "dip":
            _dip_step(data, price, send, chat, candles if candles is not None else fetch_candles("5m"))
            _save(data)
            return
        candles = candles if candles is not None else fetch_candles(data.get("timeframe", "1h"))
        if candles and candles[-1]["t"] != data.get("last_candle"):
            data["last_candle"] = candles[-1]["t"]
            side, a, _ = signal(candles)
            pos = data[pos_key]
            if side and pos and pos["side"] != side:
                if _close(data, price, "reversal", send, chat):
                    pos = None
            if side and not pos and a > 0:
                _open(data, side, price, a, send, chat)
        _save(data)


def _account_value(data):
    pos_key, _ = _keys(data)
    pos = data[pos_key]
    base = real_equity() if data["mode"] == "real" else data["balance"]
    return base + (pos["collateral"] if pos else 0)


def _dip_add(data, price, send, chat):
    """One buy of US$ 25 at DIP_LEV x (first buy or re-buy). Returns True if it went through."""
    real = data["mode"] == "real"
    pos_key, _ = _keys(data)
    pos = data[pos_key]
    n = len(pos["entries"]) + 1 if pos else 1
    collateral, size = DIP_USD, DIP_USD * DIP_LEV
    entry, signature = price, None
    if real:
        import jupiter_perps as jp
        try:
            ensure_usdc(collateral)
            onchain = jp.open_position("long", collateral, DIP_LEV, price,
                                       min_size=(pos["size_usd"] if pos else 0))
        except Exception as exc:
            send(chat, "⛔ Compra " + str(n) + " não executada: " + str(exc)[:300])
            return False
        signature = onchain.get("signature")
        entry = price
    else:
        if data["balance"] < collateral:
            send(chat, "💤 Saldo simulado insuficiente para a compra " + str(n) + " ($25).")
            return False
        data["balance"] -= collateral
    if not pos:
        pos = data[pos_key] = {"side": "long", "entry": entry, "collateral": 0.0, "size_usd": 0.0,
                               "leverage": DIP_LEV, "stop": 0.0, "best": entry, "atr": 0.0,
                               "opened_at": time.time(), "timeframe": "5m", "strategy": "dip", "entries": []}
    pos["entries"].append({"price": entry, "size": size, "collateral": collateral, "at": time.time()})
    pos["entry"] = sum(e["size"] for e in pos["entries"]) / sum(e["size"] / e["price"] for e in pos["entries"])
    pos["size_usd"] += size
    pos["collateral"] += collateral
    target = DIP_TP_FIRST if n == 1 else DIP_TP_MULTI
    send(chat, ("🟢 COMPRA NO FUNDO" if n == 1 else "🔁 RECOMPRA " + str(n) + "/" + str(DIP_MAX_BUYS)) +
         " — BTC LONG " + str(DIP_LEV) + "x " + ("💵 REAL" if real else "(simulado)") + "\nPreço: $" + format(entry, ",.0f") +
         " · garantia $25 → posição $" + format(size, ".0f") + "\nTotal: " + str(n) + " compra(s), $" + format(pos["collateral"], ".0f") +
         " de garantia, preço médio $" + format(pos["entry"], ",.0f") +
         "\n🎯 Fecha tudo com lucro líquido ≥ $" + format(target, ".2f") +
         (" · próxima recompra em $" + format(entry * (1 - DIP_GAP), ",.0f") if n < DIP_MAX_BUYS else " · sem mais recompras") +
         "\n🛑 Stop de emergência: $" + format(pos["entry"] * (1 - DIP_EMERGENCY), ",.0f") +
         ("\n🔗 https://solscan.io/tx/" + signature if signature else ""))
    return True


def _dip_step(data, price, send, chat, candles):
    """Manage the open dip position every ~10 s; look for a new bottom at each closed 5m candle."""
    pos_key, _ = _keys(data)
    pos = data[pos_key]
    if pos and pos.get("strategy") == "dip":
        net = _pnl(pos, price)
        n = len(pos["entries"])
        if net >= (DIP_TP_FIRST if n == 1 else DIP_TP_MULTI):
            _close(data, price, "take_profit", send, chat)
        elif DIP_EMERGENCY and price <= pos["entry"] * (1 - DIP_EMERGENCY):
            _close(data, price, "emergency", send, chat)
        elif n < DIP_MAX_BUYS and price <= pos["entries"][-1]["price"] * (1 - DIP_GAP):
            _dip_add(data, price, send, chat)
        return
    if pos:
        return                                   # a trend position is open; leave it alone
    if not candles or candles[-1]["t"] == data.get("last_candle"):
        return
    data["last_candle"] = candles[-1]["t"]
    ok, info = dip_signal(candles)
    data["dip_last"] = info
    if time.time() - data.get("dip_report_at", 0) >= 3600:
        data["dip_report_at"] = time.time()
        send(chat, "🎯 Compra no fundo ligada: esperando queda. Agora o BTC está " + format(info["drop"] * 100, ".2f") +
             "% abaixo da máxima de 2 h (precisa ≥ " + format(DIP_DROP * 100, ".1f") + "%), RSI " +
             format(info["rsi"], ".0f") + " (precisa < " + str(DIP_RSI) + ").")
    if ok:
        _dip_add(data, price, send, chat)


def test_real_order(send, chat, price=None):
    """Owner-triggered check: open a US$ 5, 2x LONG on Jupiter Perps and close it right away."""
    import jupiter_perps as jp
    price = price or fetch_price()
    send(chat, "🧪 TESTE REAL: abrindo LONG BTC 2x com $5 de garantia na Jupiter Perps...")
    try:
        ensure_usdc(5)
        opened = jp.open_position("long", 5, 2, price)
        send(chat, "✅ Abriu: posição $" + format(opened["size_usd"], ".2f") + " a $" + format(opened["price"], ",.0f") +
             "\n🔗 https://solscan.io/tx/" + str(opened.get("signature")) + "\nFechando...")
        closed = jp.close_position("long", fetch_price())
        with _LOCK:
            data = _load(); data["real_test_ok"] = True; _save(data)
        send(chat, "✅ Fechou. Teste OK — a ligação com a Jupiter Perps funciona.\n🔗 https://solscan.io/tx/" +
             str(closed.get("signature")) + "\nAgora pode usar ₿ Modo REAL.")
        return True
    except Exception as exc:
        send(chat, "❌ Teste falhou: " + str(exc)[:350] + "\nNada foi deixado aberto se a simulação recusou. "
             "Confira em jup.ag/perps com a carteira do bot. Mande este print para o suporte.")
        return False


def summary():
    with _LOCK:
        data = _load()
        pos_key, hist_key = _keys(data)
        pos = data[pos_key]
        if data["mode"] == "real":
            try:
                equity = real_equity() + (pos["collateral"] if pos else 0)
            except Exception:
                equity = 0.0
            start = data.get("real_start") or equity
        else:
            equity = data["balance"] + (pos["collateral"] if pos else 0)
            start = data["start"]
        return {"enabled": data.get("enabled", False), "leverage": data.get("leverage", DEFAULT_LEVERAGE),
                "mode": data["mode"], "timeframe": data.get("timeframe", "1h"), "strategy": data.get("strategy", "trend"),
                "balance": data["balance"], "equity": equity, "start": start, "position": pos,
                "history": list(data[hist_key])}


def status_text(price=None):
    s = summary()
    real = s["mode"] == "real"
    lines = ["₿ ROBÔ BTC ALAVANCADO — " + ("🟢 LIGADO" if s["enabled"] else "🔴 DESLIGADO"),
             "Modo: " + ("💵 DINHEIRO REAL (carteira do bot, Jupiter Perps)" if real else "🧪 SIMULAÇÃO com preço real"),
             ("Estratégia: 🎯 COMPRA NO FUNDO (só long, $25 × " + str(DIP_LEV) + "x, até 3 compras, recompra a cada −" + format(DIP_GAP * 100, ".0f") + "%)" if s.get("strategy") == "dip" else
              "Estratégia: 📈 Tendência · gráfico " + s["timeframe"]),
             "Alavancagem: " + str(s["leverage"]) + "x | 20% da conta por ordem",
             ("Conta real" if real else "Saldo simulado") + ": $" + format(s["equity"], ".2f") +
             ("" if real else " (início $" + format(s["start"], ".2f") + ", " +
              format((s["equity"] / s["start"] - 1) * 100 if s["start"] else 0, "+.1f") + "%)")]
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
        lines.append("Posição: nenhuma (esperando rompimento a favor da média de 200)")
    closed = [t for t in s["history"] if t.get("pnl_usd") is not None]
    if closed:
        wins = sum(1 for t in closed if t["pnl_usd"] > 0)
        lines.append("Operações: " + str(len(closed)) + " | acerto " + format(wins / len(closed) * 100, ".0f") +
                     "% | P/L " + format(sum(t["pnl_usd"] for t in closed), "+.2f") + " USD")
    lines.append("⚠️ Alavancagem multiplica ganhos E perdas. Nenhuma estratégia garante lucro.")
    return "\n".join(lines)
