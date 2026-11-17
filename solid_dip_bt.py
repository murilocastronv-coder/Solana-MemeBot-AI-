"""Backtest (owner, Nov 17): buy SOLID Solana coins when they are down, sell at +8%, NO stop loss.

Coins: big, liquid Solana-ecosystem tokens with years of exchange history (no new memecoins).
Data: 1-hour candles of the last 365 days (Binance; Coinbase if Binance refuses), real prices, nothing invented.
Rule: when a coin closes DROP% or more below its highest price of the last LOOKBACK hours and the bot holds no
position in it, buy US$ SIZE at that close; sell the whole position when a later candle reaches entry +TARGET
(limit sale at exactly +8%). No stop loss: a position that never gets back to +8% stays open and is shown at the
last price (that is the risk of this strategy). Costs: COST per side (Jupiter fee + slippage on liquid coins).
Portfolio: US$ CAPITAL, max one position per coin, cash can never go negative. Equity is marked to market every
hour (drawdown includes the open losses). Choice: first 8 months (training), result on the last 4 months (test).
"""
import logging
import threading
import time

import requests

import signals as sg

E = sg._f
COINS = (("SOL", "SOLUSDT", "SOL-USD"), ("JUP", "JUPUSDT", "JUP-USD"), ("BONK", "BONKUSDT", "BONK-USD"),
         ("WIF", "WIFUSDT", "WIF-USD"), ("JTO", "JTOUSDT", "JTO-USD"), ("RAY", "RAYUSDT", None),
         ("PYTH", "PYTHUSDT", "PYTH-USD"), ("ORCA", "ORCAUSDT", "ORCA-USD"), ("W", "WUSDT", "W-USD"),
         ("RENDER", "RENDERUSDT", "RENDER-USD"))
TARGET = E("DIP_BT_TARGET", 0.08)
CAPITAL = E("DIP_BT_CAPITAL", 60)
SIZE = E("DIP_BT_SIZE", 10)
COST = E("DIP_BT_COST_PER_SIDE", 0.0025)
DAYS = int(E("DIP_BT_DAYS", 365))
DROPS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
LOOKBACKS = ((72, "3 dias"), (168, "7 dias"), (720, "30 dias"))
H = 3600
_RUN = threading.Lock()
_http = requests.Session()


def _get(url):
    r = _http.get(url, timeout=15, headers={"User-Agent": "DipBacktest/1.0"})
    r.raise_for_status()
    return r.json()


def fetch(binance, coinbase, days=DAYS, now=None, get=None):
    """[{t, h, c}] hourly candles of the last `days` days (closed candles only)."""
    get = get or _get
    now = int(now or time.time())
    end = now // H * H
    start = end - days * 24 * H
    rows = []
    try:
        cur = start * 1000
        while cur < end * 1000:
            page = get("https://api.binance.com/api/v3/klines?symbol=" + binance + "&interval=1h&limit=1000&startTime=" +
                       str(cur) + "&endTime=" + str(end * 1000 - 1))
            if not page:
                break
            rows += [{"t": int(r[0]) // 1000, "h": float(r[2]), "c": float(r[4])} for r in page]
            cur = (rows[-1]["t"] + H) * 1000
    except Exception:
        rows = []
        if not coinbase:
            return []
        cur = start
        while cur < end:
            stop = min(cur + 300 * H, end)
            try:
                page = get("https://api.exchange.coinbase.com/products/" + coinbase + "/candles?granularity=3600&start=" +
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cur)) + "&end=" +
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stop)))
            except Exception:
                page = []
            rows += [{"t": int(r[0]), "h": float(r[2]), "c": float(r[4])} for r in page or []]
            cur = stop
    uniq = {r["t"]: r for r in rows if r["t"] + H <= now}
    return [uniq[t] for t in sorted(uniq)]


_TOPS = {}


def tops(cs, lookback):
    """Highest high of the `lookback` candles BEFORE each candle (sliding window, computed once)."""
    key = (id(cs), lookback)
    if key in _TOPS:
        return _TOPS[key]
    from collections import deque
    out, q = [None] * len(cs), deque()
    for i in range(len(cs)):
        while q and q[0] < i - lookback:
            q.popleft()
        out[i] = cs[q[0]]["h"] if q and i >= lookback else None
        while q and cs[q[-1]]["h"] <= cs[i]["h"]:
            q.pop()
        q.append(i)
    _TOPS[key] = out
    return out


