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

def pnl_summary():
    """Realized profit/loss from confirmed sells (USD)."""
    with _LOCK:
        state = _load()
        if state["day"] != datetime.now(timezone.utc).date().isoformat():
            day = 0.0
        else:
            day = float(state.get("day_pnl_usd", 0.0))
        return {"day_pnl_usd": day, "total_pnl_usd": float(state.get("total_pnl_usd", 0.0)),
                "trades_closed": int(state.get("trades_closed", 0)),
                "trades_won": int(state.get("trades_won", 0))}

def set_position_label(mint, symbol, mode="normal"):
    import time as _time
    with _LOCK:
        state = _load()
        if mint in state["positions"]:
            state["positions"][mint]["symbol"] = str(symbol)[:20]
            state["positions"][mint]["mode"] = mode
            state["positions"][mint]["opened_at"] = _time.time()
            _save(state)

def set_sniper(enabled):
    with _LOCK:
        state = _load()
        state["sniper_enabled"] = bool(enabled)
        _save(state)

def sniper_enabled():
    with _LOCK:
        return bool(_load().get("sniper_enabled", False))

def positions_by_mode(mode):
    with _LOCK:
        return sum(1 for p in _load()["positions"].values() if p.get("mode", "normal") == mode)

def stop():
    with _LOCK:
        state = _load()
        state["enabled"] = False
        state["halted"] = True
        _save(state)
        return status()

def start():
    """Owner pressed start. Refuses while any order is unreconciled."""
    with _LOCK:
        state = _load()
        if state["pending"]:
            raise RuntimeError("Há transação pendente de conciliação")
        state["enabled"] = True
        state["halted"] = False
        _save(state)
        return status()

def _loss_limit(state):
    return float(state.get("max_daily_loss_usd") or MAX_DAILY_LOSS_USD)

def set_daily_loss_limit(usd):
    """Scale the daily loss limit with account size (set once per UTC day)."""
    with _LOCK:
        state = _load()
        today = datetime.now(timezone.utc).date().isoformat()
        if state.get("loss_limit_day") == today:
            return _loss_limit(state)
        state["max_daily_loss_usd"] = max(MAX_DAILY_LOSS_USD, round(float(usd), 2))
        state["loss_limit_day"] = today
        _save(state)
        return state["max_daily_loss_usd"]

def daily_loss_limit():
    with _LOCK:
        return _loss_limit(_load())

def update_peak(mint, ratio):
    with _LOCK:
        state = _load()
        position = state["positions"].get(mint)
        if position is not None and float(ratio) > float(position.get("peak_ratio", 0)):
            position["peak_ratio"] = float(ratio)
            _save(state)

def can_open():
    with _LOCK:
        state = _load()
        if state["day"] != datetime.now(timezone.utc).date().isoformat():
            # Never reset a limit while any settlement is uncertain.
            if state["pending"]:
                return False, "Pending transactions need reconciliation"
            state["day"] = datetime.now(timezone.utc).date().isoformat()
            state["daily_loss_usd"] = 0.0
            state["day_pnl_usd"] = 0.0
            _save(state)
        if not state["enabled"] or state["halted"]:
            return False, "Trading disabled"
        if state["pending"]:
            return False, "Unreconciled transaction"
        if len(state["positions"]) >= MAX_POSITIONS:
            return False, "Position limit"
        if state["daily_loss_usd"] >= _loss_limit(state):
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

def record_settlement(order_id, signature, position=None, realized_loss_usd=0.0, pnl_usd=None):
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
        if pnl_usd is not None:
            state["day_pnl_usd"] = float(state.get("day_pnl_usd", 0.0)) + float(pnl_usd)
            state["total_pnl_usd"] = float(state.get("total_pnl_usd", 0.0)) + float(pnl_usd)
            state["trades_closed"] = int(state.get("trades_closed", 0)) + 1
            state["trades_won"] = int(state.get("trades_won", 0)) + (1 if pnl_usd > 0 else 0)
        del state["pending"][order_id]
        if state["daily_loss_usd"] >= _loss_limit(state):
            state["enabled"] = False
            state["halted"] = True
        _save(state)

