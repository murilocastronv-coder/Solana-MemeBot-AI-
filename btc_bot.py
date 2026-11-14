"""BTC 20x on Jupiter Perpetuals with the Sniper's strategy: go LONG when buyers flood in.

Real money, owner's choice: US$ 20 of USDC collateral per trade, 20x leverage (US$ 400 position),
one position at a time, wallet of the bot (SOL is swapped to USDC for the collateral and back after).

Entry (1-minute BTC candles with taker-buy volume):
- last 5 min: price up +0.25% .. +1.5% (moving, not a vertical spike);
- 5-min volume >= 1.5x the average 5-min volume of the previous hour (a lot of people trading);
- aggressive buyers >= 58% of the 5-min volume (most of them buying);
- price above the 60-minute EMA (uptrend).
Exit, on the position value (collateral + P/L - fees), the same rules as the Sniper:
- stop loss -30% of the collateral (BTC about -1.5% at 20x; liquidation is near -4.5%);
- trailing stop from +15%: sells 10% below the best value;
- 60 minutes without reaching +15%: closes;
- 5 losses in a row: pauses itself. No daily loss limit.
Leverage cuts both ways: at 20x a 1.5% BTC move is 30% of the collateral. Nothing here is a promise.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests


def _f(name, default):
    return float(os.getenv(name, str(default)))


MARGIN_USD = _f("BTC_MARGIN_USD", 20)
LEVERAGE = _f("BTC_LEVERAGE", 20)
FEE_RATE = 0.0006                      # Jupiter Perps open/close fee per side, on the position size
STOP_LOSS = _f("BTC_STOP_LOSS", 0.30)
TRAIL_ARM = _f("BTC_TRAIL_ARM", 0.15)
TRAIL_DROP = _f("BTC_TRAIL_DROP", 0.10)
NO_MOVE_S = _f("BTC_NO_MOVE_MINUTES", 60) * 60
MIN_UP_5M = _f("BTC_MIN_UP_5M", 0.0025)
MAX_UP_5M = _f("BTC_MAX_UP_5M", 0.015)
MIN_VOL_MULT = _f("BTC_MIN_VOL_MULT", 1.5)
MIN_BUY_SHARE = _f("BTC_MIN_BUY_SHARE", 0.58)
TREND_EMA = int(_f("BTC_TREND_EMA", 60))
COOLDOWN_S = _f("BTC_COOLDOWN_MINUTES", 30) * 60
MAX_LOSS_STREAK = int(_f("BTC_MAX_LOSS_STREAK", 5))
CHECK_EVERY_S = _f("BTC_CHECK_SECONDS", 20)
SOURCES = ("https://api.binance.com", "https://api.binance.us")

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "why": "", "price": None}


# ------------------------------------------------------------------ state
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_bot.json")


def _load():
    d = {"enabled": True, "open": None, "history": [], "last_close": 0, "paused_why": ""}
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btc.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        if on:
            d["paused_why"] = ""
        _save(d)


def enabled():
    return bool(_load()["enabled"])


# ------------------------------------------------------------------ market data
def klines(limit=90, get=None):
    """1-minute BTCUSDT candles [(t, open, high, low, close, volume, taker_buy_volume)], oldest first."""
    last_err = None
    for base in SOURCES:
        try:
            if get:
                rows = get(base)
            else:
                r = _http.get(base + "/api/v3/klines", params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
                              timeout=10)
                r.raise_for_status()
                rows = r.json()
            return [(int(x[0]) / 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[9]))
                    for x in rows]
        except Exception as exc:
            last_err = exc
    raise RuntimeError("sem dados do BTC (" + type(last_err).__name__ + ")")


def ema(values, n):
    k, out = 2 / (n + 1), None
    for v in values:
        out = v if out is None else v * k + out * (1 - k)
    return out


def entry_reason(cs):
    """'' when buyers are flooding BTC right now (same idea as the Sniper); else why not."""
    if len(cs) < 70:
        return "poucas velas"
    closed = cs[:-1]                                  # the last candle is still forming
    last5, prev = closed[-5:], closed[-65:-5]
    up = closed[-1][4] / closed[-6][4] - 1
    vol5 = sum(c[5] for c in last5)
    avg5 = sum(c[5] for c in prev) / len(prev) * 5
    buy_share = sum(c[6] for c in last5) / vol5 if vol5 else 0
    trend = ema([c[4] for c in closed[-TREND_EMA * 2:]], TREND_EMA)
    if up < MIN_UP_5M:
        return "sem subida forte em 5 min (" + format(up * 100, "+.2f") + "%)"
    if up > MAX_UP_5M:
        return "pico vertical (" + format(up * 100, "+.2f") + "%)"
    if avg5 and vol5 < MIN_VOL_MULT * avg5:
        return "pouca gente operando (volume " + format(vol5 / avg5, ".1f") + "x)"
    if buy_share < MIN_BUY_SHARE:
        return "compradores fracos (" + format(buy_share * 100, ".0f") + "%)"
    if closed[-1][4] < trend:
        return "abaixo da média de 60 min"
    return ""


def position_ratio(pos, price):
    """Value of the position / collateral, with the open + close fees."""
    size, entry, coll = pos["size_usd"], pos["entry"], pos["collateral_usd"]
    pnl = size * (price / entry - 1)
    return (coll + pnl - 2 * FEE_RATE * size) / coll


def exit_reason(pos, price, now=None):
    now = now or time.time()
    ratio = position_ratio(pos, price)
    peak = max(float(pos.get("peak", 1.0)), ratio)
    pos["peak"] = peak
    if ratio <= 1 - STOP_LOSS:
        return "stop_loss", ratio
    if peak >= 1 + TRAIL_ARM and ratio <= peak * (1 - TRAIL_DROP):
        return "trailing", ratio
    if NO_MOVE_S > 0 and now - pos["at"] > NO_MOVE_S and peak < 1 + TRAIL_ARM:
        return "time_stop", ratio
    return "", ratio


def levels(price):
    """BTC prices of the stop and of the +15% trailing start (for the messages)."""
    move = lambda r: price * (1 + (r - 1 + 2 * FEE_RATE * LEVERAGE) / LEVERAGE)
    return move(1 - STOP_LOSS), move(1 + TRAIL_ARM), price * (1 - 0.9 / LEVERAGE)


# ------------------------------------------------------------------ real orders
def _open_real(price):
    import copy_trader as ct
    import jupiter_perps as jp
    jp.ensure_usdc(MARGIN_USD, ct._sol_usd())
    pos = jp.open_position("long", MARGIN_USD, LEVERAGE, price)
    return {"entry": float(pos.get("price") or price), "size_usd": float(pos.get("size_usd") or MARGIN_USD * LEVERAGE),
            "collateral_usd": float(pos.get("collateral_usd") or MARGIN_USD), "signature": pos.get("signature")}


def _close_real(price):
    import jupiter_perps as jp
    out = jp.close_position("long", price)
    try:
        jp.usdc_to_sol()
    except Exception as exc:
        logging.warning("btc: USDC -> SOL after close failed: %s", type(exc).__name__)
    return out


def loss_streak(history):
    n = 0
    for h in reversed(history):
        if h["pnl_usd"] > 0:
            break
        n += 1
    return n


REASON = {"stop_loss": "🛑 STOP LOSS", "trailing": "🚀 STOP MÓVEL", "time_stop": "⏱️ 60 min sem andar"}


def cycle(send=None, chat=None, cs=None, now=None, opener=None, closer=None):
    """One check: manage the open position, or open one when buyers flood BTC."""
    now = now or time.time()
    d = _load()
    if not d["enabled"] and not d["open"]:
        return ""
    cs = cs if cs is not None else klines()
    price = cs[-1][4]
    LAST.update(at=now, price=price)
    pos = d["open"]
    if pos:
        why, ratio = exit_reason(pos, price, now)
        with _LOCK:
            dd = _load()
            if dd["open"]:
                dd["open"]["peak"] = pos["peak"]
                if pos["peak"] >= 1 + TRAIL_ARM and not dd["open"].get("armed"):
                    dd["open"]["armed"] = True
                    if send and chat:
                        send(chat, "🚀 STOP MÓVEL ATIVADO — BTC 20x " + format((ratio - 1) * 100, "+.0f") +
                             "%\nAgora vendo se cair " + format(TRAIL_DROP * 100, ".0f") + "% abaixo do topo.")
                _save(dd)
        if not why:
            LAST["why"] = "posição aberta " + format((ratio - 1) * 100, "+.1f") + "%"
            return ""
        if send and chat:
            send(chat, REASON[why] + " (" + format((ratio - 1) * 100, "+.1f") + "%) — FECHANDO BTC 20x")
        try:
            out = (closer or _close_real)(price)
        except Exception as exc:
            if send and chat:
                send(chat, "⛔ BTC: não consegui fechar (" + str(exc)[:150] + "). Tento de novo em 20 s.")
            return "close_failed"
        pnl = pos["collateral_usd"] * (ratio - 1)
        with _LOCK:
            dd = _load()
            dd["open"] = None
            dd["last_close"] = now
            dd["history"] = (dd["history"] + [{"at": pos["at"], "closed": now, "entry": pos["entry"], "exit": price,
                                               "pnl_usd": round(pnl, 2), "reason": why}])[-200:]
            if loss_streak(dd["history"]) >= MAX_LOSS_STREAK:
                dd.update(enabled=False, paused_why=str(MAX_LOSS_STREAK) + " perdas seguidas")
            _save(dd)
        if send and chat:
            send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — VENDEU BTC 20x\n" + REASON[why] +
                 "\n💵 Entrada US$ " + format(pos["entry"], ",.0f") + " → saída US$ " + format(price, ",.0f") +
                 "\n📊 Resultado: " + format(pnl, "+.2f") + " USD (" + format((ratio - 1) * 100, "+.1f") + "% da garantia)" +
                 ("\n🔗 https://solscan.io/tx/" + str(out.get("signature")) if isinstance(out, dict) and out.get("signature") else ""))
            if not _load()["enabled"]:
                send(chat, "⏸️ BTC 20x PAUSADO SOZINHO: " + str(MAX_LOSS_STREAK) + " perdas seguidas. Ligue de novo no botão.")
        return "closed"
    if not d["enabled"]:
        return ""
    if now - float(d.get("last_close") or 0) < COOLDOWN_S:
        LAST["why"] = "esperando " + format((COOLDOWN_S - (now - d["last_close"])) / 60, ".0f") + " min depois da última"
        return ""
    why = entry_reason(cs)
    LAST["why"] = why or "entrando"
    if why:
        return ""
    sl, tp1, liq = levels(price)
    if send and chat:
        send(chat, "₿ BTC 20x: muitos compradores agora — abrindo LONG (garantia US$ " + format(MARGIN_USD, ".0f") +
             ", posição US$ " + format(MARGIN_USD * LEVERAGE, ".0f") + ").")
    try:
        real = (opener or _open_real)(price)
    except Exception as exc:
        with _LOCK:
            dd = _load()
            dd["last_close"] = now                 # don't retry every 20 s
            _save(dd)
        if send and chat:
            send(chat, "⛔ BTC 20x não abriu: " + str(exc)[:200])
        return "open_failed"
    pos = dict(real, at=now, peak=1.0)
    with _LOCK:
        dd = _load()
        dd["open"] = pos
        _save(dd)
    if send and chat:
        sl, tp1, liq = levels(pos["entry"])
        send(chat, "🟢 ₿ COMPROU BTC 20x (LONG) na Jupiter\n💵 Entrada: `" + format(pos["entry"], ".2f") + "`\n"
             "🛑 Stop loss (−" + format(STOP_LOSS * 100, ".0f") + "% da garantia): `" + format(sl, ".2f") + "`\n"
             "🎯 Stop móvel a partir de: `" + format(tp1, ".2f") + "` (vende " + format(TRAIL_DROP * 100, ".0f") +
             "% abaixo do topo)\n⏱️ 60 min sem chegar lá: fecha\n💀 Liquidação ≈ " + format(liq, ",.0f") +
             ("\n🔗 https://solscan.io/tx/" + str(pos["signature"]) if pos.get("signature") else ""))
    return "opened"


def status_text():
    d = _load()
    lines = ["₿ BTC 20x NA JUPITER (dinheiro real) — " + ("LIGADO" if d["enabled"] else "DESLIGADO") +
             (" (" + d["paused_why"] + ")" if not d["enabled"] and d.get("paused_why") else ""),
             "Compra LONG quando muita gente compra o BTC: +" + format(MIN_UP_5M * 100, ".2f") + "% a +" +
             format(MAX_UP_5M * 100, ".1f") + "% em 5 min, volume " + format(MIN_VOL_MULT, "g") + "x a média, " +
             format(MIN_BUY_SHARE * 100, ".0f") + "%+ compradores, acima da média de 60 min.",
             "💵 Garantia US$ " + format(MARGIN_USD, ".0f") + " × " + format(LEVERAGE, ".0f") + " = posição US$ " +
             format(MARGIN_USD * LEVERAGE, ".0f") + " · 🛑 stop −" + format(STOP_LOSS * 100, ".0f") + "% da garantia · 🚀 stop móvel de +" +
             format(TRAIL_ARM * 100, ".0f") + "% (" + format(TRAIL_DROP * 100, ".0f") + "% abaixo do topo) · ⏱️ 60 min sem andar: fecha · "
             "⏸️ pausa com " + str(MAX_LOSS_STREAK) + " perdas seguidas"]
    if d["open"]:
        p = d["open"]
        now_ratio = position_ratio(p, LAST["price"]) if LAST.get("price") else None
        lines.append("📦 Aberta: entrada US$ " + format(p["entry"], ",.0f") +
                     (" · agora " + format((now_ratio - 1) * 100, "+.1f") + "%" if now_ratio else "") +
                     " · topo " + format((p.get("peak", 1) - 1) * 100, "+.1f") + "%" + (" · 🚀 stop móvel ATIVO" if p.get("armed") else ""))
    h = d["history"]
    if h:
        won = sum(1 for x in h if x["pnl_usd"] > 0)
        lines.append("📊 " + str(len(h)) + " fechadas · " + str(won) + " no lucro · total " +
                     format(sum(x["pnl_usd"] for x in h), "+.2f") + " USD")
    if LAST["why"]:
        lines.append("🔎 Agora: " + LAST["why"] + (" · BTC US$ " + format(LAST["price"], ",.0f") if LAST.get("price") else ""))
    lines.append("⚠️ 20x: o BTC cair 1,5% já leva 30% da garantia. Não há garantia de lucro.")
    return "\n".join(lines)