def simulate(data, start, end, drop, lookback, size=None, capital=None):
    """data: {coin: [candles]}. -> summary with trades, open positions, marked-to-market equity and drawdown."""
    size = SIZE if size is None else size
    capital = CAPITAL if capital is None else capital
    idx = {c: {r["t"]: i for i, r in enumerate(cs)} for c, cs in data.items()}
    tp = {c: tops(cs, lookback) for c, cs in data.items()}
    hours = sorted({r["t"] for cs in data.values() for r in cs if start <= r["t"] < end})
    cash, pos, trades, peak_eq, dd, last_px = capital, {}, [], capital, 0.0, {}
    worst_open = 0.0
    no_cash = 0
    for t in hours:
        for coin, cs in data.items():
            i = idx[coin].get(t)
            if i is None:
                continue
            r = cs[i]
            last_px[coin] = r["c"]
            p = pos.get(coin)
            if p:
                if r["h"] >= p["entry"] * (1 + TARGET):
                    back = p["qty"] * p["entry"] * (1 + TARGET) * (1 - COST)
                    cash += back
                    trades.append({"coin": coin, "opened": p["t"], "closed": t, "pnl": round(back - size, 4),
                                   "days": round((t - p["t"]) / 86400, 1), "worst": round(p["worst"] * 100, 1)})
                    del pos[coin]
                else:
                    p["worst"] = min(p["worst"], r["c"] / p["entry"] - 1)
                continue
            top = tp[coin][i]
            if top is not None and r["c"] <= top * (1 - drop):
                if cash < size:
                    no_cash += 1
                    continue
                cash -= size
                pos[coin] = {"t": t, "entry": r["c"], "qty": size * (1 - COST) / r["c"], "worst": 0.0}
        eq = cash + sum(p["qty"] * last_px.get(c, p["entry"]) for c, p in pos.items())
        peak_eq = max(peak_eq, eq)
        dd = max(dd, (peak_eq - eq) / peak_eq if peak_eq else 0)
    for p in pos.values():
        worst_open = min(worst_open, p["worst"])
    open_val = sum(p["qty"] * last_px.get(c, p["entry"]) for c, p in pos.items())
    open_cost = size * len(pos)
    final = cash + open_val
    closed = sum(t["pnl"] for t in trades)
    return {"final": round(final, 2), "pnl": round(final - capital, 2), "closed_pnl": round(closed, 2), "trades": len(trades),
            "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1) if trades else 0.0,
            "max_days": max((t["days"] for t in trades), default=0.0),
            "worst_during": min((t["worst"] for t in trades), default=0.0),
            "open": [{"coin": c, "since": p["t"], "pct": round((last_px.get(c, p["entry"]) / p["entry"] - 1) * 100, 1)} for c, p in pos.items()],
            "open_pnl": round(open_val - open_cost, 2), "dd": round(dd * 100, 1), "no_cash": no_cash,
            "per_coin": _per_coin(trades), "trade_list": trades[-60:]}


def _per_coin(trades):
    out = {}
    for t in trades:
        n, p = out.get(t["coin"], (0, 0.0))
        out[t["coin"]] = (n + 1, round(p + t["pnl"], 2))
    return out


def hold_sol(data, start, end, capital=None):
    cs = [r for r in data.get("SOL", []) if start <= r["t"] < end]
    if len(cs) < 2:
        return None
    return round((capital or CAPITAL) * (cs[-1]["c"] / cs[0]["c"] - 1), 2)


def run(now=None, get=None):
    now = now or time.time()
    data, missing = {}, []
    for coin, b, cb in COINS:
        cs = fetch(b, cb, now=now, get=get)
        if len(cs) > 24 * 30:
            data[coin] = cs
        else:
            missing.append(coin)
    if not data:
        return None
    start = min(cs[0]["t"] for cs in data.values())
    end = max(cs[-1]["t"] for cs in data.values()) + H
    cut = start + (end - start) * 8 // 12
    grid = []
    for lb, lb_label in LOOKBACKS:
        for d in DROPS:
            tr = simulate(data, start, cut, d, lb)
            grid.append((tr["pnl"], d, lb, lb_label, tr))
    grid.sort(key=lambda x: -x[0])
    best = grid[0]
    test = simulate(data, cut, end, best[1], best[2])
    full = simulate(data, start, end, best[1], best[2])
    simple = simulate(data, start, end, 0.10, 168)
    return {"start": start, "end": end, "cut": cut, "coins": list(data), "missing": missing,
            "best": {"drop": best[1], "lookback": best[3], "train": best[4], "test": test, "full": full},
            "simple": simple, "grid": [(round(g[1] * 100), g[3], g[0], g[4]["trades"], g[4]["open_pnl"]) for g in grid],
            "hold_sol_test": hold_sol(data, cut, end), "hold_sol_full": hold_sol(data, start, end)}


