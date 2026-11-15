"""BTC robot with the strategy that earned the most in the out-of-sample months of the research:
RSI2 (Larry Connors) 10/90 in the direction of the EMA200, on 4-hour candles.

Real money on Jupiter Perpetuals, owner's choice (Nov 15): US$ 100 of collateral per trade, leverage
chosen by the owner between 2x and 5x (never more), one position at a time. Starts OFF.

Rules (same as the research backtest):
- after each 4-hour candle closes: RSI(2) < 10 and close above the EMA200 -> LONG;
  RSI(2) > 90 and close below the EMA200 -> SHORT (entered right after the close, like the next open);
- stop: 1 ATR(14) from the entry (at most 70% of the way to liquidation); checked every minute;
- no target; exit on the opposite signal or after 24 candles (4 days);
- safety: pauses itself when the closed trades lose US$ 50 in total (half the capital).
Picked as the best result AFTER seeing the out-of-sample months, so the future tends to be worse.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

import btc_research as br

TF = 14400
ENTRY = {"family": "rsi_rev", "period": 2, "low": 10, "high": 90, "trend": True}
STOP_ATR = 1.0
MAX_BARS = 24
CAPITAL_USD = float(os.getenv("BTC_CAPITAL_USD", "100"))
LEVERAGES = (2, 3, 4, 5)
MAX_LEVERAGE = 5
DEFAULT_LEVERAGE = 3
FEE_RATE = 0.0006
MAX_TOTAL_LOSS = float(os.getenv("BTC_MAX_TOTAL_LOSS_USD", "50"))
ENTRY_WINDOW_S = 60 * 60          # enter only within 1 h of the candle close (not late after a restart)
CHECK_EVERY_S = 60
DESC = "RSI2 Connors 10/90 a favor da EMA200 · gráfico 4h · stop 1 ATR · sem alvo · máx. 4 dias"

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "why": "", "price": None}


# ------------------------------------------------------------------ state
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_live.json")


def _load():
    d = {"enabled": False, "leverage": DEFAULT_LEVERAGE, "open": None, "history": [], "last_signal_t": 0,
         "paused_why": ""}
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
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btclive.")
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


def set_leverage(lev):
    lev = int(lev)
    if lev not in LEVERAGES:
        raise ValueError("alavancagem permitida: 2x a 5x")
    with _LOCK:
        d = _load()
        d["leverage"] = lev
        _save(d)
    return lev


def leverage():
    return min(int(_load().get("leverage") or DEFAULT_LEVERAGE), MAX_LEVERAGE)


# ------------------------------------------------------------------ market data
def _get(url, params=None):
    r = _http.get(url, params=params, timeout=12, headers={"User-Agent": "BTCLive/1.0"})
    r.raise_for_status()
    return r.json()


def klines_4h(limit=1000):
    """4-hour BTC candles (the last one is still forming), oldest first."""
    for base in ("https://api.binance.com", "https://api.binance.us"):
        try:
            rows = _get(base + "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "4h", "limit": limit})
            return [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                     "v": float(r[5])} for r in rows]
        except Exception:
            continue
    # Coinbase has no 4 h candles: build them from 1 h ones (300 per call)
    now = int(time.time()) // 3600 * 3600 + 3600
    rows = {}
    for k in range(8):
        end, start = now - k * 300 * 3600, now - (k + 1) * 300 * 3600
        for r in _get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                      {"granularity": 3600, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                       "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end))}):
            rows[int(r[0])] = {"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4]),
                               "v": float(r[5])}
    hourly = [rows[t] for t in sorted(rows)]
    groups = {}
    for c in hourly:
        groups.setdefault(c["t"] // TF * TF, []).append(c)
    out = []
    for t in sorted(groups):
        g = groups[t]
        out.append({"t": t, "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
                    "c": g[-1]["c"], "v": sum(x["v"] for x in g)})
    return out


def last_signal(closed):
    """(signal, atr, candle time) of the last CLOSED 4 h candle."""
    data = br.Data(closed, TF)
    return br.signals(data, ENTRY)[-1], data.atr[-1], closed[-1]["t"]


# ------------------------------------------------------------------ position math
def pnl_usd(pos, price):
    s = 1 if pos["side"] == "long" else -1
    return pos["size_usd"] * (price / pos["entry"] - 1) * s - 2 * FEE_RATE * pos["size_usd"]


def stop_price(side, entry, atr_v, lev):
    dist = min(STOP_ATR * atr_v, 0.7 * (0.9 / lev) * entry)
    return entry - dist if side == "long" else entry + dist


def exit_reason(pos, price, sig, sig_t, now):
    long_ = pos["side"] == "long"
    if (price <= pos["stop"]) if long_ else (price >= pos["stop"]):
        return "stop"
    if sig and sig != pos["side"] and sig_t > pos["signal_t"]:
        return "signal"
    if now - pos["at"] >= MAX_BARS * TF:
        return "time"
    return ""


REASON = {"stop": "🛑 STOP (1 ATR)", "signal": "🔄 SINAL CONTRÁRIO", "time": "⏱️ 4 DIAS (limite de tempo)"}


# ------------------------------------------------------------------ real orders
def _open_real(side, price, lev):
    import copy_trader as ct
    import jupiter_perps as jp
    jp.ensure_usdc(CAPITAL_USD, ct._sol_usd())
    pos = jp.open_position(side, CAPITAL_USD, lev, price)
    return {"entry": float(pos.get("price") or price), "size_usd": float(pos.get("size_usd") or CAPITAL_USD * lev),
            "collateral_usd": float(pos.get("collateral_usd") or CAPITAL_USD), "signature": pos.get("signature")}


def _close_real(side, price):
    import jupiter_perps as jp
    out = jp.close_position(side, price)
    try:
        jp.usdc_to_sol()
    except Exception as exc:
        logging.warning("btc live: USDC -> SOL after close failed: %s", type(exc).__name__)
    return out


def _tx(out):
    sig = out.get("signature") if isinstance(out, dict) else None
    return "\n🔗 https://solscan.io/tx/" + str(sig) if sig else ""


def cycle(send=None, chat=None, cs=None, now=None, opener=None, closer=None):
    """One check: manage the open position (stop every minute) or enter after a 4 h candle closes."""
    now = now or time.time()
    d = _load()
    if not d["enabled"] and not d["open"]:
        return ""
    cs = cs if cs is not None else klines_4h()
    price = cs[-1]["c"]
    closed = [c for c in cs if c["t"] + TF <= now]
    LAST.update(at=now, price=price)
    sig, atr_v, sig_t = last_signal(closed)
    pos = d["open"]
    if pos:
        why = exit_reason(pos, price, sig, sig_t, now)
        if not why:
            LAST["why"] = "posição aberta " + format(pnl_usd(pos, price), "+.2f") + " USD"
            return ""
        try:
            out = (closer or _close_real)(pos["side"], price)
        except Exception as exc:
            if send and chat:
                send(chat, "⛔ BTC: não consegui fechar (" + str(exc)[:150] + "). Tento de novo em 1 min.")
            return "close_failed"
        pnl = pnl_usd(pos, price)
        with _LOCK:
            dd = _load()
            dd["open"] = None
            dd["last_signal_t"] = max(dd.get("last_signal_t") or 0, sig_t)
            dd["history"] = (dd["history"] + [{"at": pos["at"], "closed": now, "side": pos["side"], "entry": pos["entry"],
                                               "exit": price, "lev": pos["lev"], "pnl_usd": round(pnl, 2),
                                               "reason": why}])[-300:]
            total = sum(h["pnl_usd"] for h in dd["history"])
            if total <= -MAX_TOTAL_LOSS:
                dd.update(enabled=False, paused_why="perda total de US$ " + format(-total, ".2f"))
            _save(dd)
        if send and chat:
            send(chat, ("💰 LUCRO" if pnl >= 0 else "🔻 PREJUÍZO") + " — FECHOU BTC " + pos["side"].upper() + " " +
                 str(pos["lev"]) + "x\n" + REASON[why] + "\n💵 Entrada US$ " + format(pos["entry"], ",.0f") +
                 " → saída US$ " + format(price, ",.0f") + "\n📊 Resultado: " + format(pnl, "+.2f") + " USD (" +
                 format(pnl / pos["collateral_usd"] * 100, "+.1f") + "% da garantia)" + _tx(out))
            if not _load()["enabled"] and d["enabled"]:
                send(chat, "⏸️ ROBÔ BTC PAUSADO SOZINHO: perda total chegou a US$ " + format(MAX_TOTAL_LOSS, ".0f") +
                     ". Ligue de novo no botão se quiser continuar.")
        return "closed"
    if not d["enabled"]:
        return ""
    if not sig:
        LAST["why"] = "esperando sinal (RSI2 < 10 acima da EMA200 ou > 90 abaixo)"
        return ""
    if sig_t <= (d.get("last_signal_t") or 0) or now - (sig_t + TF) > ENTRY_WINDOW_S:
        LAST["why"] = "esperando o próximo sinal"
        return ""
    lev = min(int(d.get("leverage") or DEFAULT_LEVERAGE), MAX_LEVERAGE)
    with _LOCK:
        dd = _load()
        dd["last_signal_t"] = sig_t                # one attempt per signal
        _save(dd)
    try:
        real = (opener or _open_real)(sig, price, lev)
    except Exception as exc:
        if send and chat:
            send(chat, "⛔ BTC " + sig.upper() + " " + str(lev) + "x não abriu: " + str(exc)[:200])
        return "open_failed"
    pos = dict(real, side=sig, lev=lev, at=now, signal_t=sig_t, stop=stop_price(sig, real["entry"], atr_v, lev))
    with _LOCK:
        dd = _load()
        dd["open"] = pos
        _save(dd)
    if send and chat:
        liq = pos["entry"] * (1 - 0.9 / lev if sig == "long" else 1 + 0.9 / lev)
        send(chat, ("🟢" if sig == "long" else "🔴") + " ₿ ABRIU BTC " + sig.upper() + " " + str(lev) + "x na Jupiter\n"
             "💵 Entrada: `" + format(pos["entry"], ".2f") + "` · garantia US$ " + format(pos["collateral_usd"], ".0f") +
             " · posição US$ " + format(pos["size_usd"], ".0f") + "\n🛑 Stop: `" + format(pos["stop"], ".2f") +
             "` (≈ " + format(pnl_usd(pos, pos["stop"]), "+.2f") + " USD)\n🔄 Sai no sinal contrário ou em 4 dias\n"
             "💀 Liquidação ≈ " + format(liq, ",.0f") + _tx(real))
    return "opened"


def status_text():
    d = _load()
    lev = min(int(d.get("leverage") or DEFAULT_LEVERAGE), MAX_LEVERAGE)
    lines = ["₿ ROBÔ BTC (dinheiro real, Jupiter) — " + ("🟢 LIGADO" if d["enabled"] else "🔴 DESLIGADO") +
             (" (" + d["paused_why"] + ")" if not d["enabled"] and d.get("paused_why") else ""),
             "📈 " + DESC,
             "💵 Garantia US$ " + format(CAPITAL_USD, ".0f") + " × " + str(lev) + "x = posição US$ " +
             format(CAPITAL_USD * lev, ".0f") + " · pausa sozinho se perder US$ " + format(MAX_TOTAL_LOSS, ".0f") + " no total"]
    if d["open"]:
        p = d["open"]
        lines.append("📦 Aberta: " + p["side"].upper() + " " + str(p["lev"]) + "x · entrada US$ " + format(p["entry"], ",.0f") +
                     " · stop US$ " + format(p["stop"], ",.0f") +
                     (" · agora " + format(pnl_usd(p, LAST["price"]), "+.2f") + " USD" if LAST.get("price") else ""))
    h = d["history"]
    if h:
        won = sum(1 for x in h if x["pnl_usd"] > 0)
        lines.append("📊 " + str(len(h)) + " fechadas · " + str(won) + " no lucro · total " +
                     format(sum(x["pnl_usd"] for x in h), "+.2f") + " USD")
    if LAST["why"]:
        lines.append("🔎 Agora: " + LAST["why"] + (" · BTC US$ " + format(LAST["price"], ",.0f") if LAST.get("price") else ""))
    lines.append("⚠️ Opera poucas vezes (≈ 7 por mês no teste). Acerto ~30%: muitas perdas pequenas, poucos ganhos "
                 "maiores. No teste: +7% em 4 meses em 2x com 20% da conta. Não é promessa de lucro.")
    return "\n".join(lines)
