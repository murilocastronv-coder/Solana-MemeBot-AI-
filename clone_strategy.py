"""Autonomous bot that trades a wallet's measured strategy WITHOUT copying the wallet.

wallet_profile measures how a wallet trades (platform, how old the coin is and its
market value when it buys, take profit, stop loss, hold time). This bot looks for
coins with that same profile on its own (DexScreener feeds of hot / new Solana
tokens), runs the anti-scam checks, buys a fixed amount and exits with the wallet's
own TP / SL / max hold.

The wallet's real reasons to enter (social signals, private info, its own reading of
the chart) are not on chain, so this reproduces its measurable profile only.
Simulation by default; real money only when the owner switches it on.
"""
import json
import os
import tempfile
import threading
import time
from collections import deque

import requests

DEX = "https://api.dexscreener.com"
FEEDS = ("/token-boosts/latest/v1", "/token-profiles/latest/v1", "/token-boosts/top/v1")
DEX_PLATFORM = {"pumpfun": "pump.fun", "pumpswap": "PumpSwap", "raydium": "Raydium", "launchlab": "Raydium LaunchLab",
                "meteora": "Meteora", "meteoradbc": "Meteora DBC", "orca": "Orca"}
ORDER_USD = float(os.getenv("CLONE_ORDER_USD", os.getenv("COPY_ORDER_USD", "10")))
MAX_POSITIONS = int(os.getenv("CLONE_MAX_POSITIONS", "3"))
MIN_LIQUIDITY = float(os.getenv("CLONE_MIN_LIQUIDITY_USD", "5000"))
RETRY_MINT_AFTER = 24 * 3600
START_USD = 100.0
NETWORK_FEE_USD = 0.003

_LOCK = threading.RLock()
_http = requests.Session()
EVENTS = deque(maxlen=30)


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("clone_strategy.json")


def _default():
    return {"enabled": False, "real": False, "config": None, "balance": START_USD, "positions": {},
            "history": [], "tried": {}}


def _load():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default()
    d = _default()
    d.update(data)
    return d


def _save(d):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".clone_strategy.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, path)


def _event(text):
    EVENTS.append((time.time(), text))


# ---------------------------------------------------------------- configuration
def config_from_profile(address, summary):
    """Entry filters + exits from a wallet_profile summary. Ranges use the wallet's 10-90% band."""
    plats = summary.get("platforms") or {}
    total = max(1, sum(plats.values()))
    allowed = [p for p, n in plats.items() if n / total >= 0.15 and p != "outro"] or list(plats)[:2]

    def widen(lo, hi, factor):
        if lo is None or hi is None:
            return None, None
        return lo / factor, hi * factor
    age_lo, age_hi = widen(summary.get("token_age_p10"), summary.get("token_age_p90"), 1.25)
    mc_lo, mc_hi = widen(summary.get("mcap_p10"), summary.get("mcap_p90"), 1.25)
    return {
        "source": address,
        "platforms": allowed,
        "age_min": age_lo, "age_max": age_hi,                 # minutes since the pair was created
        "mcap_min": mc_lo, "mcap_max": mc_hi,                 # USD market value at entry
        "tp": round(min(max(float(summary.get("suggest_tp") or 0.5), 0.05), 10.0), 4),
        "sl": round(min(max(float(summary.get("suggest_sl") or 0.15), 0.03), 0.9), 4),
        "max_hold_s": int(summary["suggest_max_hold_min"] * 60) if summary.get("suggest_max_hold_min") else None,
        "win_rate": summary.get("win_rate"), "positions": summary.get("positions"),
    }


def configure(address, summary):
    cfg = config_from_profile(address, summary)
    with _LOCK:
        d = _load()
        d["config"] = cfg
        _save(d)
    return cfg


def set_mode(on, real=False):
    with _LOCK:
        d = _load()
        if on and not d.get("config"):
            raise ValueError("primeiro analise uma carteira: /estrategia ENDEREÇO")
        d["enabled"], d["real"] = bool(on), bool(on and real)
        _save(d)


def enabled():
    with _LOCK:
        return bool(_load()["enabled"])


# ---------------------------------------------------------------- entry
def _get_json(path):
    r = _http.get(DEX + path, timeout=15, headers={"Accept": "application/json", "User-Agent": "SolanaCloneBot/1.0"})
    r.raise_for_status()
    return r.json()


