"""Launch sniper: buy pump.fun tokens seconds after they are created, sell within 1 minute.

How it works
- Listens to the pump.fun program through the RPC websocket (logsSubscribe) and
  decodes each CreateEvent (name, symbol, metadata URI, mint, creator) the moment
  a token is created.
- Fast filters only (there is no history yet): metadata must list a website or
  socials, and creators who launched several tokens in the last hour are skipped.
  Pump.fun itself does not let the creator mint more or freeze holders.
- Buys LAUNCH_FRACTION of the account through the normal live engine (Jupiter),
  then watches the price every ~2.5 s and sells AS SOON AS it is in profit after
  fees (+1%), or when: it falls 8% below its best
  value (near the peak if in profit), it did not rise +5% in the first 15 s,
  it hits the -20% stop, or LAUNCH_HOLD_SECONDS is over.

Very high risk: most brand-new tokens die or are rugged within minutes.
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

LAUNCH_FRACTION = float(os.getenv("LAUNCH_FRACTION", "0.12"))
LAUNCH_HOLD_SECONDS = int(os.getenv("LAUNCH_HOLD_SECONDS", "60"))
LAUNCH_STOP = float(os.getenv("LAUNCH_STOP", "0.20"))          # sell at -20%
LAUNCH_TRAIL_ARM = float(os.getenv("LAUNCH_TRAIL_ARM", "0.15"))  # from +15% ...
LAUNCH_TRAIL_DROP = float(os.getenv("LAUNCH_TRAIL_DROP", "0.08"))  # sell 8% below the best value (also before profit)
# Take profit as soon as the executable sell value beats the cost by this much.
# cost_usd already includes the buy fees and the Jupiter sell quote includes the
# sell fees/price impact; the margin covers the network fee and a quote moving.
LAUNCH_TAKE_PROFIT = float(os.getenv("LAUNCH_TAKE_PROFIT", "0.01"))
LAUNCH_NO_MOVE_SECONDS = int(os.getenv("LAUNCH_NO_MOVE_SECONDS", "15"))  # if it did not go up fast...
LAUNCH_MIN_MOVE = float(os.getenv("LAUNCH_MIN_MOVE", "0.05"))           # ...(at least +5%), sell
LAUNCH_MAX_AGE_SECONDS = 8      # skip a launch we saw too late
LAUNCH_MIN_GAP_SECONDS = 20     # at most one buy every 20 s
CREATOR_MAX_PER_HOUR = 2        # serial launchers are skipped

_queue = queue.Queue(maxsize=50)
_creators = {}                  # creator -> [timestamps]
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


def events_from_logs(logs):
    out = []
    if not any("Instruction: Create" in line for line in logs or []):
        return out
    for line in logs:
        if line.startswith("Program data: "):
            try:
                event = decode_create_event(base64.b64decode(line[len("Program data: "):]))
            except Exception:
                event = None
            if event:
                out.append(event)
    return out


# ---------------------------------------------------------------- filters
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
    """Fast checks at creation time; empty string = buy."""
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
def exit_decision(ratio, peak, held_seconds):
    """Return a sell reason or None. ratio/peak are value/cost."""
    if ratio <= 1 - LAUNCH_STOP:
        return "stop_loss"
    if ratio >= 1 + LAUNCH_TAKE_PROFIT:
        return "launch_profit"
    if ratio <= peak * (1 - LAUNCH_TRAIL_DROP):
        # started falling: near the peak if it was in profit, otherwise cut early
        return "launch_peak" if peak >= 1 + LAUNCH_TRAIL_ARM else "launch_drop"
    if held_seconds >= LAUNCH_NO_MOVE_SECONDS and peak < 1 + LAUNCH_MIN_MOVE:
        return "launch_flat"
    if held_seconds >= LAUNCH_HOLD_SECONDS:
        return "launch_time"
    return None


def watch_and_sell(mint, send, chat, opened_at, check_every=2.5):
    """Follow the position for up to LAUNCH_HOLD_SECONDS and sell near the peak."""
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
            reason = exit_decision(ratio, peak, held)
        except Exception:
            if held >= LAUNCH_HOLD_SECONDS:
                reason = "launch_time"
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


# ---------------------------------------------------------------- loops
def _handle(event, send, chat):
    global _last_buy
    reason = reject_reason(event)
    if reason:
        _count(reason)
        return None
    if time.time() - _last_buy < LAUNCH_MIN_GAP_SECONDS:
        _count("intervalo mínimo entre compras")
        return None
    if not engine.ready():
        _count("robô desligado/bloqueado")
        return None
    allowed, why = state.can_open()
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


def worker(send_fn, chat_fn):
    while True:
        event = _queue.get()
        try:
            chat = chat_fn()
            if chat and enabled():
                _handle(event, send_fn, chat)
        except Exception:
            LOG.exception("launch handling failed")


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
                for event in events_from_logs(value.get("logs")):
                    STATS["seen"] += 1
                    event["seen_at"] = time.time()
                    try:
                        _queue.put_nowait(event)
                    except queue.Full:
                        pass
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
    threading.Thread(target=worker, args=(send_fn, chat_fn), daemon=True).start()


def status_line():
    top = sorted(STATS["reasons"].items(), key=lambda kv: -kv[1])[:3]
    conn = "conectado" if STATS["connected"] else ("sem conexão" + (" (" + STATS["error"] + ")" if STATS["error"] else ""))
    return ("🆕 Lançamento (" + format(LAUNCH_FRACTION * 100, ".0f") + "% da conta): " +
            ("LIGADO" if enabled() else "DESLIGADO") + " · " + conn +
            " · moedas criadas vistas: " + str(STATS["seen"]) + " · compras: " + str(STATS["bought"]) +
            ((" · última: " + STATS["last"]) if STATS["last"] else "") +
            ("\n   recusas: " + "; ".join(str(n) + "× " + r for r, n in top) if top else ""))
