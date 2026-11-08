"""Conservative atomic SOL/token/SOL arbitrage via Jupiter Swap V2 + Jito.

The strategy only trades against public pool prices. It never reads pending
user transactions and never constructs sandwich/front-running transactions.
Live permission is on by default (MEV_AUTOTRADE_ENABLED / MEV_LIVE_ENABLED
can be set to "false" to block it). Real bundles are still only sent after the
owner arms real mode in Telegram and every preflight check passes.
"""
import base64
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path

import requests
import re
import jupiter_rate_limiter
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from bot_wallet_service import create_or_get, export_private_key, get_address

LOG = logging.getLogger("jito_arbitrage")
JUPITER_BUILD = "https://api.jup.ag/swap/v2/build"
SOL_MINT = "So11111111111111111111111111111111111111112"
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
JITO_URL = os.getenv("JITO_BLOCK_ENGINE_URL", "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles").rstrip("/")
_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()
_LAST_BUILD_AT = 0.0
_BUILD_MIN_INTERVAL_SECONDS = max(1.05, float(os.getenv("JUPITER_BUILD_MIN_INTERVAL_SECONDS", "1.1")))


def _int_env(name, default, minimum=0, maximum=None):
    value = int(os.getenv(name, str(default)))
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(name + " outside allowed range")
    return value


def _wallet():
    address, _created = create_or_get()
    configured = get_address()
    if address != configured:
        raise RuntimeError("Bot wallet address mismatch")
    password = os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE", "")
    public, secret = export_private_key(password)
    signer = Keypair.from_base58_string(secret)
    if str(signer.pubkey()) != public or public != address:
        raise RuntimeError("Bot signer does not match stored wallet")
    return signer


def _headers():
    key = os.getenv("JUPITER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("JUPITER_API_KEY is required")
    return {"x-api-key": key, "Accept": "application/json"}


def _build(input_mint, output_mint, amount, taker, dexes=None, slippage_bps=50):
    global _LAST_BUILD_AT
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
        "taker": taker,
        "slippageBps": str(int(max(0, min(50, slippage_bps)))),
        "computeUnitPricePercentile": "medium",
        "forJitoBundle": "true",
        "wrapAndUnwrapSol": "true",
        "transactionVersion": "0",
    }
    if dexes:
        params["dexes"] = ",".join(dexes) if isinstance(dexes, (list, tuple)) else str(dexes)
    with _BUILD_LOCK:
        wait = _BUILD_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_BUILD_AT)
        if wait > 0:
            time.sleep(wait)
        try:
            response = jupiter_rate_limiter.get(JUPITER_BUILD, params=params, headers=_headers(), timeout=20)
        finally:
            _LAST_BUILD_AT = time.monotonic()
    response.raise_for_status()
    result = response.json()
    for field in ("outAmount", "otherAmountThreshold", "swapInstruction", "blockhashWithMetadata"):
        if not result.get(field):
            raise RuntimeError("Jupiter build response missing " + field)
    return result


def _instruction(raw):
    if not isinstance(raw, dict) or not raw.get("programId"):
        raise ValueError("Malformed Jupiter instruction")
    accounts = [AccountMeta(Pubkey.from_string(item["pubkey"]), bool(item["isSigner"]), bool(item["isWritable"]))
                for item in raw.get("accounts", [])]
    data = base64.b64decode(raw.get("data", ""), validate=True)
    return Instruction(Pubkey.from_string(raw["programId"]), data, accounts)


def _priority_fee_enabled():
    """Jito bundles are ranked by the tip, so the per-CU priority fee is off by default."""
    return os.getenv("MEV_PRIORITY_FEE_ENABLED", "false").strip().lower() == "true"


def _is_cu_limit(raw):
    try:
        data = base64.b64decode((raw or {}).get("data", ""), validate=True)
    except ValueError:
        return False
    return (raw or {}).get("programId") == str(COMPUTE_BUDGET_PROGRAM) and data[:1] == b"\x02"


