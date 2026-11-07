"""Backtest of the BTC strategies on recent history, with Jupiter Perps costs.

Uses the same signal functions as the live robot (btc_perps.scalp_signal and
btc_perps.signal), 20% of equity as collateral per trade, fees of 0.06% per
side plus hourly borrow, and the same stops. Entries happen at the next
candle's open (no look-ahead). Stops are checked against each candle's
high/low; if a stop is touched the trade exits at the stop price.
Past results do not guarantee future results.
"""
import time

import btc_perps as b

START = 100.0
DAYS = 30


def fetch_history(interval, days=DAYS, extra=250):
    """Closed candles for the last `days` (+ warm-up), oldest first (Binance, Coinbase fallback)."""
    secs = {"5m": 300, "1h": 3600}[interval]
    need = days * 86400 // secs + extra
    end = int(time.time() * 1000)
    out = []
    try:
        while len(out) < need:
            rows = b._get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=" + interval +
                          "&limit=1000&endTime=" + str(end))
            if not rows:
                break
            out = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                   for r in rows] + out
            end = int(rows[0][0]) - 1
    except Exception:
        out, end_s = [], int(time.time())
        while len(out) < need:
            start_s = end_s - 300 * secs
            rows = b._get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=" + str(secs) +
                          "&start=" + str(start_s) + "&end=" + str(end_s))
            if not rows:
                break
            out = sorted(({"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4])}
                          for r in rows), key=lambda c: c["t"]) + out
            end_s = start_s
    seen, uniq = set(), []
    for c in out:
        if c["t"] not in seen:
            seen.add(c["t"]); uniq.append(c)
    now = time.time()
    return [c for c in uniq if c["t"] + secs <= now][-need:]


class Account:
    def __init__(self, leverage):
        self.lev = leverage
        self.equity = START
        self.peak = START
        self.max_dd = 0.0
        self.fees = 0.0
        self.trades = []

    def close(self, pos, exit_price, hours):
        move = (exit_price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "long" else -1)
        fee = pos["size"] * (b.FEE_RATE * 2 + b.BORROW_RATE_HOUR * hours)
        pnl = max(pos["size"] * move - fee, -pos["collateral"])
        self.equity += pnl
        self.fees += fee
        self.trades.append(pnl)
        self.peak = max(self.peak, self.equity)
        self.max_dd = max(self.max_dd, (self.peak - self.equity) / self.peak if self.peak else 0)

    def open(self, side, price, atr_value, stop_mult):
        collateral = self.equity * b.ORDER_FRACTION
        if collateral < 1:
            return None
        dist = min(stop_mult * atr_value / price, b.MAX_STOP_OF_LIQ * b.LIQ_LOSS / self.lev)
        return {"side": side, "entry": price, "collateral": collateral, "size": collateral * self.lev,
                "stop": price * (1 - dist if side == "long" else 1 + dist), "best": price, "atr": atr_value}

    def report(self):
        n = len(self.trades)
        wins = sum(1 for t in self.trades if t > 0)
        return {"leverage": self.lev, "trades": n, "win_rate": wins / n * 100 if n else 0.0,
                "final": self.equity, "pnl_pct": (self.equity / START - 1) * 100, "fees": self.fees,
                "max_dd": self.max_dd * 100, "best": max(self.trades, default=0.0), "worst": min(self.trades, default=0.0)}


def _stop_hit(pos, candle):
    if pos["side"] == "long" and candle["l"] <= pos["stop"]:
        return min(pos["stop"], candle["o"])          # gap below the stop exits at the open
    if pos["side"] == "short" and candle["h"] >= pos["stop"]:
        return max(pos["stop"], candle["o"])
    return None


def scalp_signals(candles, warmup=120):
    return [b.scalp_signal(candles[i - warmup + 1:i + 1]) if i >= warmup - 1 else (None, 0.0, "")
            for i in range(len(candles))]


def run_scalp(candles, leverage, warmup=120, sigs=None):
    """M5 momentum scalping: signal on candle i, enter at i+1 open, exit at i+1 close (or stop)."""
    sigs = sigs or scalp_signals(candles, warmup)
    acc = Account(leverage)
    pos, day, day_start, paused = None, None, START, None
    for i in range(warmup, len(candles) - 1):
        side, a, _ = sigs[i]
        nxt = candles[i + 1]
        d = time.strftime("%Y-%m-%d", time.gmtime(nxt["t"]))
        if d != day:
            day, day_start, paused = d, acc.equity, None
        if paused == d:
            continue
        if not pos:
            if not side or a <= 0:
                continue
            pos = acc.open(side, nxt["o"], a, b.SCALP_STOP_ATR)
            if not pos:
                continue
            pos["open_i"] = i + 1
        exit_price = _stop_hit(pos, nxt)
        if exit_price is not None:
            acc.close(pos, exit_price, (i + 2 - pos["open_i"]) * 5 / 60)
            pos = None
        else:
            # keep through the next candle only if it signals the same direction; else exit at this close
            next_side = sigs[i + 1][0]
            if next_side != pos["side"]:
                acc.close(pos, nxt["c"], (i + 2 - pos["open_i"]) * 5 / 60)
                pos = None
        if acc.equity <= day_start * (1 - b.SCALP_DAILY_LOSS):
            paused = d
    return acc.report()


def trend_signals(candles, warmup=250):
    return [b.signal(candles[i - warmup + 1:i + 1]) if i >= warmup - 1 else (None, 0.0, 0.0)
            for i in range(len(candles))]


def run_trend(candles, leverage, warmup=250, sigs=None):
    """1h trend: Donchian 20 breakout with EMA 200 filter, 2xATR stop, 3xATR trailing, reversal."""
    sigs = sigs or trend_signals(candles, warmup)
    acc = Account(leverage)
    pos = None
    for i in range(warmup, len(candles) - 1):
        nxt = candles[i + 1]
        side, a, _ = sigs[i]
        if pos and side and side != pos["side"]:
            acc.close(pos, nxt["o"], (i + 1 - pos["open_i"]))
            pos = None
        if not pos and side and a > 0:
            pos = acc.open(side, nxt["o"], a, b.STOP_ATR)
            if pos:
                pos["open_i"] = i + 1
        if pos:
            exit_price = _stop_hit(pos, nxt)
            if exit_price is not None:
                acc.close(pos, exit_price, (i + 2 - pos["open_i"]))
                pos = None
                continue
            if pos["side"] == "long":
                pos["best"] = max(pos["best"], nxt["h"])
                pos["stop"] = max(pos["stop"], pos["best"] - b.TRAIL_ATR * pos["atr"])
            else:
                pos["best"] = min(pos["best"], nxt["l"])
                pos["stop"] = min(pos["stop"], pos["best"] + b.TRAIL_ATR * pos["atr"])
    if pos:
        acc.close(pos, candles[-1]["c"], len(candles) - 1 - pos["open_i"])
    return acc.report()


LEVERAGES = (2, 3, 5, 10)


def _line(r):
    return (format(r["leverage"], "d") + "x: " + ("🟢 " if r["pnl_pct"] > 0 else "🔴 ") +
            format(r["pnl_pct"], "+.1f") + "% ($100 → $" + format(r["final"], ".2f") + ") | " +
            str(r["trades"]) + " ops | acerto " + format(r["win_rate"], ".0f") + "% | taxas $" +
            format(r["fees"], ".2f") + " | queda máx " + format(r["max_dd"], ".0f") + "%")


def report_text(m5, h1, days=DAYS):
    lines = ["📊 BACKTEST — últimos " + str(days) + " dias de BTC (com taxas da Jupiter)",
             "Começando com $100, 20% da conta por ordem. Entrada na abertura da vela seguinte, sem olhar o futuro.", ""]
    if m5:
        move = (m5[-1]["c"] / m5[-1 - min(len(m5) - 1, days * 288)]["c"] - 1) * 100
        lines.append("BTC no período: " + format(move, "+.1f") + "%")
        lines.append("")
        lines.append("⚡ SCALPING M5 (fecha no fim da vela):")
        sigs = scalp_signals(m5)
        lines += ["  " + _line(run_scalp(m5, lev, sigs=sigs)) for lev in LEVERAGES]
    if h1:
        lines.append("")
        lines.append("📈 TENDÊNCIA 1h (rompimento + stop móvel):")
        sigs = trend_signals(h1)
        lines += ["  " + _line(run_trend(h1, lev, sigs=sigs)) for lev in LEVERAGES]
    lines += ["", "⚠️ Resultado passado não garante o futuro. 30 dias é pouco: use como sinal de alerta, não como promessa.",
              "Se a estratégia ficou 🔴 com taxas, ela tende a perder dinheiro com dinheiro real."]
    return "\n".join(lines)


def run(days=DAYS):
    m5 = fetch_history("5m", days, extra=150)
    h1 = fetch_history("1h", days, extra=260)
    return report_text(m5, h1, days)
