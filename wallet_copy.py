"""Discover and rank Solana wallets for paper copy-trading from confirmed blocks.

This module is analytics-only: it never signs, submits, or copies a swap.
"""
import json
import os
import threading
import time
from pathlib import Path

import requests


def _tx_deltas(tx):
    """Extract owner token and SOL deltas from a confirmed transaction."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return {}, {}
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    owners = [key.get("pubkey") if isinstance(key, dict) else str(key) for key in keys]
    sol = {}
    before, after = meta.get("preBalances") or [], meta.get("postBalances") or []
    for i, owner in enumerate(owners):
        if i < len(before) and i < len(after):
            sol[owner] = int(after[i]) - int(before[i])
    values = {}
    for sign, rows in ((-1, meta.get("preTokenBalances") or []),
                       (1, meta.get("postTokenBalances") or [])):
        for item in rows:
            owner, mint = item.get("owner"), item.get("mint")
            amount = (item.get("uiTokenAmount") or {}).get("amount")
            decimals = (item.get("uiTokenAmount") or {}).get("decimals")
            if not owner or not mint or amount is None:
                continue
            key = (owner, mint)
            old, _ = values.get(key, (0, int(decimals or 0)))
            values[key] = (old + sign * int(amount), int(decimals or 0))
    tokens = {}
    for (owner, mint), (amount, decimals) in values.items():
        if amount:
            tokens.setdefault(mint, {})[owner] = (amount / (10 ** decimals), amount)
    return tokens, sol

_LOCK = threading.RLock()
MIN_HOLD_SECONDS = 300
MIN_CLOSED_TRADES = 3
MIN_WIN_RATE = 0.55
MAX_EVENTS = 20_000


def empty_state():
    return {"last_slot": None, "positions": {}, "wallets": {}, "pending": {},
            "paper_signals": [], "transactions_scanned": 0, "blocks_scanned": 0}


def _qualifies(stats):
    trades = int(stats.get("closed_trades", 0))
    win_rate = float(stats.get("wins", 0)) / trades if trades else 0
    avg_hold = float(stats.get("total_hold_seconds", 0)) / trades if trades else 0
    return (trades >= MIN_CLOSED_TRADES and float(stats.get("realized_lamports", 0)) > 0
            and win_rate >= MIN_WIN_RATE and avg_hold >= MIN_HOLD_SECONDS)


def process_confirmed_block(state, block, slot=None):
    """Update wallet history and paper signals from one confirmed block."""
    now = int(block.get("blockTime") or time.time())
    txs = block.get("transactions") or []
    new_paper = []
    for tx in txs:
        token_deltas, sol_deltas = _tx_deltas(tx)
        signatures = (tx.get("transaction") or {}).get("signatures") or []
        signature = signatures[0] if signatures else None
        for mint, owners in token_deltas.items():
            for owner, (delta, raw) in owners.items():
                key = owner + ":" + mint
                position = state["positions"].get(key)
                sol_delta = int(sol_deltas.get(owner, 0))
                if delta > 0:
                    if sol_delta >= 0:
                        continue  # ignore airdrops/transfers; require a SOL-funded buy
                    if position is None:
                        position = {"owner": owner, "mint": mint, "amount": 0.0,
                                    "cost_lamports": 0, "opened_at": now}
                        state["positions"][key] = position
                    position["amount"] = float(position.get("amount", 0)) + float(delta)
                    position["cost_lamports"] = int(position.get("cost_lamports", 0)) + max(0, -sol_delta)
                    state["pending"].setdefault(key, {"owner": owner, "mint": mint,
                        "signal_at": now, "buy_signature": signature})
                elif delta < 0 and position is not None:
                    sold = min(float(position.get("amount", 0)), abs(float(delta)))
                    old_amount = float(position.get("amount", 0))
                    if old_amount <= 0:
                        state["positions"].pop(key, None)
                        state["pending"].pop(key, None)
                        continue
                    allocated_cost = int(position.get("cost_lamports", 0) * sold / old_amount)
                    proceeds = max(0, sol_delta)
                    hold = max(0, now - int(position.get("opened_at", now)))
                    stats = state["wallets"].setdefault(owner, {"closed_trades": 0, "wins": 0,
                        "realized_lamports": 0, "total_hold_seconds": 0, "long_holds": 0,
                        "hold_samples": []})
                    stats["closed_trades"] += 1
                    stats["wins"] += int(proceeds > allocated_cost)
                    stats["realized_lamports"] += proceeds - allocated_cost
                    stats["cost_lamports_closed"] = int(stats.get("cost_lamports_closed", 0)) + allocated_cost
                    stats["total_hold_seconds"] += hold
                    stats["long_holds"] += int(hold >= MIN_HOLD_SECONDS)
                    stats.setdefault("hold_samples", []).append(hold)
                    stats["hold_samples"] = stats["hold_samples"][-30:]
                    position["amount"] = old_amount - sold
                    position["cost_lamports"] = max(0, int(position.get("cost_lamports", 0)) - allocated_cost)
                    if position["amount"] <= 1e-12:
                        state["positions"].pop(key, None)
                        state["pending"].pop(key, None)
                        if state.get("copied", {}).get(mint) == owner:
                            new_paper.append({"status": "paper_copy_exit", "wallet": owner,
                                "mint": mint, "detected_at": now, "source_held_seconds": hold,
                                "sell_signature": signature, "slot": slot})
    for key, signal in list(state["pending"].items()):
        held = state["positions"].get(key)
        if not held:
            state["pending"].pop(key, None)
            continue
        if now - int(signal["signal_at"]) < MIN_HOLD_SECONDS:
            continue
        stats = state["wallets"].get(signal["owner"], {})
        if _qualifies(stats):
            item = {"status": "paper_copy_opportunity", "wallet": signal["owner"],
                    "mint": signal["mint"], "detected_at": now, "source_held_seconds": now - int(signal["signal_at"]),
                    "wallet_closed_trades": int(stats.get("closed_trades", 0)),
                    "wallet_win_rate": round(float(stats.get("wins", 0)) / int(stats["closed_trades"]), 4),
                    "wallet_realized_lamports": int(stats.get("realized_lamports", 0)),
                    "buy_signature": signal.get("buy_signature"), "slot": slot}
            state["paper_signals"].append(item)
            new_paper.append(item)
        state["pending"].pop(key, None)
    state["paper_signals"] = state["paper_signals"][-MAX_EVENTS:]
    return new_paper


def best_wallets(state=None, limit=10):
    state = state or _load_state(_state_path())
    rows = []
    for owner, stats in state.get("wallets", {}).items():
        if not _qualifies(stats):
            continue
        trades = int(stats["closed_trades"])
        rows.append({"wallet": owner, "closed_trades": trades,
            "win_rate": float(stats["wins"]) / trades,
            "realized_lamports": int(stats["realized_lamports"]),
            "average_buy_lamports": int(stats.get("cost_lamports_closed", 0) / trades),
            "average_hold_seconds": int(stats["total_hold_seconds"] / trades),
            "long_holds": int(stats.get("long_holds", 0)),
            "hold_samples": list(stats.get("hold_samples", []))})
    # best first: profit x win rate x experience (same score copy_trader uses)
    rows.sort(key=lambda x: (max(0, x["realized_lamports"]) / 1e9 * x["win_rate"] * min(x["closed_trades"], 25) ** 0.5,
                             x["realized_lamports"]), reverse=True)
    return rows[:max(1, min(int(limit), 50))]


def _state_path():
    from bot_wallet_service import wallet_path
    return wallet_path().with_name("wallet_copy_analysis.json")


def mark_copied(mint, wallet):
    with _LOCK:
        path = _state_path()
        state = _load_state(path)
        state.setdefault("copied", {})[mint] = wallet
        _save_state(path, state)


def mark_sold(mint):
    with _LOCK:
        path = _state_path()
        state = _load_state(path)
        state.setdefault("copied", {}).pop(mint, None)
        _save_state(path, state)


def copied_wallet(mint):
    with _LOCK:
        return _load_state(_state_path()).get("copied", {}).get(mint)


def _load_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else empty_state()
    except (OSError, ValueError):
        return empty_state()


def _save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def scan_confirmed(rpc_url=None, max_blocks=25):
    """Discover wallets from confirmed block transactions and persist history."""
    rpc_url = (rpc_url or os.getenv("SOLANA_RPC_URL", "")).strip()
    if not rpc_url:
        raise RuntimeError("Dedicated Solana RPC missing")
    with _LOCK:
        path = _state_path()
        state = _load_state(path)
        session = requests.Session()

        def rpc(method, params):
            response = session.post(rpc_url, json={"jsonrpc": "2.0", "id": 2,
                "method": method, "params": params}, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError("Solana RPC " + method + " failed")
            return payload.get("result")

        latest = int(rpc("getSlot", [{"commitment": "confirmed"}]))
        prior = state.get("last_slot")
        start = max(0, latest - max_blocks + 1) if prior is None else int(prior) + 1
        if prior is not None and latest - start >= max_blocks:
            # Process a bounded chunk and leave the cursor at the last processed
            # slot; subsequent cycles catch up instead of silently skipping data.
            start = int(prior) + 1
        end = min(latest, start + max_blocks - 1)
        if start > latest:
            return []
        slots = rpc("getBlocks", [start, end, {"commitment": "confirmed"}]) or []
        paper = []
        tx_count = 0
        for slot in slots:
            block = rpc("getBlock", [slot, {"encoding": "json", "transactionDetails": "full",
                "rewards": False, "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
            if not isinstance(block, dict):
                continue
            transactions = block.get("transactions") or []
            tx_count += len(transactions)
            paper.extend(process_confirmed_block(state, block, slot=slot))
        state["last_slot"] = max(slots) if slots else end
        state["blocks_scanned"] = int(state.get("blocks_scanned", 0)) + len(slots)
        state["transactions_scanned"] = int(state.get("transactions_scanned", 0)) + tx_count
        state["updated_at"] = int(time.time())
        _save_state(path, state)
        return paper


def summary(now=None):
    now = int(time.time() if now is None else now)
    try:
        state = _load_state(_state_path())
    except Exception:
        state = empty_state()
    recent = [x for x in state.get("paper_signals", []) if now - int(x.get("detected_at", 0)) < 86400]
    return {"wallets_seen": len(state.get("wallets", {})),
            "qualified_wallets": len(best_wallets(state, 50)),
            "paper_signals_24h": len(recent),
            "transactions_scanned": int(state.get("transactions_scanned", 0)),
            "blocks_scanned": int(state.get("blocks_scanned", 0)),
            "dex_transactions_scanned": int(state.get("dex_transactions_scanned", 0)),
            "dex_tokens": list(state.get("dex_tokens", [])),
            "wallets_analyzed": len(state.get("dex_analyzed", {})),
            "last_cycle": dict(state.get("dex_last_cycle", {})),
            "last_slot": state.get("last_slot")}