def _compile_and_sign(build, signer, extra_instructions=()):
    cu_limit = _int_env("MEV_COMPUTE_UNIT_LIMIT", 1_000_000, 200_000, 1_400_000)
    instructions = [Instruction(COMPUTE_BUDGET_PROGRAM, b"\x02" + struct.pack("<I", cu_limit), [])]
    for raw in build.get("computeBudgetInstructions", []):
        if _is_cu_limit(raw):
            continue  # we already set our own limit; a duplicate makes the tx invalid
        if not _priority_fee_enabled() and raw.get("programId") == str(COMPUTE_BUDGET_PROGRAM):
            continue  # drop setComputeUnitPrice: pay the Jito tip only
        instructions.append(_instruction(raw))
    instructions.extend(_instruction(ix) for ix in build.get("setupInstructions", []))
    instructions.append(_instruction(build["swapInstruction"]))
    if build.get("cleanupInstruction"):
        instructions.append(_instruction(build["cleanupInstruction"]))
    instructions.extend(_instruction(ix) for ix in build.get("otherInstructions", []))
    instructions.extend(extra_instructions)

    alts = []
    raw_alts = build.get("addressesByLookupTableAddress") or {}
    for key, addresses in raw_alts.items():
        alts.append(AddressLookupTableAccount(
            key=Pubkey.from_string(key),
            addresses=[Pubkey.from_string(address) for address in addresses],
        ))
    blockhash_bytes = bytes(build["blockhashWithMetadata"]["blockhash"])
    recent_blockhash = Hash.from_bytes(blockhash_bytes)
    message = MessageV0.try_compile(signer.pubkey(), instructions, alts, recent_blockhash)
    return VersionedTransaction(message, [signer])


def _priority_fee_lamports(build):
    """Estimate the priority fee implied by Jupiter's compute-budget price."""
    if not _priority_fee_enabled():
        return 0
    cu_limit = _int_env("MEV_COMPUTE_UNIT_LIMIT", 1_000_000, 200_000, 1_400_000)
    price_micro_lamports = 0
    for raw in build.get("computeBudgetInstructions", []):
        if raw.get("programId") != str(COMPUTE_BUDGET_PROGRAM):
            continue
        try:
            data = base64.b64decode(raw.get("data", ""), validate=True)
            if len(data) >= 9 and data[0] == 3:
                price_micro_lamports = max(price_micro_lamports, struct.unpack("<Q", data[1:9])[0])
        except (ValueError, struct.error):
            raise RuntimeError("Malformed Jupiter compute-budget instruction") from None
    return (price_micro_lamports * cu_limit + 999_999) // 1_000_000


