"""Launch sniper: pump.fun tokens confirmed by real early buying, sold fast.

How it works
- Listens to the pump.fun program through the RPC websocket (logsSubscribe) and
  decodes CreateEvent (new token) and TradeEvent (every buy/sell) live.
- Static checks at creation: metadata must list a website or socials, and the
  creator must not be a serial launcher. Pump.fun itself does not let the
  creator mint more or freeze holders.
- Confirmation window: instead of buying blind at second zero (where most rugs
  happen), it follows each token for CONFIRM_SECONDS and only buys if real,
  broad demand shows up: many different buyers, real SOL coming in, few sells,
  no single wallet dominating the buys (bundle), and the creator has not sold.
- Exits (checked every ~2.5 s): creator sells -> sell at once; trailing stop
  near the peak once in profit; early cut if it falls or does not move; stop
  loss; hard take profit; time limit.

Very high risk: most brand-new tokens die or are rugged within minutes.
The decision is taken after watching the token for 3 minutes (LAUNCH_CONFIRM_SECONDS).
"""
import base64
import hashlib
import json
import logging
import os
import queue
import struct
import threading
import time

import requests

import live_engine as engine
import live_trade_state as state

LOG = logging.getLogger("launch_sniper")
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_EVENT = hashlib.sha256(b"event:CreateEvent").digest()[:8]
TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]


def _env(name, default):
    return float(os.getenv(name, str(default)))


LAUNCH_FRACTION = _env("LAUNCH_FRACTION", 0.12)

# Confirmation (all measured inside the first CONFIRM_SECONDS, creator excluded)
# Watch each new token for 3 minutes before deciding (the first minutes are
# where most rugs happen); thresholds are sized for a 3-minute window.
CONFIRM_SECONDS = int(_env("LAUNCH_CONFIRM_SECONDS", 180))
CONFIRM_WINDOW = int(_env("LAUNCH_CONFIRM_WINDOW", 120))  # still buyable until 5 min of age
MIN_BUYS = int(_env("LAUNCH_MIN_BUYS", 40))
MIN_BUYERS = int(_env("LAUNCH_MIN_BUYERS", 30))
MIN_SOL_IN = _env("LAUNCH_MIN_SOL_IN", 5.0)          # SOL bought by others
MIN_NET_SOL = _env("LAUNCH_MIN_NET_SOL", 2.5)        # bought minus sold: money is staying in
MAX_SELL_RATIO = _env("LAUNCH_MAX_SELL_RATIO", 0.5)   # sells <= 50% of buys
MAX_TOP_BUYER = _env("LAUNCH_MAX_TOP_BUYER", 0.35)    # one wallet <= 35% of the SOL bought

# Exits
LAUNCH_STOP = _env("LAUNCH_STOP", 0.15)               # -15%
# Sell everything as soon as there is a small profit after fees: these tokens
# often spike right after the buy and then dump. The ratio uses the executable
# sell quote (sell fees/impact included) against the cost (buy fees included).
LAUNCH_TAKE_PROFIT = _env("LAUNCH_TAKE_PROFIT", 0.03)  # +3% net
LAUNCH_TRAIL_ARM = _env("LAUNCH_TRAIL_ARM", 0.10)      # from +10% ...
LAUNCH_TRAIL_DROP = _env("LAUNCH_TRAIL_DROP", 0.06)    # ... sell 6% below the peak
LAUNCH_EARLY_DROP = _env("LAUNCH_EARLY_DROP", 0.10)    # before profit: 10% below the best value -> out
LAUNCH_NO_MOVE_SECONDS = int(_env("LAUNCH_NO_MOVE_SECONDS", 25))
LAUNCH_MIN_MOVE = _env("LAUNCH_MIN_MOVE", 0.04)        # must reach +4% within 25 s
LAUNCH_HOLD_SECONDS = int(_env("LAUNCH_HOLD_SECONDS", 120))

LAUNCH_MAX_AGE_SECONDS = 10     # static check must start within 10 s of creation
LAUNCH_MIN_GAP_SECONDS = 20     # at most one buy every 20 s
CREATOR_MAX_PER_HOUR = 2        # serial launchers are skipped
TRACK_TTL_SECONDS = 900

_queue = queue.Queue(maxsize=200)
_creators = {}                  # creator -> [timestamps]
TRACK = {}                      # mint -> live stats of a recently created token
_track_lock = threading.Lock()
STATS = {"seen": 0, "bought": 0, "last": "", "reasons": {}, "connected": False, "error": ""}
_started = False
_last_buy = 0.0


