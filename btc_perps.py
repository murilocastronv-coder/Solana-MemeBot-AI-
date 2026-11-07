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


# ------------------------------------------------------------------ M5 momentum scalping (EMA + RSI + Volume + ATR)
# Rules from the owner's spec. Every trade closes by the end of the M5 candle it was opened in.
SCALP_SL_PCT = 0.0025        # stop-loss 0.25%
SCALP_TP_PCT = 0.0040        # take-profit 0.40%
SCALP_FRACTION = 0.10        # US$ 10 on a US$ 100 account
SCALP_MIN_ATR_PCT = 0.0015   # ATR must be big enough to beat costs
SCALP_MAX_EMA9_DIST = 0.003  # price not too far from EMA 9 (0.3%)
SCALP_DAILY_LOSS = 0.03      # stop new trades after losing 3% of the account in the day
SCALP_MAX_LOSS_STREAK = 3    # pause after 3 consecutive losses ...
SCALP_STREAK_PAUSE = 3600    # ... until the next 1-hour window


def rsi(closes, length=14):
    gains = losses = 0.0
    for a, b in zip(closes[-length - 1:-1], closes[-length:]):
        d = b - a
        gains += max(d, 0); losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / length) / (losses / length)
    return 100 - 100 / (1 + rs)


def scalp_signal(candles):
    """EMA 9/21 + RSI 14 + volume > 20-candle average + momentum of the last closed candle + ATR,
    price close to EMA 9. Returns (side|None, atr, why)."""
    if len(candles) < 40:
        return None, 0.0, "histórico curto"
    closes = [c["c"] for c in candles]
    last, prev = candles[-1], candles[-2]
    a = atr(candles)
    price = last["c"]
    if a / price < SCALP_MIN_ATR_PCT:
        return None, a, "volatilidade baixa (ATR não cobre os custos)"
    vols = [c.get("v", 0.0) for c in candles[-21:-1]]
    if not vols or last.get("v", 0.0) <= sum(vols) / len(vols):
        return None, a, "volume abaixo da média de 20 velas"
    e9, e21 = ema(closes, 9), ema(closes, 21)
    if abs(price - e9) / price > SCALP_MAX_EMA9_DIST:
        return None, a, "preço longe demais da EMA 9"
    r = rsi(closes)
    if e9 > e21 and 52 <= r <= 70 and last["c"] > last["o"] and last["c"] > prev["c"]:
        return "long", a, ""
    if e9 < e21 and 30 <= r <= 48 and last["c"] < last["o"] and last["c"] < prev["c"]:
        return "short", a, ""
    return None, a, "sem confirmação (EMA/RSI/momentum)"


# ------------------------------------------------------------------ simulated account
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_state.json")


def _default():
    return {"enabled": False, "leverage": DEFAULT_LEVERAGE, "balance": START_USD, "start": START_USD,
            "position": None, "history": [], "last_candle": 0, "mode": "sim", "timeframe": "1h",
            "real_position": None, "real_history": [], "real_start": None,
            "strategy": "trend", "scalp_day": "", "scalp_day_start": 0.0, "scalp_paused_day": "",
            "scalp_stats": {"candles": 0, "signals": 0, "last_reason": ""}}


def _load():
    path = _path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = _default()
    merged.update(data)
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
    allowed = {"mode": ("sim", "real"), "timeframe": tuple(TIMEFRAMES), "strategy": ("trend", "scalp")}
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


