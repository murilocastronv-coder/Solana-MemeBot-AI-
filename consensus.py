"""Consensus robot: buys a coin when several of the wallets the bot follows are buying it.

- Every buy seen from a followed wallet (copy_trader.cycle; not paused/leaving, at least
  COPY_MIN_BUY_USD) is recorded here. When CONSENSUS_MIN_WALLETS different wallets (default 3)
  bought the same coin within CONSENSUS_WINDOW_MINUTES (60) - and, if CONSENSUS_MIN_SHARE is set,
  at least that share of the followed wallets (0.5 = the majority) - the robot buys it.
- Protections: copy_trader.safety_reason (mint/freeze revoked, top 10 < 25%, buy/sell test >= 92%)
  and the sniper's Token-2022 trap check.
- Exit (live engine mode "consensus"): stop -40%; from +25% a trailing stop sells 10% below the top.
- Owner only, SIMULATION or REAL (main wallet). US$ 32 per buy, max 3 positions, never the same
  coin twice within 24 h.
"""
import json
import os
import tempfile
import threading
import time

SOL_MINT = "So11111111111111111111111111111111111111112"


def _f(name, default):
    return float(os.getenv(name, str(default)))


MIN_WALLETS = int(_f("CONSENSUS_MIN_WALLETS", 3))
# Market-wide: many different wallets buying the coin in the window, with some of them proven
# (wallets the bot analysed with a profitable history), even if they are not copied.
MARKET_MIN_BUYERS = int(_f("CONSENSUS_MARKET_MIN_BUYERS", 15))
MARKET_MIN_GOOD = int(_f("CONSENSUS_MARKET_MIN_GOOD", 0))         # wallets with profit history (0 = not needed)
MARKET_MIN_BUY_SHARE = _f("CONSENSUS_MARKET_MIN_BUY_SHARE", 0.65)  # "most people are buying": 65%+ of traders buy
# DexScreener (many more coins): trades in the last hour
DEX_ENABLED = os.getenv("CONSENSUS_DEX", "true").strip().lower() != "false"
DEX_MIN_BUYS_1H = int(_f("CONSENSUS_DEX_MIN_BUYS_1H", 150))
DEX_MIN_LIQ = _f("CONSENSUS_DEX_MIN_LIQUIDITY_USD", 10_000)
_DEX_CACHE = {"at": 0.0, "pairs": []}
MARKET_ENABLED = os.getenv("CONSENSUS_MARKET", "true").strip().lower() != "false"
MIN_SHARE = _f("CONSENSUS_MIN_SHARE", 0)            # 0.5 = the majority of the followed wallets
WINDOW_S = _f("CONSENSUS_WINDOW_MINUTES", 60) * 60
ORDER_USD = _f("CONSENSUS_ORDER_USD", 32)
MAX_POSITIONS = int(_f("CONSENSUS_MAX_POSITIONS", 3))
COOLDOWN_S = _f("CONSENSUS_COOLDOWN_HOURS", 24) * 3600
STOP_LOSS = _f("CONSENSUS_STOP_LOSS", 0.40)
TRAIL_ARM = _f("CONSENSUS_TRAIL_ARM", 0.25)          # trailing stop starts at +25%
TRAIL_DROP = _f("CONSENSUS_TRAIL_DROP", 0.10)        # ... and sells 10% below the top
START_SIM_USD = 100.0
EXIT_RULES = {"stop_loss": str(STOP_LOSS), "arm": str(1 + TRAIL_ARM), "drop": str(TRAIL_DROP)}

_LOCK = threading.RLock()
LAST = {"at": 0, "candidates": [], "last_reject": ""}


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("consensus.json")


def _default():
    return {"enabled": False, "real": False, "balance": START_SIM_USD, "positions": {}, "history": [],
            "buys": {}, "market": {}, "tried": {}}


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
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".consensus.")
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


# ---------------------------------------------------------------- signals
def record_buy(mint, wallet, now=None):
    """A followed wallet bought `mint` (called by copy_trader.cycle)."""
    now = now or time.time()
    with _LOCK:
        d = _load()
        buys = d["buys"].setdefault(mint, {})
        buys[wallet] = now
        # forget old buys
        d["buys"] = {m: {w: t for w, t in ws.items() if now - t < WINDOW_S}
                     for m, ws in d["buys"].items() if any(now - t < WINDOW_S for t in ws.values())}
        _save(d)


