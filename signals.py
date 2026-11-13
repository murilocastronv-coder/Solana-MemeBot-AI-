"""📡 Signals bot: Solana memecoin entry signals (does NOT trade; tells the owner when and where).

1. Coins: DexScreener new/boosted Solana pairs + GeckoTerminal trending / pump.fun / PumpSwap pools.
   Liquidity US$ 10k-300k, pair >= 3 h old (enough 5-min candles), SOL/USDC quote.
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
MAX_LIQ = _f("SIGNAL_MAX_LIQUIDITY_USD", 300_000)
MIN_AGE_S = _f("SIGNAL_MIN_AGE_MINUTES", 10) * 60     # new pump.fun coins too (from 10 minutes old)
AUTO_USD = _f("SIGNAL_AUTO_ORDER_USD", 10)            # auto trade: every signal buy is US$ 10 (owner wallet)
AUTO_MAX_OPEN = int(_f("SIGNAL_AUTO_MAX_OPEN", 5))     # ... at most 5 signal positions open at the same time
AUTO_RESERVE_USD = _f("SIGNAL_AUTO_RESERVE_USD", 1)   # left for network fees
# pump.fun only: coins still on the launch curve (dexId pumpfun) or already on PumpSwap
PUMP_ONLY = os.getenv("SIGNAL_PUMP_ONLY", "true").strip().lower() != "false"
PUMP_DEXES = {"pumpfun", "pumpswap"}
FAST_CANDLES_UNDER_S = _f("SIGNAL_FAST_CANDLES_UNDER_HOURS", 8) * 3600   # younger coins: 1-min candles
MIN_BUYS_1H = int(_f("SIGNAL_MIN_BUYS_1H", 30))
MIN_BUY_SHARE = _f("SIGNAL_MIN_BUY_SHARE", 0.50)
MIN_VOL_24H = _f("SIGNAL_MIN_VOLUME_24H", 10_000)
PULLBACK_MIN = _f("SIGNAL_PULLBACK_MIN", 0.06)
PULLBACK_MAX = _f("SIGNAL_PULLBACK_MAX", 0.40)
RSI_DIP = _f("SIGNAL_RSI_DIP", 45)
SL_MIN = _f("SIGNAL_SL_MIN", 0.10)     # fair stop: never tighter than 10% (memecoins wick and come back)
SL_MAX = _f("SIGNAL_SL_MAX", 0.30)
SL_ATR_MULT = _f("SIGNAL_SL_ATR", 1.5)  # stop = bottom of the dip minus 1.5x the normal candle range (ATR)
SL_CONFIRM_S = _f("SIGNAL_SL_CONFIRM_SECONDS", 90)   # price must stay under the stop this long (no wick stops)
SL_CRASH = _f("SIGNAL_SL_CRASH", 0.10)               # ... unless it is 10% under it: then at once
TP1_R = _f("SIGNAL_TP1_R", 1.5)
TP2_R = _f("SIGNAL_TP2_R", 3.0)
EXPIRE_S = _f("SIGNAL_EXPIRE_HOURS", 24) * 3600
EXPIRE_RUNNER_S = _f("SIGNAL_EXPIRE_RUNNER_HOURS", 72) * 3600   # after TP2 the runner has more time
POTENTIAL_LABEL = {1: "1x (+100%)", 2: "2x (+200%)", 3: "3x ou mais (+300%)"}
COOLDOWN_S = _f("SIGNAL_COOLDOWN_HOURS", 6) * 3600
SCAN_EVERY_S = _f("SIGNAL_SCAN_SECONDS", 120)
CANDLE_CHECKS = int(_f("SIGNAL_CANDLE_CHECKS", 12))     # coins whose candles are read per scan (API limits)

DEX = "https://api.dexscreener.com"
DEX_FEEDS = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1",
             "/community-takeovers/latest/v1", "/ads/latest/v1")
# DexScreener search (pairs): GeckoTerminal and pump.fun block the server, DexScreener does not
DEX_SEARCHES = ("pumpswap", "pumpfun", "pump", "pump sol", "solana meme", "sol pump", "pumpswap sol", "pump.fun",
                "raydium", "meteora", "launchlab", "solana")
GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_FEEDS = tuple("/networks/solana/trending_pools?page=" + str(n) for n in (1, 2, 3)) + tuple(
    "/networks/solana/new_pools?page=" + str(n) for n in (1, 2)) + tuple(
    "/networks/solana/dexes/" + dex + "/pools?page=" + str(page) + "&sort=h24_tx_count_desc"
    for dex in ("pump-fun", "pumpswap") for page in (1, 2, 3))
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
MIN_LP_LOCKED = _f("SIGNAL_MIN_LP_LOCKED", 80)
BAD_EXTENSIONS = {"transferHook": "token com gatilho de transferência (pode travar a venda)",
                  "permanentDelegate": "criador pode tirar as moedas da sua carteira",
                  "nonTransferable": "token não pode ser transferido",
                  "defaultAccountState": "contas novas nascem congeladas"}

_LOCK = threading.RLock()
_http = requests.Session()
LAST = {"at": 0, "coins": 0, "popular": 0, "checked": 0, "signals": 0, "why": {}}
PAIRS = {"at": 0.0, "pairs": []}
_OFF_LOG = [0.0]


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("signals.json")


def _default():
    return {"enabled": False, "auto": True, "open": {}, "history": [], "sent": {}}


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


def set_auto(on):
    """Auto trade: buy every signal with real money in the owner's wallet and sell at its stop / targets."""
    with _LOCK:
        d = _load()
        d["auto"] = bool(on)
        if on:
            d["enabled"] = True
        _save(d)


