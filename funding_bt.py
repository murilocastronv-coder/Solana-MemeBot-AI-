"""FUNDING-RATE ARBITRAGE backtest (owner, Nov 16): "faz somente o backtest para um capital de 60 USD".

Strategy (delta neutral, no bet on the price): hold the coin and a SHORT perpetual of the same size on Drift. The price
moves cancel out; what is left is the hourly funding: when it is positive the longs pay the shorts (you receive), when
negative you pay. REAL Drift funding records (Drift data API / Drift's public history files), hourly.
Capital US$ CAPITAL, two ways of holding it:
- "1x simples": half buys the coin, half is the margin of the short -> notional = 50% of the capital;
- "coin as collateral on Drift": the coin itself backs the short (Drift gives it a collateral weight) -> ~85% of the capital.
Two ways of running it:
- "sempre ligado": open once, keep it the whole period;
- "liga/desliga": open only when the average funding of the last 24 h is positive, close when it turns negative
  (each switch pays the costs again).
Costs: COST_SIDE per side on each leg (swap + Drift taker fee), entry and exit. Nothing here is promised.
"""
import logging
import threading
import time

import requests

CAPITAL = 60.0
COST_SIDE = 0.001                    # 0.1% per side per leg (Jupiter swap / Drift taker), entry and exit
MARKETS = ("SOL-PERP", "BTC-PERP", "ETH-PERP", "JUP-PERP", "HYPE-PERP",
           "WIF-PERP", "1MBONK-PERP", "POPCAT-PERP", "FARTCOIN-PERP")   # owner: the smaller coins too
SMALL = {"WIF-PERP", "1MBONK-PERP", "POPCAT-PERP", "FARTCOIN-PERP", "JUP-PERP"}
SMALL_COST_SIDE = 0.003              # small coins: buying/selling the coin costs more (0.3% per side)
HL = "https://api.hyperliquid.xyz/info"
HL_COIN = {"SOL-PERP": "SOL", "BTC-PERP": "BTC", "ETH-PERP": "ETH", "JUP-PERP": "JUP", "HYPE-PERP": "HYPE",
           "WIF-PERP": "WIF", "1MBONK-PERP": "kBONK", "POPCAT-PERP": "POPCAT", "FARTCOIN-PERP": "FARTCOIN"}
API = "https://data.api.drift.trade/fundingRates?marketName="
S3 = ("https://drift-historical-data-v2.s3.eu-west-1.amazonaws.com/program/dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH"
      "/market/{m}/fundingRateRecords/{y}/{d}")
DAYS = 90
_RUNNING = threading.Lock()