def record_market_buys(rows, now=None):
    """[(mint, wallet, time, 'b'|'s')] traded in the market, seen by the wallet search (any wallet,
    copied or not). 3-item rows count as buys."""
    now = now or time.time()
    with _LOCK:
        d = _load()
        m = d.setdefault("market2", {})
        for row in rows:
            mint, wallet, when = row[0], row[1], float(row[2])
            side = row[3] if len(row) > 3 else "b"
            if now - when < WINDOW_S:
                m.setdefault(mint, {"b": {}, "s": {}})[side][wallet] = when
        keep = {}
        for mint, sides in m.items():
            b = dict(sorted(((w, t) for w, t in sides.get("b", {}).items() if now - t < WINDOW_S), key=lambda kv: kv[1])[-500:])
            sl = dict(sorted(((w, t) for w, t in sides.get("s", {}).items() if now - t < WINDOW_S), key=lambda kv: kv[1])[-500:])
            if b or sl:
                keep[mint] = {"b": b, "s": sl}
        d["market2"] = keep
        d.pop("market", None)
        _save(d)


def good_wallets():
    """Wallets the bot analysed with a profitable history (wallet search), copied or not."""
    try:
        import wallet_copy
        st = wallet_copy._load_state(wallet_copy._state_path())
        return {w for w, s in st.get("wallets", {}).items() if wallet_copy._qualifies(s)}
    except Exception:
        return set()


def market_candidates(now=None, good=None):
    """On-chain (hot coins read by the wallet search): [(mint, buyers, sellers, proven buyers)] where many
    different wallets traded the coin in the window and most of them are buying."""
    if not MARKET_ENABLED:
        return []
    now = now or time.time()
    if good is None:
        good = good_wallets() if MARKET_MIN_GOOD > 0 else set()
    out = []
    for mint, sides in _load().get("market2", {}).items():
        buyers = {w for w, t in sides.get("b", {}).items() if now - t < WINDOW_S}
        sellers = {w for w, t in sides.get("s", {}).items() if now - t < WINDOW_S}
        smart = buyers & good
        share = len(buyers) / (len(buyers) + len(sellers)) if buyers else 0
        if len(buyers) >= MARKET_MIN_BUYERS and share >= MARKET_MIN_BUY_SHARE and len(smart) >= MARKET_MIN_GOOD:
            out.append((mint, sorted(buyers), sorted(sellers), sorted(smart)))
    return sorted(out, key=lambda x: -len(x[1]))


def dex_pairs(now=None):
    """Pairs from DexScreener (+ pump.fun via the sniper's sources), refreshed every 2 min."""
    now = now or time.time()
    try:
        import sniper
        if sniper.LAST_PAIRS.get("pairs") and now - sniper.LAST_PAIRS["at"] < 90:
            return sniper.LAST_PAIRS["pairs"]
    except Exception:
        pass
    if now - _DEX_CACHE["at"] >= 120:
        _DEX_CACHE["at"] = now
        try:
            import sniper
            _DEX_CACHE["pairs"] = sniper.candidate_pairs(None, pump=([], {}))
        except Exception:
            pass
    return _DEX_CACHE["pairs"]


def dex_candidates(pairs):
    """DexScreener: [(mint, buys 1h, sells 1h)] where most trades of the last hour are buys and it is
    still being bought in the last 5 min."""
    if not DEX_ENABLED:
        return []
    out = []
    for p in pairs or []:
        mint = ((p or {}).get("baseToken") or {}).get("address")
        tx = p.get("txns") or {}
        h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
        b, sl = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if (mint and b >= DEX_MIN_BUYS_1H and b / max(b + sl, 1) >= MARKET_MIN_BUY_SHARE
                and int(m5.get("buys") or 0) >= int(m5.get("sells") or 0) and liq >= DEX_MIN_LIQ):
            out.append((mint, b, sl))
    return sorted(out, key=lambda x: -x[1] / max(x[1] + x[2], 1))


def followed_count():
    import copy_trader as ct
    return sum(1 for w in ct._load()["wallets"].values() if not w.get("paused") and not w.get("leaving"))


