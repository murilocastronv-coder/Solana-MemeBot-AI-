"""Durable, fail-closed trading state. Does not authorize or execute transfers."""
import json, os, tempfile, threading
from pathlib import Path
from datetime import datetime, timezone

_LOCK = threading.RLock()
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "12"))   # all real open orders together (copy + robot)
ORDER_USD = 10.0
MAX_DAILY_LOSS_USD = 3.0
# Fixed daily loss limit for spot/sniper trading (owner choice); env overrides.
# No daily loss limit by default (owner's choice). Set DAILY_LOSS_LIMIT_ENABLED=true to use
# DAILY_LOSS_LIMIT_USD again.
DAILY_LOSS_LIMIT_ENABLED = os.getenv("DAILY_LOSS_LIMIT_ENABLED", "false").strip().lower() == "true"
DAILY_LOSS_LIMIT_USD = float(os.getenv("DAILY_LOSS_LIMIT_USD", "50")) if DAILY_LOSS_LIMIT_ENABLED else float("inf")

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
    # Read-only: the file is replaced atomically, so no need to wait for the lock (a reconciliation
    # can hold it while it reads the blockchain); the panel answers at once.
    state = _load()
    return {"enabled": bool(state["enabled"]), "halted": bool(state["halted"]),
            "positions": len(state["positions"]), "pending": len(state["pending"]),
            "daily_loss_usd": float(state["daily_loss_usd"])}

def pnl_summary():
    """Realized profit/loss from confirmed sells (USD). Read-only, never waits for the lock."""
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

def set_flag(name, value):
    with _LOCK:
        state = _load()
        state[name] = bool(value)
        _save(state)

def flag(name):
    with _LOCK:
        return bool(_load().get(name, False))

def _env_on(name):
    """MEV switches default to ON; set the variable to "false" to turn it off."""
    return os.getenv(name, "true").strip().lower() != "false"

def mev_autotrade_allowed():
    return _env_on("MEV_AUTOTRADE_ENABLED")

def mev_live_allowed():
    return mev_autotrade_allowed() and _env_on("MEV_LIVE_ENABLED")

MEV_DEFAULTS = {"input_lamports": 50_000_000, "min_profit_bps": 10, "jito_tip_lamports": 10_000}


def mev_config():
    """Persisted settings for the Telegram MEV controls."""
    defaults = dict(MEV_DEFAULTS)
    with _LOCK:
        state = _load()
        saved = state.get("mev_settings", {})
        # One-time move from the old defaults (0.01 SOL entry, minimum tip) to
        # 0.05 SOL and the maximum tip: fixed fees weigh less and bundles win
        # more often. Values the owner sets afterwards are kept.
        if not state.get("mev_defaults_v2"):
            if saved.get("input_lamports", 10_000_000) == 10_000_000:
                saved["input_lamports"] = MEV_DEFAULTS["input_lamports"]
            if saved.get("jito_tip_lamports", 1_000) == 1_000:
                saved["jito_tip_lamports"] = MEV_DEFAULTS["jito_tip_lamports"]
            state["mev_settings"] = saved
            state["mev_defaults_v2"] = True
            _save(state)
        # The previous 0.75% value was the shipped default; migrate it to this requested test threshold.
        if saved.get("min_profit_bps") == 75:
            saved["min_profit_bps"] = 10
            state["mev_settings"] = saved
            _save(state)
        return {key: int(saved.get(key, value)) for key, value in defaults.items()}

def update_mev_setting(name, value):
    bounds = {"input_lamports": (1_000_000, 100_000_000),
              "min_profit_bps": (1, 500), "jito_tip_lamports": (1_000, 10_000)}
    if name not in bounds or isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Configuração MEV inválida")
    low, high = bounds[name]
    if not low <= value <= high:
        raise ValueError("Valor fora dos limites MEV")
    with _LOCK:
        state = _load()
        settings = state.setdefault("mev_settings", {})
        settings[name] = value
        _save(state)
        return {key: int(settings.get(key, default)) for key, default in MEV_DEFAULTS.items()}