def _fmt(s):
    return ("US$ " + format(CAPITAL, "g") + " → " + format(s["final"], ".2f") + " (" + format(s["pnl"], "+.2f") + ") · " +
            str(s["trades"]) + " vendas com +8% (média " + format(s["avg_days"], "g") + " dias, máx. " + format(s["max_days"], "g") +
            " dias) · ainda presas: " + str(len(s["open"])) + " (" + format(s["open_pnl"], "+.2f") + ") · pior queda segurando " +
            format(s["worst_during"], "g") + "% · drawdown " + format(s["dd"], "g") + "%")


def text(rep):
    if not rep:
        return "⛔ Backtest compra na baixa: não consegui baixar as velas (Binance e Coinbase)."
    b = rep["best"]
    d = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))
    L = ["📉 BACKTEST: COMPRA NA BAIXA, VENDE COM +8%, SEM STOP — " + d(rep["start"]) + " a " + d(rep["end"]),
         "Moedas sólidas da Solana: " + ", ".join(rep["coins"]) + (" (sem dados: " + ", ".join(rep["missing"]) + ")" if rep["missing"] else "") +
         " · velas de 1 h reais · US$ " + format(SIZE, "g") + " por compra, 1 por moeda, capital US$ " + format(CAPITAL, "g") +
         " · custo " + format(COST * 100, "g") + "% por lado",
         "Escolhe a queda e o período nos 8 primeiros meses, confere nos 4 últimos (fora da amostra).",
         "🏆 Melhor no treino: comprar quando cai " + format(b["drop"] * 100, "g") + "% do topo de " + b["lookback"],
         "  • treino: " + _fmt(b["train"]), "  • TESTE (4 últimos meses): " + _fmt(b["test"]), "  • ano todo: " + _fmt(b["full"])]
    if b["full"]["open"]:
        L.append("  • presas no fim: " + ", ".join(o["coin"] + " " + format(o["pct"], "+.0f") + "% desde " + d(o["since"]) for o in b["full"]["open"]))
    if b["full"]["per_coin"]:
        L.append("  • por moeda (vendas, lucro): " + ", ".join(c + " " + str(n) + " / " + format(p, "+.2f") for c, (n, p) in sorted(b["full"]["per_coin"].items())))
    L.append("📌 Regra simples (cai 10% do topo de 7 dias), ano todo: " + _fmt(rep["simple"]))
    if rep.get("hold_sol_full") is not None:
        L.append("⚖️ Só comprar SOL e segurar: ano " + format(rep["hold_sol_full"], "+.2f") + " · últimos 4 meses " +
                 format(rep["hold_sol_test"] or 0, "+.2f"))
    L.append("🔢 Todas as combinações no treino (queda · período: resultado, vendas, presas):")
    for drop, lb, pnl, n, op in rep["grid"][:8]:
        L.append("  • " + str(drop) + "% · " + lb + ": " + format(pnl, "+.2f") + " · " + str(n) + " vendas · presas " + format(op, "+.2f"))
    L.append("⚠️ Sem stop: o lucro de cada venda é pequeno (+8%) e uma moeda que cai muito fica presa e segura o dinheiro. "
             "O resultado inclui as presas pelo preço de hoje. Passado não garante futuro.")
    return "\n".join(L)


def start(send, chat):
    if not _RUN.acquire(blocking=False):
        return "já está rodando"

    def go():
        try:
            send(chat, text(run()))
        except Exception as exc:
            logging.exception("dip backtest")
            send(chat, "⛔ Backtest compra na baixa falhou: " + type(exc).__name__)
        finally:
            _RUN.release()
    threading.Thread(target=go, daemon=True).start()
    return ""