def auto_enabled():
    d = _load()
    return bool(d["enabled"] and d.get("auto", True))


# ---------------------------------------------------------------- data
GECKO_GAP_S = _f("SIGNAL_GECKO_GAP_SECONDS", 2.2)   # GeckoTerminal free API: ~30 calls/min, so space them
GECKO_PAUSE_S = _f("SIGNAL_GECKO_PAUSE_SECONDS", 90)  # after a 429 skip GeckoTerminal for a while
_gecko_last = [0.0]
_gecko_block = [0.0]


class GeckoBusy(RuntimeError):
    pass


def _get(url):
    gecko = url.startswith(GECKO)
    if gecko:
        if time.time() < _gecko_block[0]:
            raise GeckoBusy("GeckoTerminal limit")
        wait = _gecko_last[0] + GECKO_GAP_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _gecko_last[0] = time.time()
    r = _http.get(url, timeout=12, headers={"Accept": "application/json", "User-Agent": "SolanaSignals/1.0"})
    if gecko and r.status_code == 429:                       # rate limited: don't insist, use DexScreener data
        _gecko_block[0] = time.time() + GECKO_PAUSE_S
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
    for q in DEX_SEARCHES:
        try:
            rows = fetch(DEX + "/latest/dex/search?q=" + q.replace(" ", "%20"))
        except Exception:
            continue
        for p in (rows or {}).get("pairs") or [] if isinstance(rows, dict) else []:
            mint = ((p or {}).get("baseToken") or {}).get("address")
            if p.get("chainId") == "solana" and mint:
                mints.append(mint)
    for feed in GECKO_FEEDS:
        if fetch is _get and time.time() < _gecko_block[0]:
            break
        try:
            rows = fetch(GECKO + feed)
        except Exception:
            continue
        for pool in (rows or {}).get("data", []) if isinstance(rows, dict) else []:
            base = ((((pool or {}).get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if base.startswith("solana_"):
                mints.append(base[len("solana_"):])
    mints = [m for m in dict.fromkeys(mints) if m not in QUOTES][:360]
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
    if PUMP_ONLY and not is_pump(p):
        return "não é da pump.fun"
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
    h1c, h6c, h24c = (float(ch.get(k) or 0) for k in ("h1", "h6", "h24"))
    if max(h6c, h24c) <= 0 or h1c < -30:            # in an uptrend (6 h or 24 h) and not crashing now
        return "fora de tendência de alta"
    return ""


def popularity(p):
    h1 = (p.get("txns") or {}).get("h1") or {}
    b, s = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    return b * (b / max(b + s, 1))


def is_pump(p):
    """A pump.fun coin: on the launch curve, on PumpSwap, or a pump.fun mint (address ending in 'pump')."""
    mint = ((p.get("baseToken") or {}).get("address")) or ""
    return p.get("dexId") in PUMP_DEXES or mint.endswith("pump")


def candle_minutes(p, now=None):
    """1-minute candles for young coins (pump.fun moves fast), 5-minute for the older ones."""
    created = p.get("pairCreatedAt")
    age = (now or time.time()) - float(created) / 1000 if created else 1e9
    return 1 if age < FAST_CANDLES_UNDER_S else 5


def _ohlcv_url(pool, minutes):
    return (GECKO + "/networks/solana/pools/" + pool + "/ohlcv/minute?aggregate=" + str(int(minutes)) +
            "&limit=100&currency=usd")


def gecko_pool(mint, fetch=None):
    """The main GeckoTerminal pool of a coin (used when GeckoTerminal does not know the DexScreener pair)."""
    data = (fetch or _get)(GECKO + "/networks/solana/tokens/" + mint + "/pools?page=1")
    for pool in (data or {}).get("data") or []:
        addr = ((pool or {}).get("attributes") or {}).get("address")
        if addr:
            return addr
    return None


def candles(pool, fetch=None, minutes=5, mint=None):
    """Candles [(ts, open, high, low, close, volume)] oldest first (GeckoTerminal), 1 or 5 minutes."""
    fetch = fetch or _get
    try:
        data = fetch(_ohlcv_url(pool, minutes))
    except requests.HTTPError as exc:
        code = getattr(exc.response, "status_code", None)
        alt = gecko_pool(mint, fetch) if mint and code in (400, 404) else None
        if not alt or alt == pool:
            raise
        data = fetch(_ohlcv_url(alt, minutes))
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
    if len(cs) < 10:
        return None, "poucas velas"
    short = len(cs) < 30                           # brand-new coin (10-30 min): faster averages and RSI
    closes = [c[4] for c in cs]
    highs, lows = [c[2] for c in cs], [c[3] for c in cs]
    if short:
        # new coin: it already pumped (+20% from its first candle) and the dip holds above where it started
        if max(highs) < closes[0] * 1.2 or min(lows[-6:]) <= closes[0]:
            return None, "moeda nova sem força (não subiu desde o lançamento)"
    else:
        e20, e50 = ema(closes, 20), ema(closes, 50)
        if e20[-1] < e50[-1] * 0.98:             # uptrend (a small dip under the EMA50 is the correction)
            return None, "tendência de 5 min não é de alta"
    top = max(highs[-24:])                       # top of the last 2 hours
    bottom = min(lows[-6:])                      # bottom of the last 30 minutes
    pull = (top - bottom) / top if top else 0
    if pull < PULLBACK_MIN:
        return None, "sem correção (perto do topo)"
    if pull > PULLBACK_MAX:
        return None, "caiu demais (quebrou a tendência)"
    r = rsi(closes, 5 if short else 14)
    if min(r[-6:]) > RSI_DIP:
        return None, "não chegou no fundo (RSI)"
    if r[-1] <= r[-2]:
        return None, "ainda caindo (RSI)"
    last, prev = cs[-1], cs[-2]
    if not (last[4] > last[1] and last[4] > prev[4]):   # green candle closing above the previous one
        return None, "sem vela de confirmação"
    entry = last[4]
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(len(cs) - 14, len(cs))]
    atr = sum(trs) / len(trs)
    # fair stop: below the bottom of the dip, with room for the coin's normal swings (ATR)
    sl = min(bottom * 0.97, bottom - SL_ATR_MULT * atr)
    risk = (entry - sl) / entry
    if risk < SL_MIN:
        sl, risk = entry * (1 - SL_MIN), SL_MIN
    if risk > SL_MAX:
        return None, "stop ficaria longe demais"
    vols = [c[5] for c in cs]
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) > 21 else 0
    return {"entry": entry, "sl": sl, "tp1": entry * (1 + TP1_R * risk), "tp2": entry * (1 + TP2_R * risk),
            "risk": risk, "atr_pct": atr / entry, "pullback": pull, "rsi": r[-1], "rsi_min": min(r[-6:]), "top": top,
            "vol_ratio": (vols[-1] / avg_vol) if avg_vol else 1.0}, ""


DEX_PULLBACK_MIN = _f("SIGNAL_DEX_PULLBACK_MIN", 0.04)   # without a chart: 4%+ down in the last hour


def dex_setup(p):
    """Entry from DexScreener price changes when the candles can't be read: the coin is in an uptrend
    (6 h / 24 h), corrected in the last hour and is turning up in the last 5 minutes with buyers."""
    ch = p.get("priceChange") or {}
    m5, h1 = float(ch.get("m5") or 0), float(ch.get("h1") or 0)
    entry = float(p.get("priceUsd") or 0)
    if entry <= 0:
        return None, "sem preço"
    if h1 > -DEX_PULLBACK_MIN * 100:
        return None, "sem correção (perto do topo)"
    if h1 < -PULLBACK_MAX * 100:
        return None, "caiu demais (quebrou a tendência)"
    t5 = (p.get("txns") or {}).get("m5") or {}
    b5, s5 = int(t5.get("buys") or 0), int(t5.get("sells") or 0)
    if m5 <= 0 or b5 < s5:
        return None, "ainda caindo (5 min)"
    pull = -h1 / 100
    risk = max(SL_MIN, min(SL_MAX, pull * 0.8))
    vol = p.get("volume") or {}
    v5, vh = float(vol.get("m5") or 0), float(vol.get("h1") or 0)
    return {"entry": entry, "sl": entry * (1 - risk), "tp1": entry * (1 + TP1_R * risk), "tp2": entry * (1 + TP2_R * risk),
            "risk": risk, "atr_pct": risk / SL_ATR_MULT, "pullback": pull, "rsi": 50.0, "rsi_min": RSI_DIP - 3,
            "top": entry / (1 - pull), "vol_ratio": (v5 * 12 / vh) if vh else 1.0, "source": "dexscreener"}, ""


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
    score = min(97.0, 50.0 + sum(parts.values()))
    return round(score), parts


def level(score):
    """90 / 80 / 70 or None (below the minimum: no signal)."""
    for lv in (90, 80, 70):
        if score >= lv and score >= MIN_STRENGTH:
            return lv
    return None


LEVEL_ICON = {90: "🔥", 80: "🟢", 70: "🟡"}


def potential(p, score, buzz=None):
    """1, 2 or 3: estimated room to make 1x (+100%), 2x (+200%) or 3x+ (+300%) on the capital.
    Smaller market cap = more room; high volume vs market cap, growing Twitter and a strong signal
    add room; a coin that already went up a lot today has less."""
    mc = float(p.get("marketCap") or p.get("fdv") or 0)
    vol = float((p.get("volume") or {}).get("h24") or 0)
    h24 = float((p.get("priceChange") or {}).get("h24") or 0)
    tier = 3 if mc and mc < 300_000 else (2 if mc and mc < 1_000_000 else 1)
    if mc and vol / mc >= 1:
        tier += 1
    if buzz and buzz.get("growth", 0) >= 2:
        tier += 1
    if score >= 90:
        tier += 1
    if h24 >= 300:
        tier -= 1
    return max(1, min(3, tier))


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
            "🚀 TAKE PROFIT 2: US$ " + _fmt_price(s["tp2"]) + " (" + pct(s["tp2"]) + ") — vender 1/4, stop vai para o TP1\n"
            "🌙 TAKE PROFIT 3 (potencial " + POTENTIAL_LABEL.get(s.get("potential"), "?") + "): US$ " +
            _fmt_price(s.get("tp3", s["tp2"])) + " (" + pct(s.get("tp3", s["tp2"])) + ") — vender o último 1/4\n"
            "Stop justo: abaixo do fundo com folga para o sobe-e-desce normal da moeda; só vale se o preço " +
            "ficar abaixo dele (pavio rápido não conta).\n\n"
            "Liquidez US$ " + format(s["liquidity"] / 1000, ".0f") + " mil · 🛡️ anti-golpe OK\n" + s["mint"] +
            "\ndexscreener.com/solana/" + s["pair"] + "\n⚠️ Sinal não é garantia: respeite o stop.")


