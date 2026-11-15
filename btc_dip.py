"""Backtest of the owner's "buy the bottom" BTC strategy (research only: places no orders).

Rules (owner, Nov 15):
- 1st buy when BTC is 15% below its top (the highest price since the last sale; at the start, the
  highest of the 30 days before the test);
- buys again at each further -15% below the previous buy (at most 3 buys);
- sells EVERYTHING when the price is +7% above the average buy price (take profit);
- US$ 100 of collateral per buy, tested at 1x..5x leverage: the buys merge into one position
  (as on Jupiter Perps), liquidated when its losses eat 90% of the collateral.
Fills: buys at the trigger price when the hourly low reaches it; the take profit at its price when
the hourly high reaches it (never in the same candle as a buy: conservative); costs 0.06% per side
plus borrow per hour on leverage.
"""
import time

import btc_research as br

HOUR = 3600
FEE = 0.0006
BORROW_H = 0.00003
LIQ_LOSS = 0.90
DROP = 0.15
TP = 0.07
MAX_BUYS = 3
USD = 100.0
DAYS = 183
REF_DAYS = 30
LEVERAGES = (1, 2, 3, 4, 5)
RESULT = {"running": False, "text": ""}


def fetch(days=DAYS, ref_days=REF_DAYS):
    return br.resample(br.fetch_year(days=days, warm=ref_days * 96), HOUR)


def backtest(cs, drop=DROP, tp=TP, max_buys=MAX_BUYS, usd=USD, lev=1, start=None):
    """cs: hourly candles; the test runs from index `start` (before it: only for the first top)."""
    start = start if start is not None else max(len(cs) - DAYS * 24, 0)
    top = max([c["h"] for c in cs[:start]] or [cs[start]["o"]])
    pos = None                      # {"buys": [(t, price)], "size", "qty", "col", "opened"}
    cycles, realized = [], 0.0
    worst_open = 0.0                # most negative open P/L (US$) seen
    max_in = 0.0                    # most collateral in at once

    def open_pnl(p):
        hours = (c["t"] - pos["opened"]) / HOUR
        return pos["qty"] * p - pos["size"] - pos["size"] * (FEE * 2 + (BORROW_H * hours if lev > 1 else 0))

    for c in cs[start:]:
        bought_now = False
        if pos is None:
            trigger = top * (1 - drop)
            if c["l"] <= trigger:
                price = min(trigger, c["o"])
                pos = {"buys": [(c["t"], price)], "size": usd * lev, "qty": usd * lev / price, "col": usd,
                       "opened": c["t"]}
                bought_now = True
            else:
                top = max(top, c["h"])
                continue
        # more buys at each -drop below the previous one
        while len(pos["buys"]) < max_buys and c["l"] <= pos["buys"][-1][1] * (1 - drop):
            price = min(pos["buys"][-1][1] * (1 - drop), c["o"])
            pos["buys"].append((c["t"], price))
            pos["size"] += usd * lev
            pos["qty"] += usd * lev / price
            pos["col"] += usd
            bought_now = True
        max_in = max(max_in, pos["col"])
        low_pnl = open_pnl(c["l"])
        worst_open = min(worst_open, low_pnl)
        if lev > 1 and low_pnl <= -LIQ_LOSS * pos["col"]:
            realized -= pos["col"]
            cycles.append({"buys": pos["buys"], "avg": pos["size"] / pos["qty"], "exit_t": c["t"], "exit": None,
                           "pnl": -pos["col"], "col": pos["col"], "liq": True})
            pos, top = None, c["h"]
            continue
        avg = pos["size"] / pos["qty"]
        if not bought_now and c["h"] >= avg * (1 + tp):
            price = max(avg * (1 + tp), c["o"])
            pnl = open_pnl(price)
            realized += pnl
            cycles.append({"buys": pos["buys"], "avg": avg, "exit_t": c["t"], "exit": price, "pnl": pnl,
                           "col": pos["col"], "liq": False})
            pos, top = None, c["h"]
    last = cs[-1]
    c = last                        # open_pnl() reads the current candle
    open_now = None
    if pos:
        open_now = {"buys": pos["buys"], "avg": pos["size"] / pos["qty"], "pnl": open_pnl(last["c"]), "col": pos["col"]}
    total = realized + (open_now["pnl"] if open_now else 0.0)
    return {"lev": lev, "cycles": cycles, "realized": realized, "open": open_now, "total": total,
            "worst_open": worst_open, "max_in": max_in, "liquidations": sum(1 for x in cycles if x["liq"]),
            "first": cs[start]["t"], "last": last["t"], "btc_start": cs[start]["o"], "btc_end": last["c"],
            "top_start": max([x["h"] for x in cs[:start]] or [cs[start]["o"]])}


