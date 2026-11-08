"""Fail-closed bridge between persistent order ledger and Jupiter Swap V2.

Live execution requires an explicitly enabled service, a verified owner,
and pre-existing funded positions. Never retry an ambiguous submission.
This module does not enable autonomous trading by itself.
"""
import os
import uuid
from decimal import Decimal, ROUND_DOWN
from solders.pubkey import Pubkey
import live_trade_state as state
import telegram_owner
from jupiter_v2_executor import order, sign_order, execute_signed, USDC_MINT, SOL_MINT, OrderError
from solana_settlement import wait_for_finalized, get_token_balance, get_sol_balance

class TradingNotReady(RuntimeError):
    pass

LAMPORTS_PER_SOL = 1_000_000_000
FEE_RESERVE_LAMPORTS = 10_000_000

MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "100"))
MIN_ORDER_USD = 1.0

def sol_usd_price():
    quote = order(SOL_MINT, USDC_MINT, LAMPORTS_PER_SOL)
    price = Decimal(int(quote["outAmount"])) / Decimal(1_000_000)
    if not price.is_finite() or price <= 0:
        raise TradingNotReady("Invalid SOL/USD quote")
    return price

def sol_for_usd(wallet_address, usd):
    """Quote `usd` worth of SOL without signing or spending."""
    lamports = int((Decimal(str(usd)) * LAMPORTS_PER_SOL / sol_usd_price()).to_integral_value(rounding=ROUND_DOWN))
    if lamports <= 0 or get_sol_balance(wallet_address) < lamports + FEE_RESERVE_LAMPORTS:
        raise TradingNotReady("Insufficient SOL for USD " + format(float(usd), ".2f") + " buy plus 0.01 SOL fee reserve")
    return lamports

def sol_for_ten_usd(wallet_address):
    """Quote USD 10 worth of SOL without signing or spending."""
    quote = order(SOL_MINT, USDC_MINT, LAMPORTS_PER_SOL)
    usd_per_sol = Decimal(int(quote["outAmount"])) / Decimal(1_000_000)
    if not usd_per_sol.is_finite() or usd_per_sol <= 0:
        raise TradingNotReady("Invalid SOL/USD quote")
    lamports = int((Decimal("10") * LAMPORTS_PER_SOL / usd_per_sol).to_integral_value(rounding=ROUND_DOWN))
    if lamports <= 0 or get_sol_balance(wallet_address) < lamports + FEE_RESERVE_LAMPORTS:
        raise TradingNotReady("Insufficient SOL for USD 10 buy plus 0.01 SOL fee reserve")
    return lamports

def live_trading_allowed():
    """Spot trading permission is on by default; LIVE_TRADING_ENABLED=false blocks it."""
    return os.getenv("LIVE_TRADING_ENABLED","true").strip().lower()!="false"

def preflight():
    if not live_trading_allowed():
        raise TradingNotReady("Live trading switch disabled")
    owner = telegram_owner.get_owner()
    configured = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not owner:
        raise TradingNotReady("Private Telegram owner has not been registered")
    if configured and str(owner) != configured:
        raise TradingNotReady("Configured Telegram owner does not match the registered private owner")
    if not os.getenv("JUPITER_API_KEY"):
        raise TradingNotReady("Jupiter API key required for reliable live execution")
    if not os.getenv("SOLANA_RPC_URL") or os.getenv("SOLANA_RPC_URL") == "https://api.mainnet-beta.solana.com":
        raise TradingNotReady("Dedicated Solana RPC required for reliable live execution")
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
    if mint in (USDC_MINT, SOL_MINT):
        raise ValueError("Invalid target mint")
    from bot_wallet_service import get_address
    if wallet_address != get_address():
        raise TradingNotReady("Wallet address does not match encrypted signer")
    if side == "buy" and not MIN_ORDER_USD * 1_000_000 <= int(amount_raw) <= MAX_ORDER_USD * 1_000_000:
        raise TradingNotReady("Buy size must be between USD 1 and MAX_ORDER_USD (" + format(MAX_ORDER_USD, ".0f") + ")")
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
    order_usd = int(amount_raw) / 1_000_000 if side == "buy" else 0.0
    swap_amount = sol_for_usd(wallet_address, order_usd) if side == 'buy' else int(amount_raw)
    order_id = uuid.uuid4().hex
    state.reserve(order_id, mint, side)
    # From this point, failures require explicit reconciliation; do not
    # automatically delete the pending order or resend the transaction.
    input_mint, output_mint = (SOL_MINT, mint) if side == "buy" else (mint, SOL_MINT)
    stage = "quoting"
    try:
        before = get_token_balance(wallet_address, mint)
        before_sol = get_sol_balance(wallet_address)
        state.annotate_pending(order_id, before_token_raw=before, before_sol_raw=before_sol, input_amount_raw=swap_amount, cost_usd=order_usd, stage="quoting")
        response = order(input_mint, output_mint, swap_amount, taker=wallet_address)
        stage = "signing"
        state.annotate_pending(order_id, request_id=response["requestId"], stage="signing")
        signed = sign_order(response, os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"])
        state.annotate_pending(order_id, stage="submitting")
        stage = "submitting"
        try:
            result = execute_signed(signed, response["requestId"])
        except OrderError:
            stage = "rejected"  # Jupiter answered explicitly that the swap failed: nothing landed
            raise
        stage = "submitted"
        state.annotate_pending(order_id, signature=result["signature"], stage="submitted")
        finalized = wait_for_finalized(result["signature"])
        state.annotate_pending(order_id, stage="finalized")
        after = get_token_balance(wallet_address, mint)
        if side == "buy" and after <= before:
            raise TradingNotReady("Buy token balance not increased: reconciliation needed")
        if side == "sell" and after >= before:
            raise TradingNotReady("Sell token balance not decreased: reconciliation needed")
        return {"order_id": order_id, "side": side, "mint": mint,
                "signature": result["signature"], "finalized": finalized["finalized"],
                "before_token_raw": before, "after_token_raw": after,
                "input_amount_raw": swap_amount, "input_mint": input_mint, "output_mint": output_mint,
                "reconciliation_required": True}
    except Exception as exc:
        # Never include credentials or signed transactions in the reason.
        reason = type(exc).__name__ + ": " + str(exc)[:160]
        if stage in ("quoting", "signing", "rejected"):
            # Nothing was sent (or Jupiter confirmed failure): release immediately.
            state.discard_pending(order_id)
            raise TradingNotReady("Não enviada (" + stage + "): " + reason) from None
        raise TradingNotReady("Resultado incerto (" + stage + "): " + reason) from None
