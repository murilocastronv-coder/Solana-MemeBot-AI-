"""Strategy B — token trading in SIMULATION with real market prices.

Buys and sells are priced with live Jupiter quotes (the same executable prices a
real order would get), but no transaction is signed or sent and the bot wallet
is never touched. Real mode is intentionally not wired here: it stays off until
the owner authorises it after these results are reviewed.

Rules (all adjustable with environment variables, defaults from the owner's spec)
- Entry: liquidity, volume and buy pressure right now, healthy rise without a
  spike, plus safety checks (mint/freeze revoked, top holders not concentrated,
  sell test >= 92% so taxes/honeypots are rejected).
- Size: US$ 10 per buy, at most 5 open positions.
- Exit: take profit +15% / stop loss -8% on the executable sell value, after an
  estimated network fee per side.
"""
import json
import logging
import os
import tempfile
import threading
import time
from collections import Counter, deque

LOG = logging.getLogger("trading_sim")
SOL_MINT = "So11111111111111111111111111111111111111112"


def _env(name, default):
    return float(os.getenv(name, str(default)))


ORDER_USD = _env("SIM_ORDER_USD", 10)
MAX_POSITIONS = int(_env("SIM_MAX_POSITIONS", 5))
TAKE_PROFIT = _env("SIM_TAKE_PROFIT", 0.15)
STOP_LOSS = _env("SIM_STOP_LOSS", 0.08)
START_USD = _env("SIM_START_USD", 100)
NETWORK_FEE_USD = _env("SIM_NETWORK_FEE_USD", 0.003)   # per side (base + priority, estimated)
MIN_LIQUIDITY = _env("SIM_MIN_LIQUIDITY_USD", 50_000)
MIN_VOLUME_1H = _env("SIM_MIN_VOLUME_1H_USD", 50_000)
MIN_AGE_HOURS = _env("SIM_MIN_AGE_HOURS", 6)
MAX_TOP_HOLDERS = _env("SIM_MAX_TOP_HOLDERS", 0.25)
MIN_ROUNDTRIP = _env("SIM_MIN_ROUNDTRIP", 0.92)
RETRY_SECONDS = 2 * 3600       # same token again only after 2 h
HISTORY_MAX = 300

_LOCK = threading.RLock()
_tried = {}
_safety_cache = {}
_RECENT = deque(maxlen=20000)  # (time, kind, detail) for the hourly panel
LAST_ERROR = {"at": 0.0, "text": ""}


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("trading_sim.json")


def _default():
    return {"enabled": False, "balance": START_USD, "start": START_USD, "positions": {}, "history": []}


def _load():
    path = _path()
    if not path.exists():
        return _default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default()
    base = _default()
    base.update(data)
    return base


def _save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trading_sim.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def enabled():
    with _LOCK:
        return bool(_load()["enabled"])


def set_enabled(value):
    with _LOCK:
        data = _load()
        data["enabled"] = bool(value)
        _save(data)


def reset():
    with _LOCK:
        data = _default()
        data["enabled"] = _load()["enabled"]
        _save(data)


def _note(kind, detail=""):
    _RECENT.append((time.time(), kind, detail))


def _error(text):
    LAST_ERROR.update(at=time.time(), text=str(text)[:160])


# ---------------------------------------------------------------- market filters
def _f(d, *path):
    for key in path:
        d = (d or {}).get(key) if isinstance(d, dict) else None
    try:
        return float(d or 0)
    except (TypeError, ValueError):
        return 0.0


