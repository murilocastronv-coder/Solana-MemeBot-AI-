"""CONCENTRATED LIQUIDITY backtest - SOL/USDC (Orca / Meteora style), capital US$ 60 (owner, Nov 16).

What a passive "market maker" pool position does: your SOL+USDC sits in a price range; every swap that crosses it pays
you a fee. If the price leaves the range you stop earning and hold only the coin that lost (impermanent loss); the bot
re-centres the range (paying swap costs).
Data:
- price path: REAL hourly SOL candles of the last DAYS days (Hyperliquid public API);
- fee yield: TODAY's SOL/USDC pools on DexScreener - fee APR of the average LP = 24 h volume x fee / liquidity
  (fee tier FEE assumed, 0.04%, the common SOL/USDC tier). A narrower range earns more per dollar than the average LP
  (assumed to sit at +-20%): multiplier = capital efficiency of the range / efficiency of +-20%, capped at MAX_MULT.
Exact concentrated-liquidity math for the position value (Uniswap v3 / Orca whirlpool formula).
Re-centre when the price is out of range for REBAL_H hours; each re-centre costs REBAL_COST of the position.
Honest limits: fee APR is today's (volume changes a lot), in-range is checked on the hourly close, other LPs
concentrating more would take a bigger share. Only a backtest - nothing is opened.
"""
import logging
import math
import threading
import time

import requests

