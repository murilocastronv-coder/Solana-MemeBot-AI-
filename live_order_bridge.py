"""Fail-closed bridge between persistent order ledger and Jupiter Swap V2.

Live execution requires an explicitly enabled service, a verified owner,
and pre-existing funded positions. Never retry an ambiguous submission.
This module does not enable autonomous trading by itself.
"""
import os
import uuid
from solders.pubkey import Pubkey
import live_trade_state as state
import telegram_owner
from jupiter_v2_executor import order, sign_order, execute_signed, USDC_MINT
from solana_settlement import wait_for_finalized, get_token_balance

class TradingNotReady(RuntimeError):
    pass

def preflight():
    if os.getenv("LIVE_TRADING_ENABLED") != "true":
        raise TradingNotReady("Live trading switch disabled")
    owner = telegram_owner.get_owner()
    configured = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not owner or not configured or owner != configured:
        raise TradingNotReady("Owner must be verified and pinned in TELEGRAM_CHAT_ID")
    if not os.getenv("JUPITER_API_KEY"):
        raise TradingNotReady("Dedicated Jupiter API key required for live execution")
    if not os.getenv("SOLANA_RPC_URL"):
        raise TradingNotReady("Dedicated Solana RPC required for live execution")
    if not os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE"):
        raise TradingNotReady("Wallet decryption secret missing")
    if state._load()["pending"]:
        raise TradingNotReady("Unreconciled transaction: trading paused")
    return True

def submit_swap(mint, side, amount_raw, wallet_address):
    """Persist intent before network call; leave pending on any uncertainty.

    Does NOT settle position or PnL: actual on-chain balance differences and
    fees must be reconciled by a separate validated settlement step.
    """
    preflight()
    Pubkey.from_string(mint)
    if side not in ("buy", "sell") or int(amount_raw) <= 0:
        raise ValueError("Invalid order")
    if mint == USDC_MINT:
        raise ValueError("Invalid target mint")
    if side == "buy":
        allowed, reason = state.can_open()
        if not allowed:
            raise TradingNotReady(reason)
        if mint in state._load()["positions"]:
            raise TradingNotReady("Position already open")
    else:
        if mint not in state._load()["positions"]:
            raise TradingNotReady("Cannot sell an untracked position")
        tracked = int(state._load()["positions"][mint]["token_amount_raw"])
        if int(amount_raw) != tracked:
            raise TradingNotReady("Sell amount must equal tracked position")
    order_id = uuid.uuid4().hex
    state.reserve(order_id, mint, side)
    # From this point, failures require explicit reconciliation; do not
    # automatically delete the pending order or resend the transaction.
    input_mint, output_mint = (USDC_MINT, mint) if side == "buy" else (mint, USDC_MINT)
    try:
        before = get_token_balance(wallet_address, mint)
        response = order(input_mint, output_mint, int(amount_raw), taker=wallet_address)
        signed = sign_order(response, os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"])
        result = execute_signed(signed, response["requestId"])
        finalized = wait_for_finalized(result["signature"])
        after = get_token_balance(wallet_address, mint)
        if side == "buy" and after <= before:
            raise TradingNotReady("Buy token balance not increased: reconciliation needed")
        if side == "sell" and after >= before:
            raise TradingNotReady("Sell token balance not decreased: reconciliation needed")
        return {"order_id": order_id, "side": side, "mint": mint,
                "signature": result["signature"], "finalized": finalized["finalized"],
                "before_token_raw": before, "after_token_raw": after,
                "reconciliation_required": True}
    except Exception:
        # Do not log credentials or signed transactions.
        raise TradingNotReady("Order outcome uncertain or failed; pending reservation retained. Reconcile before retrying.") from None
