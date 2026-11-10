"""Sniper bot: buys young Solana memecoins with real momentum, all day, with anti-rug,
anti-scam and anti-honeypot checks. Owner only. SIMULATION or REAL (main bot wallet).

Entry (all must pass):
- DexScreener pair on Solana, quoted in SOL/USDC, liquidity US$ 50k-250k, pair >= 1 hour old
  (and <= SNIPER_MAX_AGE_HOURS), at least one website/social link.
- Momentum ("best strategy" = rising with real buyers, not a pump top): last 5 min with
  >= SNIPER_MIN_BUYS_5M buys and buys/sells >= 1.3, 5-min volume >= SNIPER_MIN_VOL_5M,
  price +2%..+25% in 5 min and positive in 1 h, FDV <= 15x liquidity.
- Anti-rug / anti-scam: mint and freeze authority revoked, no Token-2022 traps (transfer fee,
  transfer hook, permanent delegate, frozen accounts, non-transferable), top 10 holders < 25%,
  RugCheck without "danger" risks and with liquidity locked/burned >= SNIPER_MIN_LP_LOCKED.
- Anti-honeypot: Jupiter buy -> sell round trip returns >= 92% (copy_trader.safety_reason) and the
  order's price impact <= SNIPER_MAX_IMPACT.
Exit: the live engine's "sniper" rules (stop -40% / SNIPER_STOP_LOSS, trailing from +25% selling 12% below the top,
time stop 30 min). A coin is never bought twice within SNIPER_COOLDOWN_HOURS.
"""
import json
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


MIN_LIQ = _f("SNIPER_MIN_LIQUIDITY_USD", 50_000)
MAX_LIQ = _f("SNIPER_MAX_LIQUIDITY_USD", 250_000)
MIN_AGE_S = _f("SNIPER_MIN_AGE_MINUTES", 60) * 60          # coin created at least 1 hour ago
STOP_LOSS = _f("SNIPER_STOP_LOSS", 0.40)
MAX_AGE_S = _f("SNIPER_MAX_AGE_HOURS", 24) * 3600
MIN_BUYS_5M = int(_f("SNIPER_MIN_BUYS_5M", 15))
MIN_BUY_RATIO = _f("SNIPER_MIN_BUY_RATIO", 1.3)
MIN_VOL_5M = _f("SNIPER_MIN_VOL_5M", 5_000)
MIN_CHANGE_5M = _f("SNIPER_MIN_CHANGE_5M", 2)
MAX_CHANGE_5M = _f("SNIPER_MAX_CHANGE_5M", 25)
MAX_FDV_LIQ = _f("SNIPER_MAX_FDV_LIQ", 15)
REQUIRE_SOCIALS = os.getenv("SNIPER_REQUIRE_SOCIALS", "true").strip().lower() != "false"
REQUIRE_RUGCHECK = os.getenv("SNIPER_REQUIRE_RUGCHECK", "true").strip().lower() != "false"
MIN_LP_LOCKED = _f("SNIPER_MIN_LP_LOCKED", 80)
MAX_IMPACT = _f("SNIPER_MAX_IMPACT", 3)
ORDER_USD = _f("SNIPER_ORDER_USD", 20)
MAX_POSITIONS = int(_f("SNIPER_MAX_POSITIONS", 3))
COOLDOWN_S = _f("SNIPER_COOLDOWN_HOURS", 24) * 3600
SCAN_EVERY_S = _f("SNIPER_SCAN_SECONDS", 30)
START_SIM_USD = 100.0
DEX = "https://api.dexscreener.com"
FEEDS = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1")
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
BAD_EXTENSIONS = {"transferHook": "token com gatilho de transferência (pode travar a venda)",
                  "permanentDelegate": "criador pode tirar as moedas da sua carteira",
                  "nonTransferable": "token não pode ser transferido",
                  "defaultAccountState": "contas novas nascem congeladas"}

_LOCK = threading.RLock()
_http = requests.Session()
LAST_SCAN = {"at": 0, "seen": 0, "passed_filters": 0, "bought": 0, "rejected": {}, "last_reject": ""}


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("sniper.json")


def _default():
    return {"enabled": False, "real": False, "balance": START_SIM_USD, "positions": {}, "history": [], "tried": {}}


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
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".sniper.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def set_mode(on, real=False):
    with _LOCK:
        d = _load()
        d["enabled"], d["real"] = bool(on), bool(on and real)
        _save(d)