def _result_pct(s, outcome, price=None):
    """% of a signal traded as: 1/2 sold at TP1 (stop -> entry), 1/4 at TP2 (stop -> TP1), 1/4 at TP3."""
    e = s["entry"]
    g = lambda x: x / e - 1
    tp3 = s.get("tp3", s["tp2"])
    if outcome == "sl":
        return g(s["sl"]) * 100
    if outcome == "be":                                  # TP1, then back to the entry
        return g(s["tp1"]) * 0.5 * 100
    if outcome == "tp2_back":                            # TP1 + TP2, then back to TP1
        return (g(s["tp1"]) * 0.75 + g(s["tp2"]) * 0.25) * 100
    if outcome in ("tp3", "tp2"):
        return (g(s["tp1"]) * 0.5 + g(s["tp2"]) * 0.25 + g(tp3) * 0.25) * 100
    p = price if price is not None else e                # expired: the rest at the current price
    if s.get("tp2_hit"):
        return (g(s["tp1"]) * 0.5 + g(s["tp2"]) * 0.25 + g(p) * 0.25) * 100
    if s.get("tp1_hit"):
        return (g(s["tp1"]) * 0.5 + g(p) * 0.5) * 100
    return g(p) * 100


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
                stop = s["tp1"] if s.get("tp2_hit") else (s["entry"] if s.get("tp1_hit") else s["sl"])
                tp3 = s.get("tp3", s["tp2"])
                if p >= tp3:
                    outcome = "tp3"
                elif p <= stop:
                    # fair stop: a quick wick is not enough, the price must stay under it (or crash through it)
                    if p <= stop * (1 - SL_CRASH) or (s.get("below_since") and now - s["below_since"] >= SL_CONFIRM_S):
                        outcome = "tp2_back" if s.get("tp2_hit") else ("be" if s.get("tp1_hit") else "sl")
                    else:
                        s.setdefault("below_since", now)
                else:
                    s.pop("below_since", None)
                    if p >= s["tp2"] and not s.get("tp2_hit"):
                        if s.get("traded"):
                            SELLS.append((s["mint"], 0.5 if s.get("tp1_hit") else 0.75, "signal_tp2"))
                        s["tp1_hit"] = s["tp2_hit"] = True
                        msgs.append("🚀 TP2 ATINGIDO — " + s["symbol"] + " " + format((s["tp2"] / s["entry"] - 1) * 100, "+.0f") +
                                    "%: venda 1/4 e suba o stop para o TP1 (US$ " + _fmt_price(s["tp1"]) + "). O último 1/4 vai " +
                                    "atrás do potencial (TP3 US$ " + _fmt_price(tp3) + ").")
                    elif p >= s["tp1"] and not s.get("tp1_hit"):
                        if s.get("traded"):
                            SELLS.append((s["mint"], 0.5, "signal_tp1"))
                        s["tp1_hit"] = True
                        msgs.append("✅ TP1 ATINGIDO — " + s["symbol"] + " " + format((s["tp1"] / s["entry"] - 1) * 100, "+.0f") +
                                    "%: venda metade e coloque o stop na entrada (US$ " + _fmt_price(s["entry"]) + ").")
            if outcome is None and now - s["at"] > (EXPIRE_RUNNER_S if s.get("tp2_hit") else EXPIRE_S):
                outcome = "expired"
            if outcome:
                if s.get("traded"):
                    SELLS.append((s["mint"], None, "signal_" + outcome))
                res = _result_pct(s, outcome, p)
                d["open"].pop(sid)
                d["history"] = (d["history"] + [{"mint": s["mint"], "symbol": s["symbol"], "outcome": outcome,
                                                 "pct": round(res, 2), "at": s["at"], "closed": now,
                                                 "level": s.get("level"), "potential": s.get("potential"),
                                                 "peak_x": round(float(s.get("peak") or s["entry"]) / s["entry"], 3)}])[-500:]
                label = {"tp3": "🌙 TP3 ATINGIDO (potencial)", "tp2": "🚀 TP2 ATINGIDO", "sl": "🛑 STOP LOSS",
                         "be": "↩️ VOLTOU À ENTRADA (depois do TP1)", "tp2_back": "↩️ VOLTOU AO TP1 (depois do TP2)",
                         "expired": "⏱️ SINAL ENCERRADO (tempo)"}[outcome]
                msgs.append(label + " — " + s["symbol"] + ": resultado do sinal " + format(res, "+.1f") + "%")
        _save(d)
    if send and chat:
        for m in msgs:
            send(chat, m)
    return msgs


