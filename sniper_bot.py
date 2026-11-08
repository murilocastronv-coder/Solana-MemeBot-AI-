"""Volume sniper: pump.fun tokens 5 min to 3 h old with more buyers than sellers.

Source: Jupiter Tokens API v2 (recent + trending pump.fun tokens) with age,
liquidity, holders, audit data and 5m/1h trading stats.

Buy only if ALL pass:
- 5 min to 3 h old (SNIPER_MIN_AGE_MIN / SNIPER_MAX_AGE_MIN);
- more buyers than sellers in the last 5 min (buys >= 1.2x sells) and in 1 h;
- strong 5-minute volume (>= US$ 10k) and liquidity >= US$ 15k;
- anti-scam: mint/freeze disabled, top holders < 25%, dev < 5%, >= 100
  holders, not flagged suspicious, organic score not low, has socials;
- not already parabolic; on-chain re-check and a sell test >= 92%.
Size: US$ 10 per buy, at most 3 positions.
Exit: stop -12%; from +25% a trailing stop 12% below the peak; sell after
30 min if it did not take off.

Simulation uses real Jupiter quotes and touches nothing. Real mode (owner
authorised) buys through the live engine; the exit monitor (mode "sniper")
sells it.
"""
import json
import os
import tempfile
import threading
import time
from collections import Counter, deque

import copy_trader as base

SOL_MINT = base.SOL_MINT


def _env(name, default):
    return float(os.getenv(name, str(default)))


ORDER_USD = _env("SNIPER_ORDER_USD", 10)
MAX_POSITIONS = int(_env("SNIPER_MAX_POSITIONS", 3))
MIN_AGE_MIN, MAX_AGE_MIN = _env("SNIPER_MIN_AGE_MIN", 5), _env("SNIPER_MAX_AGE_MIN", 180)
MIN_LIQUIDITY = _env("SNIPER_MIN_LIQUIDITY_USD", 15_000)
MIN_VOLUME_5M = _env("SNIPER_MIN_VOLUME_5M_USD", 10_000)
BUY_SELL_RATIO = _env("SNIPER_BUY_SELL_RATIO", 1.2)
MIN_HOLDERS = _env("SNIPER_MIN_HOLDERS", 100)
MAX_TOP_HOLDERS_PCT = _env("SNIPER_MAX_TOP_HOLDERS_PCT", 25)
MAX_DEV_PCT = _env("SNIPER_MAX_DEV_PCT", 5)
REQUIRE_GRADUATED = os.getenv("SNIPER_REQUIRE_GRADUATED", "false").strip().lower() == "true"
STOP = "0.12"
ARM = "1.25"
DROP = "0.12"
MAX_HOLD_SECONDS = 30 * 60
HISTORY_MAX = 300

_LOCK = threading.RLock()
_tried = {}
WATCH = {}          # pump.fun mint -> creation time (ms): tokens followed until MAX_AGE_MIN
SCORE = {}          # mint -> liquidity + 5m volume last seen (refresh priority)
WATCH_MAX = 3000
SEARCH_URL = "https://api.jup.ag/tokens/v2/search"
MAX_REFRESH = int(_env("SNIPER_MAX_REFRESH", 300))
_RECENT = deque(maxlen=20000)
LAST_ERROR = {"text": ""}


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("sniper_bot.json")


def _default():
    return {"enabled": False, "real": False, "balance": base.START_USD, "start": base.START_USD,
            "positions": {}, "history": []}


def _load():
    path = _path()
    if not path.exists():
        return _default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default()
    d = _default()
    d.update(data)
    return d


def _save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sniper_bot.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def enabled():
    with _LOCK:
        return bool(_load()["enabled"])


def real_enabled():
    with _LOCK:
        d = _load()
        return bool(d["enabled"] and d["real"])


def set_mode(on, real=False):
    with _LOCK:
        d = _load()
        d["enabled"], d["real"] = bool(on), bool(on and real)
        _save(d)


def _note(kind, detail=""):
    _RECENT.append((time.time(), kind, detail))


# ---------------------------------------------------------------- filters
def _num(d, *path):
    for key in path:
        d = d.get(key) if isinstance(d, dict) else None
    try:
        return float(d) if d is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _created_ms(t):
    import pumpfun
    return pumpfun._created_ms(t)


