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
            "buys": {}, "tried": {}}


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


def _open_real(mint, n, send, chat):
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
        send(chat, "🤝 CONSENSO: " + str(n) + " carteiras compraram " + mint[:6] + "… na última " +
             format(WINDOW_S / 3600, ".0f") + " h — comprando.")
    live_engine._execute_buy({"baseToken": {"address": mint, "symbol": mint[:6]}}, mint, mint[:6], send, chat,
                             live_engine.get_address(), None, "consensus", usd=ORDER_USD)
    return ""


def _open_sim(mint, n, send, chat):
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
        send(chat, "🤝 SIM CONSENSO COMPROU " + mint[:6] + "… · " + str(n) + " carteiras comprando · US$ " +
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
    found = candidates(now)
    LAST.update(at=now, candidates=[(m, len(ws)) for m, ws in found[:5]])
    for mint, ws in found:
        if now - float(_load()["tried"].get(mint, 0)) < COOLDOWN_S:
            continue
        why = safety_reason(mint)
        if why:
            LAST["last_reject"] = mint[:6] + ": " + why
            if "indisponível" not in why:
                _mark_tried(mint, now)
            continue
        why = _open_real(mint, len(ws), send, chat) if real else _open_sim(mint, len(ws), send, chat)
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
    if LAST["last_reject"]:
        lines.append("Última recusa: " + LAST["last_reject"])
    if not d["real"]:
        closed = d["history"]
        lines.append("Simulação: saldo US$ " + format(d["balance"], ".2f") + " · abertas " + str(len(d["positions"])) +
                     " · resultado " + format(sum(h["pnl_usd"] for h in closed), "+.2f") + " USD em " + str(len(closed)) +
                     " operações")
    return "\n".join(lines)