def entry_reasons(p, now_ms=None):
    """Why a pair is NOT a buy (empty list = buy signal)."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    liq, vol1h = _f(p, "liquidity", "usd"), _f(p, "volume", "h1")
    m5, h1, h6 = _f(p, "priceChange", "m5"), _f(p, "priceChange", "h1"), _f(p, "priceChange", "h6")
    b5, s5 = _f(p, "txns", "m5", "buys"), _f(p, "txns", "m5", "sells")
    b1, s1 = _f(p, "txns", "h1", "buys"), _f(p, "txns", "h1", "sells")
    fdv, created = _f(p, "fdv"), _f(p, "pairCreatedAt")
    age_h = (now_ms - created) / 3_600_000 if created else 0
    reasons = []
    if p.get("chainId", "solana") != "solana" or not (p.get("baseToken") or {}).get("address") or _f(p, "priceUsd") <= 0:
        reasons.append("dados inválidos")
    if liq < MIN_LIQUIDITY:
        reasons.append("liquidez baixa")
    if vol1h < MIN_VOLUME_1H:
        reasons.append("pouco volume na última hora")
    if b5 + s5 < 40 or b5 <= s5 or b1 <= s1 * 1.05:
        reasons.append("sem pressão compradora")
    if not (-1 <= m5 <= 8 and 1 <= h1 <= 50 and h6 > 0):
        reasons.append("sem subida saudável")
    if age_h < MIN_AGE_HOURS:
        reasons.append("moeda muito nova")
    if not (fdv > 0 and liq > 0 and fdv / liq <= 50):
        reasons.append("valor alto demais para a liquidez")
    return reasons


# ---------------------------------------------------------------- safety (real on-chain / quote checks)
def _rpc(method, params):
    from live_swap_executor import rpc
    return rpc(method, params)


def _order(input_mint, output_mint, amount):
    from jupiter_v2_executor import order
    return order(input_mint, output_mint, int(amount))


def _sol_usd():
    import live_order_bridge
    return float(live_order_bridge.sol_usd_price())


def safety_reason(mint):
    """'' when safe; otherwise why the token is blocked. Network errors block too."""
    hit = _safety_cache.get(mint)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        info = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        parsed = (((info or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
        data = parsed.get("info") or {}
        if parsed.get("type") != "mint":
            reason = "não é um token válido"
        elif data.get("mintAuthority"):
            reason = "criador ainda pode criar moedas"
        elif data.get("freezeAuthority"):
            reason = "criador pode congelar carteiras"
        else:
            supply = int(_rpc("getTokenSupply", [mint])["value"]["amount"])
            largest = sorted((int(a["amount"]) for a in _rpc("getTokenLargestAccounts", [mint])["value"]), reverse=True)
            share = sum(largest[1:11]) / supply if supply else 1.0   # biggest account = pool
            reason = ("10 maiores donos têm " + format(share * 100, ".0f") + "%") if share >= MAX_TOP_HOLDERS else ""
            if not reason:
                lamports = int(ORDER_USD / _sol_usd() * 1e9)
                tokens = int(_order(SOL_MINT, mint, lamports)["outAmount"])
                back = int(_order(mint, SOL_MINT, tokens)["outAmount"])
                ratio = back / lamports if lamports else 0
                reason = ("teste de venda devolveu " + format(ratio * 100, ".0f") + "%") if ratio < MIN_ROUNDTRIP else ""
    except Exception as exc:
        return "verificação de segurança indisponível (" + type(exc).__name__ + ")"
    _safety_cache[mint] = (time.monotonic(), reason)
    return reason


# ---------------------------------------------------------------- trading
def _value_usd(mint, tokens):
    """Executable sell value: token -> SOL quote x SOL price."""
    sol_out = int(_order(mint, SOL_MINT, tokens)["outAmount"])
    return sol_out / 1e9 * _sol_usd()


def buy_cycle(rows):
    """Open simulated positions on the best candidates (most 1h volume first)."""
    if not enabled():
        return []
    opened = []
    now = time.monotonic()
    ranked = sorted((p for p in rows or [] if isinstance(p, dict)), key=lambda p: -_f(p, "volume", "h1"))
    for p in ranked:
        reasons = entry_reasons(p)
        if reasons:
            _note("rejected", reasons[0])
            continue
        mint = p["baseToken"]["address"]
        symbol = str(p["baseToken"].get("symbol", "?"))[:20]
        with _LOCK:
            data = _load()
            if len(data["positions"]) >= MAX_POSITIONS:
                _note("blocked", "5 posições abertas")
                break
            if data["balance"] < ORDER_USD + NETWORK_FEE_USD:
                _note("blocked", "saldo simulado insuficiente")
                break
            if mint in data["positions"] or now - _tried.get(mint, -RETRY_SECONDS) < RETRY_SECONDS:
                continue
        _note("opportunity", symbol)
        _tried[mint] = now
        reason = safety_reason(mint)
        if reason:
            _note("unsafe", reason)
            continue
        try:
            lamports = int(ORDER_USD / _sol_usd() * 1e9)
            tokens = int(_order(SOL_MINT, mint, lamports)["outAmount"])
            if tokens <= 0:
                raise ValueError("cotação sem tokens")
        except Exception as exc:
            _error("compra simulada falhou: " + type(exc).__name__)
            _note("failed", "cotação de compra falhou")
            continue
        cost = ORDER_USD + NETWORK_FEE_USD
        with _LOCK:
            data = _load()
            data["balance"] -= cost
            data["positions"][mint] = {"symbol": symbol, "tokens": tokens, "cost_usd": cost, "opened_at": time.time()}
            _save(data)
        _note("bought", symbol)
        opened.append({"mint": mint, "symbol": symbol, "cost_usd": cost})
    return opened


def exit_decision(value_usd, cost_usd):
    net = value_usd - NETWORK_FEE_USD
    change = net / cost_usd - 1
    if change >= TAKE_PROFIT:
        return "take_profit", net
    if change <= -STOP_LOSS:
        return "stop_loss", net
    return None, net


def exit_cycle():
    """Close simulated positions at TP/SL using executable sell quotes."""
    closed = []
    with _LOCK:
        positions = dict(_load()["positions"])
    for mint, pos in positions.items():
        try:
            value = _value_usd(mint, int(pos["tokens"]))
        except Exception as exc:
            _error("cotação de venda falhou (" + pos.get("symbol", mint[:6]) + "): " + type(exc).__name__)
            continue
        reason, net = exit_decision(value, float(pos["cost_usd"]))
        if not reason:
            continue
        pnl = net - float(pos["cost_usd"])
        with _LOCK:
            data = _load()
            if mint not in data["positions"]:
                continue
            data["positions"].pop(mint)
            data["balance"] += net
            data["history"] = (data["history"] + [{"symbol": pos.get("symbol"), "mint": mint, "reason": reason,
                                                   "cost_usd": round(float(pos["cost_usd"]), 4),
                                                   "proceeds_usd": round(net, 4), "pnl_usd": round(pnl, 4),
                                                   "opened_at": pos.get("opened_at"), "closed_at": time.time()}])[-HISTORY_MAX:]
            _save(data)
        _note("sold", reason)
        closed.append({"mint": mint, "symbol": pos.get("symbol"), "reason": reason, "pnl_usd": pnl})
    return closed


# ---------------------------------------------------------------- panel
def status_text():
    with _LOCK:
        data = _load()
    hist = data["history"]
    wins = sum(1 for h in hist if h["pnl_usd"] > 0)
    pnl = sum(h["pnl_usd"] for h in hist)
    open_cost = sum(float(p["cost_usd"]) for p in data["positions"].values())
    lines = ["🧪 ESTRATÉGIA B — TRADING EM SIMULAÇÃO (preço real, dinheiro de mentira)",
             "Estado: " + ("LIGADA" if data["enabled"] else "DESLIGADA") + " · modo REAL: desativado até sua autorização",
             "Regras: US$ " + format(ORDER_USD, ".0f") + " por compra · máx. " + str(MAX_POSITIONS) +
             " posições · TP +" + format(TAKE_PROFIT * 100, ".0f") + "% · SL −" + format(STOP_LOSS * 100, ".0f") + "%",
             "Saldo simulado: US$ " + format(data["balance"], ".2f") + " (+ US$ " + format(open_cost, ".2f") +
             " em posições) · início US$ " + format(data["start"], ".2f"),
             "Operações fechadas: " + str(len(hist)) + " (" + str(wins) + " no lucro) · P/L líquido: " +
             format(pnl, "+.2f") + " USD"]
    for mint, p in list(data["positions"].items())[:5]:
        lines.append("📦 " + str(p.get("symbol")) + " · custo US$ " + format(float(p["cost_usd"]), ".2f"))
    for h in hist[-3:]:
        lines.append(("💰 " if h["pnl_usd"] > 0 else "🔻 ") + str(h["symbol"]) + " " + format(h["pnl_usd"], "+.2f") +
                     " USD · " + ("TP" if h["reason"] == "take_profit" else "SL"))
    lines.append(hour_text())
    if LAST_ERROR["text"]:
        lines.append("Último erro: " + LAST_ERROR["text"])
    return "\n".join(lines)


def hour_text(now=None):
    now = time.time() if now is None else now
    rows = [(k, d) for t, k, d in _RECENT if now - t < 3600]
    if not rows:
        return "Última hora: ainda sem análises."
    count = Counter(k for k, _ in rows)
    out = ("Última hora: " + str(len(rows)) + " moedas analisadas · oportunidades: " + str(count["opportunity"]) +
           " · rejeitadas: " + str(count["rejected"] + count["unsafe"]) + " · compras: " + str(count["bought"]) +
           " · vendas: " + str(count["sold"]) + " · falhas: " + str(count["failed"]))
    reasons = Counter(d for k, d in rows if k in ("rejected", "unsafe", "blocked"))
    if reasons:
        out += "\nMotivos: " + "; ".join(str(n) + "× " + r for r, n in reasons.most_common(4))
    if not count["bought"] and not count["opportunity"]:
        out += "\nSem compras porque nenhuma moeda passou em todos os critérios."
    return out