def reasons(t, now_ms=None):
    """Why a pump.fun token is NOT a sniper buy (empty = buy signal)."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    out = []
    created = _created_ms(t)
    age = (now_ms - created) / 60000 if created else -1
    liq = _num(t, "liquidity")
    mcap = _num(t, "mcap") or _num(t, "fdv")
    audit = t.get("audit") or {}
    b5, s5 = _num(t, "stats5m", "numBuys"), _num(t, "stats5m", "numSells")
    vol5 = _num(t, "stats5m", "buyVolume") + _num(t, "stats5m", "sellVolume")
    if not MIN_AGE_MIN <= age <= MAX_AGE_MIN:
        out.append("fora da faixa de idade")
    if REQUIRE_GRADUATED and not (t.get("graduatedPool") or t.get("graduatedAt")):
        out.append("ainda na curva do pump.fun (liquidez não queimada)")
    if liq < MIN_LIQUIDITY:
        out.append("liquidez baixa")
    if not (liq > 0 and 0 < mcap / liq <= 20):
        out.append("valor alto demais para a liquidez")
    if _num(t, "holderCount") < MIN_HOLDERS:
        out.append("poucos donos")
    if audit.get("mintAuthorityDisabled") is not True or audit.get("freezeAuthorityDisabled") is not True:
        out.append("criador pode criar/congelar")
    if _num(audit, "topHoldersPercentage") >= MAX_TOP_HOLDERS_PCT or audit.get("topHoldersPercentage") is None:
        out.append("maiores donos concentrados")
    if _num(audit, "devBalancePercentage") >= MAX_DEV_PCT:
        out.append("dev com muitas moedas")
    if t.get("isSus") or audit.get("isSus"):
        out.append("marcada como suspeita")
    if str(t.get("organicScoreLabel") or "").lower() == "low":
        out.append("volume falso (organic baixo)")
    if not (t.get("twitter") or t.get("website") or t.get("telegram")):
        out.append("sem site/redes")
    if not (0 < _num(t, "stats5m", "priceChange") <= 40 and _num(t, "stats1h", "priceChange") <= 300):
        out.append("sem subida ou já disparou")
    b1, s1 = _num(t, "stats1h", "numBuys"), _num(t, "stats1h", "numSells")
    if not (b5 >= 25 and b5 >= s5 * BUY_SELL_RATIO and b1 > s1):
        out.append("não tem mais compradores que vendedores")
    if not (MIN_VOLUME_5M <= vol5 <= max(liq, 1) * 3):
        out.append("pouco volume em 5 min")
    return out


# ---------------------------------------------------------------- cycles
def _real_room():
    import live_engine
    import live_trade_state as st
    if not live_engine.ready():
        return "robô real bloqueado: " + str(live_engine.why_stopped() or "verificações")
    allowed, why = st.can_open()
    if not allowed:
        return {"Daily loss limit": "limite de perda do dia atingido"}.get(why, why)
    if st.positions_by_mode("sniper") >= MAX_POSITIONS:
        return str(MAX_POSITIONS) + " posições sniper abertas"
    return ""


def _real_buy(t, mint, symbol, send, chat):
    import live_engine
    pair = {"baseToken": {"address": mint, "symbol": symbol}}
    return live_engine._execute_buy(pair, mint, symbol, send, chat, live_engine.get_address(), None, "sniper",
                                    usd=ORDER_USD)


def track(tokens, now_ms=None):
    """Remember new pump.fun tokens so they are re-checked while 5-25 min old."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    for t in tokens or []:
        mint = isinstance(t, dict) and (t.get("id") or t.get("address"))
        created = _created_ms(t) if mint else 0
        if mint and created and (now_ms - created) / 60000 < MAX_AGE_MIN:
            WATCH.setdefault(mint, created)
            SCORE[mint] = _num(t, "liquidity") + _num(t, "stats5m", "buyVolume") + _num(t, "stats5m", "sellVolume")
    for mint in [m for m, c in WATCH.items() if (now_ms - c) / 60000 > MAX_AGE_MIN]:
        WATCH.pop(mint, None)
        SCORE.pop(mint, None)
    if len(WATCH) > WATCH_MAX:   # keep the ones with the most liquidity/volume
        for mint in sorted(WATCH, key=lambda m: SCORE.get(m, 0))[:len(WATCH) - WATCH_MAX]:
            WATCH.pop(mint, None)
            SCORE.pop(mint, None)