def report_text(results, compare=None):
    day = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))
    r1 = results[0]
    L = ["🧪 COMPRA NO FUNDO — BTC, últimos 6 meses (" + day(r1["first"]) + " → " + day(r1["last"]) + ")",
         "Regras: compra quando cai " + format(DROP * 100, ".0f") + "% do topo, compra de novo a cada −" +
         format(DROP * 100, ".0f") + "% (máx. " + str(MAX_BUYS) + " compras de US$ " + format(USD, ".0f") +
         "), vende tudo com +" + format(TP * 100, ".0f") + "% acima do preço médio. Taxas incluídas.",
         "₿ BTC no período: US$ " + format(r1["btc_start"], ",.0f") + " → US$ " + format(r1["btc_end"], ",.0f") +
         " (" + format((r1["btc_end"] / r1["btc_start"] - 1) * 100, "+.1f") + "%) · topo antes do início US$ " +
         format(r1["top_start"], ",.0f") + " → 1ª compra em US$ " + format(r1["top_start"] * (1 - DROP), ",.0f")]
    if not r1["cycles"] and not r1["open"]:
        L += ["", "Nenhuma compra: o BTC não caiu " + format(DROP * 100, ".0f") + "% do topo nesses 6 meses."]
    else:
        L += ["", "Operações (em 1x):"]
        for x in r1["cycles"]:
            L.append("• " + " + ".join(day(t) + " US$ " + format(p, ",.0f") for t, p in x["buys"]) + " → média US$ " +
                     format(x["avg"], ",.0f") + " → vendeu " + day(x["exit_t"]) + " US$ " + format(x["exit"], ",.0f") +
                     " · " + format(x["pnl"], "+.2f") + " USD")
        if r1["open"]:
            o = r1["open"]
            L.append("• AINDA ABERTA: " + " + ".join(day(t) + " US$ " + format(p, ",.0f") for t, p in o["buys"]) +
                     " → média US$ " + format(o["avg"], ",.0f") + " · agora " + format(o["pnl"], "+.2f") + " USD")
    L += ["", "Resultado por alavancagem (US$ " + format(USD, ".0f") + " por compra):"]
    for r in results:
        L.append(str(r["lev"]) + "x → " + format(r["total"], "+.2f") + " USD" +
                 " · " + str(len(r["cycles"]) - r["liquidations"]) + " vendas no lucro" +
                 (" · 💀 " + str(r["liquidations"]) + " liquidação(ões)" if r["liquidations"] else "") +
                 (" · aberta " + format(r["open"]["pnl"], "+.2f") if r["open"] else "") +
                 " · pior momento " + format(r["worst_open"], "+.2f") + " · até US$ " + format(r["max_in"], ".0f") + " investidos")
    if compare:
        L += ["", "Comparação em 1x (queda para comprar → resultado):"]
        L.append(" · ".join(format(dp * 100, ".0f") + "% → " + format(r["total"], "+.2f") + " (" + str(len(r["cycles"])) +
                            " vendas)" for dp, r in compare))
    L += ["", "⚠️ Comprar na queda ganha enquanto o BTC volta a subir; numa queda longa (como 2022, −75%) fica preso "
          "com prejuízo grande, e com alavancagem é liquidado. 6 meses é pouco para provar. Teste histórico, "
          "nenhuma ordem enviada."]
    return "\n".join(L)


def run(cs=None):
    cs = cs or fetch()
    start = max(len(cs) - DAYS * 24, 0)
    results = [backtest(cs, lev=lev, start=start) for lev in LEVERAGES]
    compare = [(dp, backtest(cs, drop=dp, start=start)) for dp in (0.10, 0.15, 0.20)]
    return report_text(results, compare)