SELLS = []   # (mint, fraction or None = all, reason) for auto-traded signals, run by run_sells


def run_sells(send=None, chat=None):
    """Sell the auto-traded positions whose signal hit a target, the stop or the time limit."""
    import live_engine
    while SELLS:
        mint, fraction, reason = SELLS.pop(0)
        try:
            if fraction is None:
                live_engine.sell_now(mint, reason, send, chat)
            else:
                live_engine.sell_part(mint, fraction, reason, send, chat)
        except Exception as exc:
            logging.warning("signal sell %s failed: %s", mint[:8], type(exc).__name__)
            if send and chat:
                send(chat, "⛔ Sinal automático: não consegui vender " + mint[:6] + " (" + type(exc).__name__ +
                     "). O stop de segurança continua protegendo a posição.")


def auto_buy(sig, send=None, chat=None):
    """Buy a signal with real money (US$ AUTO_USD) in the owner's wallet. '' when bought, else why not."""
    import live_engine
    import live_trade_state as st
    mint, symbol = sig["mint"], sig["symbol"]
    if not live_engine.ready():
        return "robô real não está pronto: " + str(live_engine.why_stopped() or "")
    for _ in range(15):                          # another order (copy trade) still settling: wait a bit
        if not st.pending_orders():
            break
        time.sleep(2)
    allowed, why = st.can_open()
    if not allowed:
        return why
    positions = st._load()["positions"]
    if mint in positions:
        return "já tem a moeda na carteira"
    if st.positions_by_mode("signal") >= AUTO_MAX_OPEN:
        return "já tem " + str(AUTO_MAX_OPEN) + " operações de sinal abertas"
    if len(positions) >= st.MAX_POSITIONS:
        return "limite de posições da carteira"
    import copy_trader as ct
    free = ct.wallet_usd(live_engine.get_address(), max_age=0)
    if free is None:
        return "saldo indisponível"
    if free < AUTO_USD + AUTO_RESERVE_USD:
        return "saldo livre US$ " + format(free, ".2f") + " (precisa US$ " + format(AUTO_USD + AUTO_RESERVE_USD, ".0f") + ")"
    settled = live_engine._execute_buy({"baseToken": {"address": mint, "symbol": symbol}}, mint, symbol, send, chat,
                                       live_engine.get_address(), None, "signal", usd=AUTO_USD)
    if not settled:
        return "compra não confirmada"
    if settled.get("status") == "settled_buy":
        st.set_position_fields(mint, sig_risk=round(float(sig["risk"]), 4))
    return ""


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


