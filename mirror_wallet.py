"""Mirror robot: trades exactly like ONE wallet, at the same moment it trades.

Watches the target wallet in real time (WebSocket on the dedicated RPC, with polling
as a fallback) and, for every swap it makes:
  * first buy of a coin  -> we buy the same coin (size scaled to how much it put in);
  * it buys more         -> we add to the position in the same proportion;
  * it sells part        -> we sell the same fraction of our position;
  * it sells everything  -> we sell everything.
The coins are the wallet's own coins, so the coin type is the same by definition.

On top of that, the wallet's history is measured (wallet_profile) every few hours to set
a SAFETY stop at its own worst losses and a max hold a bit longer than it usually holds,
used only if its sale is missed (bot offline, RPC failure, the wallet never sells a rug).
Simulation by default; real money only when the owner switches it on.
"""
import json
import logging
import os
import tempfile
import threading
import time
from collections import deque

DEFAULT_TARGET = "EHg5YkU2SZBTvuT87rUsvxArGp3HLeye1fXaSDfuMyaf"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {SOL_MINT, USDC_MINT, USDT_MINT}


def _env(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


ORDER_USD = _env("MIRROR_ORDER_USD", os.getenv("COPY_ORDER_USD", "5"))
SIZE_MODE = os.getenv("MIRROR_SIZE_MODE", "proporcional").strip().lower()   # proporcional | fixo
SIZE_MIN_X = _env("MIRROR_SIZE_MIN_X", 0.5)       # smallest order = 0.5x ORDER_USD
SIZE_MAX_X = _env("MIRROR_SIZE_MAX_X", 2.0)       # biggest order = 2x ORDER_USD
MAX_POSITIONS = int(_env("MIRROR_MAX_POSITIONS", 5))
MAX_ADDS = int(_env("MIRROR_MAX_ADDS", 3))         # add-buys copied per coin
MAX_DELAY_S = _env("MIRROR_MAX_DELAY_SECONDS", 90)  # never copy a buy older than this
MIN_BUY_USD = _env("MIRROR_MIN_BUY_USD", 0)        # 0 = copy every buy
ANTI_SCAM = os.getenv("MIRROR_ANTI_GOLPE", "true").strip().lower() != "false"
POLL_S = max(1.0, _env("MIRROR_POLL_SECONDS", 3))
POLL_WS_S = max(POLL_S, _env("MIRROR_POLL_WS_SECONDS", 15))
REPROFILE_S = 12 * 3600
WALLET_CHECK_S = 60
REAL_MIN_ORDER_USD = 3.0
START_USD = 100.0
NETWORK_FEE_USD = 0.003

_LOCK = threading.RLock()      # file reads / writes (quick)
_CYCLE = threading.Lock()      # one mirror pass at a time (may wait for a real order to settle)
EVENTS = deque(maxlen=30)
WAKE = threading.Event()
WS_STATE = {"connected": False, "since": 0.0}


def _event(text):
    EVENTS.append((time.time(), text))


# ---------------------------------------------------------------- state
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("mirror_wallet.json")


def _default():
    return {"enabled": False, "real": False, "target": os.getenv("MIRROR_WALLET", DEFAULT_TARGET).strip(),
            "last_sig": None, "profile": None, "profiled_at": 0, "balance": START_USD,
            "positions": {}, "history": []}


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
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mirror_wallet.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, path)


def target():
    with _LOCK:
        return _load()["target"]


def set_target(address):
    from solders.pubkey import Pubkey
    Pubkey.from_string(address)
    with _LOCK:
        d = _load()
        if d["positions"] and address != d["target"]:
            raise ValueError("feche as posições abertas do espelho antes de trocar de carteira")
        if address != d["target"]:
            d.update(target=address, last_sig=None, profile=None, profiled_at=0)
        _save(d)
    WAKE.set()


def set_mode(on, real=False):
    with _LOCK:
        d = _load()
        d["enabled"], d["real"] = bool(on), bool(on and real)
        if on:
            d["last_sig"] = None     # start from now: never copy old trades
        _save(d)
    WAKE.set()


def enabled():
    with _LOCK:
        return bool(_load()["enabled"])