def _jito(method, params):
    response = requests.post(JITO_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    if response.status_code >= 400:
        raise RuntimeError("Jito " + method + " HTTP " + str(response.status_code) + _jito_error_text(response))
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Jito " + method + " recusou" + _jito_error_text(response))
    return payload.get("result")


def _jito_error_text(response):
    try:
        error = response.json().get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
    except ValueError:
        message = response.text
    message = re.sub(r"https?://\S+", "", str(message or "")).strip()
    return (": " + message[:90]) if message else ""


def _rpc(method, params):
    """Call the configured Solana RPC; errors never include the RPC URL (it may hold an API key)."""
    rpc_url = os.getenv("SOLANA_RPC_URL", "").strip()
    try:
        response = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    except requests.RequestException:
        raise RuntimeError("RPC " + method + " sem conexão") from None
    if response.status_code >= 400:
        raise RuntimeError("RPC " + method + " HTTP " + str(response.status_code))
    try:
        return response.json()
    except ValueError:
        raise RuntimeError("RPC " + method + " resposta inválida") from None


_TIP_CACHE = {"at": 0.0, "accounts": []}


def _tip_account():
    # The tip account list rarely changes: keep it 10 min instead of asking on every send.
    if time.monotonic() - _TIP_CACHE["at"] > 600 or not _TIP_CACHE["accounts"]:
        accounts = _jito("getTipAccounts", [])
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("Jito returned no tip accounts")
        _TIP_CACHE.update(at=time.monotonic(), accounts=accounts)
    accounts = _TIP_CACHE["accounts"]
    # Stable rotation avoids repeatedly contending for the first account.
    return Pubkey.from_string(accounts[int(time.time()) % len(accounts)])


def _bundle_status(bundle_id):
    result = _jito("getInflightBundleStatuses", [[bundle_id]])
    if isinstance(result, dict):
        values = result.get("value") or []
        if values:
            return str(values[0].get("status", "unknown")).lower()
    return "unknown"


def _bundle_detail(bundle_id):
    result = _jito("getBundleStatuses", [[bundle_id]])
    values = result.get("value", []) if isinstance(result, dict) else []
    return values[0] if values and isinstance(values[0], dict) else None


def _realized_net_lamports(bundle):
    """Sum the bot wallet's on-chain SOL balance delta across landed bundle txs."""
    signatures = bundle.get("transactions") or bundle.get("signatures") or []
    if not signatures:
        return None
    owner = get_address()
    rpc_url = os.getenv("SOLANA_RPC_URL", "").strip()
    if not owner or not rpc_url:
        return None
    total = 0
    for item in signatures:
        signature = item.get("signature") or item.get("transactionId") if isinstance(item, dict) else item
        if not isinstance(signature, str) or not signature:
            return None
        response = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
            "method": "getTransaction", "params": [signature, {
                "encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}]}, timeout=15)
        response.raise_for_status()
        result = response.json().get("result")
        if not isinstance(result, dict) or not isinstance(result.get("meta"), dict):
            return None
        message = (result.get("transaction") or {}).get("message") or {}
        keys = message.get("accountKeys") or []
        key_strings = [key.get("pubkey") if isinstance(key, dict) else key for key in keys]
        if owner not in key_strings:
            return None
        index = key_strings.index(owner)
        before, after = result["meta"].get("preBalances", []), result["meta"].get("postBalances", [])
        if index >= len(before) or index >= len(after):
            return None
        total += int(after[index]) - int(before[index])
    return total


def _resolve_one(record):
    """Return a terminal event for one bundle, or None while it is still in flight."""
    bundle_id = record.get("bundle_id")
    inflight = "unknown"
    try:
        inflight = _bundle_status(bundle_id)
    except Exception as exc:
        LOG.warning("Jito inflight status lookup failed type=%s", type(exc).__name__)
    detail = None
    try:
        detail = _bundle_detail(bundle_id)
    except Exception as exc:
        LOG.warning("Jito bundle detail lookup failed type=%s", type(exc).__name__)
    status = str((detail or {}).get("confirmation_status") or
                 (detail or {}).get("status") or inflight).lower()
    if status not in ("landed", "confirmed", "finalized", "failed", "invalid"):
        return None
    landed = status in ("landed", "confirmed", "finalized")
    signatures = (detail or {}).get("transactions") or []
    realized = None
    if landed:
        try:
            realized = _realized_net_lamports(detail or {})
        except Exception as exc:
            LOG.warning("MEV realized P/L lookup failed type=%s", type(exc).__name__)
    return {"status": "landed" if landed else "not_landed",
            "mint": record.get("mint", ""), "bundle_id": bundle_id,
            "estimated_net_lamports": record.get("estimated_net_lamports"),
            "submitted_at": record.get("created_at"),
            "realized_net_lamports": realized,
            "signature": signatures[0] if signatures else None,
            "slot": (detail or {}).get("slot") or (detail or {}).get("landed_slot")}


def reconcile_pending_bundle():
    """Resolve every in-flight Jito bundle and record chain-confirmed P/L for Telegram."""
    import live_trade_state
    resolved = []
    with _PENDING_LOCK:
        pending = _load_pending()
    for key, record in list(pending.items()):
        if not record.get("bundle_id"):
            continue  # uncertain send: fail closed and never retry
        event = _resolve_one(record)
        if not event:
            continue
        live_trade_state.record_mev_event(event)
        _remove_pending(key)
        LOG.info("Resolved Jito bundle status=%s id=%s", event["status"], record["bundle_id"][:10])
        resolved.append(event)
    return resolved[-1] if resolved else None


def _pending_path():
    configured = os.getenv("MEV_PENDING_FILE", "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise RuntimeError("MEV_PENDING_FILE must be an absolute path on persistent storage")
        return path
    from bot_wallet_service import wallet_path
    return wallet_path().with_name("mev_pending_bundle.json")


_PENDING_LOCK = threading.Lock()
MAX_PENDING = int(os.getenv("MEV_MAX_PENDING", "5"))


def _load_pending():
    """{key: record}. Old single-record files are read as one entry."""
    path = _pending_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"unreadable": {"status": "submitting"}}  # fail closed
    if isinstance(data, dict) and isinstance(data.get("bundles"), dict):
        return data["bundles"]
    return {"legacy": data} if isinstance(data, dict) and data else {}


def _write_pending(bundles):
    path = _pending_path()
    if not bundles:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"bundles": bundles}), encoding="utf-8")
    os.replace(temporary, path)


def _save_pending(record, key="legacy"):
    with _PENDING_LOCK:
        bundles = _load_pending()
        bundles[key] = record
        _write_pending(bundles)


def _remove_pending(key):
    with _PENDING_LOCK:
        bundles = _load_pending()
        bundles.pop(key, None)
        _write_pending(bundles)


def pending_summary():
    with _PENDING_LOCK:
        bundles = _load_pending()
    uncertain = sum(1 for r in bundles.values() if not r.get("bundle_id"))
    return {"in_flight": len(bundles) - uncertain, "uncertain": uncertain,
            "mints": {r.get("mint") for r in bundles.values()}}


def _has_unresolved_bundle():
    """Any bundle still pending (used to keep housekeeping out of the way)."""
    with _PENDING_LOCK:
        return bool(_load_pending())


def _can_submit(mint):
    """Up to MAX_PENDING bundles in flight; an uncertain send blocks everything."""
    summary = pending_summary()
    if summary["uncertain"]:
        return False, "envio anterior incerto: confira no Solscan"
    if summary["in_flight"] >= MAX_PENDING:
        return False, "já há " + str(MAX_PENDING) + " ordens aguardando confirmação"
    if mint in summary["mints"]:
        return False, "já há ordem dessa moeda aguardando confirmação"
    return True, ""