def enabled():
    return state.flag("launch_enabled")


def ws_url():
    explicit = os.getenv("SOLANA_WS_URL", "").strip()
    if explicit:
        return explicit
    rpc = os.getenv("SOLANA_RPC_URL", "").strip()
    return "wss://" + rpc[len("https://"):] if rpc.startswith("https://") else ""


# ---------------------------------------------------------------- decoding
def _read_string(data, offset):
    (length,) = struct.unpack_from("<I", data, offset)
    if length > 200:
        raise ValueError("string too long")
    offset += 4
    return data[offset:offset + length].decode("utf-8", "replace"), offset + length


def decode_create_event(raw):
    """Decode pump.fun CreateEvent bytes -> dict or None."""
    from solders.pubkey import Pubkey
    if len(raw) < 8 + 12 + 96 or raw[:8] != CREATE_EVENT:
        return None
    offset = 8
    name, offset = _read_string(raw, offset)
    symbol, offset = _read_string(raw, offset)
    uri, offset = _read_string(raw, offset)
    keys = []
    for _ in range(3):
        keys.append(str(Pubkey.from_bytes(raw[offset:offset + 32])))
        offset += 32
    mint, bonding_curve, user = keys
    return {"name": name, "symbol": symbol, "uri": uri, "mint": mint,
            "bonding_curve": bonding_curve, "creator": user}


def decode_trade_event(raw):
    """Decode pump.fun TradeEvent: mint, sol_amount, token_amount, is_buy, user."""
    from solders.pubkey import Pubkey
    if len(raw) < 8 + 32 + 8 + 8 + 1 + 32 or raw[:8] != TRADE_EVENT:
        return None
    mint = str(Pubkey.from_bytes(raw[8:40]))
    sol_amount, token_amount = struct.unpack_from("<QQ", raw, 40)
    is_buy = raw[56]
    if is_buy not in (0, 1):
        return None
    user = str(Pubkey.from_bytes(raw[57:89]))
    return {"mint": mint, "sol": sol_amount / 1e9, "tokens": token_amount, "is_buy": bool(is_buy), "user": user}


def _program_data(logs):
    for line in logs or []:
        if line.startswith("Program data: "):
            try:
                yield base64.b64decode(line[len("Program data: "):])
            except Exception:
                continue


def events_from_logs(logs):
    """CreateEvents in a transaction's logs."""
    if not any("Instruction: Create" in line for line in logs or []):
        return []
    out = []
    for raw in _program_data(logs):
        try:
            event = decode_create_event(raw)
        except Exception:
            event = None
        if event:
            out.append(event)
    return out


def trades_from_logs(logs):
    out = []
    for raw in _program_data(logs):
        try:
            trade = decode_trade_event(raw)
        except Exception:
            trade = None
        if trade:
            out.append(trade)
    return out


# ---------------------------------------------------------------- tracking
def track_create(event, now=None):
    now = time.time() if now is None else now
    with _track_lock:
        TRACK[event["mint"]] = {"event": event, "created": now, "static": None, "decided": False,
                                "buys": 0, "sells": 0, "buyers": set(), "sol_in": 0.0, "sol_out": 0.0,
                                "by_user": {}, "creator_sold": False}


def track_trade(trade):
    with _track_lock:
        t = TRACK.get(trade["mint"])
        if not t:
            return
        creator = t["event"]["creator"]
        if trade["user"] == creator:
            if not trade["is_buy"]:
                t["creator_sold"] = True
            return  # creator's own buys do not count as demand
        if trade["is_buy"]:
            t["buys"] += 1
            t["buyers"].add(trade["user"])
            t["sol_in"] += trade["sol"]
            t["by_user"][trade["user"]] = t["by_user"].get(trade["user"], 0.0) + trade["sol"]
        else:
            t["sells"] += 1
            t["sol_out"] += trade["sol"]


def confirm_reasons(t):
    """Why a tracked token is NOT confirmed (empty list = buy)."""
    reasons = []
    if t["creator_sold"]:
        reasons.append("criador vendeu")
    if t["buys"] < MIN_BUYS or len(t["buyers"]) < MIN_BUYERS:
        reasons.append("poucos compradores")
    if t["sol_in"] < MIN_SOL_IN:
        reasons.append("pouco SOL entrando")
    if t["sol_in"] - t["sol_out"] < MIN_NET_SOL:
        reasons.append("dinheiro saindo (vendas comendo as compras)")
    if t["buys"] and t["sells"] > t["buys"] * MAX_SELL_RATIO:
        reasons.append("muita venda")
    top = max(t["by_user"].values(), default=0.0)
    if t["sol_in"] > 0 and top / t["sol_in"] > MAX_TOP_BUYER:
        reasons.append("uma carteira domina as compras (bundle)")
    return reasons