# ---------------------------------------------------------------- strategy measurement
def exits_from_profile(summary):
    """Safety exits from the wallet's own numbers. Its real exit is its sale (mirrored);
    these only fire when that sale is missed, so they sit a bit beyond what it does."""
    env_sl, env_tp, env_hold = os.getenv("MIRROR_STOP_LOSS"), os.getenv("MIRROR_TAKE_PROFIT"), os.getenv("MIRROR_MAX_HOLD_MIN")
    sl = None
    worst = summary.get("sl_p25") if summary else None
    if worst is not None:
        sl = abs(float(worst)) * 1.1
    if env_sl:
        sl = float(env_sl)
    sl = round(min(max(sl if sl is not None else 0.5, 0.05), 0.95), 4)
    tp = float(env_tp) if env_tp else None          # no target by default: it sells with the wallet
    hold_min = float(env_hold) if env_hold else (float(summary["hold_p90"]) * 2
                                                 if summary and summary.get("hold_p90") else None)
    return {"sl": sl, "tp": tp, "max_hold_s": int(hold_min * 60) if hold_min else None}


def measure(rpc=None, get_json=None, sol_usd=None):
    """Read the target's history and store its strategy (runs in the background)."""
    import wallet_profile
    address = target()
    if rpc is None:
        import dex_wallets
        rpc = dex_wallets._rpc_factory(os.getenv("SOLANA_RPC_URL"))
        get_json = get_json or dex_wallets._get_json
    summary, _closed, text = wallet_profile.analyze(address, rpc, sol_usd or _sol_usd(), get_json=get_json)
    keep = ("positions", "open", "win_rate", "pnl_usd", "avg_win", "avg_loss", "tp_median", "tp_p25", "tp_p75",
            "sl_median", "sl_p25", "hold_median", "hold_p10", "hold_p90", "size_median", "size_p25", "size_p75",
            "scale_in", "scale_out", "platforms", "top_hours", "trades_per_day", "token_age_median",
            "mcap_median", "mcap_p10", "mcap_p90")
    profile = {k: summary.get(k) for k in keep}
    profile["exits"] = exits_from_profile(summary if summary.get("positions") else None)
    with _LOCK:
        d = _load()
        if d["target"] == address:
            d["profile"], d["profiled_at"] = profile, time.time()
            _save(d)
    return profile, text


# ---------------------------------------------------------------- reading the wallet
def _rpc(method, params):
    from live_swap_executor import rpc
    return rpc(method, params)


def _order(input_mint, output_mint, amount):
    from jupiter_v2_executor import order
    return order(input_mint, output_mint, int(amount))


def _sol_usd():
    import live_order_bridge
    return float(live_order_bridge.sol_usd_price())


def trade_from_tx(tx, wallet, sol_usd):
    """The wallet's swap in one transaction: dict(side, mint, before, after, usd, t) or None.
    before/after = the wallet's token balance (UI units) around the swap."""
    import wallet_profile
    swap = wallet_profile.parse_swap(tx, wallet)
    if not swap:
        return None
    meta = tx.get("meta") or {}
    bal = {"pre": 0.0, "post": 0.0}
    for side, key in (("preTokenBalances", "pre"), ("postTokenBalances", "post")):
        for b in meta.get(side) or []:
            if b.get("owner") == wallet and b.get("mint") == swap["mint"]:
                ui = b.get("uiTokenAmount") or {}
                bal[key] += int(ui.get("amount") or 0) / (10 ** int(ui.get("decimals") or 0))
    return {"side": swap["side"], "mint": swap["mint"], "before": bal["pre"], "after": bal["post"],
            "tokens": swap["tokens"], "usd": swap["sol"] * sol_usd + swap["stable"],
            "platform": swap["platform"], "t": swap["t"] or int(time.time())}