def _mint_is_fixed(mint):
    rpc_url = os.getenv("SOLANA_RPC_URL", "").strip()
    if not rpc_url or rpc_url == "https://api.mainnet-beta.solana.com":
        return False
    response = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]}, timeout=12)
    response.raise_for_status()
    result = response.json().get("result", {}).get("value") or {}
    parsed = (result.get("data") or {}).get("parsed") or {}
    info = parsed.get("info") or {}
    return parsed.get("type") == "mint" and not info.get("mintAuthority") and not info.get("freezeAuthority")


def _simulation_reason(value):
    """Short reason from a failed simulateBundle result (which leg, which error)."""
    text = json.dumps(value.get("summary"), default=str)
    for index, item in enumerate(value.get("transactionResults") or []):
        if item.get("err"):
            logs = [line for line in (item.get("logs") or []) if "rror" in line or "failed" in line or "Slippage" in line]
            text = ("compra" if index == 0 else "venda") + ": " + json.dumps(item.get("err"), default=str) + (" | " + logs[-1] if logs else "")
            break
    text = re.sub(r"[1-9A-HJ-NP-Za-km-z]{60,}", "", text)  # drop signatures
    return text[:160]


def _simulate_bundle(transactions):
    """Pre-check the bundle before sending.

    The public Jito block engine does not offer simulateBundle (it answers
    HTTP 400), so try it on the configured RPC (Jito-enabled RPCs support it)
    and otherwise simulate the buy leg with simulateTransaction. The sell leg
    depends on the buy leg's tokens and cannot be simulated alone; that is
    safe because a Jito bundle is all-or-nothing: if the sell would fail,
    nothing lands and nothing is paid.
    """
    encoded = [base64.b64encode(bytes(tx)).decode("ascii") for tx in transactions]
    try:
        payload = _rpc("simulateBundle", [{"encodedTransactions": encoded}])
    except RuntimeError:
        payload = {"error": "unsupported"}
    if not isinstance(payload, dict) or payload.get("error"):
        sim = _rpc("simulateTransaction", [encoded[0], {"encoding": "base64", "sigVerify": False,
                                                          "replaceRecentBlockhash": False, "commitment": "processed"}])
        if not isinstance(sim, dict) or sim.get("error"):
            raise RuntimeError("RPC simulateTransaction recusou a compra")
        if ((sim.get("result") or {}).get("value") or {}).get("err"):
            raise RuntimeError("simulation: a compra falharia na blockchain")
        return encoded
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Jito bundle simulation returned no result")
    value = result.get("value") or result
    if str(value.get("summary", "")).lower() != "succeeded":
        raise RuntimeError("simulation failed: " + _simulation_reason(value))
    for item in value.get("transactionResults", []):
        if item.get("err"):
            raise RuntimeError("Jito bundle simulation reported a transaction error")
    return encoded


def _live_enabled():
    try:
        import live_trade_state
        return live_trade_state.mev_live_allowed() and live_trade_state.flag("mev_real_armed")
    except Exception:
        return False


_BALANCE_CACHE = {"at": 0.0, "owner": "", "lamports": -1}


def _cached_balance(owner, max_age=15):
    """Wallet SOL balance, re-read at most every 15 s (saves a round trip per send)."""
    if _BALANCE_CACHE["owner"] != owner or time.monotonic() - _BALANCE_CACHE["at"] > max_age:
        payload = _rpc("getBalance", [owner, {"commitment": "confirmed"}])
        lamports = int(((payload or {}).get("result") or {}).get("value", -1))
        _BALANCE_CACHE.update(at=time.monotonic(), owner=owner, lamports=lamports)
    return _BALANCE_CACHE["lamports"]