def _send_parts(send, chat, text, limit=3500):
    """Telegram refuses messages over ~4096 characters: send line blocks."""
    cur = ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            send(chat, cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        send(chat, cur)


# ---------------------------------------------------------------- data
def _rate(row):
    """Hourly funding as a fraction of the price (Drift: fundingRate 1e9 precision, oracle TWAP 1e6)."""
    fr = float(row.get("fundingRate") or row.get("funding_rate") or 0)
    px = float(row.get("oraclePriceTwap") or row.get("oracle_price_twap") or 0)
    if px <= 0:
        return None
    return fr / 1e9 / (px / 1e6)


def from_api(market, get=None):
    get = get or (lambda u: requests.get(u, timeout=20).json())
    data = get(API + market) or {}
    rows = data.get("fundingRates") or data.get("data") or []
    out = []
    for r in rows:
        x = _rate(r)
        t = int(float(r.get("ts") or r.get("timestamp") or 0))
        if x is not None and t:
            out.append((t, x))
    return sorted(set(out))


def from_s3(market, days=DAYS, now=None, get_text=None):
    """Drift's public daily CSV files (hourly records)."""
    import csv
    import io
    get_text = get_text or (lambda u: requests.get(u, timeout=20).text)
    now = now or time.time()
    out = []
    misses = 0
    for i in range(days, 0, -1):
        if misses >= 5 and not out:                       # host unreachable / no files: stop early
            break
        day = time.gmtime(now - i * 86400)
        url = S3.format(m=market, y=day.tm_year, d=time.strftime("%Y%m%d", day))
        try:
            txt = get_text(url)
        except Exception:
            misses += 1
            continue
        if not txt or not txt[:200].count(","):
            misses += 1
            continue
        for r in csv.DictReader(io.StringIO(txt)):
            x = _rate(r)
            t = int(float(r.get("ts") or 0))
            if x is not None and t:
                out.append((t, x))
    return sorted(set(out))


def from_hyperliquid(market, days=DAYS, now=None, post=None):
    """Hourly funding history (public, no key). Used when Drift's data host can't be reached from the server;
    the funding of the same coin moves together across the big perp venues (same longs paying shorts)."""
    post = post or (lambda body: requests.post(HL, json=body, timeout=20).json())
    coin = HL_COIN.get(market)
    if not coin:
        return []
    now = now or time.time()
    start = int((now - days * 86400) * 1000)
    out = []
    for _ in range(12):                                   # 500 records per call
        rows = post({"type": "fundingHistory", "coin": coin, "startTime": start}) or []
        if not rows:
            break
        for r in rows:
            out.append((int(r["time"]) // 1000, float(r["fundingRate"])))
        last = int(rows[-1]["time"])
        if len(rows) < 500:
            break
        start = last + 1
    return sorted(set(out))


def history(market, days=DAYS, get=None, get_text=None, post=None):
    """(records [(ts, hourly rate)], source)."""
    try:
        rows = from_s3(market, days, get_text=get_text)
        if len(rows) > 24 * 20:
            return rows, "arquivos históricos da Drift"
    except Exception as exc:
        logging.info("funding s3 %s: %s", market, type(exc).__name__)
    try:
        rows = from_api(market, get)
        if rows:
            return rows, "API da Drift (últimos ~30 dias)"
    except Exception as exc:
        logging.info("funding api %s: %s", market, type(exc).__name__)
    try:
        rows = from_hyperliquid(market, days, post=post)
        if rows:
            return rows, "Hyperliquid (mesma moeda; a Drift não respondeu)"
    except Exception as exc:
        logging.info("funding hyperliquid %s: %s", market, type(exc).__name__)
    return [], "sem dados"


# ---------------------------------------------------------------- simulation
def simulate(rows, notional, mode="always", cost=COST_SIDE):
    """Profit in USD for a short of `notional` USD hedged by the coin. Returns dict."""
    if not rows:
        return None
    open_cost = notional * cost * 2                    # coin buy + short open
    close_cost = notional * cost * 2
    pnl, fees, on, switches, paid_h, got_h = 0.0, 0.0, False, 0, 0, 0
    window = []
    for t, r in rows:
        window.append(r)
        window = window[-24:]
        if mode == "always":
            want = True
        else:
            want = len(window) >= 24 and sum(window) / len(window) > 0
        if want and not on:
            fees += open_cost
            on = True
            switches += 1
        elif not want and on:
            fees += close_cost
            on = False
        if on:
            pnl += notional * r                        # short receives a positive funding
            if r >= 0:
                got_h += 1
            else:
                paid_h += 1
    if on:
        fees += close_cost
    days = max(1.0, (rows[-1][0] - rows[0][0]) / 86400)
    net = pnl - fees
    return {"days": days, "funding": pnl, "fees": fees, "net": net, "per_month": net / days * 30,
            "apr": net / notional / days * 365 if notional else 0.0, "switches": switches, "got_h": got_h, "paid_h": paid_h}


def report(results, capital=CAPITAL):
    L = ["📈 BACKTEST ARBITRAGEM DE FUNDING (Drift) — capital US$ " + format(capital, "g") + ", sem apostar na direção do preço",
         "Compra a moeda e abre um SHORT do mesmo tamanho: o preço se anula, sobra a taxa de funding de cada hora.",
         "Custos por lado em cada perna (entrada e saída): " + format(COST_SIDE * 100, "g") + "% nas grandes, " +
         format(SMALL_COST_SIDE * 100, "g") + "% nas menores (WIF, BONK, POPCAT, FARTCOIN, JUP)."]
    best = None
    for m, (rows, src) in results.items():
        if not rows:
            L.append("• " + m + ": sem dados")
            continue
        avg = sum(r for _, r in rows) / len(rows)
        pos = sum(1 for _, r in rows if r > 0) / len(rows)
        L.append("• " + m + " — " + str(len(rows)) + " horas (" + src + "), funding médio " + format(avg * 24 * 365 * 100, "+.1f") +
                 "% ao ano, positivo em " + format(pos * 100, ".0f") + "% das horas")
        cost = SMALL_COST_SIDE if m in SMALL else COST_SIDE
        for label, notional in (("1x simples", capital * 0.5), ("moeda como garantia", capital * 0.85)):
            for mode, mlabel in (("always", "sempre ligado"), ("switch", "liga/desliga")):
                s = simulate(rows, notional, mode, cost)
                L.append("   " + label + " · " + mlabel + ": " + format(s["net"], "+.2f") + " USD em " + format(s["days"], ".0f") +
                         " dias → " + format(s["per_month"], "+.2f") + " USD/mês (" + format(s["apr"] * 100, "+.1f") +
                         "% ao ano) · taxas pagas " + format(s["fees"], ".2f"))
                if best is None or s["per_month"] > best[0]["per_month"]:
                    best = (s, m, label, mlabel)
    if best:
        s, m, label, mlabel = best
        L.append("🏆 Melhor: " + m + " · " + label + " · " + mlabel + " → " + format(s["per_month"], "+.2f") + " USD por mês com US$ " +
                 format(capital, "g"))
    L.append("⚠️ Funding passado não garante o futuro (pode virar negativo por semanas); não inclui risco de liquidação "
             "do short numa alta muito forte nem a variação do peso da garantia. Só backtest — nada foi ligado.")
    return "\n".join(L)


def start(send=None, chat=None, markets=MARKETS, get=None, get_text=None, post=None):
    if not _RUNNING.acquire(blocking=False):
        return "o backtest já está rodando"

    def work():
        try:
            res = {m: history(m, get=get, get_text=get_text, post=post) for m in markets}
            txt = report(res)
            if send and chat:
                _send_parts(send, chat, txt)
        except Exception as exc:
            logging.exception("funding backtest failed")
            if send and chat:
                send(chat, "⛔ Backtest de funding falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
