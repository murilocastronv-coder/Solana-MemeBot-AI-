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
            out = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                        "v": float(r[5])}
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
            out = sorted(({"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4]),
                          "v": float(r[5])}
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
        return {"leverage": self.lev, "trades": n, "wins": wins, "losses": n - wins,
                "win_rate": wins / n * 100 if n else 0.0, "pnl_usd": self.equity - START,
                "gross_usd": self.equity - START + self.fees,
                "final": self.equity, "pnl_pct": (self.equity / START - 1) * 100, "fees": self.fees,
                "max_dd": self.max_dd * 100, "best": max(self.trades, default=0.0), "worst": min(self.trades, default=0.0)}


def _stop_hit(pos, candle):
    if pos["side"] == "long" and candle["l"] <= pos["stop"]:
        return min(pos["stop"], candle["o"])          # gap below the stop exits at the open
    if pos["side"] == "short" and candle["h"] >= pos["stop"]:
        return max(pos["stop"], candle["o"])
    return None


SLIPPAGE = 0.0002   # 0.02% per side (spread + slippage), on top of the 0.06% fee


def scalp_signals(candles, warmup=60):
    return [b.scalp_signal(candles[i - warmup + 1:i + 1]) if i >= warmup - 1 else (None, 0.0, "")
            for i in range(len(candles))]


def run_scalp(candles, leverage=1, warmup=60, sigs=None, sl=None, tp=None, fee_mult=1.0, slip_mult=1.0):
    """Owner's M5 strategy: signal on closed candle i, enter at candle i+1 open, exit inside candle i+1 at
    SL/TP (if both are touched in the same candle the stop counts — worst case) or at its close.
    10% of equity per trade, 3% daily loss stop, 1-hour pause after 3 consecutive losses."""
    sigs = sigs or scalp_signals(candles, warmup)
    sl = b.SCALP_SL_PCT if sl is None else sl
    tp = b.SCALP_TP_PCT if tp is None else tp
    fee, slip = b.FEE_RATE * fee_mult, SLIPPAGE * slip_mult
    acc = Account(leverage)
    day, day_start, streak, pause_until = None, START, 0, 0
    for i in range(warmup, len(candles) - 1):
        side, _, _ = sigs[i]
        nxt = candles[i + 1]
        d = time.strftime("%Y-%m-%d", time.gmtime(nxt["t"]))
        if d != day:
            day, day_start = d, acc.equity
        if not side or nxt["t"] < pause_until or acc.equity <= day_start * (1 - b.SCALP_DAILY_LOSS):
            continue
        size = acc.equity * b.SCALP_FRACTION * leverage
        if size < 1:
            break
        entry = nxt["o"]
        if side == "long":
            stop, target = entry * (1 - sl), entry * (1 + tp)
            exit_price = stop if nxt["l"] <= stop else target if nxt["h"] >= target else nxt["c"]
            move = exit_price / entry - 1
        else:
            stop, target = entry * (1 + sl), entry * (1 - tp)
            exit_price = stop if nxt["h"] >= stop else target if nxt["l"] <= target else nxt["c"]
            move = 1 - exit_price / entry
        cost = size * (fee * 2 + slip * 2)
        pnl = size * move - cost
        acc.equity += pnl
        acc.fees += cost
        acc.trades.append(pnl)
        acc.peak = max(acc.peak, acc.equity)
        acc.max_dd = max(acc.max_dd, (acc.peak - acc.equity) / acc.peak if acc.peak else 0)
        streak = streak + 1 if pnl <= 0 else 0
        if streak >= b.SCALP_MAX_LOSS_STREAK:
            pause_until, streak = nxt["t"] + b.SCALP_STREAK_PAUSE, 0
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


SL_GRID = (0.0015, 0.0020, 0.0025, 0.0030)
TP_GRID = (0.0025, 0.0035, 0.0040, 0.0050)
LEVERAGES = (1,)


def _money(v):
    return ("+" if v >= 0 else "−") + "$" + format(abs(v), ".2f")


def _pct(x):
    return format(x * 100, ".2f").rstrip("0").rstrip(".") + "%"


def report_text(m5, h1=None, days=DAYS):
    lines = ["📊 BACKTEST — ESTRATÉGIA M5 (EMA 9/21 + RSI + Volume + ATR), últimos " + str(days) + " dias de BTC",
             "$100 inicial · $10 por operação (10% da conta) · sem alavancagem · fecha no fim da vela",
             "Custos: taxa 0,06% + deslize 0,02% em cada lado. Entra na abertura da vela seguinte ao sinal.", ""]
    if not m5:
        return "\n".join(lines + ["Sem dados de preço."])
    move = (m5[-1]["c"] / m5[-1 - min(len(m5) - 1, days * 288)]["c"] - 1) * 100
    sigs = scalp_signals(m5)
    r = run_scalp(m5, 1, sigs=sigs)
    win = r["pnl_usd"] > 0
    lines += ["💵 P/L TOTAL: " + ("🟢 LUCRO " if win else "🔴 PREJUÍZO ") + _money(r["pnl_usd"]) +
              " (" + format(r["pnl_pct"], "+.2f") + "%) · $100 → $" + format(r["final"], ".2f"),
              "Stop 0,25% / alvo 0,40% (padrão)",
              "Operações: " + str(r["trades"]) + " (" + str(r["wins"]) + " ganhos / " + str(r["losses"]) + " perdas) · acerto " +
              format(r["win_rate"], ".0f") + "%",
              "Lucro bruto (sem custos): " + _money(r["gross_usd"]) + " · custos: $" + format(r["fees"], ".2f"),
              "Maior ganho " + _money(r["best"]) + " · maior perda " + _money(r["worst"]) + " · queda máx " +
              format(r["max_dd"], ".1f") + "%",
              "Comparação: BTC no período " + format(move, "+.1f") + "% (comprar e segurar $100 → $" +
              format(100 * (1 + move / 100), ".2f") + ")", ""]
    grid = []
    for sl in SL_GRID:
        for tp in TP_GRID:
            g = run_scalp(m5, 1, sigs=sigs, sl=sl, tp=tp)
            grid.append((g["pnl_usd"], sl, tp, g))
    grid.sort(key=lambda x: -x[0])
    lines.append("🔧 Outras combinações de stop/alvo (P/L em " + str(days) + " dias):")
    for pnl, sl, tp, g in grid[:5]:
        lines.append("  SL " + _pct(sl) + " / TP " + _pct(tp) + ": " + ("🟢 " if pnl > 0 else "🔴 ") + _money(pnl) +
                     " · acerto " + format(g["win_rate"], ".0f") + "%")
    lines.append("  … pior: SL " + _pct(grid[-1][1]) + " / TP " + _pct(grid[-1][2]) + ": " + _money(grid[-1][0]))
    stress = run_scalp(m5, 1, sigs=sigs, fee_mult=2, slip_mult=2)
    lines += ["", "🧱 Com taxas e deslize dobrados: " + ("🟢 " if stress["pnl_usd"] > 0 else "🔴 ") + _money(stress["pnl_usd"]),
              "", "⚠️ Resultado passado não garante o futuro. Escolher o melhor stop/alvo olhando o mesmo período "
              "superestima o resultado. Se ficou 🔴, a estratégia tende a perder com dinheiro real."]
    return "\n".join(lines)


def run(days=DAYS):
    return report_text(fetch_history("5m", days, extra=80), None, days)
