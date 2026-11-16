"""POOL ROBOT (owner, Nov 16): find the safe pools that pay the most per day, enter, stay at most 3 days, move the
money to another pool when the yield falls a lot or the coin drops - everything automatic.

STARTS IN SIMULATION (paper money, real data): real money needs Meteora's deposit/withdraw integration and the
liquidity backtest lost money even on SOL/USDC, so the rule must first prove itself here.

Every CYCLE_S:
1. Pools: Meteora DLMM API (fees of the last 24 h, liquidity, volume) - only pools against SOL / USDC / USDT.
2. Safe = liquidity >= MIN_LIQ, 24 h volume >= MIN_VOL, pool not hidden/blacklisted by Meteora, the pair exists on
   DexScreener for >= MIN_AGE_D days, and the coin passes the anti-scam checks (signals.safety_reason: mint/freeze
   authority, token program, RugCheck).
3. Yield per day = fees of 24 h / liquidity (what the average liquidity provider earned per dollar).
4. Position (paper US$ CAPITAL): fees accrue every hour at the pool's current rate; the coin's price move is charged
   as impermanent loss (constant-product formula: a lower bound for a concentrated DLMM position).
5. Exit and rotate when: MAX_DAYS reached, the yield fell below FALL x the entry yield, or the coin fell STOP from entry.
"""
import json
import logging
import math
import threading
import time

import requests

import signals as sg

E = sg._f
CAPITAL = E("POOL_CAPITAL_USD", 60)
CYCLE_S = E("POOL_CYCLE_S", 600)
MIN_LIQ = E("POOL_MIN_LIQ_USD", 250_000)
MIN_VOL = E("POOL_MIN_VOL_USD", 100_000)
MIN_AGE_D = E("POOL_MIN_AGE_D", 3)
MAX_DAYS = E("POOL_MAX_DAYS", 3)
FALL = E("POOL_YIELD_FALL", 0.5)                 # leave when the yield is under half of the entry yield
STOP = E("POOL_PRICE_STOP", 0.15)                # leave when the coin fell 15% from entry
MAX_DAY_YIELD = E("POOL_MAX_DAY_YIELD", 0.20)    # above 20%/day it is a pump, not a pool to sit in
METEORA = "https://dlmm-api.meteora.ag/pair/all"
QUOTES = {"So11111111111111111111111111111111111111112": "SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
KEY = "pool_bot"
_LOCK = threading.RLock()
_MEM = {}


# ---------------------------------------------------------------- state
def _load():
    with _LOCK:
        if not _MEM:
            d = {"enabled": False, "pos": None, "closed": [], "log": [], "cash": CAPITAL}
            try:
                import pg_store
                if pg_store.enabled():
                    got = pg_store.get(KEY)
                    if got:
                        d.update(got)
            except Exception:
                pass
            _MEM.update(d)
        return _MEM


def _save():
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, json.loads(json.dumps(_MEM)))
    except Exception as exc:
        logging.warning("pool: save failed (%s)", type(exc).__name__)


def note(text):
    logging.warning("pool %s", text)
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-29:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]


def set_enabled(on):
    with _LOCK:
        _load()["enabled"] = bool(on)
        _save()


def enabled():
    return bool(_load().get("enabled"))


# ---------------------------------------------------------------- data
def pools(get=None):
    """[{address, name, coin, quote, liq, vol, fees24, day_yield}] - Meteora DLMM pools against SOL/USDC/USDT."""
    get = get or (lambda u: requests.get(u, timeout=60).json())
    rows = get(METEORA) or []
    if isinstance(rows, dict):
        rows = rows.get("pairs") or rows.get("data") or []
    out = []
    for p in rows:
        if p.get("hide") or p.get("is_blacklisted"):
            continue
        x, y = p.get("mint_x"), p.get("mint_y")
        if y in QUOTES and x not in QUOTES:
            coin, quote = x, y
        elif x in QUOTES and y not in QUOTES:
            coin, quote = y, x
        else:
            continue
        liq = float(p.get("liquidity") or 0)
        vol = float(p.get("trade_volume_24h") or 0)
        fees = float(p.get("fees_24h") or 0)
        if liq < MIN_LIQ or vol < MIN_VOL or fees <= 0:
            continue
        out.append({"address": p.get("address"), "name": p.get("name") or "?", "coin": coin, "quote": QUOTES[quote],
                    "liq": liq, "vol": vol, "fees24": fees, "day_yield": fees / liq})
    out.sort(key=lambda r: -r["day_yield"])
    return out


def dex_info(mint, fetch=None):
    """(price USD, age in days) of the coin's biggest pair on DexScreener."""
    rows = (fetch or sg._get)(sg.DEX + "/tokens/v1/solana/" + mint) or []
    best = None
    for p in rows if isinstance(rows, list) else []:
        if (p.get("baseToken") or {}).get("address") != mint:
            continue
        if best is None or float((p.get("liquidity") or {}).get("usd") or 0) > float((best.get("liquidity") or {}).get("usd") or 0):
            best = p
    if not best:
        return None, None
    created = float(best.get("pairCreatedAt") or 0) / 1000
    return float(best.get("priceUsd") or 0), ((time.time() - created) / 86400 if created else None)


def safe_reason(pool, fetch=None, safety=None):
    """'' when the pool may be used, else why not."""
    if pool["day_yield"] > MAX_DAY_YIELD:
        return "rende " + format(pool["day_yield"] * 100, ".0f") + "%/dia: é euforia de memecoin, não pool para ficar"
    px, age = dex_info(pool["coin"], fetch)
    if not px:
        return "sem preço na DexScreener"
    if age is not None and age < MIN_AGE_D:
        return "moeda com menos de " + format(MIN_AGE_D, "g") + " dias"
    why = (safety or sg.safety_reason)(pool["coin"])
    return ("anti-golpe: " + why) if why else ""