def new_trades(d, sol_usd, rpc=None):
    """Swaps of the target since the last look, oldest first. The first look starts from now."""
    rpc = rpc or _rpc
    sigs = rpc("getSignaturesForAddress", [d["target"], {"limit": 25, "commitment": "confirmed"}]) or []
    if not sigs:
        return []
    newest = sigs[0]["signature"]
    if d.get("last_sig") is None:
        d["last_sig"] = newest
        return []
    fresh = []
    for s in sigs:
        if s["signature"] == d["last_sig"]:
            break
        if s.get("err") is None:
            fresh.append(s["signature"])
    out = []
    for sig in reversed(fresh):
        tx = rpc("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                          "maxSupportedTransactionVersion": int(os.getenv("SOLANA_MAX_TX_VERSION", "1"))}])
        if not isinstance(tx, dict):
            break                      # not readable yet: retry from here next time
        d["last_sig"] = sig
        tr = trade_from_tx(tx, d["target"], sol_usd)
        if tr:
            tr["sig"] = sig
            out.append(tr)
    else:
        d["last_sig"] = newest
    return out


def wallet_holds(wallet, mint, rpc=None):
    """Token amount (UI) the wallet holds now, or None when unknown."""
    rpc = rpc or _rpc
    try:
        res = rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    except Exception:
        return None
    total = 0.0
    for acc in res.get("value") or []:
        info = ((((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        total += float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
    return total


# ---------------------------------------------------------------- sizing
def order_size(wallet_usd, profile):
    """Our order in USD. Proportional mode keeps the wallet's conviction: a buy twice its usual
    size becomes twice our base size (clamped to 0.5x-2x)."""
    if SIZE_MODE == "fixo" or not wallet_usd or not profile or not profile.get("size_median"):
        return round(ORDER_USD, 2)
    ratio = float(wallet_usd) / float(profile["size_median"])
    return round(ORDER_USD * min(max(ratio, SIZE_MIN_X), SIZE_MAX_X), 2)


# ---------------------------------------------------------------- acting
def _buy(d, tr, send, chat, real):
    """Mirror a buy: open the coin or add to it. Returns a message or None."""
    mint, pos = tr["mint"], d["positions"].get(tr["mint"])
    exits = (d.get("profile") or {}).get("exits") or exits_from_profile(None)
    if pos is None:
        if time.time() - tr["t"] > MAX_DELAY_S:
            _event("ignorou compra atrasada em " + mint[:6] + " (" + format(time.time() - tr["t"], ".0f") + " s)")
            return None
        if MIN_BUY_USD and tr["usd"] < MIN_BUY_USD:
            _event("ignorou compra pequena em " + mint[:6] + ": US$ " + format(tr["usd"], ".0f"))
            return None
        if len(d["positions"]) >= MAX_POSITIONS:
            _event("limite de " + str(MAX_POSITIONS) + " posições: não entrou em " + mint[:6])
            return None
        if ANTI_SCAM:
            import copy_trader
            why = copy_trader.safety_reason(mint)
            if why:
                _event("anti-golpe recusou " + mint[:6] + ": " + why)
                return None
        usd = order_size(tr["usd"], d.get("profile"))
        add = False
    else:
        if pos.get("adds", 0) >= MAX_ADDS:
            return None
        base = float(pos.get("wallet_first_usd") or 0)
        usd = round(float(pos["first_usd"]) * (tr["usd"] / base), 2) if base > 0 else float(pos["first_usd"])
        usd = min(usd, ORDER_USD * SIZE_MAX_X)
        add = True
    if real:
        import live_engine
        import live_trade_state as st
        usd = max(usd, REAL_MIN_ORDER_USD)      # smallest real order the engine accepts
        if not live_engine.ready():
            return None
        if not add and mint in st._load()["positions"]:
            return None                # already held by another robot
        settled = live_engine._execute_buy({"baseToken": {"address": mint, "symbol": mint[:6]}}, mint, mint[:6],
                                           send, chat, live_engine.get_address(), None, "mirror", usd=usd, add=add)
        if not settled or settled.get("status") != "settled_buy":
            return None
        st.set_position_fields(mint, copy_tp=exits["tp"], copy_sl=exits["sl"], copy_max_hold_s=exits["max_hold_s"])
        tokens = 0
        msg = None
    else:
        try:
            lamports = int(usd / _sol_usd() * 1e9)
            tokens = int(_order(SOL_MINT, mint, lamports)["outAmount"])
        except Exception as exc:
            _event("cotação falhou em " + mint[:6] + ": " + type(exc).__name__)
            return None
        cost = usd + NETWORK_FEE_USD
        if d["balance"] < cost:
            _event("saldo da simulação acabou")
            return None
        d["balance"] -= cost
        msg = ("🪞 SIM " + ("reforçou " if add else "comprou ") + mint[:6] + " · US$ " + format(cost, ".2f") +
               " (a carteira pôs US$ " + format(tr["usd"], ".0f") + ")\n" + mint)
    if add:
        pos["adds"] = pos.get("adds", 0) + 1
        pos["tokens"] = int(pos.get("tokens", 0)) + tokens
        pos["cost_usd"] = float(pos["cost_usd"]) + (0 if real else usd + NETWORK_FEE_USD)
        pos["wallet_tokens"] = tr["after"]
    else:
        d["positions"][mint] = {"real": real, "tokens": tokens, "cost_usd": 0 if real else usd + NETWORK_FEE_USD,
                                "first_usd": usd, "wallet_first_usd": tr["usd"], "wallet_tokens": tr["after"],
                                "opened_at": time.time(), "adds": 0, "platform": tr.get("platform"),
                                "sl": exits["sl"], "tp": exits["tp"], "max_hold_s": exits["max_hold_s"]}
    _event(("REAL " if real else "SIM ") + ("reforço " if add else "compra ") + mint[:6] + " US$ " + format(usd, ".2f"))
    return msg


def _sell(d, mint, fraction, reason, send, chat):
    """Sell `fraction` (0-1] of our position. Returns a message or None."""
    pos = d["positions"].get(mint)
    if not pos:
        return None
    full = fraction >= 0.95
    if pos.get("real"):
        import live_engine
        try:
            result = live_engine.sell_now(mint, "mirror_follow" if reason == "mirror_follow" else reason, send, chat,
                                          fraction=None if full else fraction)
        except Exception as exc:
            _event("venda real falhou em " + mint[:6] + ": " + type(exc).__name__)
            return None
        if result in ("sold", "gone") or (full and result == "pending"):
            d["positions"].pop(mint, None)
            d["history"] = (d["history"] + [{"mint": mint, "real": True, "reason": reason, "pnl": None,
                                             "at": time.time()}])[-200:]
        return None                       # the live engine already reported it
    part = int(int(pos["tokens"]) * (1.0 if full else fraction))
    if part <= 0:
        return None
    try:
        value = int(_order(mint, SOL_MINT, part)["outAmount"]) / 1e9 * _sol_usd() - NETWORK_FEE_USD
    except Exception as exc:
        _event("cotação de venda falhou em " + mint[:6] + ": " + type(exc).__name__)
        return None
    cost_part = float(pos["cost_usd"]) * (1.0 if full else fraction)
    pnl = value - cost_part
    d["balance"] += value
    if full:
        d["positions"].pop(mint)
    else:
        pos["tokens"] = int(pos["tokens"]) - part
        pos["cost_usd"] = float(pos["cost_usd"]) - cost_part
    d["history"] = (d["history"] + [{"mint": mint, "real": False, "reason": reason, "pnl": round(pnl, 4),
                                     "fraction": round(1.0 if full else fraction, 3), "at": time.time()}])[-200:]
    why = {"mirror_follow": "a carteira vendeu", "stop_loss": "stop de segurança", "take_profit": "alvo",
           "time_stop": "tempo máximo", "wallet_gone": "a carteira não tem mais a moeda"}.get(reason, reason)
    return ("🪞 SIM " + ("💰 " if pnl > 0 else "🔻 ") + ("vendeu " if full else "vendeu " + format(fraction * 100, ".0f") + "% de ") +
            mint[:6] + " (" + why + "): " + format(pnl, "+.2f") + " USD")


def _sim_safety(d, send, chat):
    """Safety stop / target / max hold for simulated positions (real ones: live exit monitor)."""
    msgs = []
    for mint, pos in list(d["positions"].items()):
        if pos.get("real") or not pos.get("tokens"):
            continue
        try:
            value = int(_order(mint, SOL_MINT, int(pos["tokens"]))["outAmount"]) / 1e9 * _sol_usd()
        except Exception:
            continue
        change = (value - NETWORK_FEE_USD) / float(pos["cost_usd"]) - 1
        reason = None
        if pos.get("sl") and change <= -float(pos["sl"]):
            reason = "stop_loss"
        elif pos.get("tp") and change >= float(pos["tp"]):
            reason = "take_profit"
        elif pos.get("max_hold_s") and time.time() - float(pos["opened_at"]) > float(pos["max_hold_s"]):
            reason = "time_stop"
        if reason:
            m = _sell(d, mint, 1.0, reason, send, chat)
            if m:
                msgs.append(m)
    return msgs


def _sync_real(d):
    """Forget real positions the live engine already closed (safety stop, manual sale)."""
    import live_trade_state as st
    held = st._load()["positions"]
    for mint in [m for m, p in d["positions"].items() if p.get("real") and m not in held]:
        d["positions"].pop(mint)
        last = next((h for h in reversed(st.history()) if h.get("mint") == mint), {})
        d["history"] = (d["history"] + [{"mint": mint, "real": True, "reason": last.get("reason") or "fechada",
                                         "pnl": last.get("pnl_usd"), "at": time.time()}])[-200:]


_last = {"safety": 0.0, "wallet_check": 0.0}


def cycle(send=None, chat=None, rpc=None):
    """One pass: mirror the target's new swaps, then the safety exits. Returns messages."""
    with _CYCLE:
        with _LOCK:
            d = _load()
        if not d["enabled"]:
            return []
        real = bool(d["real"]) and send is not None and chat is not None
        try:
            sol_usd = _sol_usd()
        except Exception:
            return []
        trades = new_trades(d, sol_usd, rpc)
        msgs = []
        for tr in trades:
            if tr["side"] == "buy":
                m = _buy(d, tr, send, chat, real)
            else:
                pos = d["positions"].get(tr["mint"])
                if not pos:
                    continue
                before = tr["before"] or pos.get("wallet_tokens") or 0
                fraction = 1.0 if tr["after"] <= before * 0.02 or before <= 0 else min(1.0, tr["tokens"] / before)
                m = _sell(d, tr["mint"], fraction, "mirror_follow", send, chat)
                if tr["mint"] in d["positions"]:
                    d["positions"][tr["mint"]]["wallet_tokens"] = tr["after"]
            if m:
                msgs.append(m)
        now = time.monotonic()
        if d["positions"] and now - _last["wallet_check"] > WALLET_CHECK_S:
            _last["wallet_check"] = now
            for mint in list(d["positions"]):
                held = wallet_holds(d["target"], mint, rpc)
                if held is not None and held <= float(d["positions"][mint].get("wallet_tokens") or 0) * 0.02:
                    m = _sell(d, mint, 1.0, "wallet_gone", send, chat)   # we missed its sale
                    if m:
                        msgs.append(m)
        if now - _last["safety"] > 10:
            _last["safety"] = now
            msgs += _sim_safety(d, send, chat)
            if real:
                try:
                    _sync_real(d)
                except Exception:
                    pass
        with _LOCK:
            fresh = _load()
            if fresh["target"] != d["target"]:
                return msgs              # target changed meanwhile: drop this pass
            # switches and the measured profile may have changed while we traded
            for k in ("enabled", "real", "profile", "profiled_at"):
                d[k] = fresh[k]
            _save(d)
    return msgs


# ---------------------------------------------------------------- real-time watch
def _ws_url():
    url = os.getenv("SOLANA_WS_URL", "").strip()
    if url:
        return url
    rpc = os.getenv("SOLANA_RPC_URL", "").strip()
    if rpc.startswith("https://"):
        return "wss://" + rpc[len("https://"):]
    return None


def ws_listener():
    """Wake the mirror loop the moment the target signs anything (logsSubscribe)."""
    try:
        import websocket
    except ImportError:
        logging.warning("websocket-client missing: mirror uses polling only")
        return
    while True:
        url = _ws_url()
        if not url or not enabled():
            time.sleep(5)
            continue
        watched = target()
        try:
            ws = websocket.create_connection(url, timeout=30)
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                "params": [{"mentions": [watched]}, {"commitment": "confirmed"}]}))
            WS_STATE.update(connected=True, since=time.time())
            last_ping = time.monotonic()
            while enabled() and target() == watched:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    msg = None
                if msg and '"logsNotification"' in msg:
                    WAKE.set()
                if time.monotonic() - last_ping > 20:
                    ws.ping()
                    last_ping = time.monotonic()
            ws.close()
        except Exception as exc:
            logging.warning("mirror websocket: %s", type(exc).__name__)
        WS_STATE["connected"] = False
        time.sleep(3)


def wait_next():
    """Sleep until the WebSocket sees a new transaction, or the polling interval passes."""
    WAKE.wait(POLL_WS_S if WS_STATE["connected"] else POLL_S)
    WAKE.clear()


def needs_profile():
    with _LOCK:
        d = _load()
    return d["enabled"] and time.time() - float(d.get("profiled_at") or 0) > REPROFILE_S


# ---------------------------------------------------------------- panel
def _pct(x):
    return "-" if x is None else format(x * 100, "+.0f") + "%"


def _mins(m):
    return "-" if m is None else (format(m, ".0f") + " min" if m < 120 else format(m / 60, ".1f") + " h")


def status_text():
    d = _load()        # atomic file: no lock, so the panel answers even during an order
    mode = "REAL" if d["enabled"] and d["real"] else ("SIMULAÇÃO" if d["enabled"] else "DESLIGADO")
    t = d["target"]
    p = d.get("profile") or {}
    ex = p.get("exits") or exits_from_profile(None)
    lines = ["🪞 ESPELHO DE CARTEIRA — " + mode,
             "Carteira: " + t[:6] + "…" + t[-4:] + " · solscan.io/account/" + t,
             "Entra quando ela entra, reforça quando ela reforça, vende a mesma % quando ela vende.",
             "Leitura: " + ("tempo real (WebSocket)" if WS_STATE["connected"] else "a cada " + format(POLL_S, ".0f") + " s")]
    if p.get("positions"):
        plats = ", ".join(list((p.get("platforms") or {}).keys())[:3])
        lines += ["",
                  "🧠 Estratégia medida dela: " + str(p["positions"]) + " operações · acerto " +
                  format((p.get("win_rate") or 0) * 100, ".0f") + "% · lucro " + format(p.get("pnl_usd") or 0, "+.0f") + " USD",
                  "Entrada: US$ " + format(p.get("size_median") or 0, ".0f") + " por moeda · " + (plats or "-") +
                  " · compra em partes " + format((p.get("scale_in") or 0) * 100, ".0f") + "%",
                  "Saída: TP mediano " + _pct(p.get("tp_median")) + " · SL mediano " + _pct(p.get("sl_median")) +
                  " · segura " + _mins(p.get("hold_median")) + " · vende em partes " +
                  format((p.get("scale_out") or 0) * 100, ".0f") + "%"]
    else:
        lines.append("🧠 Estratégia ainda não medida (mede sozinho ao ligar; precisa de SOLANA_RPC_URL).")
    lines += ["",
              "⚙️ Ordem: US$ " + format(ORDER_USD, ".0f") + (" ajustada ao tamanho da compra dela (0,5x a 2x)"
                                                          if SIZE_MODE != "fixo" else " fixa") +
              " · máx. " + str(MAX_POSITIONS) + " posições · " + ("anti-golpe ligado" if ANTI_SCAM else "sem anti-golpe"),
              "🛡️ Segurança (só se a venda dela escapar): stop −" + format(ex["sl"] * 100, ".0f") + "%" +
              (" · alvo +" + format(ex["tp"] * 100, ".0f") + "%" if ex.get("tp") else "") +
              (" · máx. " + _mins(ex["max_hold_s"] / 60) if ex.get("max_hold_s") else ""),
              "Simulação: saldo US$ " + format(d["balance"], ".2f") + " · abertas " + str(len(d["positions"]))]
    hist = [h for h in d.get("history") or [] if h.get("pnl") is not None]
    if hist:
        wins = sum(1 for h in hist if h["pnl"] > 0)
        lines.append("Vendas: " + str(len(hist)) + " (" + str(wins) + " no lucro) · resultado " +
                     format(sum(h["pnl"] for h in hist), "+.2f") + " USD")
    for _, text in list(EVENTS)[-4:]:
        lines.append("• " + text)
    lines.append("⚠️ Você entra segundos depois dela e paga taxa: o resultado não é idêntico ao dela.")
    return "\n".join(lines)