def annotate_pending(order_id, **fields):
    """Durably store recovery metadata before advancing execution stage."""
    allowed = {"signature", "request_id", "before_token_raw", "before_sol_raw",
               "input_amount_raw", "stage", "cost_usd"}
    if set(fields) - allowed:
        raise ValueError("Unsupported pending metadata")
    with _LOCK:
        data = _load()
        pending = data["pending"].get(order_id)
        if not pending:
            raise RuntimeError("Pending order missing")
        pending.update(fields)
        _save(data)

def pending_orders():
    with _LOCK:
        return {key: dict(value) for key, value in _load()["pending"].items()}

def reconcile_known_signature(order_id, owner):
    """Read-only finalized reconciliation. Never retry ambiguous submissions."""
    from solana_settlement import get_token_balance, get_sol_balance
    from live_swap_executor import rpc
    with _LOCK:
        data = _load()
        pending = data["pending"].get(order_id)
        if not pending or not pending.get("signature"):
            return {"status": "manual_review", "reason": "Missing signature"}
        signature = pending["signature"]
        tx = rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if not tx:
            return {"status": "waiting_finality", "signature": signature}
        # Do not reconcile an unrelated signature against the owner's balances.
        message = (tx.get("transaction") or {}).get("message") or {}
        keys = message.get("accountKeys") or []
        owner_signed = any(
            (k.get("pubkey") == owner and k.get("signer") is True)
            for k in keys if isinstance(k, dict)
        )
        if not owner_signed:
            return {"status": "manual_review", "reason": "Transaction not signed by tracked wallet", "signature": signature}
        if tx.get("meta", {}).get("err") is not None:
            return {"status": "manual_review", "reason": "On-chain failure; verify fees and balances", "signature": signature}
        before_token = pending.get("before_token_raw")
        before_sol = pending.get("before_sol_raw")
        if before_token is None or before_sol is None:
            return {"status": "manual_review", "reason": "Missing pre-trade balances", "signature": signature}
        mint = pending["mint"]
        after_token = get_token_balance(owner, mint)
        after_sol = get_sol_balance(owner)
        if pending["side"] == "buy":
            acquired = after_token - int(before_token)
            if acquired <= 0 or after_sol >= int(before_sol):
                return {"status": "manual_review", "reason": "Balance mismatch", "signature": signature}
            cost = float(pending.get("cost_usd", 10.0))
            position = {"token_amount_raw": acquired, "cost_usd": cost, "buy_signature": signature}
            record_settlement(order_id, signature, position=position)
            return {"status": "settled_buy", "signature": signature, "token_amount_raw": acquired}
        sold = int(before_token) - after_token
        if sold != int(pending.get("input_amount_raw", -1)) or after_sol <= int(before_sol):
            return {"status": "manual_review", "reason": "Sell balance mismatch", "signature": signature}
        cost = float(data["positions"].get(mint, {}).get("cost_usd", 0) or 0)
        try:
            proceeds_usd = _sol_to_usd(after_sol - int(before_sol))
        except Exception:
            proceeds_usd = None
        if proceeds_usd is None or cost <= 0:
            # Cannot value the sale: settle tokens but halt for manual PnL review.
            record_settlement(order_id, signature)
            data = _load()
            data["enabled"] = False
            data["halted"] = True
            _save(data)
            return {"status": "settled_sell_review_pnl", "reason": "PnL not valued", "signature": signature}
        pnl = proceeds_usd - cost
        record_settlement(order_id, signature, realized_loss_usd=max(0.0, -pnl), pnl_usd=pnl)
        return {"status": "settled_sell", "signature": signature, "pnl_usd": pnl,
                "cost_usd": cost, "proceeds_usd": proceeds_usd,
                "sol_received": (after_sol - int(before_sol)) / 1_000_000_000}

def _sol_to_usd(lamports):
    """Value SOL received from a sale with an executable Jupiter SOL->USDC quote."""
    from jupiter_v2_executor import order, SOL_MINT, USDC_MINT
    if int(lamports) <= 0:
        return 0.0
    return int(order(SOL_MINT, USDC_MINT, int(lamports))["outAmount"]) / 1_000_000