def creator_sold(mint):
    with _track_lock:
        t = TRACK.get(mint)
        return bool(t and t["creator_sold"])


def _cleanup(now):
    held = set(state._load()["positions"])
    with _track_lock:
        for mint in [m for m, t in TRACK.items() if now - t["created"] > TRACK_TTL_SECONDS and m not in held]:
            TRACK.pop(mint, None)


# ---------------------------------------------------------------- static filters
def has_socials(uri, timeout=2.5):
    """Metadata JSON must list a website, X/Twitter or Telegram."""
    if not uri.startswith(("https://", "http://", "ipfs://")):
        return False
    if uri.startswith("ipfs://"):
        uri = "https://ipfs.io/ipfs/" + uri[len("ipfs://"):]
    try:
        meta = requests.get(uri, timeout=timeout).json()
    except Exception:
        return False
    links = [meta.get(k) or (meta.get("extensions") or {}).get(k) for k in ("twitter", "website", "telegram")]
    return any(isinstance(x, str) and x.startswith("http") for x in links)


def creator_ok(creator, now=None):
    now = time.time() if now is None else now
    recent = [t for t in _creators.get(creator, []) if now - t < 3600]
    recent.append(now)
    _creators[creator] = recent
    if len(_creators) > 20000:
        _creators.clear()
    return len(recent) <= CREATOR_MAX_PER_HOUR


def reject_reason(event, now=None):
    """Static checks at creation time; empty string = keep watching."""
    now = time.time() if now is None else now
    if now - event.get("seen_at", now) > LAUNCH_MAX_AGE_SECONDS:
        return "vista tarde demais"
    if not event["name"].strip() or not event["symbol"].strip() or len(event["symbol"]) > 12:
        return "nome/símbolo inválido"
    if not creator_ok(event["creator"], now):
        return "criador lançou várias moedas na última hora"
    if not has_socials(event["uri"]):
        return "sem site/redes sociais"
    return ""


def _count(reason):
    STATS["reasons"][reason] = STATS["reasons"].get(reason, 0) + 1


# ---------------------------------------------------------------- exits
def exit_decision(ratio, peak, held_seconds, dev_sold=False):
    """Return a sell reason or None. ratio/peak are value/cost."""
    if dev_sold:
        return "launch_dev_sold"
    if ratio <= 1 - LAUNCH_STOP:
        return "stop_loss"
    if ratio >= 1 + LAUNCH_TAKE_PROFIT:
        return "launch_profit"
    if peak >= 1 + LAUNCH_TRAIL_ARM:
        if ratio <= peak * (1 - LAUNCH_TRAIL_DROP):
            return "launch_peak"
    elif ratio <= peak * (1 - LAUNCH_EARLY_DROP):
        return "launch_drop"
    if held_seconds >= LAUNCH_NO_MOVE_SECONDS and peak < 1 + LAUNCH_MIN_MOVE:
        return "launch_flat"
    if held_seconds >= LAUNCH_HOLD_SECONDS:
        return "launch_time"
    return None


def watch_and_sell(mint, send, chat, opened_at, check_every=2.5):
    """Follow the position and sell by the exit rules."""
    from live_exit_monitor import quote_value_usd
    peak = 0.0
    while True:
        position = state._load()["positions"].get(mint)
        if not position:
            return "gone"
        held = time.time() - opened_at
        reason = None
        try:
            ratio = quote_value_usd(mint, int(position["token_amount_raw"])) / float(position["cost_usd"])
            peak = max(peak, ratio)
            reason = exit_decision(ratio, peak, held, creator_sold(mint))
        except Exception:
            if creator_sold(mint) or held >= LAUNCH_HOLD_SECONDS:
                reason = "launch_dev_sold" if creator_sold(mint) else "launch_time"
        if reason:
            for attempt in range(4):
                try:
                    return engine.sell_now(mint, reason, send, chat)
                except Exception as exc:
                    if attempt == 3:
                        engine._failure(send, chat, "venda", mint, exc)
                    time.sleep(2)
            return "failed"
        time.sleep(check_every)