def il(ratio):
    """Impermanent loss of a 50/50 constant-product position for a price ratio (<= 0)."""
    return 2 * math.sqrt(ratio) / (1 + ratio) - 1 if ratio > 0 else -1.0


# ---------------------------------------------------------------- cycle
def value_now(pos, price):
    return pos["capital"] * (1 + il(price / pos["entry_price"])) + pos["fees"]


def choose(cands, fetch=None, safety=None, skip=()):
    for p in cands[:25]:
        if p["address"] in skip:
            continue
        why = safe_reason(p, fetch, safety)
        if not why:
            return p
        logging.info("pool skip %s: %s", p["name"], why)
    return None


def cycle(send=None, chat=None, now=None, get=None, fetch=None, safety=None):
    now = now or time.time()
    d = _load()
    if not d.get("enabled"):
        return None
    cands = pools(get)
    by_addr = {p["address"]: p for p in cands}
    pos = d.get("pos")
    if pos:
        hours = (now - pos["last"]) / 3600
        cur = by_addr.get(pos["address"])
        rate = cur["day_yield"] if cur else 0.0
        px, _ = dex_info(pos["coin"], fetch)
        px = px or pos["last_price"]
        pos["fees"] += value_now(pos, px) * rate / 24 * hours
        pos["last"], pos["last_price"], pos["rate_now"] = now, px, rate
        why = ""
        if now - pos["opened"] >= MAX_DAYS * 86400:
            why = "completou " + format(MAX_DAYS, "g") + " dias"
        elif rate < pos["entry_rate"] * FALL:
            why = "rendimento caiu de " + format(pos["entry_rate"] * 100, ".2f") + "% para " + format(rate * 100, ".2f") + "% ao dia"
        elif px <= pos["entry_price"] * (1 - STOP):
            why = "moeda caiu " + format((1 - px / pos["entry_price"]) * 100, ".0f") + "%"
        if why:
            v = value_now(pos, px)
            res = v - pos["capital"]
            d["cash"] = v
            d["closed"] = (d.get("closed") or [])[-49:] + [{"name": pos["name"], "days": (now - pos["opened"]) / 86400,
                                                           "fees": pos["fees"], "il": v - pos["fees"] - pos["capital"],
                                                           "result": res, "why": why}]
            txt = ("🏊 POOL (simulação): saí de " + pos["name"] + " — " + why + " · taxas +" + format(pos["fees"], ".2f") +
                   " · perda impermanente " + format(v - pos["fees"] - pos["capital"], "+.2f") + " → " + format(res, "+.2f") + " USD")
            note(txt)
            if send and chat:
                send(chat, txt)
            d.setdefault("exited", {})[pos["address"]] = now
            d["pos"] = pos = None
    if not d.get("pos"):
        recent = {a for a, t in (d.get("exited") or {}).items() if now - t < 86400}   # no going back for 24 h
        pick = choose(cands, fetch, safety, skip=recent)
        if pick:
            px, _ = dex_info(pick["coin"], fetch)
            cap = float(d.get("cash") or CAPITAL)
            d["pos"] = {"address": pick["address"], "name": pick["name"], "coin": pick["coin"], "opened": now, "last": now,
                        "entry_price": px, "last_price": px, "entry_rate": pick["day_yield"], "rate_now": pick["day_yield"],
                        "capital": cap, "fees": 0.0}
            txt = ("🏊 POOL (simulação): entrei em " + pick["name"] + " (Meteora) com US$ " + format(cap, ".2f") + " · rende " +
                   format(pick["day_yield"] * 100, ".2f") + "% ao dia · liquidez US$ " + format(pick["liq"] / 1e6, ".1f") + " mi")
            note(txt)
            if send and chat:
                send(chat, txt)
    _save()
    return d.get("pos")


def status_text():
    d = _load()
    pos = d.get("pos")
    closed = d.get("closed") or []
    L = ["🏊 ROBÔ DE POOLS (Meteora) — " + ("🟡 LIGADO · SIMULAÇÃO (dinheiro de mentira, dados reais)" if d.get("enabled") else "⏸️ DESLIGADO"),
         "⚙️ Só pools seguras: liquidez ≥ US$ " + format(MIN_LIQ / 1000, "g") + " mil, volume ≥ US$ " + format(MIN_VOL / 1000, "g") +
         " mil/24 h, moeda com ≥ " + format(MIN_AGE_D, "g") + " dias e anti-golpe · fica no máximo " + format(MAX_DAYS, "g") +
         " dias · troca se o rendimento cair à metade ou a moeda cair " + format(STOP * 100, ".0f") + "%"]
    if pos:
        v = value_now(pos, pos["last_price"])
        L.append("📍 Agora em " + pos["name"] + " há " + format((time.time() - pos["opened"]) / 3600, ".0f") + " h · rendia " +
                 format(pos["entry_rate"] * 100, ".2f") + "%/dia, agora " + format(pos.get("rate_now", 0) * 100, ".2f") +
                 "%/dia · taxas +" + format(pos["fees"], ".2f") + " · valor US$ " + format(v, ".2f"))
    tot = sum(c["result"] for c in closed)
    L.append("📊 " + str(len(closed)) + " pools fechadas · resultado " + format(tot, "+.2f") + " USD · capital simulado US$ " +
             format(float(d.get("cash") or CAPITAL) if not pos else value_now(pos, pos["last_price"]), ".2f"))
    for x in (d.get("log") or [])[-4:]:
        L.append("• " + x)
    return "\n".join(L)
