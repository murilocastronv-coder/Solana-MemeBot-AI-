"""30-day backtest of the BTC leveraged trend strategy (same rules as btc_perps).

Real BTC/USDT 1-hour candles (Binance, Coinbase fallback). For each leverage
it replays the strategy candle by candle, without looking ahead:
- signal on the close of each candle (20-hour breakout in the direction of the
  200-hour EMA); entry at that close;
- initial stop 2x ATR(14), trailing stop 3x ATR from the best price, the
  opposite signal closes and flips; the stop is always placed before the
  liquidation price (as the live bot does);
- inside each candle the worst case is assumed first (stop/liquidation is
  checked against the low for a long, the high for a short);
- fees 0.06% per side on the position size + borrow ~0.003%/hour;
- collateral per trade = ORDER_FRACTION of equity (the bot uses 20%), and an
  "all-in" run (100% of equity as collateral) to show what aggressive sizing
  does. Liquidation = losing 90% of the collateral.
"""
import time

import btc_perps as bp

START_USD = 50.0
DAYS = 30


def fetch_history(days=DAYS, warmup=bp.EMA_LEN + bp.CHANNEL + 5):
    """1-hour candles for the last `days` days plus indicator warm-up, oldest first."""
    hours = days * 24 + warmup
    end_ms = int(time.time() // 3600 * 3600 * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    try:
        rows = bp._get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000"
                       "&startTime=" + str(start_ms) + "&endTime=" + str(end_ms - 1))
        candles = [{"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
                   for r in rows]
    except Exception:
        candles = []
        cursor = start_ms // 1000
        while cursor < end_ms // 1000:
            stop = min(cursor + 300 * 3600, end_ms // 1000)
            rows = bp._get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600"
                           "&start=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cursor)) +
                           "&end=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stop)))
            candles += [{"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]), "c": float(r[4])}
                        for r in rows]
            cursor = stop
        candles = sorted({c["t"]: c for c in candles}.values(), key=lambda c: c["t"])
    return candles


def run(candles, leverage, fraction=bp.ORDER_FRACTION, start=START_USD, test_hours=DAYS * 24):
    """Replay the strategy on the last `test_hours` candles. Returns a result dict."""
    cfg = bp.TIMEFRAMES["1h"]
    first = max(len(candles) - test_hours, bp.EMA_LEN + 2)
    equity, peak_equity, max_dd = start, start, 0.0
    pos = None
    trades, wins, liquidations = 0, 0, 0

    def close(price, hours_held, liquidated=False):
        nonlocal equity, pos, trades, wins, liquidations
        move = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "long" else -1)
        pnl = pos["size"] * move - pos["size"] * (bp.FEE_RATE * 2 + bp.BORROW_RATE_HOUR * hours_held)
        if liquidated:
            pnl = -pos["collateral"]
            liquidations += 1
        pnl = max(pnl, -pos["collateral"])
        equity += pnl
        trades += 1
        wins += 1 if pnl > 0 else 0
        pos = None

    for i in range(first, len(candles)):
        c = candles[i]
        if pos:
            held = (c["t"] - pos["t"]) / 3600 + 1
            liq_move = bp.LIQ_LOSS * pos["collateral"] / pos["size"]
            if pos["side"] == "long":
                liq = pos["entry"] * (1 - liq_move)
                if c["l"] <= max(pos["stop"], liq):
                    close(pos["stop"], held) if pos["stop"] > liq else close(liq, held, liquidated=True)
                else:
                    pos["best"] = max(pos["best"], c["h"])
                    pos["stop"] = max(pos["stop"], pos["best"] - cfg["trail_atr"] * pos["atr"])
            else:
                liq = pos["entry"] * (1 + liq_move)
                if c["h"] >= min(pos["stop"], liq):
                    close(pos["stop"], held) if pos["stop"] < liq else close(liq, held, liquidated=True)
                else:
                    pos["best"] = min(pos["best"], c["l"])
                    pos["stop"] = min(pos["stop"], pos["best"] + cfg["trail_atr"] * pos["atr"])
        side, a, price = bp.signal(candles[:i + 1])
        if side and pos and pos["side"] != side:
            close(price, (c["t"] - pos["t"]) / 3600 + 1)
        if side and not pos and a > 0 and equity > 1:
            collateral = equity * fraction
            size = collateral * leverage
            liq_move = bp.LIQ_LOSS * collateral / size
            stop_move = min(cfg["stop_atr"] * a / price, liq_move * bp.MAX_STOP_OF_LIQ)
            stop = price * (1 - stop_move) if side == "long" else price * (1 + stop_move)
            pos = {"side": side, "entry": price, "stop": stop, "best": price, "atr": a, "t": c["t"],
                   "collateral": collateral, "size": size}
        value = equity
        if pos:
            move = (c["c"] - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "long" else -1)
            value += max(pos["size"] * move, -pos["collateral"])
        peak_equity = max(peak_equity, value)
        max_dd = max(max_dd, (peak_equity - value) / peak_equity if peak_equity else 0)
    if pos:
        close(candles[-1]["c"], (candles[-1]["t"] - pos["t"]) / 3600 + 1)
    return {"leverage": leverage, "fraction": fraction, "final": equity, "return_pct": (equity / start - 1) * 100,
            "trades": trades, "wins": wins, "liquidations": liquidations, "max_dd_pct": max_dd * 100}


def report(candles=None):
    """Telegram text: $50 over the last 30 days at every leverage, normal and all-in sizing."""
    candles = candles if candles is not None else fetch_history()
    if len(candles) < bp.EMA_LEN + 50:
        return "Backtest indisponível: poucas velas de BTC recebidas (" + str(len(candles)) + ")."
    start_t = candles[max(len(candles) - DAYS * 24, 0)]["t"]
    btc_move = (candles[-1]["c"] / candles[max(len(candles) - DAYS * 24, 0)]["c"] - 1) * 100
    lines = ["📊 BACKTEST BTC ALAVANCADO — últimos " + str(DAYS) + " dias",
             "Período: " + time.strftime("%d/%m", time.gmtime(start_t)) + " → " +
             time.strftime("%d/%m", time.gmtime(candles[-1]["t"])) + " · BTC " + format(btc_move, "+.1f") + "% no período",
             "Estratégia do bot: rompimento de 20 h a favor da média de 200 h, stop 2×ATR, stop móvel 3×ATR, "
             "taxas 0,06%/lado + juros, liquidação a −90% da garantia.",
             "", "Começando com US$ " + format(START_USD, ".0f") + ":", "",
             "▶ Como o bot opera (20% da conta como garantia):"]
    for lev in bp.LEVERAGES:
        r = run(candles, lev)
        lines.append("  " + str(lev) + "x → US$ " + format(r["final"], ".2f") + " (" + format(r["return_pct"], "+.1f") +
                     "%) · " + str(r["trades"]) + " ops, " + str(r["wins"]) + " no lucro · queda máx. " +
                     format(r["max_dd_pct"], ".0f") + "%" + (" · ⚠️ " + str(r["liquidations"]) + " liquidação(ões)" if r["liquidations"] else ""))
    lines += ["", "▶ Agressivo (100% da conta como garantia):"]
    best = 0.0
    for lev in bp.LEVERAGES:
        r = run(candles, lev, fraction=1.0)
        best = max(best, r["final"])
        lines.append("  " + str(lev) + "x → US$ " + format(r["final"], ".2f") + " (" + format(r["return_pct"], "+.1f") +
                     "%) · queda máx. " + format(r["max_dd_pct"], ".0f") + "%" +
                     (" · ⚠️ " + str(r["liquidations"]) + " liquidação(ões)" if r["liquidations"] else ""))
    lines += ["", ("✅ Alguma configuração chegaria a US$ 200 neste mês." if best >= 200 else
                   "❌ Nenhuma configuração chegaria a US$ 200 (4×) neste mês."),
              "Resultado passado não garante o futuro: o mesmo mês com o BTC indo contra a tendência zera a conta nas alavancagens altas."]
    return "\n".join(lines)