def in_window(now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    return [m for m, c in WATCH.items() if MIN_AGE_MIN <= (now_ms - c) / 60000 <= MAX_AGE_MIN]


def _search(mints):
    """Fresh Jupiter data (stats, audit, holders) for up to 100 mints per request."""
    import jupiter_rate_limiter
    from jupiter_v2_executor import headers
    out = []
    for i in range(0, len(mints), 100):
        r = jupiter_rate_limiter.get(SEARCH_URL, params={"query": ",".join(mints[i:i + 100])},
                                     headers=headers(), timeout=12)
        r.raise_for_status()
        data = r.json()
        out += [t for t in (data if isinstance(data, list) else data.get("tokens") or []) if isinstance(t, dict)]
    return out


def candidates():
    """Feed tokens + fresh data for every followed token now 5-25 min old."""
    import pumpfun
    tokens = pumpfun.fetch()
    track(tokens)
    merged = {(t.get("id") or t.get("address")): t for t in tokens if isinstance(t, dict)}
    # most liquidity/volume first, capped to keep within the Jupiter rate limit (3 requests per pass)
    window = sorted(in_window(), key=lambda m: -SCORE.get(m, 0))[:MAX_REFRESH]
    if window:
        try:
            fresh = _search(window)
            track(fresh)    # refresh the priority scores
            for t in fresh:
                merged[t.get("id") or t.get("address")] = t
        except Exception as exc:
            LAST_ERROR["text"] = "atualização das moedas em acompanhamento falhou: " + type(exc).__name__
    return list(merged.values())


def buy_cycle(tokens=None, send=None, chat=None):
    if not enabled():
        return []
    if tokens is None:
        tokens = candidates()
    real = real_enabled() and send is not None and chat is not None
    opened = []
    now = time.monotonic()
    for t in tokens or []:
        if not isinstance(t, dict):
            continue
        mint = t.get("id") or t.get("address")
        symbol = str(t.get("symbol") or "?")[:20]
        why = reasons(t)
        if why:
            _note("rejected", why[0])
            continue
        if now - _tried.get(mint, -86400) < 86400:
            continue
        if real:
            room = _real_room()
            if room:
                _note("blocked", room)
                break
        else:
            with _LOCK:
                d = _load()
                if len(d["positions"]) >= MAX_POSITIONS:
                    _note("blocked", str(MAX_POSITIONS) + " posições abertas")
                    break
                if d["balance"] < ORDER_USD + base.NETWORK_FEE_USD:
                    _note("blocked", "saldo simulado insuficiente")
                    break
        _tried[mint] = now
        _note("opportunity", symbol)
        unsafe = base.safety_reason(mint)
        if unsafe:
            _note("unsafe", unsafe)
            continue
        if real:
            settled = _real_buy(t, mint, symbol, send, chat)
            if settled and settled.get("status") == "settled_buy":
                _note("bought", symbol)
                opened.append({"mint": mint, "symbol": symbol, "real": True})
                continue
            _note("failed", "compra real não executada")
            break
        try:
            lamports = int(ORDER_USD / base._sol_usd() * 1e9)
            tokens_out = int(base._order(SOL_MINT, mint, lamports)["outAmount"])
            if tokens_out <= 0:
                raise ValueError("sem tokens")
        except Exception as exc:
            LAST_ERROR["text"] = "cotação de compra falhou: " + type(exc).__name__
            _note("failed", "cotação de compra falhou")
            continue
        with _LOCK:
            d = _load()
            cost = ORDER_USD + base.NETWORK_FEE_USD
            d["balance"] -= cost
            d["positions"][mint] = {"symbol": symbol, "tokens": tokens_out, "cost_usd": cost,
                                    "opened_at": time.time(), "peak": 0.0}
            _save(d)
        _note("bought", symbol)
        opened.append({"mint": mint, "symbol": symbol, "real": False, "cost_usd": cost})
    if len(_tried) > 20000:
        _tried.clear()
    return opened


def exit_decision(cost, value, peak, held_seconds):
    """Same rules the real exit monitor uses for mode 'sniper'."""
    from live_exit_rules import trailing_exit_signal
    signal = trailing_exit_signal(cost, max(value, 1e-9), peak, stop_loss=STOP, arm=ARM, drop=DROP)
    if signal["action"] == "hold" and held_seconds > MAX_HOLD_SECONDS and signal["reason"] != "trailing":
        signal.update(action="sell", reason="time_stop")
    return signal


def exit_cycle():
    """Simulated exits (real positions are sold by the live exit monitor)."""
    closed = []
    with _LOCK:
        positions = dict(_load()["positions"])
    for mint, pos in positions.items():
        try:
            value = int(base._order(mint, SOL_MINT, int(pos["tokens"]))["outAmount"]) / 1e9 * base._sol_usd() - base.NETWORK_FEE_USD
        except Exception as exc:
            LAST_ERROR["text"] = "cotação de venda falhou: " + type(exc).__name__
            continue
        signal = exit_decision(float(pos["cost_usd"]), value, pos.get("peak", 0.0),
                               time.time() - float(pos["opened_at"]))
        with _LOCK:
            d = _load()
            if mint not in d["positions"]:
                continue
            if signal["action"] != "sell":
                d["positions"][mint]["peak"] = signal["peak_ratio"]
                _save(d)
                continue
            d["positions"].pop(mint)
            d["balance"] += max(value, 0.0)
            pnl = max(value, 0.0) - float(pos["cost_usd"])
            d["history"] = (d["history"] + [{"symbol": pos["symbol"], "reason": signal["reason"],
                                             "pnl_usd": round(pnl, 4), "closed_at": time.time()}])[-HISTORY_MAX:]
            _save(d)
        _note("sold", signal["reason"])
        closed.append({"mint": mint, "symbol": pos["symbol"], "reason": signal["reason"], "pnl_usd": pnl})
    return closed


# ---------------------------------------------------------------- panel
def status_text():
    with _LOCK:
        d = _load()
    mode = "REAL" if d["enabled"] and d["real"] else ("SIMULAÇÃO" if d["enabled"] else "DESLIGADO")
    hist = d["history"]
    lines = ["📈 SNIPER DE VOLUME " + format(MIN_AGE_MIN, ".0f") + " MIN–" + format(MAX_AGE_MIN / 60, "g") + " H (pump.fun) — " + mode,
             "US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " + str(MAX_POSITIONS) +
             " posições · stop −12% · stop móvel 12% abaixo do topo após +25% · sai em 30 min",
             "Simulação: saldo US$ " + format(d["balance"], ".2f") + " · operações " + str(len(hist)) + " (" +
             str(sum(1 for h in hist if h["pnl_usd"] > 0)) + " no lucro) · P/L " +
             format(sum(h["pnl_usd"] for h in hist), "+.2f") + " USD"]
    for p in list(d["positions"].values())[:3]:
        lines.append("🧪 " + p["symbol"] + " · custo US$ " + format(p["cost_usd"], ".2f"))
    if d["real"]:
        try:
            import live_trade_state as st
            pos = [p for p in st._load()["positions"].values() if p.get("mode") == "sniper"]
            lines.append("REAL · posições sniper: " + str(len(pos)) + "/" + str(MAX_POSITIONS) +
                         " · perda do dia: US$ " + format(st.status()["daily_loss_usd"], ".2f") +
                         " / " + format(st.daily_loss_limit(), ".2f"))
        except Exception:
            pass
    now = time.time()
    rows = [(k, x) for t, k, x in _RECENT if now - t < 3600]
    c = Counter(k for k, _ in rows)
    lines.append("Acompanhando " + str(len(WATCH)) + " moedas novas · " + str(len(in_window())) + " agora entre " +
                 format(MIN_AGE_MIN, ".0f") + " e " + format(MAX_AGE_MIN, ".0f") + " min")
    lines.append("Última hora: " + str(len(rows)) + " análises · oportunidades: " + str(c["opportunity"]) +
                 " · compras: " + str(c["bought"]) + " · vendas: " + str(c["sold"]))
    top = Counter(x for k, x in rows if k in ("rejected", "unsafe", "blocked")).most_common(4)
    if top:
        lines.append("Recusas: " + "; ".join(str(n) + "× " + r for r, n in top))
    if LAST_ERROR["text"]:
        lines.append("Último erro: " + LAST_ERROR["text"])
    return "\n".join(lines)