def _update_mev_daily(state, previous, item):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.get("mev_daily", {})
    if daily.get("day") != today:
        daily = {"day": today, "bundle_ids": [], "bundle_count": 0, "realized_net_lamports": 0}
    bundle_id = item.get("bundle_id") or (previous or {}).get("bundle_id")
    submitted_at = item.get("submitted_at") or (previous or {}).get("submitted_at")
    if bundle_id and submitted_at and datetime.fromtimestamp(int(submitted_at), timezone.utc).date().isoformat() == today:
        bundle_ids = daily.setdefault("bundle_ids", [])
        if bundle_id not in bundle_ids:
            bundle_ids.append(bundle_id)
            daily["bundle_count"] = int(daily.get("bundle_count", 0)) + 1
    old_pnl = (previous or {}).get("realized_net_lamports") if (previous or {}).get("status") == "landed" else None
    new_pnl = item.get("realized_net_lamports") if item.get("status") == "landed" else None
    settled_at = item.get("settled_at") or (previous or {}).get("settled_at")
    settled_today = bool(settled_at) and datetime.fromtimestamp(int(settled_at), timezone.utc).date().isoformat() == today
    if settled_today and isinstance(new_pnl, int) and not isinstance(new_pnl, bool):
        if isinstance(old_pnl, int) and not isinstance(old_pnl, bool) and (previous or {}).get("settled_at"):
            old_settled = datetime.fromtimestamp(int(previous["settled_at"]), timezone.utc).date().isoformat()
            if old_settled == today:
                daily["realized_net_lamports"] = int(daily.get("realized_net_lamports", 0)) + new_pnl - old_pnl
            else:
                daily["realized_net_lamports"] = int(daily.get("realized_net_lamports", 0)) + new_pnl
        else:
            daily["realized_net_lamports"] = int(daily.get("realized_net_lamports", 0)) + new_pnl
    daily["bundle_ids"] = daily.get("bundle_ids", [])[-100:]
    state["mev_daily"] = daily

def record_mev_event(event):
    """Keep secret-free MEV history and update an existing bundle as it settles."""
    import time as _time
    allowed = ("status", "mint", "bundle_id", "estimated_net_lamports",
               "realized_net_lamports", "signature", "slot", "error", "notification_sent", "submitted_at", "settled_at")
    item = {key: event[key] for key in allowed if key in event}
    item["timestamp"] = int(_time.time())
    if item.get("status") == "submitted_pending":
        item.setdefault("submitted_at", item["timestamp"])
    if item.get("status") in ("landed", "not_landed"):
        item.setdefault("settled_at", item["timestamp"])
        item.setdefault("notification_sent", False)
    with _LOCK:
        state = _load()
        history = state.setdefault("mev_history", [])
        bundle_id = item.get("bundle_id")
        if bundle_id:
            for index, previous in enumerate(history):
                if previous.get("bundle_id") == bundle_id:
                    old_pnl = previous.get("realized_net_lamports") if previous.get("status") == "landed" else None
                    new_pnl = item.get("realized_net_lamports") if item.get("status") == "landed" else None
                    if isinstance(new_pnl, int) and not isinstance(new_pnl, bool):
                        if isinstance(old_pnl, int) and not isinstance(old_pnl, bool):
                            state["mev_realized_total_lamports"] = int(state.get("mev_realized_total_lamports", 0)) + new_pnl - old_pnl
                        else:
                            state["mev_realized_total_lamports"] = int(state.get("mev_realized_total_lamports", 0)) + new_pnl
                            state["mev_confirmed_bundles"] = int(state.get("mev_confirmed_bundles", 0)) + 1
                    _update_mev_daily(state, previous, item)
                    history[index] = {**previous, **item}
                    state["mev_history"] = history[:30]
                    _save(state)
                    return
        new_pnl = item.get("realized_net_lamports") if item.get("status") == "landed" else None
        if isinstance(new_pnl, int) and not isinstance(new_pnl, bool):
            state["mev_realized_total_lamports"] = int(state.get("mev_realized_total_lamports", 0)) + new_pnl
            state["mev_confirmed_bundles"] = int(state.get("mev_confirmed_bundles", 0)) + 1
        _update_mev_daily(state, None, item)
        state["mev_history"] = ([item] + history)[:30]
        _save(state)

def mev_history(limit=5):
    with _LOCK:
        return list(_load().get("mev_history", []))[:max(1, min(int(limit), 20))]

def mev_notifications_pending(limit=5):
    with _LOCK:
        events = _load().get("mev_history", [])
        return [dict(item) for item in events
                if item.get("status") in ("landed", "not_landed")
                and not item.get("notification_sent")][:max(1, min(int(limit), 20))]

def mark_mev_notification_sent(bundle_id):
    if not bundle_id:
        return
    with _LOCK:
        state = _load()
        for item in state.get("mev_history", []):
            if item.get("bundle_id") == bundle_id and item.get("status") in ("landed", "not_landed"):
                item["notification_sent"] = True
                _save(state)
                return