def cycle(send=None, chat=None, fetch=None, now=None, safety=None, buzz=None, trader=None):
    """One scan: follow the open signals, then look for new entries (and buy them when auto trade is on)."""
    trader = trader if trader is not None else (auto_buy if fetch is None else None)
    if not enabled():
        if time.time() - _OFF_LOG[0] > 600:
            _OFF_LOG[0] = time.time()
            logging.info("signals scan: OFF (botão Ligar sinais)")
        return []
    now = now or time.time()
    t0 = time.time()
    d = _load()
    if d["open"]:
        follow_up(prices_now({s["mint"] for s in d["open"].values()}, fetch), send, chat, now)
        if SELLS:
            run_sells(send, chat)
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
        try:
            if checked >= CANDLE_CHECKS or (fetch is None and time.time() < _gecko_block[0]):
                raise GeckoBusy("sem gráfico nesta rodada")        # API limit: decide with DexScreener data
            checked += 1
            setup, r = entry_setup(candles(p["pairAddress"], fetch, candle_minutes(p, now), mint=mint))
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            key = "gráfico indisponível (" + type(exc).__name__ + (" " + str(code) if code else "") + ")"
            why[key] = why.get(key, 0) + 1
            setup, r = dex_setup(p)                 # no candles: use DexScreener's 5 min / 1 h moves
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
        pot = potential(p, score, tw)
        tp3 = max(setup["entry"] * (1 + pot), setup["tp2"] * 1.5)
        sig = dict(setup, mint=mint, symbol=symbol, score=score, level=lv, buzz=tw, potential=pot, tp3=tp3,
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
        if trader and auto_enabled():
            try:
                why_not = trader(sig, send, chat)
            except Exception as exc:
                why_not = type(exc).__name__
            if why_not:
                if send and chat:
                    send(chat, "🤖 Sinal automático não comprou " + symbol + ": " + why_not)
            else:
                with _LOCK:
                    dd = _load()
                    for k, v in dd["open"].items():
                        if v["mint"] == mint:
                            v["traded"] = True
                    _save(dd)
    LAST.update(at=now, coins=len(pairs), popular=len(popular), checked=checked, signals=len(sent), why=why)
    if fetch is None:
        PAIRS.update(at=time.time(), pairs=pairs)      # the Dip Buying Bot reuses this scan
    top = ", ".join(k + "=" + str(v) for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:6])
    logging.info("signals scan: %s coins, %s popular, %s candle checks, %s signals (%.0fs) | %s", len(pairs),
                 len(popular), checked, len(sent), time.time() - t0, top)
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
            "growth": (growth - 1) * 100, "tp2": sum(1 for x in h if x["outcome"] in ("tp2", "tp3", "tp2_back")),
            "sl": sum(1 for x in h if x["outcome"] == "sl")}


