"""Backtest (owner, Nov 17): buy SOLID Solana coins when they are down, sell at +TARGET, stop at -STOP.
Owner, later: take profit +25% and stop loss -40% (first version: +8%, no stop).

Coins: big, liquid Solana-ecosystem tokens with years of exchange history (no new memecoins).
Data: 1-hour candles of the last 365 days (Binance; Coinbase if Binance refuses), real prices, nothing invented.
Rule: when a coin closes DROP% or more below its highest price of the last LOOKBACK hours and the bot holds no
position in it, buy US$ SIZE at that close; sell the whole position when a later candle reaches entry +TARGET or
entry -STOP (stop first when both are inside one candle). Positions still open at the end are shown at the last price. Costs: COST per side (Jupiter fee + slippage on liquid coins).
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
TARGET = E("DIP_BT_TARGET", 0.25)
STOP = E("DIP_BT_STOP", 0.40)                  # 0 = no stop loss
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
            rows += [{"t": int(r[0]) // 1000, "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in page]
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
            rows += [{"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "c": float(r[4])} for r in page or []]
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
                low = r.get("l", r["c"])
                exit_px, why = None, ""
                if STOP and low <= p["entry"] * (1 - STOP):
                    # stop first when both levels are inside the same candle (the cautious assumption); a gap sells lower
                    stop_px = p["entry"] * (1 - STOP)
                    exit_px, why = (r["c"] if r["h"] < stop_px else stop_px), "stop"   # whole candle below: sells at its close
                elif r["h"] >= p["entry"] * (1 + TARGET):
                    exit_px, why = p["entry"] * (1 + TARGET), "alvo"
                if exit_px:
                    back = p["qty"] * exit_px * (1 - COST)
                    cash += back
                    trades.append({"coin": coin, "opened": p["t"], "closed": t, "pnl": round(back - size, 4), "exit": why,
                                   "days": round((t - p["t"]) / 86400, 1), "worst": round(min(p["worst"], low / p["entry"] - 1) * 100, 1)})
                    del pos[coin]
                else:
                    p["worst"] = min(p["worst"], low / p["entry"] - 1)
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
            "wins": sum(1 for x in trades if x.get("exit") == "alvo"), "stops": sum(1 for x in trades if x.get("exit") == "stop"),
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
    return analyse(data, missing)


def analyse(data, missing=(), kind="solid", extra=None):
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
    return {"start": start, "end": end, "cut": cut, "coins": list(data), "missing": list(missing), "kind": kind, "extra": extra or {},
            "best": {"drop": best[1], "lookback": best[3], "train": best[4], "test": test, "full": full},
            "simple": simple, "grid": [(round(g[1] * 100), g[3], g[0], g[4]["trades"], g[4]["open_pnl"]) for g in grid],
            "hold_sol_test": hold_sol(data, cut, end), "hold_sol_full": hold_sol(data, start, end)}


def _fmt(s):
    return ("US$ " + format(CAPITAL, "g") + " → " + format(s["final"], ".2f") + " (" + format(s["pnl"], "+.2f") + ") · " +
            str(s["wins"]) + " no alvo +" + format(TARGET * 100, "g") + "% · " + str(s["stops"]) + " no stop −" + format(STOP * 100, "g") +
            "% · acerto " + (format(s["wins"] / s["trades"] * 100, ".0f") if s["trades"] else "0") + "% (média " + format(s["avg_days"], "g") +
            " dias, máx. " + format(s["max_days"], "g") +
            " dias) · ainda presas: " + str(len(s["open"])) + " (" + format(s["open_pnl"], "+.2f") + ") · pior queda segurando " +
            format(s["worst_during"], "g") + "% · drawdown " + format(s["dd"], "g") + "%")


def text(rep):
    if not rep:
        return "⛔ Backtest compra na baixa: não consegui baixar as velas (Binance e Coinbase)."
    b = rep["best"]
    d = lambda t: time.strftime("%d/%m/%Y", time.gmtime(t))
    L = ["📉 BACKTEST: COMPRA NA BAIXA, ALVO +" + format(TARGET * 100, "g") + "%, " + ("STOP −" + format(STOP * 100, "g") + "%" if STOP else "SEM STOP") + " — " + d(rep["start"]) + " a " + d(rep["end"]),
         ("Memecoins da Solana (liquidez ≥ US$ " + format(MEME_MIN_LIQ, ",.0f") + " hoje, compra só com 7+ dias de vida): " if rep.get("kind") == "meme"
          else "Moedas sólidas da Solana: ") + ", ".join(rep["coins"]) + (" (sem dados: " + ", ".join(rep["missing"]) + ")" if rep["missing"] else "") +
         " · velas de 1 h reais · US$ " + format(SIZE, "g") + " por compra, 1 por moeda, capital US$ " + format(CAPITAL, "g") +
         " · custo " + format(COST * 100, "g") + "% por lado",
         "Escolhe a queda e o período nos primeiros 2/3 do tempo, confere no último 1/3 (" + d(rep["cut"]) + " a " + d(rep["end"]) +
         ", fora da amostra).",
         "🏆 Melhor no treino: comprar quando cai " + format(b["drop"] * 100, "g") + "% do topo de " + b["lookback"],
         "  • treino: " + _fmt(b["train"]), "  • TESTE (último 1/3): " + _fmt(b["test"]), "  • período todo: " + _fmt(b["full"])]
    if b["full"]["open"]:
        L.append("  • abertas no fim: " + ", ".join(o["coin"] + " " + format(o["pct"], "+.0f") + "% desde " + d(o["since"]) for o in b["full"]["open"]))
    if b["full"]["per_coin"]:
        L.append("  • por moeda (vendas, lucro): " + ", ".join(c + " " + str(n) + " / " + format(p, "+.2f") for c, (n, p) in sorted(b["full"]["per_coin"].items())))
    L.append("📌 Regra simples (cai 10% do topo de 7 dias), período todo: " + _fmt(rep["simple"]))
    if rep.get("hold_sol_full") is not None:
        L.append("⚖️ Só comprar SOL e segurar: período todo " + format(rep["hold_sol_full"], "+.2f") + " · último 1/3 " +
                 format(rep["hold_sol_test"] or 0, "+.2f"))
    L.append("🔢 Todas as combinações no treino (queda · período: resultado, vendas, ainda abertas):")
    for drop, lb, pnl, n, op in rep["grid"][:8]:
        L.append("  • " + str(drop) + "% · " + lb + ": " + format(pnl, "+.2f") + " · " + str(n) + " vendas · presas " + format(op, "+.2f"))
    if rep.get("kind") == "meme":
        L.append("⚠️ VIÉS DE SOBREVIVÊNCIA: só entram memecoins que AINDA têm ≥ US$ " + format(MEME_MIN_LIQ, ",.0f") + " de liquidez hoje; "
                 "as que morreram no caminho não aparecem, então o resultado real tende a ser PIOR que este. "
                 "Liquidez histórica não existe no GeckoTerminal (filtro só pela liquidez de hoje).")
    L.append("⚠️ Quando o alvo e o stop cabem na mesma vela de 1 h, conto como stop (o caso pior). As posições ainda abertas entram "
             "pelo preço de hoje. Passado não garante futuro.")
    return "\n".join(L)


# ---------------------------------------------------------------- memecoins (owner, Nov 17)
MEME_MIN_LIQ = E("DIP_BT_MEME_MIN_LIQ", 250_000)
MEME_MIN_AGE_D = 7
MEME_DAYS = int(E("DIP_BT_MEME_DAYS", 180))
MEME_MAX = int(E("DIP_BT_MEME_MAX", 25))
GECKO = "https://api.geckoterminal.com/api/v2"
NOT_MEME = {"SOL", "WSOL", "USDC", "USDT", "USDS", "PYUSD", "USDG", "EURC", "JUP", "JTO", "RAY", "PYTH", "ORCA", "W", "RENDER",
            "JLP", "MSOL", "JITOSOL", "BSOL", "INF", "JUPSOL", "BBSOL", "HSOL", "CBBTC", "WBTC", "ETH", "WETH", "KMNO", "DRIFT",
            "TNSR", "HNT", "MOBILE", "IO", "ZEUS", "CLOUD", "SONIC", "MPLX", "SAMO", "BTC", "XBTC", "LBTC", "SYRUPUSDC", "ONDO"}
_gk = [0.0]


def _gecko(url, get=None):
    """GeckoTerminal free API (~30 calls/min): spaced 2.3 s, waits and retries on 429."""
    if get:
        return get(url)
    for _ in range(4):
        wait = _gk[0] + 2.3 - time.time()
        if wait > 0:
            time.sleep(wait)
        _gk[0] = time.time()
        r = _http.get(url, timeout=20, headers={"Accept": "application/json", "User-Agent": "DipBacktest/1.0"})
        if r.status_code == 429:
            time.sleep(30)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("GeckoTerminal ocupado (429)")


def meme_pools(now=None, get=None, pages=10):
    """Today's Solana memecoin pools with liquidity >= MEME_MIN_LIQ and created 7+ days ago (best pool per coin)."""
    now = now or time.time()
    best = {}
    for page in range(1, pages + 1):
        try:
            rows = (_gecko(GECKO + "/networks/solana/pools?page=" + str(page) + "&sort=h24_volume_usd_desc", get) or {}).get("data") or []
        except Exception as exc:
            logging.info("dip meme pools p%s: %s", page, type(exc).__name__)
            break
        for p in rows:
            a = p.get("attributes") or {}
            name = str(a.get("name") or "")
            sym = name.split("/")[0].strip().upper()
            quote = name.split("/")[-1].strip().upper() if "/" in name else ""
            liq = float(a.get("reserve_in_usd") or 0)
            try:
                created = time.mktime(time.strptime(str(a.get("pool_created_at"))[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            except Exception:
                continue
            if not sym or sym in NOT_MEME or quote not in ("SOL", "WSOL", "USDC", "USDT") or liq < MEME_MIN_LIQ:
                continue
            if now - created < MEME_MIN_AGE_D * 86400:
                continue
            if sym not in best or liq > best[sym]["liq"]:
                best[sym] = {"sym": sym, "pool": a.get("address"), "liq": liq, "created": created}
    return sorted(best.values(), key=lambda x: -x["liq"])[:MEME_MAX]


def gecko_hours(pool, days, now=None, get=None):
    """Hourly candles (oldest first) of the last `days` days from GeckoTerminal, paged 1000 at a time."""
    now = int(now or time.time())
    start = now - days * 86400
    out, before = {}, now
    for _ in range(days * 24 // 1000 + 2):
        try:
            d = _gecko(GECKO + "/networks/solana/pools/" + pool + "/ohlcv/hour?aggregate=1&limit=1000&currency=usd&before_timestamp=" +
                       str(before), get)
        except Exception as exc:
            logging.info("dip meme ohlcv: %s", type(exc).__name__)
            break
        rows = ((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        if not rows:
            break
        for r in rows:
            t = int(r[0])
            if t >= start and t + H <= now:
                out[t] = {"t": t, "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
        oldest = min(int(r[0]) for r in rows)
        if oldest <= start or len(rows) < 1000:
            break
        before = oldest
    return [out[t] for t in sorted(out)]


def run_meme(now=None, get=None):
    now = now or time.time()
    pools = meme_pools(now, get)
    data, missing = {}, []
    for p in pools:
        cs = gecko_hours(p["pool"], MEME_DAYS, now, get)
        cs = [c for c in cs if c["t"] >= p["created"] + MEME_MIN_AGE_D * 86400]   # never buy before 7 days of life
        if len(cs) > 24 * 14:
            data[p["sym"]] = cs
        else:
            missing.append(p["sym"])
    if not data:
        return None
    for coin, b, cb in COINS[:1]:                       # SOL as the buy-and-hold reference
        sol = fetch(b, cb, days=MEME_DAYS, now=now, get=None if get is None else get)
        if sol:
            data_ref = dict(data, SOL=sol)
            rep = analyse(data, missing, "meme", {"pools": len(pools)})
            rep["hold_sol_full"] = hold_sol(data_ref, rep["start"], rep["end"])
            rep["hold_sol_test"] = hold_sol(data_ref, rep["cut"], rep["end"])
            return rep
    return analyse(data, missing, "meme", {"pools": len(pools)})


def start(send, chat, kind="solid"):
    if not _RUN.acquire(blocking=False):
        return "já está rodando"

    def go():
        try:
            send(chat, text(run_meme() if kind == "meme" else run()))
        except Exception as exc:
            logging.exception("dip backtest")
            send(chat, "⛔ Backtest compra na baixa falhou: " + type(exc).__name__)
        finally:
            _RUN.release()
    threading.Thread(target=go, daemon=True).start()
    return ""