def mev_daily_guard():
    """Cap confirmed live bundles and realized daily loss in UTC."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    max_bundles = int(os.getenv("MEV_MAX_BUNDLES_PER_DAY", "100"))  # failed bundles cost nothing
    max_loss_lamports = 10_000_000  # 0.01 SOL
    with _LOCK:
        daily = _load().get("mev_daily", {})
        if daily.get("day") != today:
            count, daily_pnl = 0, 0
        else:
            count = int(daily.get("bundle_count", 0))
            daily_pnl = int(daily.get("realized_net_lamports", 0))
        if count >= max_bundles:
            return False, "Limite diário de " + str(max_bundles) + " bundles atingido", count, daily_pnl
        if daily_pnl <= -max_loss_lamports:
            return False, "Limite diário de perda de 0,01 SOL atingido", count, daily_pnl
        return True, "Ready", count, daily_pnl

def mev_pnl_summary():
    """Return lifetime confirmed realized P/L in lamports and bundle count."""
    with _LOCK:
        state = _load()
        if "mev_realized_total_lamports" in state or "mev_confirmed_bundles" in state:
            return int(state.get("mev_realized_total_lamports", 0)), int(state.get("mev_confirmed_bundles", 0))
        events = state.get("mev_history", [])
        confirmed = [item for item in events
                     if item.get("status") == "landed"
                     and isinstance(item.get("realized_net_lamports"), int)
                     and not isinstance(item.get("realized_net_lamports"), bool)]
        return sum(item["realized_net_lamports"] for item in confirmed), len(confirmed)

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

def apply_mev_only_once():
    """Owner's choice: keep only MEV running. Turns every memecoin strategy off
    once (open positions keep their exit rules) and keeps the MEV scan on.
    Returns True the first time it is applied."""
    with _LOCK:
        state = _load()
        if state.get("mev_only_v1"):
            return False
        state["enabled"] = False
        state["halted"] = True
        state["sniper_enabled"] = False
        for name in ("pumpfun_enabled", "candle_enabled", "launch_enabled"):
            state[name] = False
        state["hot_disabled"] = True
        state["mev_enabled"] = True
        state["mev_only_v1"] = True
        _save(state)
        return True

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
    return DAILY_LOSS_LIMIT_USD

def set_daily_loss_limit(usd):
    """The daily loss limit is a fixed USD amount (DAILY_LOSS_LIMIT_USD)."""
    return DAILY_LOSS_LIMIT_USD

def _scaled_daily_loss_limit(usd):
    """Old behaviour (limit scaled with account size); kept for reference."""
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
    return _loss_limit(_load())

def adopt_position(mint, amount_raw, cost_usd, symbol, mode="copy", **fields):
    """Register a coin the wallet holds but the engine lost track of, so it can be sold by the bot."""
    import time as _time
    with _LOCK:
        state = _load()
        if mint in state["positions"]:
            return False
        state["positions"][mint] = {"token_amount_raw": int(amount_raw), "cost_usd": round(float(cost_usd), 4),
                                    "symbol": str(symbol)[:20], "mode": mode, "opened_at": _time.time(),
                                    "adopted": True, **fields}
        _save(state)
        return True

def set_position_fields(mint, **fields):
    """Attach extra settings to an open position (e.g. a cloned wallet's own TP/SL)."""
    with _LOCK:
        state = _load()
        position = state["positions"].get(mint)
        if position is None:
            return False
        position.update(fields)
        _save(state)
        return True

def update_peak(mint, ratio):
    with _LOCK:
        state = _load()
        position = state["positions"].get(mint)
        if position is not None and float(ratio) > float(position.get("peak_ratio", 0)):
            position["peak_ratio"] = float(ratio)
            _save(state)

def can_open(mint=None):
    """mint: a coin already held may be bought again without taking a new position slot."""
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
        if len(state["positions"]) >= MAX_POSITIONS and not (mint and mint in state["positions"]):
            return False, "Position limit"
        if state["daily_loss_usd"] >= _loss_limit(state):
            return False, "Daily loss limit"
        return True, "Ready"

def reserve(order_id, mint, side):
    """Reserve before signing; pending entries require explicit chain reconciliation."""
    if side not in ("buy", "sell") or not order_id or not mint:
        raise ValueError("Invalid order")
    with _LOCK:
        allowed, reason = can_open(mint) if side == "buy" else (True, "")
        if not allowed:
            raise RuntimeError(reason)
        state = _load()
        if state["pending"] or order_id in state["positions"]:
            raise RuntimeError("Order already pending or duplicated")
        state["pending"][order_id] = {"mint": mint, "side": side,
            "created_at": datetime.now(timezone.utc).isoformat()}
        _save(state)

HISTORY_MAX = 300


def _append_history(state, position, mint, pnl_usd, reason=None):
    import time as _time
    position = position or {}
    cost = float(position.get("cost_usd", 0) or 0)
    opened = float(position.get("opened_at") or 0)
    state.setdefault("history", []).append({
        "mint": mint, "symbol": position.get("symbol") or mint[:6], "mode": position.get("mode", "normal"),
        "cost_usd": round(cost, 4), "pnl_usd": None if pnl_usd is None else round(float(pnl_usd), 4),
        "reason": reason or position.get("exit_reason") or "?", "closed_at": _time.time(),
        "minutes": round((_time.time() - opened) / 60, 1) if opened else None})
    state["history"] = state["history"][-HISTORY_MAX:]


def history():
    with _LOCK:
        return list(_load().get("history", []))


def mark_exit_reason(mint, reason):
    with _LOCK:
        state = _load()
        if mint in state["positions"]:
            state["positions"][mint]["exit_reason"] = reason
            _save(state)


def record_settlement(order_id, signature, position=None, realized_loss_usd=0.0, pnl_usd=None):
    """Call only after independently verifying finalized on-chain outcome."""
    with _LOCK:
        state = _load()
        pending = state["pending"].get(order_id)
        if not pending or not signature:
            raise RuntimeError("No pending order or missing signature")
        if position is not None:
            old = state["positions"].get(pending["mint"])
            if old is not None and pending["side"] == "buy":
                # another buy of a coin already held (the copied wallet bought it again): add to it
                merged = dict(old)
                merged["token_amount_raw"] = int(old.get("token_amount_raw") or 0) + int(position.get("token_amount_raw") or 0)
                merged["cost_usd"] = float(old.get("cost_usd") or 0) + float(position.get("cost_usd") or 0)
                merged["buy_signature"] = position.get("buy_signature", old.get("buy_signature"))
                merged["buys"] = int(old.get("buys") or 1) + 1
                position = merged
            state["positions"][pending["mint"]] = position
        if pending["side"] == "sell":
            closed = state["positions"].pop(pending["mint"], None)
            _append_history(state, closed, pending["mint"], pnl_usd)
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
        tx = rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": int(os.getenv("SOLANA_MAX_TX_VERSION", "1"))}])
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
        position = dict(data["positions"].get(mint, {}))
        held = int(position.get("token_amount_raw") or 0) or int(before_token) or 1
        part = min(1.0, sold / held)                       # < 1: only part of the position was sold
        cost = float(position.get("cost_usd", 0) or 0) * part
        cost_left = float(position.get("cost_usd", 0) or 0) - cost
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
        leftover = held - sold
        partial = part < 0.95 and leftover > 0 and after_token > 0
        if partial:                                          # keep tracking the part not sold
            with _LOCK:
                data = _load()
                position.update(token_amount_raw=min(leftover, after_token), cost_usd=cost_left)
                data["positions"][mint] = position
                _save(data)
        return {"status": "settled_sell", "signature": signature, "pnl_usd": pnl,
                "cost_usd": cost, "proceeds_usd": proceeds_usd, "partial": partial, "part": part,
                "sol_received": (after_sol - int(before_sol)) / 1_000_000_000}

def _sol_to_usd(lamports):
    """Value SOL received from a sale with an executable Jupiter SOL->USDC quote."""
    from jupiter_v2_executor import order, SOL_MINT, USDC_MINT
    if int(lamports) <= 0:
        return 0.0
    return int(order(SOL_MINT, USDC_MINT, int(lamports))["outAmount"]) / 1_000_000


def set_position_amount(mint, amount_raw):
    """Track the amount actually held (some tokens charge transfer fees)."""
    with _LOCK:
        state = _load()
        if mint in state["positions"] and int(amount_raw) > 0:
            state["positions"][mint]["token_amount_raw"] = int(amount_raw)
            _save(state)

def write_off(mint):
    """Close a worthless position (rug/no route) as a full loss."""
    with _LOCK:
        state = _load()
        position = state["positions"].pop(mint, None)
        if position is None:
            return None
        loss = float(position.get("cost_usd", 0) or 0)
        _append_history(state, position, mint, -loss, "dead")
        state["daily_loss_usd"] += loss
        state["day_pnl_usd"] = float(state.get("day_pnl_usd", 0.0)) - loss
        state["total_pnl_usd"] = float(state.get("total_pnl_usd", 0.0)) - loss
        state["trades_closed"] = int(state.get("trades_closed", 0)) + 1
        if state["daily_loss_usd"] >= _loss_limit(state):
            state["enabled"] = False
            state["halted"] = True
        _save(state)
        return {"cost_usd": loss, "halted": state["halted"]}

def drop_position(mint):
    """Wallet no longer holds the token (sold outside the bot or rugged)."""
    with _LOCK:
        state = _load()
        position = state["positions"].pop(mint, None)
        _save(state)
        return position

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
