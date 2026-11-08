"""Conservative atomic SOL/token/SOL arbitrage via Jupiter Swap V2 + Jito.

The strategy only trades against public pool prices. It never reads pending
user transactions and never constructs sandwich/front-running transactions.
Dry-run is the default. Live execution requires two dedicated switches:
MEV_AUTOTRADE_ENABLED=true and MEV_LIVE_ENABLED=true.
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


def _build(input_mint, output_mint, amount, taker):
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
        "taker": taker,
        "slippageBps": "50",
        "computeUnitPricePercentile": "medium",
        "forJitoBundle": "true",
        "wrapAndUnwrapSol": "true",
        "transactionVersion": "0",
    }
    response = requests.get(JUPITER_BUILD, params=params, headers=_headers(), timeout=20)
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


def _compile_and_sign(build, signer, extra_instructions=()):
    cu_limit = _int_env("MEV_COMPUTE_UNIT_LIMIT", 1_000_000, 200_000, 1_400_000)
    instructions = [Instruction(COMPUTE_BUDGET_PROGRAM, b"\x02" + struct.pack("<I", cu_limit), [])]
    for group in ("computeBudgetInstructions", "setupInstructions"):
        instructions.extend(_instruction(ix) for ix in build.get(group, []))
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
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError("Jito " + method + " request failed")
    return payload.get("result")


def _tip_account():
    accounts = _jito("getTipAccounts", [])
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("Jito returned no tip accounts")
    # Stable rotation avoids repeatedly contending for the first account.
    return Pubkey.from_string(accounts[int(time.time()) % len(accounts)])


def _bundle_status(bundle_id):
    result = _jito("getInflightBundleStatuses", [[bundle_id]])
    if isinstance(result, dict):
        values = result.get("value") or []
        if values:
            return str(values[0].get("status", "unknown")).lower()
    return "unknown"


def _pending_path():
    configured = os.getenv("MEV_PENDING_FILE", "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise RuntimeError("MEV_PENDING_FILE must be an absolute path on persistent storage")
        return path
    from bot_wallet_service import wallet_path
    return wallet_path().with_name("mev_pending_bundle.json")


def _save_pending(record):
    path = _pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, path)


def _has_unresolved_bundle():
    path = _pending_path()
    if not path.exists():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # corrupted state blocks new spends
    bundle_id = record.get("bundle_id")
    if not bundle_id:
        return True  # send outcome was uncertain; never resend automatically
    try:
        status = _bundle_status(bundle_id)
        if status == "unknown":
            result = _jito("getBundleStatuses", [[bundle_id]])
            values = result.get("value", []) if isinstance(result, dict) else []
            if values:
                status = str(values[0].get("confirmation_status") or values[0].get("status") or "unknown").lower()
        if status in ("landed", "confirmed", "finalized", "failed", "invalid"):
            path.unlink(missing_ok=True)
            LOG.info("Resolved previous Jito bundle status=%s id=%s", status, bundle_id[:10])
            return False
    except Exception as exc:
        LOG.warning("Could not reconcile previous Jito bundle: %s", type(exc).__name__)
    return True


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


def _simulate_bundle(transactions):
    encoded = [base64.b64encode(bytes(tx)).decode("ascii") for tx in transactions]
    result = _jito("simulateBundle", [encoded, {"encoding": "base64"}])
    if not isinstance(result, dict):
        raise RuntimeError("Jito bundle simulation returned no result")
    value = result.get("value") or result
    if str(value.get("summary", "")).lower() != "succeeded":
        raise RuntimeError("Jito bundle simulation did not succeed")
    for item in value.get("transactionResults", []):
        if item.get("err"):
            raise RuntimeError("Jito bundle simulation reported a transaction error")
    return encoded


def _live_enabled():
    try:
        import live_trade_state
        armed = live_trade_state.flag("mev_real_armed")
    except Exception:
        armed = False
    return (os.getenv("MEV_AUTOTRADE_ENABLED") == "true" and
            os.getenv("MEV_LIVE_ENABLED") == "true" and armed)


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
    response = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
        "method": "getBalance", "params": [str(signer.pubkey()), {"commitment": "confirmed"}]}, timeout=12)
    response.raise_for_status()
    balance = int(response.json().get("result", {}).get("value", -1))
    if balance < amount + tip_lamports + priority + rent_reserve:
        raise RuntimeError("Bot wallet SOL balance is below the trade and fee reserve")


def try_arbitrage(mint, quote_only=False):
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
    first = _build(SOL_MINT, mint, amount, str(signer.pubkey()))
    token_amount = int(first["outAmount"])
    if token_amount <= 0:
        raise RuntimeError("First Jupiter route returned no tokens")
    second = _build(mint, SOL_MINT, token_amount, str(signer.pubkey()))
    minimum_back = int(second["otherAmountThreshold"])
    fixed_cost_reserve = 10_000 + _priority_fee_lamports(first) + _priority_fee_lamports(second)
    required_profit = max(_int_env("MEV_MIN_NET_PROFIT_LAMPORTS", 100_000, 1_000, 10_000_000),
                          amount * min_bps // 10_000)
    net_minimum = minimum_back - amount - tip_lamports - fixed_cost_reserve
    if net_minimum < required_profit:
        return {"status": "no_edge", "mint": mint, "input_lamports": amount,
                "minimum_back_lamports": minimum_back, "estimated_net_lamports": net_minimum}

    if quote_only or not _live_enabled():
        return {"status": "dry_run_opportunity", "mint": mint, "input_lamports": amount,
                "minimum_back_lamports": minimum_back, "estimated_net_lamports": net_minimum}

    # Check MEV readiness without turning on the separate spot-trading engine.
    _preflight(signer, amount, tip_lamports, first, second)
    if _has_unresolved_bundle():
        raise RuntimeError("A prior Jito bundle is unresolved; new submissions are paused")
    tip_ix = transfer(TransferParams(
        from_pubkey=signer.pubkey(), to_pubkey=_tip_account(), lamports=tip_lamports))
    tx1 = _compile_and_sign(first, signer)
    tx2 = _compile_and_sign(second, signer, [tip_ix])
    encoded = _simulate_bundle([tx1, tx2])
    _save_pending({"status": "submitting", "created_at": int(time.time()), "mint": mint})
    bundle_id = _jito("sendBundle", [encoded, {"encoding": "base64"}])
    if not isinstance(bundle_id, str) or not bundle_id:
        raise RuntimeError("Jito did not return a bundle ID")
    _save_pending({"status": "pending", "bundle_id": bundle_id, "created_at": int(time.time()), "mint": mint})
    # Do not automatically retry an uncertain bundle; the caller must reconcile it.
    return {"status": "submitted_pending", "bundle_id": bundle_id, "mint": mint,
            "estimated_net_lamports": net_minimum}


def scan_cycle(assessments, force=False, quote_only=False):
    """Use the read-only token scanner; live submits require MEV ON in Telegram."""
    import live_trade_state
    if not force and (os.getenv("MEV_AUTOTRADE_ENABLED") != "true" or
                      not live_trade_state.flag("mev_enabled")):
        return []
    if not _LOCK.acquire(blocking=False):
        return []
    try:
        candidates = []
        for item in assessments or []:
            if isinstance(item, dict) and item.get("status") == "WATCH_ONLY" and item.get("token"):
                candidates.append(item["token"])
        results = []
        for mint in list(dict.fromkeys(candidates))[:2]:
            try:
                result = try_arbitrage(mint, quote_only=quote_only)
                LOG.info("MEV cycle status=%s mint=%s net_min_lamports=%s",
                         result["status"], mint[:8], result.get("estimated_net_lamports", "n/a"))
                results.append(result)
                live_trade_state.record_mev_event(result)
                if result["status"] == "submitted_pending":
                    break
            except Exception as exc:
                live_trade_state.record_mev_event({"status": "skipped", "mint": str(mint)[:64],
                                                   "error": type(exc).__name__})
                LOG.warning("MEV cycle skipped mint=%s error=%s", str(mint)[:8], type(exc).__name__)
        return results
    finally:
        _LOCK.release()