def candidates(get=_get_json):
    """Solana pairs from DexScreener's hot / new feeds (best pair per token)."""
    mints = []
    for feed in FEEDS:
        try:
            rows = get(feed)
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress"):
                mints.append(row["tokenAddress"])
    mints = list(dict.fromkeys(mints))[:120]
    best = {}
    for i in range(0, len(mints), 30):
        try:
            pairs = get("/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        except Exception:
            continue
        for p in pairs if isinstance(pairs, list) else []:
            mint = (p.get("baseToken") or {}).get("address")
            if not mint or p.get("chainId") != "solana":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if mint not in best or liq > float((best[mint].get("liquidity") or {}).get("usd") or 0):
                best[mint] = p
    return list(best.values())


def matches(pair, cfg, now=None):
    """(True, '') when the coin looks like the ones the wallet buys, else (False, reason)."""
    now = now or time.time()
    platform = DEX_PLATFORM.get(str(pair.get("dexId") or "").lower(), str(pair.get("dexId") or ""))
    if cfg.get("platforms") and platform not in cfg["platforms"]:
        return False, "plataforma " + platform
    created = pair.get("pairCreatedAt")
    if cfg.get("age_min") is not None:
        if not created:
            return False, "idade desconhecida"
        age = (now - int(created) / 1000) / 60
        if not cfg["age_min"] <= age <= cfg["age_max"]:
            return False, "idade " + format(age, ".0f") + " min"
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    if cfg.get("mcap_min") is not None and not cfg["mcap_min"] <= mcap <= cfg["mcap_max"]:
        return False, "valor de mercado US$ " + format(mcap, ".0f")
    if float((pair.get("liquidity") or {}).get("usd") or 0) < MIN_LIQUIDITY:
        return False, "liquidez baixa"
    return True, ""


def _open(pair, cfg, real, send, chat):
    import copy_trader as ct
    mint = pair["baseToken"]["address"]
    symbol = pair["baseToken"].get("symbol") or mint[:6]
    why = ct.safety_reason(mint)
    if why:
        _event("recusou " + symbol + ": " + why)
        return None
    if real:
        import live_engine
        import live_trade_state as st
        if not live_engine.ready():
            return None
        allowed, _ = st.can_open()
        if (not allowed or len(st._load()["positions"]) >= ct.MAX_OPEN_TOTAL
                or st.positions_by_mode("clone") >= MAX_POSITIONS or mint in st._load()["positions"]):
            return None
        settled = live_engine._execute_buy(pair, mint, symbol, send, chat, live_engine.get_address(), None, "clone",
                                           usd=ORDER_USD)
        if not settled or settled.get("status") != "settled_buy":
            return None
        st.set_position_fields(mint, copy_tp=cfg["tp"], copy_sl=cfg["sl"], copy_max_hold_s=cfg["max_hold_s"])
        _event("REAL comprou " + symbol)
        return None
    try:
        lamports = int(ORDER_USD / ct._sol_usd() * 1e9)
        tokens = int(ct._order(ct.SOL_MINT, mint, lamports)["outAmount"])
    except Exception as exc:
        _event("cotação falhou em " + symbol + ": " + type(exc).__name__)
        return None
    cost = ORDER_USD + NETWORK_FEE_USD
    if ct.open_sim_total() >= ct.MAX_OPEN_TOTAL:
        return None
    with _LOCK:
        d = _load()
        if d["balance"] < cost or mint in d["positions"]:
            return None
        d["balance"] -= cost
        d["positions"][mint] = {"symbol": symbol, "tokens": tokens, "cost_usd": cost, "opened_at": time.time()}
        _save(d)
    return "🤖 SIM comprou " + symbol + " · US$ " + format(cost, ".2f") + "\n" + mint


# ---------------------------------------------------------------- exit (simulation)
def sim_exits(cfg):
    import copy_trader as ct
    msgs = []
    with _LOCK:
        positions = dict(_load()["positions"])
    for mint, pos in positions.items():
        try:
            value = int(ct._order(mint, ct.SOL_MINT, int(pos["tokens"]))["outAmount"]) / 1e9 * ct._sol_usd()
        except Exception:
            continue
        change = (value - NETWORK_FEE_USD) / float(pos["cost_usd"]) - 1
        reason = None
        if change >= cfg["tp"]:
            reason = "alvo"
        elif change <= -cfg["sl"]:
            reason = "stop"
        elif cfg.get("max_hold_s") and time.time() - float(pos["opened_at"]) > cfg["max_hold_s"]:
            reason = "tempo"
        if not reason:
            continue
        pnl = value - NETWORK_FEE_USD - float(pos["cost_usd"])
        with _LOCK:
            d = _load()
            if mint not in d["positions"]:
                continue
            d["positions"].pop(mint)
            d["balance"] += value - NETWORK_FEE_USD
            d["history"] = (d["history"] + [{"mint": mint, "symbol": pos["symbol"], "pnl": round(pnl, 4),
                                             "reason": reason, "at": time.time()}])[-200:]
            _save(d)
        msgs.append(("🤖 SIM " + ("💰 LUCRO" if pnl > 0 else "🔻 PREJUÍZO") + " — " + pos["symbol"] + ": " +
                     format(pnl, "+.2f") + " USD (" + reason + ")"))
    return msgs


# ---------------------------------------------------------------- loop
def cycle(send=None, chat=None, get=_get_json, now=None):
    with _LOCK:
        d = _load()
    cfg = d.get("config")
    if not d["enabled"] or not cfg:
        return []
    real = bool(d["real"]) and send is not None and chat is not None
    msgs = [] if real else sim_exits(cfg)
    with _LOCK:
        d = _load()
        open_count = len(d["positions"])
        tried = {m: t for m, t in d.get("tried", {}).items() if time.time() - t < RETRY_MINT_AFTER}
    import copy_trader as ct
    if not real and (open_count >= MAX_POSITIONS or ct.open_sim_total() >= ct.MAX_OPEN_TOTAL):
        return msgs
    for pair in candidates(get):
        mint = pair["baseToken"]["address"]
        if mint in tried:
            continue
        ok, why = matches(pair, cfg, now)
        if not ok:
            continue
        tried[mint] = time.time()
        m = _open(pair, cfg, real, send, chat)
        if m:
            msgs.append(m)
            open_count += 1
        if not real and (open_count >= MAX_POSITIONS or ct.open_sim_total() >= ct.MAX_OPEN_TOTAL):
            break
    with _LOCK:
        d = _load()
        d["tried"] = tried
        _save(d)
    return msgs


def status_text():
    with _LOCK:
        d = _load()
    cfg = d.get("config")
    mode = "REAL" if d["enabled"] and d["real"] else ("SIMULAÇÃO" if d["enabled"] else "DESLIGADO")
    lines = ["🤖 ROBÔ ESTRATÉGIA PRÓPRIA — " + mode]
    if not cfg:
        lines.append("Sem estratégia. Envie /estrategia ENDEREÇO para medir uma carteira e montar o robô.")
        return "\n".join(lines)

    def usd(x):
        return "-" if x is None else (format(x / 1e6, ".1f") + " mi" if x >= 1e6 else format(x / 1e3, ".0f") + " mil")

    def mins(x):
        return "-" if x is None else (format(x, ".0f") + " min" if x < 120 else format(x / 60, ".1f") + " h")
    lines += ["Estratégia medida de " + cfg["source"][:6] + "…" + cfg["source"][-4:] +
              (" (" + str(cfg.get("positions")) + " operações, acerto " + format((cfg.get("win_rate") or 0) * 100, ".0f") +
               "%)" if cfg.get("positions") else ""),
              "Compra: " + ", ".join(cfg["platforms"] or ["qualquer plataforma"]) +
              " · moeda com " + mins(cfg.get("age_min")) + " a " + mins(cfg.get("age_max")) +
              " · valor de mercado US$ " + usd(cfg.get("mcap_min")) + " a " + usd(cfg.get("mcap_max")),
              "US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " + str(MAX_POSITIONS) + " posições · anti-golpe",
              "Venda: alvo +" + format(cfg["tp"] * 100, ".0f") + "% · stop −" + format(cfg["sl"] * 100, ".0f") + "%" +
              (" · máx. " + mins(cfg["max_hold_s"] / 60) if cfg.get("max_hold_s") else ""),
              "Simulação: saldo US$ " + format(d["balance"], ".2f") + " · abertas " + str(len(d["positions"]))]
    hist = d.get("history") or []
    if hist:
        wins = sum(1 for h in hist if h["pnl"] > 0)
        lines.append("Fechadas: " + str(len(hist)) + " (" + str(wins) + " no lucro) · resultado " +
                     format(sum(h["pnl"] for h in hist), "+.2f") + " USD")
    for _, text in list(EVENTS)[-3:]:
        lines.append("• " + text)
    lines.append("⚠️ Reproduz o perfil medido da carteira, não as razões dela para entrar.")
    return "\n".join(lines)