def _preflight(signer, amount, tip_lamports, first, second):
    """MEV-only readiness gate; do not enable the separate spot trader globally."""
    import live_trade_state
    import telegram_owner
    if not _live_enabled():
        raise RuntimeError("MEV live switches are disabled")
    owner = telegram_owner.get_owner()
    configured_owner = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not owner or (configured_owner and str(owner) != configured_owner):
        raise RuntimeError("Private Telegram owner is not verified")
    if live_trade_state._load().get("pending"):
        raise RuntimeError("Unreconciled spot order: MEV paused")
    if not os.getenv("JUPITER_API_KEY", "").strip():
        raise RuntimeError("Jupiter API key missing")
    rpc_url = os.getenv("SOLANA_RPC_URL", "").strip()
    if not rpc_url or rpc_url == "https://api.mainnet-beta.solana.com":
        raise RuntimeError("Dedicated Solana RPC missing")
    if len(os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE", "")) < 24:
        raise RuntimeError("Bot wallet passphrase missing")
    if get_address() != str(signer.pubkey()):
        raise RuntimeError("Configured signer does not match the bot wallet")
    priority = _priority_fee_lamports(first) + _priority_fee_lamports(second)
    rent_reserve = _int_env("MEV_RENT_RESERVE_LAMPORTS", 10_000_000, 0, 100_000_000)
    balance = _cached_balance(str(signer.pubkey()))
    if balance < amount + tip_lamports + priority + rent_reserve:
        raise RuntimeError("Bot wallet SOL balance is below the trade and fee reserve")


_DEX_LABELS_CACHE = {"expires": 0.0, "labels": []}
_DEX_LABELS_LOCK = threading.Lock()
MIN_ARB_POOL_LIQUIDITY_USD = float(os.getenv("MEV_MIN_ARB_POOL_LIQUIDITY_USD", "10000"))


def _dex_label_names():
    now = time.monotonic()
    if now < _DEX_LABELS_CACHE["expires"]:
        return _DEX_LABELS_CACHE["labels"]
    with _DEX_LABELS_LOCK:
        now = time.monotonic()
        if now < _DEX_LABELS_CACHE["expires"]:
            return _DEX_LABELS_CACHE["labels"]
        response = jupiter_rate_limiter.get("https://api.jup.ag/swap/v2/program-id-to-label",
                                headers=_headers(), timeout=15)
        response.raise_for_status()
        payload = response.json()
        labels = sorted(set(str(value) for value in payload.values()
                            if isinstance(value, str) and value.strip()))
        _DEX_LABELS_CACHE.update(expires=now + 3600, labels=labels)
        return labels


def _labels_for_dex_id(dex_id):
    target = re.sub(r"[^a-z0-9]", "", str(dex_id).lower())
    if not target:
        return []
    try:
        labels = _dex_label_names()
    except Exception as exc:
        LOG.warning("Jupiter DEX labels unavailable type=%s", type(exc).__name__)
        return []
    matched = []
    for label in labels:
        normalized = re.sub(r"[^a-z0-9]", "", label.lower())
        if target in normalized or normalized.startswith(target):
            matched.append(label)
    return matched[:4]


def _cross_dex_route(pools):
    """Pick the strongest same-quote-token price spread across distinct DEXs."""
    parsed = []
    for pool in pools or []:
        try:
            dex_id = str(pool.get("dex_id") or "")
            price = float(pool.get("price_usd") or 0)
            liquidity = float(pool.get("liquidity_usd") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if dex_id and price > 0 and liquidity >= MIN_ARB_POOL_LIQUIDITY_USD:
            parsed.append((dex_id, price, liquidity))
    best = None
    for i, left in enumerate(parsed):
        for right in parsed[i + 1:]:
            if left[0].lower() == right[0].lower():
                continue
            buy, sell = (left, right) if left[1] < right[1] else (right, left)
            spread = sell[1] / buy[1] - 1
            if spread > 0 and (best is None or spread > best[0]):
                best = (spread, buy, sell)
    if not best:
        return None
    buy_labels = _labels_for_dex_id(best[1][0])
    sell_labels = _labels_for_dex_id(best[2][0])
    if not buy_labels or not sell_labels:
        return None
    return {"buy_dexes": buy_labels, "sell_dexes": sell_labels,
            "spread_pct": best[0] * 100, "buy_dex_id": best[1][0],
            "sell_dex_id": best[2][0]}


def _locked_slippage_bps(expected_back, required_back):
    """Largest slippage (max 0.5%) that still guarantees required_back."""
    if expected_back <= 0 or expected_back < required_back:
        return 0
    return int(min(50, (expected_back - required_back) * 10_000 // expected_back))


def try_arbitrage(mint, quote_only=False, buy_dexes=None, sell_dexes=None):
    """Quote a SOL -> token -> SOL cycle; send only if Jito simulation is profitable."""
    Pubkey.from_string(mint)
    signer = _wallet()
    if not _mint_is_fixed(mint):
        raise RuntimeError("Mint or freeze authority remains active, or the RPC safety check failed")

    import live_trade_state
    settings = live_trade_state.mev_config()
    amount = settings["input_lamports"]
    min_bps = settings["min_profit_bps"]
    tip_lamports = settings["jito_tip_lamports"]
    # Small slippage on the buy so a tiny price move does not kill the cycle;
    # the sell leg only sells the buy's guaranteed minimum, so it always has
    # the tokens. The profit floor stays locked on the sell leg, and a failed
    # leg drops the whole Jito bundle at no cost.
    first = _build(SOL_MINT, mint, amount, str(signer.pubkey()), dexes=buy_dexes,
                   slippage_bps=_int_env("MEV_BUY_SLIPPAGE_BPS", 20, 0, 50))
    token_amount = int(first["otherAmountThreshold"])
    if token_amount <= 0:
        raise RuntimeError("First Jupiter route returned no tokens")
    # One sell quote only (no second build): its minimum output is locked at the
    # expected value (MEV_SELL_SLIPPAGE_BPS=0), which saves >1 s of Jupiter rate limit.
    second = _build(mint, SOL_MINT, token_amount, str(signer.pubkey()), dexes=sell_dexes,
                    slippage_bps=_int_env("MEV_SELL_SLIPPAGE_BPS", 0, 0, 50))
    required_profit = max(_int_env("MEV_MIN_NET_PROFIT_LAMPORTS", 10_000, 1_000, 10_000_000),
                          amount * min_bps // 10_000)

    def _costs(build):
        return tip_lamports + 10_000 + _priority_fee_lamports(first) + _priority_fee_lamports(build)

    # Judge the cycle by the EXPECTED return, then lock the profit on-chain:
    # the sell leg's minimum output is set to cover entry + costs + required
    # profit. If the price moves against us, the sell fails and the Jito
    # bundle (all-or-nothing) never lands, so nothing is spent.
    expected_back = int(second["outAmount"])
    required_back = amount + _costs(second) + required_profit
    if expected_back < required_back:
        return {"status": "no_edge", "mint": mint, "input_lamports": amount,
                "expected_back_lamports": expected_back,
                "estimated_net_lamports": expected_back - amount - _costs(second)}
    minimum_back = int(second["otherAmountThreshold"])
    net_minimum = minimum_back - amount - _costs(second)
    if net_minimum < required_profit:
        return {"status": "no_edge", "mint": mint, "input_lamports": amount,
                "minimum_back_lamports": minimum_back, "estimated_net_lamports": net_minimum}

    if quote_only or not _live_enabled():
        return {"status": "dry_run_opportunity", "mint": mint, "input_lamports": amount,
                "minimum_back_lamports": minimum_back, "estimated_net_lamports": net_minimum}

    try:
        return _send_live(mint, signer, amount, tip_lamports, first, second, net_minimum)
    except Exception as exc:
        raise LiveSendError(_error_code(exc), net_minimum) from exc


class LiveSendError(RuntimeError):
    """A profitable cycle was found but could not be sent."""
    def __init__(self, reason, net):
        super().__init__(reason)
        self.reason = reason
        self.net = net


def _send_live(mint, signer, amount, tip_lamports, first, second, net_minimum):
    # Check MEV readiness without turning on the separate spot-trading engine.
    _preflight(signer, amount, tip_lamports, first, second)
    allowed, why = _can_submit(mint)
    if not allowed:
        raise RuntimeError("Pending limit: " + why)
    tip_ix = transfer(TransferParams(
        from_pubkey=signer.pubkey(), to_pubkey=_tip_account(), lamports=tip_lamports))
    tx1 = _compile_and_sign(first, signer)
    tx2 = _compile_and_sign(second, signer, [tip_ix])
    encoded = _simulate_bundle([tx1, tx2])
    key = mint + ":" + str(time.time_ns())
    _save_pending({"status": "submitting", "created_at": int(time.time()), "mint": mint}, key)
    bundle_id = _jito("sendBundle", [encoded, {"encoding": "base64"}])
    if not isinstance(bundle_id, str) or not bundle_id:
        raise RuntimeError("Jito did not return a bundle ID")
    _save_pending({"status": "pending", "bundle_id": bundle_id, "created_at": int(time.time()), "mint": mint,
                  "estimated_net_lamports": net_minimum, "input_lamports": amount}, key)
    # Do not automatically retry an uncertain bundle; the caller must reconcile it.
    return {"status": "submitted_pending", "bundle_id": bundle_id, "mint": mint,
            "estimated_net_lamports": net_minimum}


import collections
_RECENT = collections.deque(maxlen=20000)   # (time, status, reason) of every quote, for the panel


def _note(result):
    _RECENT.append((time.time(), result.get("status", "?"), str(result.get("error") or "")))


def why_text(now=None):
    """Last hour, in plain words: why orders were (not) opened."""
    now = time.time() if now is None else now
    rows = [(s, r) for t, s, r in _RECENT if now - t < 3600]
    if not rows:
        return "Última hora: ainda sem análises."
    count = collections.Counter(s for s, _ in rows)
    lines = ["Última hora: " + str(len(rows)) + " análises · com lucro: " +
             str(count["dry_run_opportunity"] + count["submitted_pending"] + count["live_failed"]) +
             " · ordens enviadas: " + str(count["submitted_pending"]) +
             " · sem lucro: " + str(count["no_edge"]) + " · puladas: " + str(count["skipped"])]
    failed = collections.Counter(r for s, r in rows if s == "live_failed")
    if failed:
        lines.append("Achou lucro mas não enviou: " + "; ".join(str(n) + "× " + r[:60] for r, n in failed.most_common(3)))
    skipped = collections.Counter(r for s, r in rows if s == "skipped")
    if skipped:
        lines.append("Puladas: " + "; ".join(str(n) + "× " + r[:40] for r, n in skipped.most_common(3)))
    if not failed and not count["submitted_pending"]:
        lines.append("Motivo de não abrir: nenhuma diferença de preço deu lucro depois das taxas.")
    try:
        summary = pending_summary()
        lines.append("Ordens aguardando confirmação: " + str(summary["in_flight"]) + "/" + str(MAX_PENDING) +
                     (" · ⚠️ envio incerto bloqueando (confira no Solscan)" if summary["uncertain"] else ""))
    except Exception:
        pass
    return "\n".join(lines)


_LAST_CHECKED = {}
LAST_RESULTS = {"at": 0.0, "results": []}
_COOLDOWN = {}          # mint -> wall-clock time until which the token is paused
_FAILS = {}             # mint -> phantom (simulation) failures so far
_COOLDOWN_LOADED = [False]
COVERAGE = {"universe": 0, "waiting": 0}
_ARB_SKIP_REASONS = {"not_solana", "missing_token_address"}


def _cooldown_path():
    return _pending_path().with_name("mev_cooldown.json")


def _load_cooldown():
    if _COOLDOWN_LOADED[0]:
        return
    _COOLDOWN_LOADED[0] = True
    try:
        data = json.loads(_cooldown_path().read_text(encoding="utf-8"))
        now = time.time()
        _COOLDOWN.update({m: t for m, t in data.get("until", {}).items() if t > now})
        _FAILS.update(data.get("fails", {}))
    except Exception:
        pass


def _persist_cooldown():
    try:
        now = time.time()
        path = _cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"until": {m: t for m, t in _COOLDOWN.items() if t > now},
                                   "fails": {m: n for m, n in _FAILS.items() if m in _COOLDOWN}}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass   # pausing still works in memory


def _pause(mint, seconds):
    _load_cooldown()
    _COOLDOWN[mint] = time.time() + seconds
    _persist_cooldown()


def _pause_phantom(mint):
    """Quotes that keep failing on-chain are phantom: 30 min, then 1 h, 2 h... up to 24 h."""
    _load_cooldown()
    _FAILS[mint] = _FAILS.get(mint, 0) + 1
    base = _int_env("MEV_FAIL_COOLDOWN_SECONDS", 1800, 0, 86400)
    _pause(mint, min(86400, base * 2 ** (_FAILS[mint] - 1)))


def _cooling(mint, now=None):
    _load_cooldown()
    return _COOLDOWN.get(mint, 0) > (time.time() if now is None else now)


def pick_candidates(assessments, limit=None, now=None):
    """Choose which tokens to quote this cycle, from ALL scanned tokens.

    The cycle is atomic (buy and sell land together or not at all), so the
    holding-risk filters of the spot scanner (age, volume, FDV) do not apply;
    only a minimum pool liquidity is required. Tokens whose pools disagree the
    most across DEXs go first, and recently quoted tokens wait their turn so
    every token gets checked over successive cycles.
    """
    limit = limit or _int_env("MEV_TOKENS_PER_CYCLE", 3, 1, 30)
    recheck = _int_env("MEV_RECHECK_SECONDS", 300, 0, 3600)
    now = time.monotonic() if now is None else now
    unique = {}
    for item in assessments or []:
        if not isinstance(item, dict) or not item.get("token"):
            continue
        if _ARB_SKIP_REASONS.intersection(item.get("reasons") or []):
            continue
        if float(item.get("liquidity_usd") or 0) < MIN_ARB_POOL_LIQUIDITY_USD:
            continue
        if _cooling(item["token"]):
            continue
        unique.setdefault(item["token"], item)
    ranked = []
    for item in unique.values():
        route = _cross_dex_route(item.get("dex_pools"))
        spread = route["spread_pct"] if route else 0.0
        # Every token waits its turn (no shortcut for "big spread" tokens, which
        # are often the same untradeable ones): the whole list rotates.
        if now - _LAST_CHECKED.get(item["token"], -1e9) >= recheck:
            ranked.append((spread, item, route))
    ranked.sort(key=lambda x: (-x[0], _LAST_CHECKED.get(x[1]["token"], -1e9)))
    if len(_LAST_CHECKED) > 5000:
        _LAST_CHECKED.clear()
    COVERAGE["universe"] = len(unique)
    COVERAGE["waiting"] = len(ranked)
    return [(item, route) for _, item, route in ranked[:limit]]


def coverage_text(now=None):
    """How many tokens the rotation is covering."""
    now = time.monotonic() if now is None else now
    last_hour = sum(1 for t in _LAST_CHECKED.values() if now - t < 3600)
    return ("Moedas na lista: " + str(COVERAGE["universe"]) + " | analisadas na última hora: " +
            str(last_hour) + " | pausadas (sem rota/autoridade/fantasma): " + str(sum(1 for t in _COOLDOWN.values() if t > time.time())))


_ERROR_LABELS = (
    ("Mint or freeze authority", "moeda com autoridade de emissão/congelamento"),
    ("MEV live switches", "modo real desligado"),
    ("Private Telegram owner", "TELEGRAM_CHAT_ID não confere com o dono"),
    ("Unreconciled spot order", "ordem antiga pendente"),
    ("Jupiter API key", "falta JUPITER_API_KEY"),
    ("Dedicated Solana RPC", "falta SOLANA_RPC_URL dedicado"),
    ("passphrase", "falta senha da carteira"),
    ("balance is below", "saldo SOL insuficiente"),
    ("Pending limit: ", None),
    ("unresolved", "bundle anterior pendente"),
    ("simulation failed: ", None),
    ("simulation", "simulação do bundle falhou"),
    ("no tokens", "rota sem tokens"),
)


def _error_code(exc):
    """Short, secret-free reason shown in the Telegram history."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return "HTTP_" + str(exc.response.status_code) + (" (sem rota nessa DEX)" if exc.response.status_code == 400 else "")
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if text.startswith(("Jito ", "RPC ")):
            return text[:120]
        for needle, label in _ERROR_LABELS:
            if needle in text:
                if label:
                    return label
                rest = text.split(needle, 1)[1]
                return (("simulação: " if needle.startswith("simulation") else "") + rest)[:160]
    return type(exc).__name__


def scan_cycle(assessments, force=False, quote_only=False):
    """Use the read-only token scanner; live submits require MEV ON in Telegram."""
    import live_trade_state
    if not force and (not live_trade_state.mev_autotrade_allowed() or
                      not live_trade_state.flag("mev_enabled")):
        return []
    if _live_enabled() and not quote_only:
        allowed, reason, _, _ = live_trade_state.mev_daily_guard()
        if not allowed:
            LOG.warning("MEV daily risk guard blocked live bundle: %s", reason)
            return []
    if not _LOCK.acquire(blocking=False):
        return []
    try:
        results = []
        for item, route in pick_candidates(assessments):
            mint = item["token"]
            _LAST_CHECKED[mint] = time.monotonic()
            try:
                result = None
                if route:
                    try:
                        result = try_arbitrage(mint, quote_only=quote_only,
                                               buy_dexes=route["buy_dexes"],
                                               sell_dexes=route["sell_dexes"])
                        result["route"] = route["buy_dex_id"] + " -> " + route["sell_dex_id"]
                        result["observed_spread_pct"] = route["spread_pct"]
                    except requests.HTTPError as exc:
                        # No route on the chosen DEXs: fall back to Jupiter's normal route.
                        if exc.response is None or exc.response.status_code != 400:
                            raise
                if result is None:
                    result = try_arbitrage(mint, quote_only=quote_only)
                LOG.info("MEV cycle status=%s mint=%s net_min_lamports=%s route=%s",
                         result["status"], mint[:8], result.get("estimated_net_lamports", "n/a"),
                         result.get("route", "aggregated"))
                results.append(result)
                _note(result)
                live_trade_state.record_mev_event(result)
                if result["status"] == "submitted_pending":
                    break
            except LiveSendError as exc:
                if exc.reason.startswith("simulação"):
                    # Quotes that keep failing on-chain are phantom; give others the slot.
                    _pause_phantom(mint)
                event = {"status": "live_failed", "mint": str(mint)[:64], "error": exc.reason,
                         "estimated_net_lamports": exc.net}
                live_trade_state.record_mev_event(event)
                results.append(event)
                _note(event)
                LOG.warning("MEV live send failed mint=%s error=%s", str(mint)[:8], exc.reason)
            except Exception as exc:
                error_code = _error_code(exc)
                # Tokens that cannot be traded must not keep taking the few slots per round.
                if "autoridade" in error_code:
                    pause = 6 * 3600
                elif error_code.startswith("HTTP_400"):
                    pause = 600
                else:
                    pause = 300
                _pause(mint, pause)
                event = {"status": "skipped", "mint": str(mint)[:64], "error": error_code}
                live_trade_state.record_mev_event(event)
                results.append(event)
                _note(event)
                LOG.warning("MEV cycle skipped mint=%s error=%s", str(mint)[:8], error_code)
        LAST_RESULTS["at"] = time.time()
        LAST_RESULTS["results"] = list(results)
        return results
    finally:
        _LOCK.release()