def discard_pending(order_id):
    """Drop a reservation whose transaction verifiably never changed balances."""
    with _LOCK:
        data = _load()
        data["pending"].pop(order_id, None)
        _save(data)


def _age_seconds(pending):
    try:
        created = datetime.fromisoformat(pending["created_at"])
        return (datetime.now(timezone.utc) - created).total_seconds()
    except Exception:
        return 1e9


def _reconcile_by_balance(order_id, pending, owner):
    """Settle from on-chain balances once any in-flight transaction has expired."""
    from solana_settlement import get_token_balance, get_sol_balance
    signature = pending.get("signature") or "balance-check"
    mint = pending["mint"]
    before_token = pending.get("before_token_raw")
    before_sol = pending.get("before_sol_raw")
    if before_token is None or before_sol is None:
        discard_pending(order_id)  # failed before anything was sent
        return {"status": "discarded", "mint": mint, "side": pending["side"]}
    token_now = get_token_balance(owner, mint)
    sol_now = get_sol_balance(owner)
    if pending["side"] == "buy":
        acquired = token_now - int(before_token)
        if acquired <= 0:
            discard_pending(order_id)
            return {"status": "discarded", "mint": mint, "side": "buy"}
        position = {"token_amount_raw": acquired, "cost_usd": float(pending.get("cost_usd") or 0) or 10.0,
                    "buy_signature": signature}
        record_settlement(order_id, signature, position=position)
        return {"status": "settled_buy", "mint": mint, "signature": signature, "token_amount_raw": acquired}
    sold = int(before_token) - token_now
    if sold <= 0:
        discard_pending(order_id)  # sell never executed; position stays and will be retried
        return {"status": "discarded", "mint": mint, "side": "sell"}
    with _LOCK:
        data = _load()
        position = dict(data["positions"].get(mint, {}))
    cost = float(position.get("cost_usd", 0) or 0)
    held = int(position.get("token_amount_raw") or before_token) or 1
    sold_fraction = min(1.0, sold / held)
    cost_sold = cost * sold_fraction
    try:
        proceeds = _sol_to_usd(max(0, sol_now - int(before_sol)))
    except Exception:
        proceeds = None
    pnl = None if proceeds is None or cost_sold <= 0 else proceeds - cost_sold
    record_settlement(order_id, signature, realized_loss_usd=max(0.0, -(pnl or 0.0)), pnl_usd=pnl)
    leftover = held - sold
    if leftover > held * 0.05 and token_now > 0:  # partial fill: keep tracking the rest
        with _LOCK:
            data = _load()
            position.update(token_amount_raw=min(leftover, token_now), cost_usd=cost - cost_sold)
            data["positions"][mint] = position
            _save(data)
    return {"status": "settled_sell", "mint": mint, "signature": signature, "pnl_usd": pnl or 0.0,
            "cost_usd": cost_sold, "proceeds_usd": proceeds or 0.0, "partial": leftover > held * 0.05}


EXPIRY_SECONDS = 150  # a Solana transaction cannot land after its blockhash expires (~90 s)


def auto_reconcile(owner, min_age_seconds=None):
    """Resolve pending orders without manual help. Never resends anything."""
    min_age = EXPIRY_SECONDS if min_age_seconds is None else min_age_seconds
    results = []
    for order_id, pending in pending_orders().items():
        if pending.get("signature"):
            try:
                result = reconcile_known_signature(order_id, owner)
            except Exception as exc:
                result = {"status": "error", "error": type(exc).__name__ + ": " + str(exc)[:150]}
            if result.get("status", "").startswith("settled"):
                result["mint"] = pending["mint"]
                results.append(result)
                continue
        if _age_seconds(pending) < min_age:
            results.append({"status": "waiting", "mint": pending["mint"],
                            "seconds_left": int(min_age - _age_seconds(pending))})
            continue
        try:
            results.append(_reconcile_by_balance(order_id, pending, owner))
        except Exception as exc:
            results.append({"status": "error", "mint": pending["mint"],
                            "error": type(exc).__name__ + ": " + str(exc)[:150]})
    return results