def status_text():
    d = _load()
    lines = ["📡 SINAIS DA PUMP.FUN (Solana) — " + ("LIGADO" if d["enabled"] else "DESLIGADO") if PUMP_ONLY else
             "📡 SINAIS DE MEMECOIN (Solana) — " + ("LIGADO" if d["enabled"] else "DESLIGADO"),
             "Moedas com liquidez US$ " + format(MIN_LIQ / 1000, ".0f") + "–" + format(MAX_LIQ / 1000, ".0f") +
             " mil, a maioria comprando, em tendência de alta. Entrada na correção: fundo (RSI baixo) + vela de virada.",
             "Stop justo abaixo do fundo com folga do sobe-e-desce (" + format(SL_MIN * 100, ".0f") + "–" +
             format(SL_MAX * 100, ".0f") + "%, só vale se o preço ficar abaixo) · TP1 " + format(TP1_R, "g") +
             "× o risco (1/2) · TP2 " + format(TP2_R, "g") + "× (1/4) · TP3 = potencial 1x/2x/3x+ (1/4)."]
    lines.append(("🤖 OPERANDO SOZINHO (dinheiro real): US$ " + format(AUTO_USD, ".0f") + " por sinal, máx. " +
                  str(AUTO_MAX_OPEN) + " abertas, vende nos alvos e no stop do sinal") if d.get("auto", True) else
                 "✋ Só avisa (não compra sozinho)")
    lines.append("🆕 Moedas a partir de " + format(MIN_AGE_S / 60, ".0f") + " minutos de vida")
    r = results()
    if r:
        lines.append("📊 Resultado dos sinais: " + str(r["n"]) + " encerrados · " + str(r["wins"]) + " no lucro (" +
                     format(r["wins"] / r["n"] * 100, ".0f") + "%) · " + str(r["tp2"]) + " no TP2, " + str(r["sl"]) +
                     " no stop · média " + format(r["avg"], "+.1f") + "% por sinal · seguindo todos: " +
                     format(r["growth"], "+.0f") + "%")
        for pot in (3, 2, 1):
            hp = [x for x in _load()["history"] if x.get("potential") == pot]
            if hp:
                hit = sum(1 for x in hp if x.get("peak_x", 0) >= 1 + pot)
                lines.append("   🌙 potencial " + POTENTIAL_LABEL[pot] + ": " + str(len(hp)) + " sinais · " + str(hit) +
                             " bateram o potencial · maior alta " + format((max(x.get("peak_x", 1) for x in hp) - 1) * 100, "+.0f") + "%")
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
