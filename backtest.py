"""Backtest of the BTC strategies on recent history, with Jupiter Perps costs.

Uses the same signal functions as the live robot (btc_perps.dip_signal and
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
DIP_START = 150.0   # needs up to 3 x $25 of collateral; close to the bot's real account


def trend_flags(candles):
    """For each 5m candle: was the last CLOSED hour above its 200-hour EMA? (no look-ahead)"""
    k = 2 / (b.DIP_TREND_EMA + 1)
    flags, closes, ema_v, cur_hour, last_close = [], [], None, None, None
    for c in candles:
        hour = c["t"] // 3600
        if cur_hour is not None and hour != cur_hour:          # previous hour just closed
            closes.append(last_close)
            if len(closes) == b.DIP_TREND_EMA:
                ema_v = sum(closes) / b.DIP_TREND_EMA
            elif len(closes) > b.DIP_TREND_EMA:
                ema_v = last_close * k + ema_v * (1 - k)
        cur_hour, last_close = hour, c["c"]
        flags.append(ema_v is not None and len(closes) > b.DIP_TREND_EMA and closes[-1] > ema_v)
    return flags


def dip_signals(candles, trend=True):
    warm = b.DIP_LOOKBACK + 16
    tf = trend_flags(candles) if trend else [True] * len(candles)
    return [tf[i] and b.dip_signal(candles[i - warm + 1:i + 1])[0] if i >= warm - 1 else False
            for i in range(len(candles))]


def run_dip(candles, sigs=None, start=DIP_START, emergency=True, fee_mult=1.0, slip_mult=1.0,
            gap=None, tp_first=None, tp_multi=None):
    """Long-only dip buying: signal on closed candle i -> buy $25 x10 at candle i+1 open. Re-buy when the
    price trades `gap` below the last buy (max 3). Close all at the net profit target, at the emergency
    stop, or by liquidation. Inside a candle the path is open->low->high->close for green candles and
    open->high->low->close for red ones; a stop and a target in the same leg count as the stop."""
    sigs = sigs or dip_signals(candles)
    gap = b.DIP_GAP if gap is None else gap
    tp_first = b.DIP_TP_FIRST if tp_first is None else tp_first
    tp_multi = b.DIP_TP_MULTI if tp_multi is None else tp_multi
    fee, slip = b.FEE_RATE * fee_mult, SLIPPAGE * slip_mult
    cash, fees_paid, borrow_paid, broke_at = start, 0.0, 0.0, None
    pos = None
    cycles, max_used, peak, max_dd = [], 0.0, start, 0.0
    monthly = {}

    def pnl_at(p, price, hours):
        move = price / p["avg"] - 1
        cost = p["size"] * (fee * 2 + slip * 2 + b.BORROW_RATE_HOUR * hours)
        return p["size"] * move - cost, cost

    def borrow_of(p, hours):
        return p["size"] * b.BORROW_RATE_HOUR * hours

    def close(p, price, reason, t):
        nonlocal cash, pos, fees_paid, borrow_paid
        hours = (t - p["t0"]) / 3600
        pnl, cost = pnl_at(p, price, hours)
        pnl = max(pnl, -p["coll"])
        cash += p["coll"] + pnl
        fees_paid += cost
        borrow_paid += borrow_of(p, hours)
        cycles.append({"pnl": pnl, "buys": p["n"], "reason": reason, "hours": hours})
        m = time.strftime("%Y-%m", time.gmtime(t))
        monthly[m] = monthly.get(m, 0.0) + pnl
        pos = None

    def buy(price, t):
        nonlocal cash, pos, max_used
        if cash < b.DIP_USD:
            return
        cash -= b.DIP_USD
        size = b.DIP_USD * b.DIP_LEV
        if pos is None:
            pos = {"size": 0.0, "coins": 0.0, "coll": 0.0, "n": 0, "last": price, "t0": t}
        pos["size"] += size; pos["coins"] += size / price; pos["coll"] += b.DIP_USD
        pos["n"] += 1; pos["last"] = price; pos["avg"] = pos["size"] / pos["coins"]
        max_used = max(max_used, pos["coll"])

    def target_price(p, t):
        hours = (t - p["t0"]) / 3600
        need = (tp_first if p["n"] == 1 else tp_multi) + p["size"] * (fee * 2 + slip * 2 + b.BORROW_RATE_HOUR * hours)
        return p["avg"] * (1 + need / p["size"])

    def go_down(level, t):
        """Price trades down to `level`: re-buys, emergency stop, liquidation."""
        while pos and pos["n"] < b.DIP_MAX_BUYS and level <= pos["last"] * (1 - gap) and cash >= b.DIP_USD:
            buy(pos["last"] * (1 - gap), t)
        if not pos:
            return
        liq = pos["avg"] * (1 - b.LIQ_LOSS * pos["coll"] / pos["size"])
        stop = pos["avg"] * (1 - b.DIP_EMERGENCY) if emergency else None
        if stop and level <= stop and stop > liq:
            close(pos, stop, "emergency", t)
        elif level <= liq:
            close(pos, liq, "liquidated", t)

    def go_up(level, t):
        if pos and level >= target_price(pos, t):
            close(pos, target_price(pos, t), "take_profit", t)

    for i in range(len(candles) - 1):
        c = candles[i + 1]
        if pos is None and sigs[i]:
            if cash < b.DIP_USD and broke_at is None:
                broke_at = c["t"]
            buy(c["o"], c["t"])
        if pos is not None:
            if c["c"] >= c["o"]:
                go_down(c["l"], c["t"]); go_up(c["h"], c["t"])
            else:
                go_up(c["h"], c["t"]); go_down(c["l"], c["t"])
        equity = cash + ((pos["coll"] + pnl_at(pos, c["c"], (c["t"] - pos["t0"]) / 3600)[0]) if pos else 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0)
    open_pnl = 0.0
    if pos:
        open_pnl = pnl_at(pos, candles[-1]["c"], (candles[-1]["t"] - pos["t0"]) / 3600)[0]
    final = cash + ((pos["coll"] + open_pnl) if pos else 0)
    wins = [x for x in cycles if x["pnl"] > 0]
    return {"start": start, "final": final, "pnl_usd": final - start, "pnl_pct": (final / start - 1) * 100,
            "cycles": len(cycles), "wins": len(wins), "losses": len(cycles) - len(wins),
            "by_buys": {k: sum(1 for x in cycles if x["buys"] == k) for k in (1, 2, 3)},
            "first_t": candles[0]["t"] if candles else 0, "last_t": candles[-1]["t"] if candles else 0,
            "emergency": sum(1 for x in cycles if x["reason"] == "emergency"),
            "liquidated": sum(1 for x in cycles if x["reason"] == "liquidated"),
            "fees": fees_paid, "borrow": borrow_paid, "broke_at": broke_at, "max_dd": max_dd * 100, "max_used": max_used,
            "worst": min((x["pnl"] for x in cycles), default=0.0), "open": bool(pos), "open_pnl": open_pnl,
            "avg_hours": sum(x["hours"] for x in cycles) / len(cycles) if cycles else 0.0, "monthly": monthly}


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


def _money(v):
    return ("+" if v >= 0 else "−") + "$" + format(abs(v), ".2f")


def _months(r):
    out, t = [], r["first_t"]
    while t <= r["last_t"] + 86400:
        m = time.strftime("%Y-%m", time.gmtime(t))
        if not out or out[-1] != m:
            out.append(m)
        t += 86400 * 27
    return out


def report_text(m5, h1=None, days=365):
    lines = ["📊 BACKTEST — 🎯 COMPRA NO FUNDO (só long), " + str(days) + " dias de BTC em velas de 5 min",
             "Conta inicial $" + format(DIP_START, ".0f") + " · cada compra $" + format(b.DIP_USD, ".0f") + " × " +
             str(b.DIP_LEV) + "x · até " + str(b.DIP_MAX_BUYS) + " compras · 1 compra fecha com +$" +
             format(b.DIP_TP_FIRST, ".2f") + " líquido, 2+ compras com +$" + format(b.DIP_TP_MULTI, ".2f") + " líquido",
             "Compra quando cai ≥ " + format(b.DIP_DROP * 100, ".1f") + "% da máxima de 2 h + RSI < " + str(b.DIP_RSI) +
             " + primeira vela verde + BTC acima da média de 200 h · recompra a −" + format(b.DIP_GAP * 100, ".0f") + "%",
             "Custos: taxa 0,06% + deslize 0,02% por lado + juros ~" + format(b.BORROW_RATE_HOUR * 100, ".3f") + "%/h.", ""]
    if not m5:
        return "\n".join(lines + ["Sem dados de preço."])
    sigs = dip_signals(m5, trend=True)
    r = run_dip(m5, sigs)
    win = r["pnl_usd"] > 0
    move = (m5[-1]["c"] / m5[0]["c"] - 1) * 100
    lines += ["💵 P/L TOTAL: " + ("🟢 LUCRO " if win else "🔴 PREJUÍZO ") + _money(r["pnl_usd"]) +
              " (" + format(r["pnl_pct"], "+.1f") + "%) · $" + format(r["start"], ".0f") + " → $" + format(r["final"], ".2f"),
              "Ciclos fechados: " + str(r["cycles"]) + " (" + str(r["wins"]) + " com lucro / " + str(r["losses"]) + " com perda)",
              "Usou 1 compra: " + str(r["by_buys"][1]) + " · 2 compras: " + str(r["by_buys"][2]),
              "🛑 Stops de emergência: " + str(r["emergency"]) + " · 💀 liquidações: " + str(r["liquidated"]) +
              " · pior ciclo: " + _money(r["worst"]),
              "Custos: $" + format(r["fees"], ".2f") + " (juros $" + format(r["borrow"], ".2f") + ") · queda máx da conta: " +
              format(r["max_dd"], ".1f") + "% · tempo médio por ciclo: " + format(r["avg_hours"], ".1f") + " h"]
    if r["broke_at"]:
        lines.append("⚠️ A conta ficou sem saldo para comprar a partir de " +
                     time.strftime("%d/%m/%Y", time.gmtime(r["broke_at"])))
    if r["open"]:
        lines.append("Posição ainda aberta no fim: " + _money(r["open_pnl"]) + " (já incluído no P/L)")
    lines.append("Comparação: BTC no período " + format(move, "+.1f") + "% (comprar e segurar $" + format(DIP_START, ".0f") +
                 " → $" + format(DIP_START * (1 + move / 100), ".2f") + ")")
    lines += ["", "📅 Resultado por mês:"]
    for m in _months(r):
        v = r["monthly"].get(m)
        lines.append("  " + m + ": " + ("sem operação" if v is None else ("🟢 " if v > 0 else "🔴 ") + _money(v)))
    no_trend = run_dip(m5, dip_signals(m5, trend=False))
    no_stop = run_dip(m5, sigs, emergency=False)
    stress = run_dip(m5, sigs, fee_mult=2, slip_mult=2)
    lines += ["", "🧪 Comparações:",
              "  Sem filtro de tendência: " + ("🟢 " if no_trend["pnl_usd"] > 0 else "🔴 ") + _money(no_trend["pnl_usd"]) +
              " · " + str(no_trend["cycles"]) + " ciclos",
              "  Sem stop de emergência: " + ("🟢 " if no_stop["pnl_usd"] > 0 else "🔴 ") + _money(no_stop["pnl_usd"]) +
              " · liquidações: " + str(no_stop["liquidated"]),
              "  Com taxas e deslize dobrados: " + ("🟢 " if stress["pnl_usd"] > 0 else "🔴 ") + _money(stress["pnl_usd"]),
              "", "⚠️ Resultado passado não garante o futuro."]
    return "\n".join(lines)


def run(days=365):
    # +2,500 candles of warm-up so the 200-hour trend filter is ready on day one
    return report_text(fetch_history("5m", days, extra=2500), None, days)