def _open(data, side, price, a, send, chat, exit_at=None):
    lev = int(data["leverage"])
    scalp = exit_at is not None
    tf = "5m" if scalp else data.get("timeframe", "1h")
    real = data["mode"] == "real"
    pos_key, _ = _keys(data)
    try:
        equity = real_equity() if real else data["balance"]
    except Exception as exc:
        send(chat, "⛔ BTC real: não consegui ler o saldo da carteira (" + str(exc)[:150] + ").")
        return
    collateral = round(equity * (SCALP_FRACTION if scalp else ORDER_FRACTION), 2)
    if collateral < (5 if real else 1):
        send(chat, "💤 BTC: saldo baixo demais para abrir posição (20% = $" + format(collateral, ".2f") +
             (", mínimo $5 no real)." if real else ")."))
        return
    size = collateral * lev
    max_dist = MAX_STOP_OF_LIQ * LIQ_LOSS / lev   # keep the stop well before liquidation
    dist = min(SCALP_SL_PCT if scalp else _tf(data)["stop_atr"] * a / price, max_dist)
    if not scalp and dist < MIN_STOP_PCT[tf]:
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
                     "trail": not scalp, "exit_at": exit_at, "strategy": "scalp" if scalp else "trend",
                     "tp": (entry * (1 + SCALP_TP_PCT if side == "long" else 1 - SCALP_TP_PCT)) if scalp else None}
    pos = data[pos_key]
    send(chat, ("🟢 LONG" if side == "long" else "🔴 SHORT") + " BTC " + str(lev) + "x " +
         ("💵 DINHEIRO REAL" if real else "(SIMULADO, preço real)") + " · gráfico " + tf + "\n"
         "Entrada: $" + format(entry, ",.0f") + "\nGarantia: $" + format(collateral, ".2f") + " (" + ("10" if scalp else "20") + "% da conta) → posição $" +
         format(size, ",.2f") + "\n🛑 Stop: $" + format(stop, ",.0f") + " (" + format(dist * 100, ".2f") + "% no preço = −" +
         format(dist * lev * 100, ".1f") + "% da garantia)\n💀 Liquidação ≈ $" + format(_liq_price(pos), ",.0f") +
         ("\n🎯 Alvo: $" + format(pos["tp"], ",.0f") + " (+0,40%)\n⏱️ Fecha no fim da vela: " +
          time.strftime("%H:%M", time.gmtime(exit_at)) + " UTC" if scalp else
          "\n📈 Stop móvel por ATR") + ("\n🔗 https://solscan.io/tx/" + signature if signature else ""))


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
    if pos.get("strategy") == "scalp":
        data["scalp_streak"] = data.get("scalp_streak", 0) + 1 if pnl <= 0 else 0
        if data["scalp_streak"] >= SCALP_MAX_LOSS_STREAK:
            data["scalp_pause_until"] = time.time() + SCALP_STREAK_PAUSE
            data["scalp_streak"] = 0
            send(chat, "⏸️ Scalping: 3 perdas seguidas. Pausa de 1 hora antes de novas entradas.")
    data[hist_key].append({"symbol": "BTC " + pos["side"].upper() + " " + str(pos["leverage"]) + "x", "mode": "btc",
                           "cost_usd": pos["collateral"], "pnl_usd": round(pnl, 4), "reason": reason,
                           "closed_at": time.time(), "minutes": round((time.time() - pos["opened_at"]) / 60, 1),
                           "mint": "BTC", "timeframe": pos.get("timeframe", "1h")})
    data[hist_key] = data[hist_key][-300:]
    names = {"stop_loss": "stop", "trailing": "stop móvel", "reversal": "virada de tendência", "liquidated": "LIQUIDADO",
             "candle_close": "fim da vela", "take_profit": "alvo +0,40%"}
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
    trail = TIMEFRAMES[pos.get("timeframe", "1h")]["trail_atr"] if pos.get("trail", True) else None
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
    tp = pos.get("tp")
    if tp and not hit and ((pos["side"] == "long" and price >= tp) or (pos["side"] == "short" and price <= tp)):
        _close(data, price, "take_profit", send, chat)
        return
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
        if data.get("strategy") == "scalp":
            _scalp_step(data, price, send, chat, candles if candles is not None else fetch_candles("5m"))
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


def _scalp_step(data, price, send, chat, candles):
    """At each closed M5 candle: close the trade that belonged to it, then enter on a fresh signal."""
    pos_key, hist_key = _keys(data)
    pos = data[pos_key]
    now = time.time()
    if pos and pos.get("strategy") == "scalp" and now > (pos.get("exit_at") or now) + 60:
        _close(data, price, "candle_close", send, chat)   # safety: candle feed late, still exit on time
        pos = data[pos_key]
    if now - data.get("scalp_report_at", 0) >= 3600:
        data["scalp_report_at"] = now
        st = data["scalp_stats"]
        send(chat, "⚡ Scalping M5 ligado: " + str(st["candles"]) + " velas analisadas, " + str(st["signals"]) +
             " sinais na última hora.\nÚltima leitura: " + (st["last_reason"] or "aguardando primeira vela"))
        data["scalp_stats"] = {"candles": 0, "signals": 0, "last_reason": st["last_reason"]}
    if not candles or candles[-1]["t"] == data.get("last_candle"):
        return
    data["last_candle"] = candles[-1]["t"]
    stats = data["scalp_stats"]
    stats["candles"] += 1
    exit_at = candles[-1]["t"] + 600          # end of the candle that is starting now
    side, a, why = scalp_signal(candles)
    stats["last_reason"] = why if not side else "sinal " + side
    if side:
        stats["signals"] += 1
    if pos and pos.get("strategy") == "scalp" and now >= (pos.get("exit_at") or 0) - 15:
        if not _close(data, price, "candle_close", send, chat):   # never carried to the next candle
            return
        pos = None
    if pos or not side:
        return
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    try:
        value = _account_value(data)
    except Exception:
        return
    if data.get("scalp_day") != today:
        data.update(scalp_day=today, scalp_day_start=value)
    if data.get("scalp_paused_day") == today or now < data.get("scalp_pause_until", 0):
        return
    if value <= data["scalp_day_start"] * (1 - SCALP_DAILY_LOSS):
        data["scalp_paused_day"] = today
        send(chat, "⏸️ Scalping pausado até amanhã (00:00 UTC): perdeu 3% da conta hoje. "
             "Proteção contra dia ruim.")
        return
    _open(data, side, price, a, send, chat, exit_at=exit_at)


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
                "scalp_stats": dict(data.get("scalp_stats", {})),
                "balance": data["balance"], "equity": equity, "start": start, "position": pos,
                "history": list(data[hist_key])}


def status_text(price=None):
    s = summary()
    real = s["mode"] == "real"
    lines = ["₿ ROBÔ BTC ALAVANCADO — " + ("🟢 LIGADO" if s["enabled"] else "🔴 DESLIGADO"),
             "Modo: " + ("💵 DINHEIRO REAL (carteira do bot, Jupiter Perps)" if real else "🧪 SIMULAÇÃO com preço real"),
             ("Estratégia: ⚡ SCALPING M5 (momentum, fecha no fim da vela)" if s.get("strategy") == "scalp" else
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
