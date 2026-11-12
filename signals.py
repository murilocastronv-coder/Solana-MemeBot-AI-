"""📡 Signals bot: Solana memecoin entry signals (does NOT trade; tells the owner when and where).

1. Coins: DexScreener new/boosted Solana pairs + GeckoTerminal trending / pump.fun / PumpSwap pools.
   Liquidity US$ 10k-150k, pair >= 3 h old (enough 5-min candles), SOL/USDC quote.
2. "Most people are buying it": last hour >= SIGNAL_MIN_BUYS_1H buys and >= 55% of the trades are
   buys, 24 h volume >= US$ 30k, and the coin is in an uptrend (price up in 6 h and 24 h).
3. Entry on the pullback ("buy the dip in an uptrend"), from 5-min candles (GeckoTerminal OHLCV):
   - uptrend: EMA20 above EMA50;
   - correction: price fell 8%-40% from the top of the last 2 h;
   - bottom: RSI(14) went down to <= 38 in the last 30 min and is turning up;
   - confirmation: the last closed candle is green and closed above the previous candle's high.
4. Anti-scam before any signal: mint/freeze authority revoked, top 10 holders, honeypot (Jupiter
   buy->sell test), Token-2022 traps, RugCheck danger risks / unlocked liquidity.
5. Levels: entry = last close; stop loss just below the bottom of the correction (6%-20% away);
   TP1 = 1.5x the risk, TP2 = 3x the risk. After TP1 the stop goes to the entry (break-even).
6. Every signal is followed (DexScreener price) and scored: TP1 / TP2 / SL / expired after 24 h.
   The status shows the real results (hit rate, average % per signal).
No signal can promise that a memecoin goes straight up: the results are measured, not assumed.
"""
import json
import logging
import os
import tempfile
import threading
import time

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTES = {SOL_MINT, USDC_MINT}


def _f(name, default):
    return float(os.getenv(name, str(default)))


MIN_LIQ = _f("SIGNAL_MIN_LIQUIDITY_USD", 10_000)
MAX_LIQ = _f("SIGNAL_MAX_LIQUIDITY_USD", 150_000)
MIN_AGE_S = _f("SIGNAL_MIN_AGE_HOURS", 3) * 3600
MIN_BUYS_1H = int(_f("SIGNAL_MIN_BUYS_1H", 80))
MIN_BUY_SHARE = _f("SIGNAL_MIN_BUY_SHARE", 0.55)
MIN_VOL_24H = _f("SIGNAL_MIN_VOLUME_24H", 30_000)
PULLBACK_MIN = _f("SIGNAL_PULLBACK_MIN", 0.08)
PULLBACK_MAX = _f("SIGNAL_PULLBACK_MAX", 0.40)
RSI_DIP = _f("SIGNAL_RSI_DIP", 38)
SL_MIN = _f("SIGNAL_SL_MIN", 0.06)
SL_MAX = _f("SIGNAL_SL_MAX", 0.20)
TP1_R = _f("SIGNAL_TP1_R", 1.5)
TP2_R = _f("SIGNAL_TP2_R", 3.0)
EXPIRE_S = _f("SIGNAL_EXPIRE_HOURS", 24) * 3600
COOLDOWN_S = _f("SIGNAL_COOLDOWN_HOURS", 6) * 3600
SCAN_EVERY_S = _f("SIGNAL_SCAN_SECONDS", 120)
CANDLE_CHECKS = int(_f("SIGNAL_CANDLE_CHECKS", 6))     # coins whose candles are read per scan (API limits)

DEX = "https://api.dexscreener.com"
DEX_FEEDS = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1")
GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_FEEDS = ("/networks/solana/trending_pools?page=1",
               "/networks/solana/dexes/pumpswap/pools?page=1&sort=h24_tx_count_desc",
               "/networks/solana/dexes/pump-fun/pools?page=1&sort=h24_tx_count_desc")
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
MIN_LP_LOCKED = _f("SIGNAL_MIN_LP_LOCKED", 80)
BAD_EXTENSIONS = {"transferHook": "token com gatilho de transferência (pode travar a venda)",
                  "permanentDelegate": "criador pode tirar as moedas da sua carteira",
                  "nonTransferable": "token não pode ser transferido",
                  "defaultAccountState": "contas novas nascem congeladas"}

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "coins": 0, "popular": 0, "checked": 0, "signals": 0, "why": {}}


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("signals.json")


def _default():
    return {"enabled": False, "open": {}, "history": [], "sent": {}}