CAPITAL = 60.0
DAYS = 90
FEE = 0.0004
REBAL_H = 2
REBAL_COST = 0.002
MAX_MULT = 4.0
RANGES = (0.05, 0.10, 0.20, 0.40)
HL = "https://api.hyperliquid.xyz/info"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=SOL%20USDC"
DEX_TOKEN = "https://api.dexscreener.com/token-pairs/v1/solana/So11111111111111111111111111111111111111112"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
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
def candles(days=DAYS, now=None, post=None):
    post = post or (lambda body: requests.post(HL, json=body, timeout=20).json())
    now = now or time.time()
    rows = post({"type": "candleSnapshot", "req": {"coin": "SOL", "interval": "1h",
                                                   "startTime": int((now - days * 86400) * 1000), "endTime": int(now * 1000)}}) or []
    return [(int(r["t"]) // 1000, float(r["c"])) for r in rows if float(r.get("c") or 0) > 0]


def pool_fee_apr(get=None):
    """(fee APR of the average LP, description) from the biggest SOL/USDC concentrated pools on DexScreener."""
    get = get or (lambda u: requests.get(u, timeout=15).json())
    pairs = []
    for url in (DEX_TOKEN, DEX_SEARCH):
        try:
            got = get(url)
        except Exception as exc:
            logging.info("lp pools %s: %s", url[-30:], type(exc).__name__)
            continue
        pairs += got if isinstance(got, list) else ((got or {}).get("pairs") or [])
    good, seen = [], set()
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        mints = {(p.get("baseToken") or {}).get("address"), (p.get("quoteToken") or {}).get("address")}
        if mints != {SOL, USDC}:
            continue
        if str(p.get("dexId")) not in ("orca", "meteora", "raydium") or p.get("pairAddress") in seen:
            continue
        seen.add(p.get("pairAddress"))
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        vol = float((p.get("volume") or {}).get("h24") or 0)
        if liq > 50_000 and vol > 0:
            good.append((liq, vol, p.get("dexId")))
    if not good:
        logging.warning("lp pools: %d pairs read, none SOL/USDC on orca/meteora/raydium", len(pairs))
        return None, "sem dados de pool (" + str(len(pairs)) + " pares lidos)"
    good.sort(reverse=True)
    top = good[:5]
    liq = sum(x[0] for x in top)
    vol = sum(x[1] for x in top)
    return vol * FEE / liq * 365, (str(len(top)) + " maiores pools SOL/USDC (" + ", ".join(sorted({x[2] for x in top})) +
                                  "): volume 24 h US$ " + format(vol / 1e6, ".0f") + " mi, liquidez US$ " + format(liq / 1e6, ".0f") + " mi")


# ---------------------------------------------------------------- concentrated liquidity math
def _per_l(p, pa, pb):
    """Position value per unit of liquidity at price p (USDC per L)."""
    sa, sb = math.sqrt(pa), math.sqrt(pb)
    if p <= pa:
        return (1 / sa - 1 / sb) * p
    if p >= pb:
        return sb - sa
    sp = math.sqrt(p)
    return 2 * sp - sa - p / sb


def efficiency(w):
    """Capital efficiency of a +-w range vs full range."""
    pa, pb = 1 - w, 1 + w
    return 1 / (1 - math.sqrt(pa / pb) ** 0.5) if pa > 0 else 1.0


def simulate(path, w, fee_apr, capital=CAPITAL):
    if len(path) < 2 or fee_apr is None:
        return None
    mult = min(MAX_MULT, efficiency(w) / efficiency(0.20))
    fee_h = fee_apr / (365 * 24) * mult
    p0 = path[0][1]
    pa, pb = p0 * (1 - w), p0 * (1 + w)
    L = capital / _per_l(p0, pa, pb)
    fees = costs = 0.0
    out_h = in_h = rebal = 0
    for _, p in path[1:]:
        v = L * _per_l(p, pa, pb)
        if pa <= p <= pb:
            in_h += 1
            out_h = 0
            fees += v * fee_h
        else:
            out_h += 1
            if out_h >= REBAL_H:                       # re-centre the range around the current price
                c = v * REBAL_COST
                costs += c
                v -= c
                pa, pb = p * (1 - w), p * (1 + w)
                L = v / _per_l(p, pa, pb)
                rebal += 1
                out_h = 0
    end = L * _per_l(path[-1][1], pa, pb)
    days = max(1.0, (path[-1][0] - path[0][0]) / 86400)
    hodl = capital / 2 + capital / 2 * path[-1][1] / p0      # 50% SOL + 50% USDC, untouched
    net = end + fees - capital
    return {"w": w, "days": days, "fees": fees, "costs": costs, "end": end, "net": net, "per_month": net / days * 30,
            "fees_month": fees / days * 30, "in_range": in_h / max(1, len(path) - 1), "rebal": rebal,
            "vs_hodl": end + fees - hodl, "mult": mult}


def report(path, fee_apr, desc, capital=CAPITAL):
    L = ["🌊 BACKTEST LIQUIDEZ CONCENTRADA SOL/USDC (tipo Orca/Meteora) — capital US$ " + format(capital, "g"),
         "Seu SOL+USDC fica numa faixa de preço e ganha taxa de cada troca; se o preço sai da faixa, o robô recentraliza "
         "(custo " + format(REBAL_COST * 100, "g") + "% cada vez)."]
    if not path or fee_apr is None:
        L.append("⛔ Sem dados: " + ("preço do SOL" if not path else desc))
        return "\n".join(L)
    move = path[-1][1] / path[0][1] - 1
    L.append("Dados: " + str(len(path)) + " horas reais do SOL (" + format(move * 100, "+.0f") + "% no período) · " + desc +
             " → taxa média dos LPs ~" + format(fee_apr * 100, ".0f") + "% ao ano (taxa de " + format(FEE * 100, "g") + "% por troca)")
    best = None
    for w in RANGES:
        s = simulate(path, w, fee_apr, capital)
        L.append("• Faixa ±" + format(w * 100, "g") + "%: taxas ganhas US$ " + format(s["fees"], ".2f") + " (" +
                 format(s["fees_month"], ".2f") + "/mês) · dentro da faixa " + format(s["in_range"] * 100, ".0f") + "% do tempo · " +
                 str(s["rebal"]) + " recentralizações (custo US$ " + format(s["costs"], ".2f") + ") · resultado final " +
                 format(s["net"], "+.2f") + " USD (" + format(s["per_month"], "+.2f") + "/mês) · contra só segurar 50/50: " +
                 format(s["vs_hodl"], "+.2f"))
        if best is None or s["net"] > best["net"]:
            best = s
    L.append("🏆 Melhor faixa: ±" + format(best["w"] * 100, "g") + "% → " + format(best["per_month"], "+.2f") +
             " USD por mês com US$ " + format(capital, "g") + " (inclui a variação do preço do SOL no período)")
    L.append("⚠️ Taxa de hoje aplicada ao passado; faixas estreitas ganham mais taxa por dólar mas perdem mais quando o preço "
             "anda (perda impermanente). O resultado inclui o SOL subir/cair — não é renda garantida. Só backtest.")
    return "\n".join(L)


def start(send=None, chat=None, post=None, get=None):
    if not _RUNNING.acquire(blocking=False):
        return "o backtest já está rodando"

    def work():
        try:
            try:
                path = candles(post=post)
            except Exception as exc:
                logging.info("lp candles: %s", type(exc).__name__)
                path = []
            try:
                apr, desc = pool_fee_apr(get)
            except Exception as exc:
                logging.info("lp pools: %s", type(exc).__name__)
                apr, desc = None, "pools não responderam"
            if send and chat:
                _send_parts(send, chat, report(path, apr, desc))
        except Exception as exc:
            logging.exception("lp backtest failed")
            if send and chat:
                send(chat, "⛔ Backtest de liquidez falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
