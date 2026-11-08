"""Pump.fun strategy: the sniper rules applied to tokens launched on pump.fun.

Source: Jupiter Tokens API v2 (recent + trending 5m), which reports each
token's launchpad, holders, audit and 5m/1h trading stats. Field access is
defensive because the feed format cannot be pinned here; if the feed cannot
be read the owner is told (hourly) instead of failing silently.
Exits are the sniper exits (-12% / trail 12% from +25% / 30-min time stop).
"""
import logging
import time
from datetime import datetime

import requests
import jupiter_rate_limiter

import live_engine as engine
import live_trade_state as state
from jupiter_v2_executor import headers

FEEDS = ("https://api.jup.ag/tokens/v2/recent",
         "https://api.jup.ag/tokens/v2/toptrending/5m?limit=100")
FRACTION = engine.SNIPER_FRACTION        # 5% of account
MAX_POSITIONS = 2
MIN_AGE_MIN, MAX_AGE_MIN = 5, 90
MIN_LIQUIDITY = 15000
MIN_HOLDERS = 150
MAX_TOP_HOLDERS_PCT = 25
MAX_DEV_PCT = 5
_cache = [0.0, []]
_seen = {}
last_scan = {"tokens": 0, "passed": 0, "error": None}


def _num(d, *path, default=0.0):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    try:
        return float(d) if d is not None else default
    except (TypeError, ValueError):
        return default


def _created_ms(t):
    raw = (t.get("firstPool") or {}).get("createdAt") or t.get("createdAt")
    if isinstance(raw, (int, float)):
        return float(raw) * (1000 if raw < 1e12 else 1)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0.0


def is_pumpfun(t):
    mint = str(t.get("id") or t.get("address") or "")
    launchpad = str(t.get("launchpad") or "").lower()
    return "pump" in launchpad or mint.endswith("pump")


def fetch():
    """Recent/trending pump.fun tokens (cached 15 s)."""
    if time.monotonic() - _cache[0] < 15:
        return _cache[1]
    tokens, error = {}, None
    for url in FEEDS:
        try:
            r = jupiter_rate_limiter.get(url, headers=headers(), timeout=12)
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("tokens") or data.get("data") or []
            for t in items:
                mint = isinstance(t, dict) and (t.get("id") or t.get("address"))
                if mint and is_pumpfun(t):
                    tokens[mint] = t
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)[:120]
    rows = list(tokens.values())
    last_scan.update(tokens=len(rows), error=error if not rows else None)
    _cache[:] = [time.monotonic(), rows]
    return rows


def candidate(t):
    """Same idea as the sniper: young, liquid, broad organic buying, not already parabolic."""
    age_min = (time.time() * 1000 - _created_ms(t)) / 60000
    liq = _num(t, "liquidity")
    mcap = _num(t, "mcap") or _num(t, "fdv")
    buys5, sells5 = _num(t, "stats5m", "numBuys"), _num(t, "stats5m", "numSells")
    vol5 = _num(t, "stats5m", "buyVolume") + _num(t, "stats5m", "sellVolume")
    audit = t.get("audit") or {}
    top = _num(audit, "topHoldersPercentage", default=100.0)
    dev = _num(audit, "devBalancePercentage", default=0.0)
    # Anti-scam extras: Jupiter's suspicious flag, low organic score (bot/wash
    # volume) and no website/socials at all are rejected.
    if t.get("isSus") or audit.get("isSus"):
        return False
    if str(t.get("organicScoreLabel") or "").lower() == "low":
        return False
    if not (t.get("twitter") or t.get("website") or t.get("telegram")):
        return False
    return (MIN_AGE_MIN <= age_min <= MAX_AGE_MIN and liq >= MIN_LIQUIDITY and
            0 < mcap / liq <= 20 and _num(t, "holderCount") >= MIN_HOLDERS and
            audit.get("mintAuthorityDisabled") is True and audit.get("freezeAuthorityDisabled") is True and
            top < MAX_TOP_HOLDERS_PCT and dev < MAX_DEV_PCT and
            0 < _num(t, "stats5m", "priceChange") <= 40 and _num(t, "stats1h", "priceChange") <= 300 and
            buys5 >= 25 and buys5 > sells5 * 1.3 and _num(t, "stats1h", "numBuys") >= 100 and
            5000 <= vol5 <= liq * 3)


def as_pair(t):
    mint = t.get("id") or t.get("address")
    return {"baseToken": {"address": mint, "symbol": str(t.get("symbol") or "?")[:20]}}


def safe(mint):
    ok, why = engine.token_safety(mint)
    if ok:
        ok, why = engine.holders_ok(mint)
    if ok:
        ok, why = engine.roundtrip_ok(mint)
    return ok, why


def candidates(exclude=()):
    """Pump.fun tokens passing the strategy filters (each re-evaluated at most every 10 min)."""
    now = time.monotonic()
    out = []
    passed = 0
    for t in fetch():
        mint = t.get("id") or t.get("address")
        if mint in exclude or not candidate(t):
            continue
        passed += 1
        if now - _seen.get(mint, -600) < 600:
            continue
        out.append(t)
    last_scan["passed"] = passed
    return out


def enabled():
    return state.flag("pumpfun_enabled")


def cycle(send, chat):
    """Real money: buy at most one pump.fun token per ~20 s cycle."""
    if not chat or not enabled() or not engine.ready():
        return None
    found = candidates(exclude=set(state._load()["positions"]))
    _report(send, chat)
    allowed, _ = state.can_open()
    if not allowed or state.positions_by_mode("pumpfun") >= MAX_POSITIONS:
        return None
    wallet = engine.get_address()
    for t in found:
        mint = t.get("id") or t.get("address")
        _seen[mint] = time.monotonic()
        if time.monotonic() - engine._tried.get(mint, -engine.RETRY_MINT_AFTER_SECONDS) < engine.RETRY_MINT_AFTER_SECONDS:
            continue
        ok, why = safe(mint)
        if not ok:
            logging.info("Pump.fun skipped %s: %s", mint[:8], why)
            continue
        p = as_pair(t)
        return engine._execute_buy(p, mint, p["baseToken"]["symbol"], send, chat, wallet, FRACTION, "pumpfun")
    return None


def _report(send, chat):
    if last_scan["error"]:
        text = "⚠️ Pump.fun: não consegui ler a lista de moedas (" + last_scan["error"] + "). Continuo tentando."
    else:
        text = ("🟣 Pump.fun ligado: última leitura " + str(last_scan["tokens"]) + " moedas do pump.fun, " +
                str(last_scan["passed"]) + " passaram nos filtros. Resumo a cada 1 hora.")
    engine._notice(send, chat, "pumpfun_report", text, engine.REPORT_EVERY_SECONDS)