def _load():
    d = _default()
    p = _path()
    if p.exists():
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".signals.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def set_enabled(on):
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        _save(d)


def enabled():
    return bool(_load()["enabled"])


# ---------------------------------------------------------------- data
def _get(url):
    r = _http.get(url, timeout=15, headers={"Accept": "application/json", "User-Agent": "SolanaSignals/1.0"})
    r.raise_for_status()
    return r.json()


def candidate_pairs(fetch=None):
    """Best DexScreener pair of each Solana coin from DexScreener and GeckoTerminal lists."""
    fetch = fetch or _get
    mints = []
    for feed in DEX_FEEDS:
        try:
            rows = fetch(DEX + feed)
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress"):
                mints.append(row["tokenAddress"])
    for feed in GECKO_FEEDS:
        try:
            rows = fetch(GECKO + feed)
        except Exception:
            continue
        for pool in (rows or {}).get("data", []) if isinstance(rows, dict) else []:
            base = ((((pool or {}).get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if base.startswith("solana_"):
                mints.append(base[len("solana_"):])
    mints = [m for m in dict.fromkeys(mints) if m not in QUOTES][:240]
    best = {}
    for i in range(0, len(mints), 30):
        try:
            pairs = fetch(DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        except Exception:
            continue
        for p in pairs if isinstance(pairs, list) else []:
            mint = ((p or {}).get("baseToken") or {}).get("address")
            if not mint or p.get("chainId") != "solana":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if mint not in best or liq > float((best[mint].get("liquidity") or {}).get("usd") or 0):
                best[mint] = p
    return list(best.values())


def popular_reason(p, now=None):
    """'' if the coin is in range, popular (most people buying) and in an uptrend; else why not."""
    now = now or time.time()
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    if liq < MIN_LIQ:
        return "liquidez < US$ " + format(MIN_LIQ / 1000, ".0f") + " mil"
    if liq > MAX_LIQ:
        return "liquidez > US$ " + format(MAX_LIQ / 1000, ".0f") + " mil"
    if ((p.get("quoteToken") or {}).get("address")) not in QUOTES:
        return "par sem SOL/USDC"
    created = p.get("pairCreatedAt")
    if not created or now - float(created) / 1000 < MIN_AGE_S:
        return "moeda nova demais (poucas velas)"
    h1 = (p.get("txns") or {}).get("h1") or {}
    b, s = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    if b < MIN_BUYS_1H:
        return "pouca gente comprando"
    if b / max(b + s, 1) < MIN_BUY_SHARE:
        return "mais venda que compra"
    if float((p.get("volume") or {}).get("h24") or 0) < MIN_VOL_24H:
        return "volume baixo"
    ch = p.get("priceChange") or {}
    if float(ch.get("h6") or 0) <= 0 or float(ch.get("h24") or 0) <= 0:
        return "fora de tendência de alta"
    return ""


def popularity(p):
    h1 = (p.get("txns") or {}).get("h1") or {}
    b, s = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    return b * (b / max(b + s, 1))


def candles(pool, fetch=None):
    """5-minute candles [(ts, open, high, low, close, volume)] oldest first (GeckoTerminal)."""
    fetch = fetch or _get
    data = fetch(GECKO + "/networks/solana/pools/" + pool + "/ohlcv/minute?aggregate=5&limit=100&currency=usd")
    rows = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    return sorted((tuple(float(x) for x in r[:6]) for r in rows), key=lambda r: r[0])


# ---------------------------------------------------------------- strategy
def ema(values, n):
    k, out = 2 / (n + 1), []
    for v in values:
        out.append(v if not out else v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    out = [50.0] * len(closes)
    gains = losses = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            gains += g
            losses += l
            if i == n:
                gains, losses = gains / n, losses / n
                out[i] = 100 - 100 / (1 + gains / losses) if losses else 100.0
            continue
        gains = (gains * (n - 1) + g) / n
        losses = (losses * (n - 1) + l) / n
        out[i] = 100 - 100 / (1 + gains / losses) if losses else 100.0
    return out


def entry_setup(cs):
    """(levels dict, '') when the candles show a dip in an uptrend that is turning up; else (None, why)."""
    if len(cs) < 60:
        return None, "poucas velas"
    closes = [c[4] for c in cs]
    highs, lows = [c[2] for c in cs], [c[3] for c in cs]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if e20[-1] <= e50[-1]:
        return None, "tendência de 5 min não é de alta"
    top = max(highs[-24:])                       # top of the last 2 hours
    bottom = min(lows[-6:])                      # bottom of the last 30 minutes
    pull = (top - bottom) / top if top else 0
    if pull < PULLBACK_MIN:
        return None, "sem correção (perto do topo)"
    if pull > PULLBACK_MAX:
        return None, "caiu demais (quebrou a tendência)"
    r = rsi(closes)
    if min(r[-6:]) > RSI_DIP:
        return None, "não chegou no fundo (RSI)"
    if r[-1] <= r[-2]:
        return None, "ainda caindo (RSI)"
    last, prev = cs[-1], cs[-2]
    if not (last[4] > last[1] and last[4] > prev[2]):
        return None, "sem vela de confirmação"
    entry = last[4]
    sl = bottom * 0.98
    risk = (entry - sl) / entry
    if risk < SL_MIN:
        sl, risk = entry * (1 - SL_MIN), SL_MIN
    if risk > SL_MAX:
        return None, "stop ficaria longe demais"
    vols = [c[5] for c in cs]
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) > 21 else 0
    return {"entry": entry, "sl": sl, "tp1": entry * (1 + TP1_R * risk), "tp2": entry * (1 + TP2_R * risk),
            "risk": risk, "pullback": pull, "rsi": r[-1], "rsi_min": min(r[-6:]), "top": top,
            "vol_ratio": (vols[-1] / avg_vol) if avg_vol else 1.0}, ""


# ---------------------------------------------------------------- signal strength (70% / 80% / 90%)
MIN_STRENGTH = _f("SIGNAL_MIN_STRENGTH", 70)


def _lin(x, lo, hi, pts):
    """0 at lo, pts at hi (linear, clipped)."""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (float(x) - lo) / (hi - lo))) * pts


def strength(p, setup, buzz=None):
    """(score 0-97, parts) — how strong the setup is. It is a quality score, not a promise: the status
    shows the real hit rate of each level as signals close."""
    h1 = (p.get("txns") or {}).get("h1") or {}
    b, s_ = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    ch = p.get("priceChange") or {}
    parts = {
        "maioria comprando": _lin(b / max(b + s_, 1), MIN_BUY_SHARE, 0.80, 10),
        "muita gente comprando": _lin(b, MIN_BUYS_1H, 600, 10),
        "tendência 24 h": _lin(float(ch.get("h24") or 0), 0, 100, 5),
        "tendência 6 h": _lin(float(ch.get("h6") or 0), 0, 20, 5),
        "fundo (RSI)": _lin(RSI_DIP - setup.get("rsi_min", RSI_DIP), 0, RSI_DIP - 30, 5),
        "volume na virada": _lin(setup.get("vol_ratio", 1.0), 1.0, 1.5, 5),
        "correção saudável": 5.0 if 0.12 <= setup.get("pullback", 0) <= 0.30 else 2.0,
    }
    if buzz:
        parts["Twitter (menções)"] = _lin(buzz.get("h1", 0), 0, 50, 8)
        parts["Twitter crescendo"] = _lin(buzz.get("growth", 0), 1.0, 2.0, 5)
    score = min(97.0, 45.0 + sum(parts.values()))
    return round(score), parts


def level(score):
    """90 / 80 / 70 or None (below the minimum: no signal)."""
    for lv in (90, 80, 70):
        if score >= lv and score >= MIN_STRENGTH:
            return lv
    return None


LEVEL_ICON = {90: "🔥", 80: "🟢", 70: "🟡"}


# ---------------------------------------------------------------- anti-scam
def token_program_reason(mint, rpc=None):
    import copy_trader as ct
    rpc = rpc or ct._rpc
    info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    parsed = (((info or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
    for ext in (parsed.get("info") or {}).get("extensions") or []:
        name = ext.get("extension")
        if name in BAD_EXTENSIONS:
            return BAD_EXTENSIONS[name]
        if name == "transferFeeConfig":
            st = ext.get("state") or {}
            fee = max(int(((st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")) or 0),
                      int(((st.get("olderTransferFee") or {}).get("transferFeeBasisPoints")) or 0))
            if fee > 0:
                return "token cobra taxa de " + format(fee / 100, ".1f") + "% em cada transferência"
    return ""


def rugcheck_reason(mint, get=None, curve=False):
    get = get or _get
    try:
        r = get(RUGCHECK.format(mint))
    except Exception as exc:
        return "RugCheck indisponível (" + type(exc).__name__ + ")"
    if not isinstance(r, dict) or not ("risks" in r or "score" in r or "score_normalised" in r):
        return "RugCheck indisponível (resposta inesperada)"
    danger = [x.get("name") for x in (r.get("risks") or []) if str(x.get("level")).lower() == "danger"]
    if danger:
        return "RugCheck: " + str(danger[0])
    locked = r.get("lpLockedPct")
    if not curve and locked is not None and float(locked) < MIN_LP_LOCKED:
        return "liquidez só " + format(float(locked), ".0f") + "% travada/queimada (risco de rug)"
    return ""


def safety_reason(mint, curve=False):
    import copy_trader as ct
    for check in (lambda: ct.safety_reason(mint), lambda: token_program_reason(mint),
                  lambda: rugcheck_reason(mint, curve=curve)):
        try:
            why = check()
        except Exception as exc:
            why = "verificação indisponível (" + type(exc).__name__ + ")"
        if why:
            return why
    return ""


# ---------------------------------------------------------------- signals
def _fmt_price(x):
    return format(x, ".10f").rstrip("0").rstrip(".") if x < 1 else format(x, ",.4f")


def signal_text(s):
    pct = lambda x: format((x / s["entry"] - 1) * 100, "+.0f") + "%"
    tw = s.get("buzz")
    return ("📡 SINAL DE ENTRADA — " + s["symbol"] + "\n"
            "Força do sinal: " + LEVEL_ICON.get(s.get("level"), "") + " " + str(s.get("score", "?")) + "% (nível " +
            str(s.get("level", "?")) + "%)\n"
            "🐦 Twitter: " + (str(tw["h1"]) + " menções na última hora, " + str(tw["h24"]) + " em 24 h" +
                             (" · crescendo " + format(tw["growth"], ".1f") + "×" if tw.get("growth", 0) >= 1.2 else "")
                             if tw else "não configurado (coloque X_BEARER_TOKEN no Render)") + "\n"
            "Moeda em alta, a maioria comprando (" + str(s["buys_1h"]) + " compras na última hora, " +
            format(s["buy_share"] * 100, ".0f") + "% compras) e voltando a subir depois da correção " +
            "(caiu " + format(s["pullback"] * 100, ".0f") + "% do topo, RSI " + format(s["rsi"], ".0f") + ").\n\n"
            "🎯 ENTRADA: US$ " + _fmt_price(s["entry"]) + " (até " + _fmt_price(s["entry"] * 1.03) + ")\n"
            "🛑 STOP LOSS: US$ " + _fmt_price(s["sl"]) + " (" + pct(s["sl"]) + ")\n"
            "✅ TAKE PROFIT 1: US$ " + _fmt_price(s["tp1"]) + " (" + pct(s["tp1"]) + ") — vender metade, stop vai para a entrada\n"
            "🚀 TAKE PROFIT 2: US$ " + _fmt_price(s["tp2"]) + " (" + pct(s["tp2"]) + ") — vender o resto\n\n"
            "Liquidez US$ " + format(s["liquidity"] / 1000, ".0f") + " mil · 🛡️ anti-golpe OK\n" + s["mint"] +
            "\ndexscreener.com/solana/" + s["pair"] + "\n⚠️ Sinal não é garantia: respeite o stop.")


def _result_pct(s, outcome, price=None):
    """% of a signal traded as: half sold at TP1 (then stop at entry), half at TP2."""
    e = s["entry"]
    if outcome == "tp2":
        return ((s["tp1"] / e - 1) * 0.5 + (s["tp2"] / e - 1) * 0.5) * 100
    if outcome == "sl":
        return (s["sl"] / e - 1) * 100
    if outcome == "be":                                  # TP1 then back to the entry
        return (s["tp1"] / e - 1) * 0.5 * 100
    p = price if price is not None else e                # expired: rest at the current price
    if s.get("tp1_hit"):
        return ((s["tp1"] / e - 1) * 0.5 + (p / e - 1) * 0.5) * 100
    return (p / e - 1) * 100


def follow_up(prices, send=None, chat=None, now=None):
    """Move each open signal with the current price {mint: usd}: TP1 / TP2 / SL / expiry."""
    now = now or time.time()
    msgs = []
    with _LOCK:
        d = _load()
        for sid, s in list(d["open"].items()):
            p = prices.get(s["mint"])
            outcome = None
            if p is not None:
                s["peak"] = max(float(s.get("peak") or 0), p)
                stop = s["entry"] if s.get("tp1_hit") else s["sl"]
                if p >= s["tp2"]:
                    outcome = "tp2"
                elif p <= stop:
                    outcome = "be" if s.get("tp1_hit") else "sl"
                elif p >= s["tp1"] and not s.get("tp1_hit"):
                    s["tp1_hit"] = True
                    msgs.append("✅ TP1 ATINGIDO — " + s["symbol"] + " " + format((s["tp1"] / s["entry"] - 1) * 100, "+.0f") +
                                "%: venda metade e coloque o stop na entrada (US$ " + _fmt_price(s["entry"]) + ").")
            if outcome is None and now - s["at"] > EXPIRE_S:
                outcome = "expired"
            if outcome:
                res = _result_pct(s, outcome, p)
                d["open"].pop(sid)
                d["history"] = (d["history"] + [{"mint": s["mint"], "symbol": s["symbol"], "outcome": outcome,
                                                 "pct": round(res, 2), "at": s["at"], "closed": now,
                                                 "level": s.get("level")}])[-500:]
                label = {"tp2": "🚀 TP2 ATINGIDO", "sl": "🛑 STOP LOSS", "be": "↩️ VOLTOU À ENTRADA (depois do TP1)",
                         "expired": "⏱️ SINAL ENCERRADO (24 h)"}[outcome]
                msgs.append(label + " — " + s["symbol"] + ": resultado do sinal " + format(res, "+.1f") + "%")
        _save(d)
    if send and chat:
        for m in msgs:
            send(chat, m)
    return msgs


def prices_now(mints, fetch=None):
    fetch = fetch or _get
    out, mints = {}, list(mints)
    for i in range(0, len(mints), 30):
        try:
            pairs = fetch(DEX + "/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        except Exception:
            continue
        for p in pairs if isinstance(pairs, list) else []:
            mint = ((p or {}).get("baseToken") or {}).get("address")
            if mint and p.get("priceUsd") and mint not in out:
                out[mint] = float(p["priceUsd"])
    return out


def cycle(send=None, chat=None, fetch=None, now=None, safety=None, buzz=None):
    """One scan: follow the open signals, then look for new entries."""
    if not enabled():
        return []
    now = now or time.time()
    d = _load()
    if d["open"]:
        follow_up(prices_now({s["mint"] for s in d["open"].values()}, fetch), send, chat, now)
    pairs = candidate_pairs(fetch)
    why = {}
    popular = []
    for p in pairs:
        r = popular_reason(p, now)
        if r:
            why[r] = why.get(r, 0) + 1
        else:
            popular.append(p)
    popular.sort(key=popularity, reverse=True)
    sent = []
    checked = 0
    d = _load()
    for p in popular:
        mint = p["baseToken"]["address"]
        if now - float(d["sent"].get(mint, 0)) < COOLDOWN_S or any(s["mint"] == mint for s in d["open"].values()):
            continue
        if checked >= CANDLE_CHECKS:
            break
        checked += 1
        try:
            setup, r = entry_setup(candles(p["pairAddress"], fetch))
        except Exception as exc:
            setup, r = None, "velas indisponíveis (" + type(exc).__name__ + ")"
        if not setup:
            why[r] = why.get(r, 0) + 1
            continue
        r = (safety or safety_reason)(mint, curve=p.get("dexId") == "pumpfun")
        if r:
            why["anti-golpe: " + r.split(" (")[0]] = why.get("anti-golpe: " + r.split(" (")[0], 0) + 1
            if "indisponível" not in r:
                with _LOCK:
                    dd = _load()
                    dd["sent"][mint] = now          # don't check it again for a while
                    _save(dd)
            continue
        symbol = (p.get("baseToken") or {}).get("symbol") or mint[:6]
        try:
            import twitter_buzz
            tw = (buzz or twitter_buzz.buzz)(symbol, mint)
        except Exception:
            tw = None
        score, parts = strength(p, setup, tw)
        lv = level(score)
        if lv is None:
            why["força abaixo de " + format(MIN_STRENGTH, ".0f") + "%"] = why.get("força abaixo de " + format(MIN_STRENGTH, ".0f") + "%", 0) + 1
            continue
        h1 = (p.get("txns") or {}).get("h1") or {}
        b, s_ = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
        sig = dict(setup, mint=mint, symbol=symbol, score=score, level=lv, buzz=tw,
                   pair=p["pairAddress"], liquidity=float((p.get("liquidity") or {}).get("usd") or 0),
                   buys_1h=b, buy_share=b / max(b + s_, 1), at=now)
        with _LOCK:
            dd = _load()
            dd["open"][mint + ":" + str(int(now))] = sig
            dd["sent"][mint] = now
            dd["sent"] = {m: t for m, t in dd["sent"].items() if now - t < COOLDOWN_S}
            _save(dd)
        sent.append(sig)
        if send and chat:
            send(chat, signal_text(sig))
    LAST.update(at=now, coins=len(pairs), popular=len(popular), checked=checked, signals=len(sent), why=why)
    logging.info("signals scan: %s coins, %s popular, %s candle checks, %s signals", len(pairs), len(popular),
                 checked, len(sent))
    return sent


def results():
    h = _load()["history"]
    if not h:
        return None
    wins = [x for x in h if x["pct"] > 0]
    growth = 1.0
    for x in h:
        growth *= 1 + x["pct"] / 100
    return {"n": len(h), "wins": len(wins), "avg": sum(x["pct"] for x in h) / len(h),
            "growth": (growth - 1) * 100, "tp2": sum(1 for x in h if x["outcome"] == "tp2"),
            "sl": sum(1 for x in h if x["outcome"] == "sl")}


def status_text():
    d = _load()
    lines = ["📡 SINAIS DE MEMECOIN (Solana) — " + ("LIGADO" if d["enabled"] else "DESLIGADO"),
             "Moedas com liquidez US$ " + format(MIN_LIQ / 1000, ".0f") + "–" + format(MAX_LIQ / 1000, ".0f") +
             " mil, a maioria comprando, em tendência de alta. Entrada na correção: fundo (RSI baixo) + vela de virada.",
             "Stop abaixo do fundo (" + format(SL_MIN * 100, ".0f") + "–" + format(SL_MAX * 100, ".0f") + "%) · TP1 " +
             format(TP1_R, "g") + "× o risco (vende metade) · TP2 " + format(TP2_R, "g") + "× o risco."]
    r = results()
    if r:
        lines.append("📊 Resultado dos sinais: " + str(r["n"]) + " encerrados · " + str(r["wins"]) + " no lucro (" +
                     format(r["wins"] / r["n"] * 100, ".0f") + "%) · " + str(r["tp2"]) + " no TP2, " + str(r["sl"]) +
                     " no stop · média " + format(r["avg"], "+.1f") + "% por sinal · seguindo todos: " +
                     format(r["growth"], "+.0f") + "%")
        for lv in (90, 80, 70):
            hl = [x for x in _load()["history"] if x.get("level") == lv]
            if hl:
                w = sum(1 for x in hl if x["pct"] > 0)
                lines.append("   " + LEVEL_ICON[lv] + " nível " + str(lv) + "%: " + str(len(hl)) + " sinais · " +
                             format(w / len(hl) * 100, ".0f") + "% no lucro · média " +
                             format(sum(x["pct"] for x in hl) / len(hl), "+.1f") + "%")
    else:
        lines.append("📊 Resultado dos sinais: ainda nenhum encerrado.")
    try:
        import twitter_buzz
        lines.append("🐦 Twitter: " + ("ligado" + (" (último erro: " + twitter_buzz.STATUS["last_error"] + ")"
                                                 if twitter_buzz.STATUS["last_error"] else "")
                                       if twitter_buzz.configured() else "desligado — coloque X_BEARER_TOKEN (API do X) no Render"))
    except Exception:
        pass
    lines.append("Só mando sinal com força ≥ " + format(MIN_STRENGTH, ".0f") + "% (🟡 70% · 🟢 80% · 🔥 90%).")
    if d["open"]:
        lines.append("Sinais abertos: " + ", ".join(s["symbol"] + (" (TP1 ✅)" if s.get("tp1_hit") else "")
                                                    for s in d["open"].values()))
    if LAST["at"]:
        top = sorted(LAST["why"].items(), key=lambda kv: -kv[1])[:4]
        lines.append("Última busca (há " + str(int(time.time() - LAST["at"])) + " s): " + str(LAST["coins"]) + " moedas, " +
                     str(LAST["popular"]) + " populares em alta, " + str(LAST["checked"]) + " gráficos analisados, " +
                     str(LAST["signals"]) + " sinais")
        if top:
            lines.append("Por que não deram sinal: " + ", ".join(k + " (" + str(v) + ")" for k, v in top))
    return "\n".join(lines)