def needed(followed):
    return max(MIN_WALLETS, int(-(-MIN_SHARE * followed // 1)) if MIN_SHARE > 0 else 0)


def candidates(now=None):
    """[(mint, wallets buying it in the window)] that reach the consensus, most wallets first."""
    now = now or time.time()
    need = needed(followed_count())
    out = []
    for mint, ws in _load()["buys"].items():
        recent = [w for w, t in ws.items() if now - t < WINDOW_S]
        if len(recent) >= need:
            out.append((mint, recent))
    return sorted(out, key=lambda x: -len(x[1]))


# ---------------------------------------------------------------- trading
def safety_reason(mint):
    import copy_trader as ct
    import sniper
    for check in (lambda: ct.safety_reason(mint), lambda: sniper.token_program_reason(mint)):
        try:
            why = check()
        except Exception as exc:
            why = "verificação indisponível (" + type(exc).__name__ + ")"
        if why:
            return why
    return ""


def _mark_tried(mint, now):
    with _LOCK:
        d = _load()
        d["tried"][mint] = now
        d["tried"] = {m: t for m, t in d["tried"].items() if now - t < COOLDOWN_S}
        _save(d)


def _open_real(mint, n, send, chat, why_text=None):
    import live_engine
    import live_order_bridge as bridge
    import live_trade_state as st
    import wallet_holdings
    if not live_engine.ready():
        return "robô real não está pronto: " + str(live_engine.why_stopped() or "")
    allowed, why = st.can_open()
    if not allowed:
        return why
    if st.positions_by_mode("consensus") >= MAX_POSITIONS or len(st._load()["positions"]) >= st.MAX_POSITIONS:
        return "limite de posições"
    if mint in st._load()["positions"]:
        return "já tem a moeda"
    ok, why = wallet_holdings.can_buy(mint, live_engine.get_address())
    if not ok:
        return why
    if bridge.MAX_ORDER_USD < ORDER_USD:
        return "MAX_ORDER_USD menor que a compra de US$ " + format(ORDER_USD, ".0f")
    if send and chat:
        send(chat, "🤝 CONSENSO: " + (why_text or (str(n) + " carteiras acompanhadas compraram")) + " " + mint[:6] +
             "… na última " + format(WINDOW_S / 3600, ".0f") + " h — comprando.")
    live_engine._execute_buy({"baseToken": {"address": mint, "symbol": mint[:6]}}, mint, mint[:6], send, chat,
                             live_engine.get_address(), None, "consensus", usd=ORDER_USD)
    return ""


def _open_sim(mint, n, send, chat, why_text=None):
    import copy_trader as ct
    with _LOCK:
        d = _load()
        if len(d["positions"]) >= MAX_POSITIONS or mint in d["positions"] or d["balance"] < ORDER_USD:
            return "limite de posições ou saldo"
    tokens = int(ct._order(SOL_MINT, mint, int(ORDER_USD / ct._sol_usd() * 1e9))["outAmount"])
    with _LOCK:
        d = _load()
        d["balance"] = round(d["balance"] - ORDER_USD, 6)
        d["positions"][mint] = {"tokens": tokens, "cost_usd": ORDER_USD, "opened_at": time.time(),
                                "peak_ratio": 0, "mode": "consensus", "wallets": n}
        _save(d)
    if send and chat:
        send(chat, "🤝 SIM CONSENSO COMPROU " + mint[:6] + "… · " + (why_text or (str(n) + " carteiras acompanhadas comprando")) + " · US$ " +
             format(ORDER_USD, ".2f") + "\n" + mint)
    return ""


def sim_exits(send=None, chat=None):
    import copy_trader as ct
    from live_exit_monitor import position_signal
    for mint, pos in list(_load()["positions"].items()):
        try:
            value = int(ct._order(mint, SOL_MINT, int(pos["tokens"]))["outAmount"]) / 1e9 * ct._sol_usd()
        except Exception:
            continue
        sig = position_signal(pos, float(pos["cost_usd"]), value)
        pnl = value - float(pos["cost_usd"])
        with _LOCK:
            d = _load()
            q = d["positions"].get(mint)
            if not q:
                continue
            q["peak_ratio"] = max(float(q.get("peak_ratio") or 0), sig["peak_ratio"])
            if sig["action"] == "sell":
                d["positions"].pop(mint)
                d["balance"] = round(d["balance"] + value, 6)
                d["history"] = (d["history"] + [{"mint": mint, "pnl_usd": round(pnl, 4), "reason": sig["reason"],
                                                 "closed_at": time.time()}])[-200:]
            _save(d)
        if sig["action"] == "sell" and send and chat:
            label = "🚀 STOP MÓVEL" if sig["reason"] == "take_profit" else "🛑 STOP"
            send(chat, "🤝 SIM " + ("💰 LUCRO" if pnl > 0 else "🔻 PREJUÍZO") + " — VENDEU " + mint[:6] + "… (" + label +
                 "): " + format(pnl, "+.2f") + " USD")


def cycle(send=None, chat=None, now=None):
    if not enabled():
        return
    now = now or time.time()
    d = _load()
    if d["positions"]:
        sim_exits(send, chat)
    real = bool(d["real"])
    found = [(m, ws, str(len(ws)) + " carteiras acompanhadas compraram") for m, ws in candidates(now)]
    market = market_candidates(now)
    found += [(m, b, "mercado: " + str(len(b)) + " carteiras comprando x " + str(len(sl)) + " vendendo (" +
               format(len(b) / (len(b) + len(sl)) * 100, ".0f") + "% compram) em")
              for m, b, sl, _ in market if m not in {f[0] for f in found}]
    dex = dex_candidates(dex_pairs(now))
    found += [(m, [None] * b, "DEX: " + str(b) + " compras x " + str(sl) + " vendas (" +
               format(b / (b + sl) * 100, ".0f") + "% compras) em")
              for m, b, sl in dex if m not in {f[0] for f in found}]
    LAST.update(at=now, candidates=[(m, len(ws)) for m, ws, _ in found[:5]],
                market=[(m, len(b), len(sl)) for m, b, sl, _ in market[:5]], dex=dex[:5])
    for mint, ws, label in found:
        if now - float(_load()["tried"].get(mint, 0)) < COOLDOWN_S:
            continue
        why = safety_reason(mint)
        if why:
            LAST["last_reject"] = mint[:6] + ": " + why
            if "indisponível" not in why:
                _mark_tried(mint, now)
            continue
        why = (_open_real(mint, len(ws), send, chat, label) if real else _open_sim(mint, len(ws), send, chat, label))
        if why:
            LAST["last_reject"] = mint[:6] + ": " + why
        else:
            _mark_tried(mint, now)


def status_text():
    d = _load()
    mode = "REAL" if d["enabled"] and d["real"] else ("SIMULAÇÃO" if d["enabled"] else "DESLIGADO")
    followed = followed_count()
    lines = ["🤝 CONSENSO DAS CARTEIRAS — " + mode,
             "Compra quando " + str(needed(followed)) + "+ das " + str(followed) + " carteiras acompanhadas compram a "
             "mesma moeda em " + format(WINDOW_S / 3600, ".0f") + " h" + (" (maioria: " + format(MIN_SHARE * 100, ".0f") + "%)"
                                                                          if MIN_SHARE > 0 else ""),
             "US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " + str(MAX_POSITIONS) + " posições · anti-golpe e teste de venda",
             "Saída: stop −" + format(STOP_LOSS * 100, ".0f") + "% · a partir de +" + format(TRAIL_ARM * 100, ".0f") +
             "% stop móvel (vende " + format(TRAIL_DROP * 100, ".0f") + "% abaixo do topo)"]
    now = time.time()
    hot = sorted(((m, len([t for t in ws.values() if now - t < WINDOW_S])) for m, ws in d["buys"].items()),
                 key=lambda x: -x[1])[:5]
    hot = [(m, n) for m, n in hot if n >= 2]
    if hot:
        lines.append("Moedas com mais carteiras comprando agora: " + ", ".join(m[:6] + "… (" + str(n) + ")" for m, n in hot))
    if MARKET_ENABLED or DEX_ENABLED:
        lines.insert(2, "Mercado (carteiras de fora do copy): compra quando a MAIORIA está comprando — na blockchain " +
                     str(MARKET_MIN_BUYERS) + "+ carteiras e " + format(MARKET_MIN_BUY_SHARE * 100, ".0f") +
                     "%+ comprando; na DexScreener " + str(DEX_MIN_BUYS_1H) + "+ compras/h e " +
                     format(MARKET_MIN_BUY_SHARE * 100, ".0f") + "%+ das negociações são compras")
        mk = []
        for m, sides in d.get("market2", {}).items():
            b = len([t for t in sides.get("b", {}).values() if now - t < WINDOW_S])
            sl = len([t for t in sides.get("s", {}).values() if now - t < WINDOW_S])
            if b >= 3:
                mk.append((m, b, sl))
        mk = sorted(mk, key=lambda x: -x[1])[:4]
        if mk:
            lines.append("Na blockchain agora: " + ", ".join(m[:6] + "… (" + str(b) + " comprando x " + str(sl) + " vendendo)"
                                                            for m, b, sl in mk))
        dx = LAST.get("dex") or []
        if dx:
            lines.append("Na DexScreener (maioria comprando): " + ", ".join(m[:6] + "… (" + str(b) + "/" + str(sl) + ")"
                                                                           for m, b, sl in dx[:4]))
    if LAST["last_reject"]:
        lines.append("Última recusa: " + LAST["last_reject"])
    if not d["real"]:
        closed = d["history"]
        lines.append("Simulação: saldo US$ " + format(d["balance"], ".2f") + " · abertas " + str(len(d["positions"])) +
                     " · resultado " + format(sum(h["pnl_usd"] for h in closed), "+.2f") + " USD em " + str(len(closed)) +
                     " operações")
    return "\n".join(lines)
