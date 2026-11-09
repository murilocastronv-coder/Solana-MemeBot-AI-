"""BTC breakout robot, 15-minute chart, 24 h a day, leverage up to 20x (simulation or real money).

Strategy (fixed rules, not optimised):
- LONG when a 15m candle CLOSES above the highest high of the previous CHANNEL candles and above
  the EMA(TREND_EMA) (uptrend); SHORT when it closes below the lowest low of the previous
  CHANNEL candles and below the EMA (downtrend). Entry right after the close (no look-ahead).
- Take profit = TP_ATR x ATR(14); stop loss = SL_MULT x the take-profit distance (2x by default,
  as requested). The stop always sits before the liquidation price.
- One position at a time; time stop after MAX_HOLD_BARS candles.
- Collateral = ORDER_FRACTION of the account; position = collateral x leverage.
Costs (Jupiter Perps): ~0.06% open + 0.06% close on the position size, borrow ~0.003%/h.

Stop 2x the target means one loss erases two wins: the strategy must win more than ~67% of
trades (plus fees) just to break even. The 1-year backtest shows whether these rules would
have made or lost money. It is a historical simulation, not a promise.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

TF_SECONDS = 900
CHANNEL = int(os.getenv("BRK_CHANNEL", "20"))
TREND_EMA = int(os.getenv("BRK_TREND_EMA", "200"))
ATR_LEN = 14
TP_ATR = float(os.getenv("BRK_TP_ATR", "1.0"))
SL_MULT = float(os.getenv("BRK_SL_MULT", "2.0"))      # stop distance = 2 x take-profit distance
MAX_HOLD_BARS = int(os.getenv("BRK_MAX_HOLD_BARS", "32"))  # 8 h
ORDER_FRACTION = float(os.getenv("BRK_ORDER_FRACTION", "0.20"))
LEVERAGES = (2, 3, 5, 10, 20)
MAX_LEVERAGE = 20
DEFAULT_LEVERAGE = 5
FEE_RATE = 0.0006            # per side, on position size
BORROW_RATE_HOUR = 0.00003
LIQ_LOSS = 0.90              # liquidated when the loss reaches 90% of the collateral
STOP_BEFORE_LIQ = 0.70       # stop at most at 70% of the distance to liquidation
START_USD = float(os.getenv("BRK_SIM_START_USD", "50"))
MIN_REAL_COLLATERAL = 5.0

_LOCK = threading.RLock()
_http = requests.Session()


# ------------------------------------------------------------------ data
def _get(url):
    r = _http.get(url, timeout=15, headers={"User-Agent": "BTCBreakoutBot/1.0"})
    r.raise_for_status()
    return r.json()


def _binance(start_ms=None, end_ms=None, limit=1000):
    url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=" + str(limit)
    if start_ms:
        url += "&startTime=" + str(int(start_ms))
    if end_ms:
        url += "&endTime=" + str(int(end_ms))
    return [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
             "v": float(r[5])} for r in _get(url)]


def _coinbase(start_s, end_s):
    out, cursor = [], start_s
    while cursor < end_s:
        stop = min(cursor + 300 * TF_SECONDS, end_s)
        rows = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900&start=" +
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cursor)) + "&end=" +
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stop)))
        out += [{"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4]),
                 "v": float(r[5])} for r in rows]
        cursor = stop
    return out


def fetch_history(days=365, now=None):
    """Closed 15m BTC candles for the last `days` days (+ indicator warm-up), oldest first."""
    now = int(now or time.time())
    end = now // TF_SECONDS * TF_SECONDS
    start = end - (days * 96 + TREND_EMA + CHANNEL + 50) * TF_SECONDS
    rows = []
    try:
        cursor = start * 1000
        while cursor < end * 1000:
            page = _binance(cursor, end * 1000 - 1)
            if not page:
                break
            rows += page
            cursor = (page[-1]["t"] + TF_SECONDS) * 1000
    except Exception:
        rows = _coinbase(start, end)
    uniq = {c["t"]: c for c in rows if c["t"] + TF_SECONDS <= now}
    return [uniq[t] for t in sorted(uniq)]


def fetch_recent(n=TREND_EMA + CHANNEL + 60, now=None):
    now = int(now or time.time())
    try:
        rows = _binance(limit=min(1000, n + 1))
    except Exception:
        end = now // TF_SECONDS * TF_SECONDS
        rows = _coinbase(end - (n + 1) * TF_SECONDS, end)
    return [c for c in sorted(rows, key=lambda c: c["t"]) if c["t"] + TF_SECONDS <= now][-n:]


def fetch_price():
    try:
        return float(_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
    except Exception:
        return float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])


# ------------------------------------------------------------------ indicators / signal
def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(candles, n=ATR_LEN):
    out = [None] * len(candles)
    if len(candles) <= n:
        return out
    trs = [candles[0]["h"] - candles[0]["l"]] + [
        max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(candles, candles[1:])]
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(candles)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


class Indicators:
    def __init__(self, candles):
        self.c = candles
        self.ema = ema([x["c"] for x in candles], TREND_EMA)
        self.atr = atr(candles)


def signal_at(ind, i):
    """'long' | 'short' | None for the CLOSED candle i (uses candles <= i only)."""
    if i < max(TREND_EMA, CHANNEL + 1) or ind.ema[i] is None or ind.atr[i] is None:
        return None
    window = ind.c[i - CHANNEL:i]
    hi, lo = max(x["h"] for x in window), min(x["l"] for x in window)
    close = ind.c[i]["c"]
    if close > hi and close > ind.ema[i]:
        return "long"
    if close < lo and close < ind.ema[i]:
        return "short"
    return None


def levels(side, entry, a, leverage):
    """(take_profit, stop, liquidation) prices. Stop = SL_MULT x TP distance, kept before liquidation."""
    tp_dist = TP_ATR * a
    sl_dist = SL_MULT * tp_dist
    liq_move = LIQ_LOSS / leverage
    sl_dist = min(sl_dist, STOP_BEFORE_LIQ * liq_move * entry)
    sign = 1 if side == "long" else -1
    return entry + sign * tp_dist, entry - sign * sl_dist, entry * (1 - sign * liq_move)


def _pnl(pos, price, hours):
    move = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "long" else -1)
    return pos["size"] * move - pos["size"] * (FEE_RATE * 2 + BORROW_RATE_HOUR * hours)


# ------------------------------------------------------------------ backtest
def backtest(candles, leverage, start=START_USD, days=365):
    """Replay the rules candle by candle. Worst case inside a candle: stop before target."""
    ind = Indicators(candles)
    first = max(len(candles) - days * 96, TREND_EMA + CHANNEL + 1)
    equity = peak = start
    max_dd = 0.0
    pos = None
    trades = wins = liqs = 0
    gross_w = gross_l = 0.0
    streak = worst_streak = 0
    pending = None
    monthly = {}

    def close(price, i, reason):
        nonlocal equity, pos, trades, wins, liqs, gross_w, gross_l, streak, worst_streak
        hours = (i - pos["i"] + 1) * TF_SECONDS / 3600
        pnl = -pos["collateral"] if reason == "liquidação" else max(_pnl(pos, price, hours), -pos["collateral"])
        if reason == "liquidação":
            liqs += 1
        equity += pnl
        trades += 1
        if pnl > 0:
            wins += 1
            gross_w += pnl
            streak = 0
        else:
            gross_l -= pnl
            streak += 1
            worst_streak = max(worst_streak, streak)
        m = time.strftime("%Y-%m", time.gmtime(candles[i]["t"]))
        monthly[m] = monthly.get(m, 0.0) + pnl
        pos = None

    for i in range(first, len(candles)):
        c = candles[i]
        if pending and pos is None and equity > 1:
            side, a = pending
            collateral = equity * ORDER_FRACTION
            entry = c["o"]
            tp, sl, liq = levels(side, entry, a, leverage)
            pos = {"side": side, "entry": entry, "collateral": collateral, "size": collateral * leverage,
                   "tp": tp, "sl": sl, "liq": liq, "i": i}
        pending = None
        if pos:
            long = pos["side"] == "long"
            adverse, favour = (c["l"], c["h"]) if long else (c["h"], c["l"])
            hit_sl = adverse <= pos["sl"] if long else adverse >= pos["sl"]
            hit_tp = favour >= pos["tp"] if long else favour <= pos["tp"]
            hit_liq = adverse <= pos["liq"] if long else adverse >= pos["liq"]
            if hit_sl:
                gap = (c["o"] <= pos["sl"]) if long else (c["o"] >= pos["sl"])
                if hit_liq and gap:
                    close(pos["liq"], i, "liquidação")
                else:
                    close(c["o"] if gap else pos["sl"], i, "stop")
            elif hit_tp:
                close(pos["tp"], i, "alvo")
            elif i - pos["i"] >= MAX_HOLD_BARS:
                close(c["c"], i, "tempo")
        if pos:
            mtm = equity + _pnl(pos, c["c"], 0)
        else:
            mtm = equity
            s = signal_at(ind, i)
            if s:
                pending = (s, ind.atr[i])
        peak = max(peak, mtm)
        max_dd = max(max_dd, (peak - mtm) / peak if peak > 0 else 0)
        if equity <= 1:
            break
    if pos:
        close(candles[-1]["c"], len(candles) - 1, "fim")
    return {"leverage": leverage, "start": start, "final": equity, "return_pct": (equity / start - 1) * 100,
            "max_dd_pct": max_dd * 100, "trades": trades, "win_rate": wins / trades if trades else 0.0,
            "profit_factor": gross_w / gross_l if gross_l else (float("inf") if gross_w else 0.0),
            "liquidations": liqs, "worst_losing_streak": worst_streak, "monthly": monthly,
            "days": (candles[-1]["t"] - candles[first]["t"]) / 86400 if len(candles) > first else 0}


def backtest_report(candles=None, leverages=LEVERAGES):
    candles = candles or fetch_history(365)
    results = [backtest(candles, lev) for lev in leverages]
    first, last = candles[max(0, len(candles) - 365 * 96)], candles[-1]
    bh = (last["c"] / first["o"] - 1) * 100
    lines = ["📊 BACKTEST 1 ANO — BTC BREAKOUT 15 MIN",
             time.strftime("%d/%m/%Y", time.gmtime(first["t"])) + " → " + time.strftime("%d/%m/%Y", time.gmtime(last["t"])) +
             " · " + str(len(candles)) + " velas reais de 15 min",
             "Regras: rompe máx./mín. de " + str(CHANNEL) + " velas a favor da média de " + str(TREND_EMA) +
             " · alvo " + format(TP_ATR, "g") + "×ATR · stop " + format(SL_MULT, "g") + "× o alvo · " +
             format(ORDER_FRACTION * 100, ".0f") + "% da conta de garantia · taxas e juros da Jupiter",
             "Saldo inicial US$ " + format(START_USD, ".0f") + " · BTC no período: " + format(bh, "+.1f") + "%", ""]
    for r in results:
        pf = "∞" if r["profit_factor"] == float("inf") else format(r["profit_factor"], ".2f")
        lines.append(str(r["leverage"]) + "x → US$ " + format(r["final"], ".2f") + " (" + format(r["return_pct"], "+.0f") +
                     "%) · queda máx. " + format(r["max_dd_pct"], ".0f") + "% · " + str(r["trades"]) + " ops · acerto " +
                     format(r["win_rate"] * 100, ".0f") + "% · fator " + pf + " · liquidações " + str(r["liquidations"]) +
                     " · pior seq. " + str(r["worst_losing_streak"]))
    need = SL_MULT / (1 + SL_MULT) * 100
    best = max(results, key=lambda r: r["final"])
    lines += ["", "Com stop " + format(SL_MULT, "g") + "× o alvo, é preciso acertar mais de " + format(need, ".0f") +
              "% (mais as taxas) só para empatar.",
              ("✅ Melhor resultado: " + str(best["leverage"]) + "x." if best["final"] > START_USD else
               "❌ NENHUMA alavancagem deu lucro no último ano com estas regras. Não recomendo dinheiro real."),
              "Simulação histórica. Não é promessa de lucro."]
    summary = {r["leverage"]: {k: v for k, v in r.items() if k != "monthly"} for r in results}
    with _LOCK:
        d = _load()
        d["backtest"] = {"at": time.time(), "results": summary}
        _save(d)
    return "\n".join(lines), results


# ------------------------------------------------------------------ state
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("btc_breakout.json")


def _default():
    return {"enabled": False, "mode": "sim", "leverage": DEFAULT_LEVERAGE, "balance": START_USD,
            "position": None, "real_position": None, "history": [], "last_bar": None, "backtest": None}


def _load():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default()
    d = _default()
    d.update(data)
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".btc_breakout.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, p)


def set_leverage(value):
    value = int(value)
    if value not in LEVERAGES or value > MAX_LEVERAGE:
        raise ValueError("alavancagem permitida: " + ", ".join(str(x) + "x" for x in LEVERAGES))
    with _LOCK:
        d = _load()
        d["leverage"] = value
        _save(d)
    return value


def set_mode(on, real=False):
    with _LOCK:
        d = _load()
        if on and real and not d.get("backtest"):
            raise ValueError("rode o 📈 Backtest 1 ano antes de ligar o dinheiro real")
        d["enabled"], d["mode"] = bool(on), ("real" if (on and real) else ("sim" if on else d["mode"]))
        _save(d)
    return d


def backtest_for(leverage):
    with _LOCK:
        bt = (_load().get("backtest") or {}).get("results") or {}
    return bt.get(str(leverage)) or bt.get(leverage)


# ------------------------------------------------------------------ live
def _key(d):
    return "real_position" if d["mode"] == "real" else "position"


def _real_equity():
    import jupiter_perps as jp
    import live_order_bridge as bridge
    from bot_wallet_service import get_address
    owner = get_address()
    from solana_settlement import get_sol_balance
    return get_sol_balance(owner) / 1e9 * float(bridge.sol_usd_price()) + jp.usdc_balance(owner)


def _open(d, side, price, a, send, chat):
    lev = int(d["leverage"])
    real = d["mode"] == "real"
    try:
        equity = _real_equity() if real else d["balance"]
    except Exception as exc:
        send(chat, "⛔ BTC breakout: não consegui ler o saldo (" + str(exc)[:150] + ").")
        return
    collateral = round(equity * ORDER_FRACTION, 2)
    if collateral < (MIN_REAL_COLLATERAL if real else 1):
        send(chat, "💤 BTC breakout: saldo baixo demais (garantia US$ " + format(collateral, ".2f") + ").")
        return
    size, entry, sig = collateral * lev, price, None
    if real:
        import jupiter_perps as jp
        from btc_usdc import ensure_usdc
        try:
            ensure_usdc(collateral)
            onchain = jp.open_position(side, collateral, lev, price)
        except Exception as exc:
            send(chat, "⛔ BTC breakout REAL: ordem " + side.upper() + " não aberta: " + str(exc)[:300])
            return
        entry, sig = onchain.get("price") or price, onchain.get("signature")
        size, collateral = onchain.get("size_usd") or size, onchain.get("collateral_usd") or collateral
    else:
        d["balance"] -= collateral
    tp, sl, liq = levels(side, entry, a, lev)
    d[_key(d)] = {"side": side, "entry": entry, "collateral": collateral, "size": size, "leverage": lev,
                  "tp": tp, "sl": sl, "liq": liq, "opened_at": time.time()}
    send(chat, ("🟢 LONG" if side == "long" else "🔴 SHORT") + " BTC " + str(lev) + "x " +
         ("💵 DINHEIRO REAL" if real else "(SIMULADO)") + " · breakout 15 min\n"
         "Entrada $" + format(entry, ",.0f") + " · garantia $" + format(collateral, ".2f") + " → posição $" +
         format(size, ",.0f") + "\n🎯 Alvo $" + format(tp, ",.0f") + " · 🛑 Stop $" + format(sl, ",.0f") +
         " · 💀 Liquidação ≈ $" + format(liq, ",.0f") + ("\n🔗 https://solscan.io/tx/" + sig if sig else ""))


def _close(d, price, reason, send, chat):
    real = d["mode"] == "real"
    pos = d[_key(d)]
    sig = None
    if real:
        import jupiter_perps as jp
        from bot_wallet_service import get_address
        try:
            before = jp.usdc_balance(jp.Pubkey.from_string(get_address()))
            res = jp.close_position(pos["side"], price)
            after = jp.usdc_balance(jp.Pubkey.from_string(get_address()))
            sig = res.get("signature")
        except Exception as exc:
            send(chat, "⛔ BTC breakout REAL: não consegui FECHAR (" + str(exc)[:250] + "). Tento de novo em ~20 s. "
                 "Se continuar, feche em jup.ag/perps com a carteira do bot.")
            return False
        pnl = (after - before) - pos["collateral"] if not res.get("already_closed") else \
            _pnl({**pos}, price, (time.time() - pos["opened_at"]) / 3600)
    else:
        pnl = max(_pnl(pos, price, (time.time() - pos["opened_at"]) / 3600), -pos["collateral"])
        d["balance"] += pos["collateral"] + pnl
    d[_key(d)] = None
    d["history"] = (d["history"] + [{"side": pos["side"], "leverage": pos["leverage"], "pnl": round(pnl, 4),
                                     "reason": reason, "real": real, "at": time.time()}])[-300:]
    send(chat, ("💰 LUCRO" if pnl > 0 else "🔻 PREJUÍZO") + " — fechou " + pos["side"].upper() + " BTC " +
         str(pos["leverage"]) + "x " + ("💵 REAL" if real else "(simulado)") + " (" + reason + ")\n$" +
         format(pos["entry"], ",.0f") + " → $" + format(price, ",.0f") + " · P/L " + format(pnl, "+.2f") + " USD" +
         ("" if real else " · saldo simulado $" + format(d["balance"], ".2f")) +
         ("\n🔗 https://solscan.io/tx/" + sig if sig else ""))
    return True


def cycle(send, chat, candles=None, price=None, now=None):
    """Manage the open position every pass; look for a new breakout once per closed 15m candle."""
    with _LOCK:
        d = _load()
        pos = d[_key(d)]
        if not d["enabled"] and not pos:
            return
        try:
            price = price or fetch_price()
        except Exception as exc:
            logging.warning("btc price failed: %s", type(exc).__name__)
            return
        if pos:
            long = pos["side"] == "long"
            held_bars = (time.time() - pos["opened_at"]) / TF_SECONDS
            reason = None
            if (price <= pos["sl"]) if long else (price >= pos["sl"]):
                reason = "stop"
            elif (price >= pos["tp"]) if long else (price <= pos["tp"]):
                reason = "alvo"
            elif held_bars >= MAX_HOLD_BARS:
                reason = "tempo"
            if reason:
                _close(d, price, reason, send, chat)
            _save(d)
            return
        if not d["enabled"]:
            return
        try:
            candles = candles or fetch_recent(now=now)
        except Exception as exc:
            logging.warning("btc candles failed: %s", type(exc).__name__)
            return
        if not candles or candles[-1]["t"] == d.get("last_bar"):
            return
        d["last_bar"] = candles[-1]["t"]
        ind = Indicators(candles)
        s = signal_at(ind, len(candles) - 1)
        if s:
            _open(d, s, price, ind.atr[-1], send, chat)
        _save(d)


def status_text(price=None):
    with _LOCK:
        d = _load()
    pos = d[_key(d)]
    mode = ("💵 REAL" if d["mode"] == "real" else "🧪 SIMULAÇÃO") if d["enabled"] else "⏹️ DESLIGADO"
    hist = [h for h in d["history"] if h["real"] == (d["mode"] == "real")]
    wins = sum(1 for h in hist if h["pnl"] > 0)
    lines = ["₿ BTC BREAKOUT 15 MIN — " + mode + " · " + str(d["leverage"]) + "x",
             "Rompe máx./mín. de " + str(CHANNEL) + " velas a favor da média de " + str(TREND_EMA) + " · alvo " +
             format(TP_ATR, "g") + "×ATR · stop " + format(SL_MULT, "g") + "× o alvo · " +
             format(ORDER_FRACTION * 100, ".0f") + "% da conta por operação",
             "Operações: " + str(len(hist)) + " (" + str(wins) + " no lucro) · resultado " +
             format(sum(h["pnl"] for h in hist), "+.2f") + " USD" +
             ("" if d["mode"] == "real" else " · saldo simulado $" + format(d["balance"], ".2f"))]
    if pos:
        lines.append("Aberta: " + pos["side"].upper() + " " + str(pos["leverage"]) + "x de $" + format(pos["entry"], ",.0f") +
                     " · alvo $" + format(pos["tp"], ",.0f") + " · stop $" + format(pos["sl"], ",.0f"))
    bt = backtest_for(d["leverage"])
    if bt:
        lines.append("Backtest 1 ano em " + str(d["leverage"]) + "x: " + format(bt.get("return_pct", 0), "+.0f") +
                     "% · queda máx. " + format(bt.get("max_dd_pct", 0), ".0f") + "% · acerto " +
                     format(bt.get("win_rate", 0) * 100, ".0f") + "% · liquidações " + str(bt.get("liquidations", 0)))
    else:
        lines.append("Backtest: ainda não rodado (📈 Backtest 1 ano).")
    return "\n".join(lines)