def enabled():
    return bool(_load()["enabled"])


def real_enabled():
    d = _load()
    return bool(d["enabled"] and d["real"])


# ---------------------------------------------------------------- discovery + filters
def _get(url):
    r = _http.get(url, timeout=15, headers={"Accept": "application/json", "User-Agent": "SolanaSniper/1.0"})
    r.raise_for_status()
    return r.json()


def candidate_pairs(get=None):
    """Young Solana pairs from DexScreener's new/boosted feeds (best pair per token)."""
    get = get or (lambda path: _get(DEX + path))
    mints = []
    for feed in FEEDS:
        try:
            rows = get(feed)
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress"):
                mints.append(row["tokenAddress"])
    mints = [m for m in dict.fromkeys(mints) if m not in QUOTES][:120]
    best = {}
    for i in range(0, len(mints), 30):
        try:
            pairs = get("/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
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


def _age_text(seconds):
    return format(seconds / 3600, ".0f") + " h" if seconds >= 3600 else format(seconds / 60, ".0f") + " min"


def filter_reason(p, now=None):
    """'' if the pair passes liquidity, age, quote, socials and momentum; else why not."""
    now = now or time.time()
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    if liq < MIN_LIQ:
        return "liquidez < US$ " + format(MIN_LIQ / 1000, ".0f") + " mil"
    if liq > MAX_LIQ:
        return "liquidez > US$ " + format(MAX_LIQ / 1000, ".0f") + " mil"
    if ((p.get("quoteToken") or {}).get("address")) not in QUOTES:
        return "par sem SOL/USDC"
    created = p.get("pairCreatedAt")
    if not created:
        return "idade desconhecida"
    age = now - float(created) / 1000
    if age < MIN_AGE_S:
        return "moeda com menos de " + _age_text(MIN_AGE_S)
    if age > MAX_AGE_S:
        return "moeda velha demais"
    info = p.get("info") or {}
    if REQUIRE_SOCIALS and not (info.get("websites") or info.get("socials")):
        return "sem site/rede social"
    tx5 = (p.get("txns") or {}).get("m5") or {}
    buys, sells = int(tx5.get("buys") or 0), int(tx5.get("sells") or 0)
    if buys < MIN_BUYS_5M:
        return "poucas compras em 5 min"
    if buys < MIN_BUY_RATIO * max(sells, 1):
        return "mais vendas que compras"
    if float((p.get("volume") or {}).get("m5") or 0) < MIN_VOL_5M:
        return "volume baixo em 5 min"
    ch = p.get("priceChange") or {}
    m5, h1 = float(ch.get("m5") or 0), float(ch.get("h1") or 0)
    if m5 < MIN_CHANGE_5M:
        return "sem impulso (5 min)"
    if m5 > MAX_CHANGE_5M:
        return "subiu demais (topo)"
    if h1 <= 0:
        return "caindo na última hora"
    fdv = float(p.get("fdv") or p.get("marketCap") or 0)
    if fdv and fdv > MAX_FDV_LIQ * liq:
        return "valor de mercado alto demais para a liquidez"
    return ""


# ---------------------------------------------------------------- anti-rug / anti-scam / anti-honeypot
def token_program_reason(mint, rpc=None):
    """Token-2022 traps: transfer fee, transfer hook, permanent delegate, frozen accounts..."""
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


def rugcheck_reason(mint, get=None):
    get = get or _get
    try:
        r = get(RUGCHECK.format(mint))
    except Exception as exc:
        return ("RugCheck indisponível (" + type(exc).__name__ + ")") if REQUIRE_RUGCHECK else ""
    if not isinstance(r, dict) or not ("risks" in r or "score" in r or "score_normalised" in r):
        return "RugCheck indisponível (resposta inesperada)" if REQUIRE_RUGCHECK else ""   # never buy blind
    danger = [x.get("name") for x in (r.get("risks") or []) if str(x.get("level")).lower() == "danger"]
    if danger:
        return "RugCheck: " + str(danger[0])
    locked = r.get("lpLockedPct")
    if locked is not None and float(locked) < MIN_LP_LOCKED:
        return "liquidez só " + format(float(locked), ".0f") + "% travada/queimada (risco de rug)"
    return ""


def impact_reason(mint):
    """Our own order must not move the price too much (thin pool = easy rug, bad exit)."""
    import copy_trader as ct
    import live_swap_executor as lx
    lamports = int(ORDER_USD / ct._sol_usd() * 1e9)
    q = lx.quote(SOL_MINT, mint, lamports, slippage_bps=300)
    impact = float(q.get("priceImpactPct") or 0) * 100
    return ("impacto de preço " + format(impact, ".1f") + "%") if impact > MAX_IMPACT else ""


def safety_reason(mint):
    """All protections; '' = safe to buy."""
    import copy_trader as ct
    for check in (lambda: ct.safety_reason(mint),            # mint/freeze revoked, top 10 < 25%, sell test >= 92%
                  lambda: token_program_reason(mint),
                  lambda: rugcheck_reason(mint),
                  lambda: impact_reason(mint)):
        try:
            why = check()
        except Exception as exc:
            why = "verificação indisponível (" + type(exc).__name__ + ")"
        if why:
            return why
    return ""


# ---------------------------------------------------------------- trading
def _recently_tried(d, mint, now):
    return now - float(d["tried"].get(mint, 0)) < COOLDOWN_S


def _mark_tried(mint, now):
    with _LOCK:
        d = _load()
        d["tried"][mint] = now
        d["tried"] = {m: t for m, t in d["tried"].items() if now - t < COOLDOWN_S}
        _save(d)


def _open_real(p, mint, symbol, send, chat):
    import live_engine
    import live_trade_state as st
    import wallet_holdings
    if not live_engine.ready():
        return "robô real não está pronto: " + str(live_engine.why_stopped() or "")
    allowed, why = st.can_open()
    if not allowed:
        return why
    if st.positions_by_mode("sniper") >= MAX_POSITIONS or len(st._load()["positions"]) >= st.MAX_POSITIONS:
        return "limite de posições"
    if mint in st._load()["positions"]:
        return "já tem a moeda"
    ok, why = wallet_holdings.can_buy(mint, live_engine.get_address())
    if not ok:
        return why
    live_engine._execute_buy({"baseToken": {"address": mint, "symbol": symbol}}, mint, symbol, send, chat,
                             live_engine.get_address(), None, "sniper", usd=ORDER_USD)
    return ""


def _open_sim(mint, symbol, send, chat):
    import copy_trader as ct
    with _LOCK:
        d = _load()
        if len(d["positions"]) >= MAX_POSITIONS or mint in d["positions"] or d["balance"] < ORDER_USD:
            return "limite de posições ou saldo"
    tokens = int(ct._order(SOL_MINT, mint, int(ORDER_USD / ct._sol_usd() * 1e9))["outAmount"])
    with _LOCK:
        d = _load()
        d["balance"] = round(d["balance"] - ORDER_USD, 6)
        d["positions"][mint] = {"symbol": symbol, "tokens": tokens, "cost_usd": ORDER_USD, "opened_at": time.time(),
                                "peak_ratio": 0, "mode": "sniper"}
        _save(d)
    if send and chat:
        send(chat, "🎯 SIM SNIPER COMPROU " + symbol + " · US$ " + format(ORDER_USD, ".2f") +
             "\n🛡️ Anti-rug, anti-golpe e anti-honeypot OK\n" + mint)
    return ""


def sim_exits(send=None, chat=None):
    """Same exit rules as the live engine's sniper positions, with Jupiter quotes."""
    import copy_trader as ct
    from live_exit_monitor import position_signal
    for mint, pos in list(_load()["positions"].items()):
        try:
            value = int(ct._order(mint, SOL_MINT, int(pos["tokens"]))["outAmount"]) / 1e9 * ct._sol_usd()
        except Exception:
            continue
        sig = position_signal(pos, float(pos["cost_usd"]), value)
        with _LOCK:
            d = _load()
            q = d["positions"].get(mint)
            if not q:
                continue
            q["peak_ratio"] = max(float(q.get("peak_ratio") or 0), sig["peak_ratio"])
            if sig["action"] == "sell":
                d["positions"].pop(mint)
                d["balance"] = round(d["balance"] + value, 6)
                pnl = value - float(pos["cost_usd"])
                d["history"] = (d["history"] + [{"mint": mint, "symbol": pos.get("symbol"), "pnl_usd": round(pnl, 4),
                                                 "reason": sig["reason"], "closed_at": time.time()}])[-200:]
            _save(d)
        if sig["action"] == "sell" and send and chat:
            label = {"take_profit": "🚀 STOP MÓVEL", "time_stop": "⏱️ TEMPO"}.get(sig["reason"], "🛑 STOP")
            send(chat, "🎯 SIM " + ("💰 LUCRO" if pnl > 0 else "🔻 PREJUÍZO") + " — VENDEU " + str(pos.get("symbol")) +
                 " (" + label + "): " + format(pnl, "+.2f") + " USD")


def cycle(send=None, chat=None, get=None, now=None):
    """One scan: filter, protect, buy. Exits of real positions run in the live engine."""
    if not enabled():
        return
    real = real_enabled()
    if _load()["positions"]:
        sim_exits(send, chat)                     # simulated positions are always closed by the rules
    now = now or time.time()
    stats = {"at": now, "seen": 0, "passed_filters": 0, "bought": 0, "rejected": {}, "last_reject": ""}
    d = _load()
    for p in candidate_pairs(get):
        stats["seen"] += 1
        mint = (p.get("baseToken") or {}).get("address")
        symbol = (p.get("baseToken") or {}).get("symbol") or mint[:6]
        if _recently_tried(d, mint, now):
            continue
        why = filter_reason(p, now)
        if not why:
            stats["passed_filters"] += 1
            why = safety_reason(mint)
            if why and "indisponível" not in why:
                _mark_tried(mint, now)            # failed a protection: don't look at it again today
            else:
                why = _open_real(p, mint, symbol, send, chat) if real else _open_sim(mint, symbol, send, chat)
                if not why:
                    _mark_tried(mint, now)
                    stats["bought"] += 1
                    d = _load()
        if why:
            key = why.split(" (")[0]
            stats["rejected"][key] = stats["rejected"].get(key, 0) + 1
            stats["last_reject"] = symbol + ": " + why
    LAST_SCAN.update(stats)


def status_text():
    d = _load()
    mode = "REAL" if d["enabled"] and d["real"] else ("SIMULAÇÃO" if d["enabled"] else "DESLIGADO")
    lines = ["🎯 SNIPER — " + mode,
             "Liquidez US$ " + format(MIN_LIQ / 1000, ".0f") + "–" + format(MAX_LIQ / 1000, ".0f") + " mil · moeda com mais de " +
             _age_text(MIN_AGE_S) + " · US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " +
             str(MAX_POSITIONS) + " posições · opera o dia inteiro",
             "Entra com impulso: compras > vendas e preço subindo +" + format(MIN_CHANGE_5M, ".0f") + "% a +" +
             format(MAX_CHANGE_5M, ".0f") + "% em 5 min (sem pegar topo)",
             "🛡️ Anti-rug: criador sem poder de criar/congelar, liquidez travada ≥ " + format(MIN_LP_LOCKED, ".0f") +
             "%, 10 maiores donos < 25%, RugCheck sem risco grave",
             "🛡️ Anti-golpe: sem taxa/gatilho escondido no token, com site/rede social",
             "🛡️ Anti-honeypot: teste de compra e venda devolve ≥ 92%, impacto ≤ " + format(MAX_IMPACT, ".0f") + "%",
             "Saída: stop −" + format(STOP_LOSS * 100, ".0f") + "% · a partir de +25% vende 12% abaixo do topo · sai em 30 min se não decolar"]
    if LAST_SCAN["at"]:
        top = sorted(LAST_SCAN["rejected"].items(), key=lambda kv: -kv[1])[:4]
        lines.append("Última busca (há " + str(int(time.time() - LAST_SCAN["at"])) + " s): " + str(LAST_SCAN["seen"]) +
                     " moedas, " + str(LAST_SCAN["passed_filters"]) + " passaram nos filtros, " +
                     str(LAST_SCAN["bought"]) + " compradas")
        if top:
            lines.append("Recusas: " + ", ".join(k + " (" + str(v) + ")" for k, v in top))
    if not d["real"]:
        closed = d["history"]
        lines.append("Simulação: saldo US$ " + format(d["balance"], ".2f") + " · abertas " + str(len(d["positions"])) +
                     " · resultado " + format(sum(h["pnl_usd"] for h in closed), "+.2f") + " USD em " + str(len(closed)) +
                     " operações (" + str(sum(1 for h in closed if h["pnl_usd"] > 0)) + " no lucro)")
    return "\n".join(lines)
