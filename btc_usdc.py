"""USDC for Jupiter Perps collateral: swap SOL -> USDC when the bot wallet lacks it."""
import os
import time

SOL_RESERVE_LAMPORTS = 50_000_000  # keep 0.05 SOL for fees and account rent


def ensure_usdc(amount):
    import jupiter_perps as jp
    import live_order_bridge as bridge
    from bot_wallet_service import get_address
    from jupiter_v2_executor import order, sign_order, execute_signed, SOL_MINT, USDC_MINT
    owner = get_address()
    have = jp.usdc_balance(owner)
    if have >= amount:
        return have
    need_usd = (amount - have) * 1.02
    lamports = int(need_usd / float(bridge.sol_usd_price()) * 1e9)
    from solana_settlement import get_sol_balance
    if get_sol_balance(owner) < lamports + SOL_RESERVE_LAMPORTS:
        raise RuntimeError("Saldo insuficiente: deposite mais SOL na carteira do bot")
    quote = order(SOL_MINT, USDC_MINT, lamports, taker=owner)
    execute_signed(sign_order(quote, os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"]), quote["requestId"])
    for _ in range(20):
        time.sleep(3)
        have = jp.usdc_balance(owner)
        if have >= amount:
            return have
    raise RuntimeError("Troca SOL→USDC não confirmou a tempo")
