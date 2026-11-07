"""Durable, fail-closed trading state. Does not authorize or execute transfers."""
import json, os, tempfile, threading
from pathlib import Path
from datetime import datetime, timezone

_LOCK = threading.RLock()
MAX_POSITIONS = 3
ORDER_USD = 10.0
MAX_DAILY_LOSS_USD = 3.0

def _path():
    wallet = os.environ.get("BOT_WALLET_FILE", "")
    if not wallet or not Path(wallet).is_absolute():
        raise RuntimeError("Persistent wallet path required")
    return Path(wallet).with_name("live_trading_state.json")

def _default():
    return {"version": 1, "enabled": False, "positions": {}, "pending": {},
            "daily_loss_usd": 0.0, "day": datetime.now(timezone.utc).date().isoformat(),
            "halted": True}

def _load():
    path = _path()
    if not path.exists():
        return _default()
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise RuntimeError("Unknown trading state version")
    return state

def _save(state):
    path = _path()
    if not path.parent.is_dir():
        raise RuntimeError("Persistent disk missing")
    fd, name = tempfile.mkstemp(prefix=".trading-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def status():
    with _LOCK:
        state = _load()
        return {"enabled": bool(state["enabled"]), "halted": bool(state["halted"]),
                "positions": len(state["positions"]), "pending": len(state["pending"]),
                "daily_loss_usd": float(state["daily_loss_usd"])}

def stop():
    with _LOCK:
        state = _load()
        state["enabled"] = False
        state["halted"] = True
        _save(state)
        return status()

def can_open():
    with _LOCK:
        state = _load()
        if state["day"] != datetime.now(timezone.utc).date().isoformat():
            # Never reset a limit while any settlement is uncertain.
            if state["pending"]:
                return False, "Pending transactions need reconciliation"
            state["day"] = datetime.now(timezone.utc).date().isoformat()
            state["daily_loss_usd"] = 0.0
            _save(state)
        if not state["enabled"] or state["halted"]:
            return False, "Trading disabled"
        if state["pending"]:
            return False, "Unreconciled transaction"
        if len(state["positions"]) >= MAX_POSITIONS:
            return False, "Position limit"
        if state["daily_loss_usd"] >= MAX_DAILY_LOSS_USD:
            return False, "Daily loss limit"
        return True, "Ready"

def reserve(order_id, mint, side):
    """Reserve before signing; pending entries require explicit chain reconciliation."""
    if side not in ("buy", "sell") or not order_id or not mint:
        raise ValueError("Invalid order")
    with _LOCK:
        allowed, reason = can_open() if side == "buy" else (True, "")
        if not allowed:
            raise RuntimeError(reason)
        state = _load()
        if state["pending"] or order_id in state["positions"]:
            raise RuntimeError("Order already pending or duplicated")
        state["pending"][order_id] = {"mint": mint, "side": side,
            "created_at": datetime.now(timezone.utc).isoformat()}
        _save(state)

def record_settlement(order_id, signature, position=None, realized_loss_usd=0.0):
    """Call only after independently verifying finalized on-chain outcome."""
    with _LOCK:
        state = _load()
        pending = state["pending"].get(order_id)
        if not pending or not signature:
            raise RuntimeError("No pending order or missing signature")
        if position is not None:
            state["positions"][pending["mint"]] = position
        if pending["side"] == "sell":
            state["positions"].pop(pending["mint"], None)
        state["daily_loss_usd"] += max(0.0, float(realized_loss_usd))
        del state["pending"][order_id]
        if state["daily_loss_usd"] >= MAX_DAILY_LOSS_USD:
            state["enabled"] = False
            state["halted"] = True
        _save(state)
