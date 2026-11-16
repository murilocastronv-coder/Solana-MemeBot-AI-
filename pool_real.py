"""Real-money side of the pool robot (owner, Nov 16: "pode colocar dinheiro real, eu já fiz isso antes").

Enter: half of the capital (USDC) is swapped to the coin and the other half to the pool's quote token (SOL/USDT, or
kept in USDC) on Jupiter; one transaction opens a Meteora DLMM position of 69 bins centred on the active bin and adds
both sides with the SpotBalanced strategy (SOL is wrapped inside the transaction). Always SIMULATED first.
Exit: one transaction claims the fees, removes all liquidity and closes the position (the position rent comes back),
SOL is unwrapped; then everything is sold back to USDC. Result = USDC after - USDC used.
Needs ~0.07 SOL in the wallet for the position rent (returned at the exit) and fees.
"""
import base64
import logging
import os

import kamino as k
import liq_bot as lb
import meteora as mt

SOL, USDC, USDT = lb.SOL, lb.USDC, lb.USDT
RENT_SOL = 0.07


def _send(rpc, msg, signers, wait_s=40):
    import time
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction(msg, signers)
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
    end = time.time() + wait_s
    while time.time() < end:
        time.sleep(2)
        st = ((rpc("getSignatureStatuses", [[sig]]) or {}).get("value") or [None])[0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, st.get("err") is None
    return sig, False


def _compile(rpc, me, ixs):
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    bh = ((rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}).get("blockhash")
    return MessageV0.try_compile(Pubkey.from_string(me), [set_compute_unit_limit(800_000), set_compute_unit_price(lb.CU_PRICE)] + ixs,
                                 [], Hash.from_string(bh))


def _accounts(rpc, me, p):
    lower, width, upper = mt.position_range(p["active_id"])
    la, ua = mt.bin_array(p["address"], mt.bin_array_index(lower)), mt.bin_array(p["address"], mt.bin_array_index(upper))
    ext = mt.bitmap_extension(p["address"])
    have = lb._multi(rpc, [la, ua, ext], {"offset": 0, "length": 0})
    return lower, width, upper, la, ua, (ext if ext in have else mt.DLMM), (la in have and ua in have)


def _sell_all(rpc, me, mint, keep_lamports=0):
    if mint == USDC:
        return
    amt = (lb.sol_lamports(rpc, me) - keep_lamports) if mint == SOL else lb.token_balance(rpc, me, mint)
    if amt > 0:
        try:
            lb._jupiter_swap(mint, USDC, amt, me)
        except Exception as exc:
            lb.note("pool: venda de volta para USDC falhou (" + type(exc).__name__ + ")")


def enter(rpc, signer, pool, capital):
    """(position dict for the robot's state, '') or (None, why)."""
    from solders.keypair import Keypair
    me = str(signer.pubkey())
    acc = lb._multi(rpc, [pool["address"]]).get(pool["address"])
    p = mt.lb_pair(pool["address"], acc or b"")
    if not p:
        return None, "pool não encontrada"
    if p["flag_x"] or p["flag_y"]:
        return None, "pool com token 2022 (não suportado ainda)"
    if p["status"]:
        return None, "pool desativada"
    if lb.sol_lamports(rpc, me) < RENT_SOL * 1e9 + lb.SOL_RESERVE * 1e9:
        return None, "precisa de ~" + format(RENT_SOL + lb.SOL_RESERVE, "g") + " SOL na carteira (aluguel da posição, volta na saída)"
    usdc0 = lb.token_balance(rpc, me, USDC)
    cap = min(capital, usdc0 / 1e6)
    if cap < 10:
        return None, "pouco USDC na carteira (US$ " + format(usdc0 / 1e6, ".2f") + ")"
    lower, width, upper, la, ua, ext, ok = _accounts(rpc, me, p)
    if not ok:
        return None, "a faixa de preço da pool não tem as contas da Meteora criadas (custaria ~US$ 10 criar)"
    quote = p["mint_y"] if p["mint_y"] in (SOL, USDC, USDT) else p["mint_x"]
    coin = p["mint_x"] if quote == p["mint_y"] else p["mint_y"]
    half = int(cap / 2 * 1e6)
    sol_before = lb.sol_lamports(rpc, me)
    lb._jupiter_swap(USDC, coin, half, me)
    if quote != USDC:
        lb._jupiter_swap(USDC, quote, half, me)
    coin_amt = lb.token_balance(rpc, me, coin)
    quote_amt = (lb.sol_lamports(rpc, me) - sol_before - 5_000_000) if quote == SOL else (
        half if quote == USDC else lb.token_balance(rpc, me, quote))
    if coin_amt <= 0 or quote_amt <= 0:
        _sell_all(rpc, me, coin)
        _sell_all(rpc, me, quote, int((lb.SOL_RESERVE + RENT_SOL) * 1e9) if quote == SOL else 0)
        return None, "troca na Jupiter não entregou as moedas"
    amount_x, amount_y = (coin_amt, quote_amt) if coin == p["mint_x"] else (quote_amt, coin_amt)
    ux, uy = k.ata(me, p["mint_x"]), k.ata(me, p["mint_y"])
    pos_kp = Keypair()
    ixs = [k.ix_create_ata(me, me, p["mint_x"], k.TOKEN), k.ix_create_ata(me, me, p["mint_y"], k.TOKEN)]
    if quote == SOL:
        wsol = k.ata(me, SOL)
        ixs += [lb._ix_transfer(me, wsol, quote_amt), lb._ix_token([17], [(wsol, False, True)])]
    ixs += [mt.ix_initialize_position(me, str(pos_kp.pubkey()), p["address"], lower, width),
            mt.ix_add_liquidity(str(pos_kp.pubkey()), p, ext, ux, uy, la, ua, me, amount_x, amount_y, lower, upper)]
    msg = _compile(rpc, me, ixs)
    ok, why = lb.simulate(rpc, msg)
    if not ok:
        _sell_all(rpc, me, coin)
        _sell_all(rpc, me, quote, int((lb.SOL_RESERVE + RENT_SOL) * 1e9) if quote == SOL else 0)
        return None, "simulação recusou a entrada: " + why
    sig, done = _send(rpc, msg, [signer, pos_kp])
    if not done:
        _sell_all(rpc, me, coin)
        _sell_all(rpc, me, quote, int((lb.SOL_RESERVE + RENT_SOL) * 1e9) if quote == SOL else 0)
        return None, "a entrada não confirmou na rede"
    return {"position": str(pos_kp.pubkey()), "pair": p, "la": la, "ua": ua, "ext": ext, "quote": quote, "coin_mint": coin,
            "usdc_used": cap, "usdc_before": usdc0, "sig": sig}, ""


def exit_(rpc, signer, real):
    """(result USD or None, why)."""
    me = str(signer.pubkey())
    p = real["pair"]
    ux, uy = k.ata(me, p["mint_x"]), k.ata(me, p["mint_y"])
    ixs = [k.ix_create_ata(me, me, p["mint_x"], k.TOKEN), k.ix_create_ata(me, me, p["mint_y"], k.TOKEN),
           mt.ix_claim_fee(real["position"], p, ux, uy, real["la"], real["ua"], me),
           mt.ix_remove_all(real["position"], p, real["ext"], ux, uy, real["la"], real["ua"], me),
           mt.ix_close_position(real["position"], p, real["la"], real["ua"], me)]
    if SOL in (p["mint_x"], p["mint_y"]):
        wsol = k.ata(me, SOL)
        ixs.append(lb._ix_token([9], [(wsol, False, True), (me, False, True), (me, True, False)]))
    msg = _compile(rpc, me, ixs)
    ok, why = lb.simulate(rpc, msg)
    if not ok:
        return None, "simulação recusou a saída: " + why
    sig, done = _send(rpc, msg, [signer])
    if not done:
        return None, "a saída não confirmou na rede (tento de novo)"
    usdc_mid = lb.token_balance(rpc, me, USDC)
    _sell_all(rpc, me, real["coin_mint"])
    if real["quote"] != USDC:
        _sell_all(rpc, me, real["quote"], int((lb.SOL_RESERVE + 0.005) * 1e9) if real["quote"] == SOL else 0)
    usdc_end = lb.token_balance(rpc, me, USDC)
    result = (usdc_end - real["usdc_before"]) / 1e6
    logging.info("pool exit %s usdc mid %s end %s", sig, usdc_mid, usdc_end)
    return result, ""