# ---------------------------------------------------------------- buying
def _buy_and_watch(event, send, chat):
    global _last_buy
    if time.time() - _last_buy < LAUNCH_MIN_GAP_SECONDS:
        _count("intervalo mínimo entre compras")
        return None
    if not engine.ready():
        _count("robô desligado/bloqueado")
        return None
    allowed, _why = state.can_open()
    if not allowed or state.positions_by_mode("launch") >= 1:
        _count("limite de posições/perda do dia" if not allowed else "já tem lançamento aberto")
        return None
    _last_buy = time.time()
    pair = {"baseToken": {"address": event["mint"], "symbol": event["symbol"][:20]}}
    settled = engine._execute_buy(pair, event["mint"], event["symbol"][:20], send, chat,
                                  engine.get_address(), LAUNCH_FRACTION, "launch")
    if not settled or settled.get("status") != "settled_buy":
        _count("compra não executada")
        return None
    STATS["bought"] += 1
    STATS["last"] = event["symbol"][:20]
    return watch_and_sell(event["mint"], send, chat, time.time())


def decide_once(send, chat, now=None):
    """Buy the first tracked token whose confirmation window shows real demand."""
    now = time.time() if now is None else now
    with _track_lock:
        ready = [(m, t) for m, t in TRACK.items()
                 if t["static"] == "" and not t["decided"] and now - t["created"] >= CONFIRM_SECONDS]
    for mint, t in sorted(ready, key=lambda mt: -mt[1]["sol_in"]):
        reasons = confirm_reasons(t)
        expired = now - t["created"] > CONFIRM_SECONDS + CONFIRM_WINDOW
        if reasons and not expired:
            continue  # keep watching inside the window
        t["decided"] = True
        if reasons:
            _count(reasons[0])
            continue
        return _buy_and_watch(t["event"], send, chat)
    return None


def static_worker():
    while True:
        event = _queue.get()
        try:
            reason = reject_reason(event)
            with _track_lock:
                t = TRACK.get(event["mint"])
                if t:
                    t["static"] = reason
                    t["decided"] = bool(reason)
            if reason:
                _count(reason)
        except Exception:
            LOG.exception("launch static check failed")


def decision_loop(send_fn, chat_fn):
    while True:
        time.sleep(1)
        try:
            chat = chat_fn()
            if chat and enabled():
                decide_once(send_fn, chat)
            _cleanup(time.time())
        except Exception:
            LOG.exception("launch decision failed")


def listener():
    import websocket
    while True:
        url = ws_url()
        if not url or not enabled():
            STATS["connected"] = False
            time.sleep(5)
            continue
        try:
            ws = websocket.create_connection(url, timeout=30)
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}]}))
            STATS.update(connected=True, error="")
            while enabled():
                msg = json.loads(ws.recv())
                value = (((msg.get("params") or {}).get("result") or {}).get("value") or {})
                if value.get("err"):
                    continue
                logs = value.get("logs")
                for event in events_from_logs(logs):
                    STATS["seen"] += 1
                    event["seen_at"] = time.time()
                    track_create(event)
                    try:
                        _queue.put_nowait(event)
                    except queue.Full:
                        pass
                for trade in trades_from_logs(logs):
                    track_trade(trade)
            ws.close()
        except Exception as exc:
            STATS.update(connected=False, error=type(exc).__name__)
            LOG.warning("launch websocket error: %s", type(exc).__name__)
            time.sleep(5)


def start(send_fn, chat_fn):
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=listener, daemon=True).start()
    threading.Thread(target=static_worker, daemon=True).start()
    threading.Thread(target=decision_loop, args=(send_fn, chat_fn), daemon=True).start()


def status_line():
    top = sorted(STATS["reasons"].items(), key=lambda kv: -kv[1])[:4]
    conn = "conectado" if STATS["connected"] else ("sem conexão" + (" (" + STATS["error"] + ")" if STATS["error"] else ""))
    return ("🆕 Lançamento (" + format(LAUNCH_FRACTION * 100, ".0f") + "% da conta): " +
            ("LIGADO" if enabled() else "DESLIGADO") + " · " + conn +
            " · moedas criadas vistas: " + str(STATS["seen"]) + " · compras: " + str(STATS["bought"]) +
            ((" · última: " + STATS["last"]) if STATS["last"] else "") +
            ("\n   recusas: " + "; ".join(str(n) + "× " + r for r, n in top) if top else ""))
