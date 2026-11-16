"""REAL Pump.fun launch of an approved memecoin proposal (owner, Nov 16: "opção 2").

One launch only happens when the owner presses APROVAR on Telegram for that proposal AND the real mode is on.
Steps (nothing is spent before step 3):
1. the logo + name/symbol/description go to Pump.fun's IPFS upload -> metadata URI;
2. one transaction: create_v2 (Token-2022 coin, SOL pair, no mayhem, regular creator fees) + the creator's token
   account + buy_exact_quote_in_v3 with the creator buy (a small, public buy at the first price of the curve);
3. the transaction is SIMULATED first; only when the simulation passes it is signed and sent.
No sell instruction exists in this module: the creator tokens are never sold automatically.
Accounts and discriminators follow pump-fun/pump-public-docs (idl/pump.json, COIN_CREATION.md, TRADE_V3.md).
"""
import base64
import logging
import struct
import time

import requests

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
MAYHEM = "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
BUYBACK_RECIPIENTS = ["5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD", "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
                      "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL", "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
                      "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6", "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
                      "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD", "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"]
DISC_CREATE_V2 = bytes([214, 144, 76, 236, 95, 139, 49, 180])
DISC_BUY_QUOTE_IN_V3 = bytes([225, 247, 80, 30, 213, 179, 132, 136])
IPFS_URL = "https://pump.fun/api/ipfs"
# curve at creation: 30 SOL virtual / 1,073,000,000 virtual tokens (6 decimals)
V_SOL, V_TOK, DECIMALS = 30.0, 1_073_000_000.0, 6


def _pk(s):
    from solders.pubkey import Pubkey
    return Pubkey.from_string(s)


def pda(seeds, program):
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([s if isinstance(s, bytes) else bytes(_pk(s)) for s in seeds], _pk(program))[0])


def ata(owner, mint, token_program):
    return pda([owner, token_program, mint], ATA_PROGRAM)


def accounts(mint, user):
    bc = pda([b"bonding-curve", mint], PUMP)
    sol_vault = pda([b"sol-vault"], MAYHEM)
    return {"mint_authority": pda([b"mint-authority"], PUMP), "bonding_curve": bc,
            "associated_bonding_curve": ata(bc, mint, TOKEN22), "associated_quote_bonding_curve": ata(bc, WSOL, TOKEN),
            "global": pda([b"global"], PUMP), "global_params": pda([b"global-params"], MAYHEM), "sol_vault": sol_vault,
            "mayhem_state": pda([b"mayhem-state", mint], MAYHEM), "mayhem_token_vault": ata(sol_vault, mint, TOKEN22),
            "event_authority": pda([b"__event_authority"], PUMP), "user_ata": ata(user, mint, TOKEN22),
            "user_wsol": ata(user, WSOL, TOKEN), "user_volume_accumulator": pda([b"user_volume_accumulator", user], PUMP),
            "fee_config": pda([b"fee_config", PUMP], FEE_PROGRAM)}


def _s(text):
    b = text.encode()
    return struct.pack("<I", len(b)) + b


def _meta(k, signer=False, writable=False):
    from solders.instruction import AccountMeta
    return AccountMeta(_pk(k), signer, writable)


def ix_create_v2(mint, user, name, symbol, uri):
    from solders.instruction import Instruction
    a = accounts(mint, user)
    data = DISC_CREATE_V2 + _s(name) + _s(symbol) + _s(uri) + bytes(_pk(user)) + b"\x00"   # creator = user, no mayhem
    metas = [_meta(mint, True, True), _meta(a["mint_authority"]), _meta(a["bonding_curve"], writable=True),
             _meta(a["associated_bonding_curve"], writable=True), _meta(a["global"]), _meta(user, True, True), _meta(SYSTEM),
             _meta(TOKEN22), _meta(ATA_PROGRAM), _meta(MAYHEM, writable=True), _meta(a["global_params"]),
             _meta(a["sol_vault"], writable=True), _meta(a["mayhem_state"], writable=True),
             _meta(a["mayhem_token_vault"], writable=True), _meta(a["event_authority"]), _meta(PUMP)]
    return Instruction(_pk(PUMP), data, metas)


def ix_user_ata(mint, user):
    from solders.instruction import Instruction
    return Instruction(_pk(ATA_PROGRAM), bytes([1]), [_meta(user, True, True), _meta(ata(user, mint, TOKEN22), writable=True),
                                                     _meta(user), _meta(mint), _meta(SYSTEM), _meta(TOKEN22)])


def ix_buy_quote_in_v3(mint, user, lamports, min_tokens_out, buyback=None):
    from solders.instruction import Instruction
    a = accounts(mint, user)
    data = DISC_BUY_QUOTE_IN_V3 + struct.pack("<QQ", int(lamports), int(min_tokens_out))
    metas = [_meta(a["global"]), _meta(mint), _meta(WSOL), _meta(TOKEN22), _meta(TOKEN),
             _meta(a["bonding_curve"], writable=True), _meta(a["associated_bonding_curve"], writable=True),
             _meta(a["associated_quote_bonding_curve"], writable=True), _meta(user, True, True),
             _meta(a["user_ata"], writable=True), _meta(a["user_wsol"], writable=True),
             _meta(a["user_volume_accumulator"], writable=True), _meta(a["fee_config"]),
             _meta(buyback or BUYBACK_RECIPIENTS[int(time.time()) % 8], writable=True), _meta(SYSTEM),
             _meta(a["event_authority"]), _meta(PUMP)]
    return Instruction(_pk(PUMP), data, metas)


def tokens_for(sol):
    """Tokens (whole units) the very first buy of `sol` (after fees) gets from a fresh curve."""
    return V_TOK * sol / (V_SOL + sol)


def min_out(lamports):
    """Slippage guard: the buy is in the same transaction as the creation, so the price is known; 2% fee margin + 5%."""
    return int(tokens_for(lamports / 1e9 * 0.98) * 0.95 * 10 ** DECIMALS)


def upload_metadata(name, symbol, description, png, website="", twitter="", telegram="", post=None):
    """Logo + texts -> Pump.fun IPFS -> metadata URI (str) or raises."""
    post = post or requests.post
    r = post(IPFS_URL, data={"name": name, "symbol": symbol, "description": description, "twitter": twitter,
                             "telegram": telegram, "website": website, "showName": "true"},
             files={"file": (symbol.lower() + ".png", png, "image/png")}, timeout=40)
    r.raise_for_status()
    uri = (r.json() or {}).get("metadataUri") or ""
    if not uri.startswith("http") or len(uri) > 200:
        raise ValueError("upload da Pump.fun não devolveu o endereço dos dados")
    return uri


def build(rpc, user, mint, name, symbol, uri, buy_lamports, cu_price=50_000):
    from solders.compute_budget import set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    # no compute-limit instruction (default 200k per instruction is enough) to keep the transaction under 1232 bytes
    ixs = [set_compute_unit_price(cu_price), ix_create_v2(mint, user, name, symbol, uri),
           ix_user_ata(mint, user)]
    if buy_lamports > 0:
        ixs.append(ix_buy_quote_in_v3(mint, user, buy_lamports, min_out(buy_lamports)))
    bh = ((rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}).get("blockhash")
    return MessageV0.try_compile(_pk(user), ixs, [], Hash.from_string(bh))


def launch(rpc, signer, name, symbol, description, png, buy_lamports, simulate, upload=upload_metadata, wait_s=45):
    """Creates the coin for real. -> ({"mint", "sig", "uri"}, '') or (None, why). Nothing is sent if any check fails."""
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    name, symbol = name[:30], symbol[:13]          # 30 (limit 32) keeps the worst case under 1232 bytes
    user = str(signer.pubkey())
    try:
        uri = upload(name, symbol, description, png)
    except Exception as exc:
        return None, "upload do logo/dados na Pump.fun falhou (" + type(exc).__name__ + ") — nada foi gasto"
    mint_kp = Keypair()
    mint = str(mint_kp.pubkey())
    msg = build(rpc, user, mint, name, symbol, uri, buy_lamports)
    ok, why = simulate(rpc, msg)
    if not ok:
        return None, "a simulação recusou o lançamento: " + why + " — nada foi gasto"
    tx = VersionedTransaction(msg, [signer, mint_kp])
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
    end = time.time() + wait_s
    while time.time() < end:
        time.sleep(2)
        st = ((rpc("getSignatureStatuses", [[sig]]) or {}).get("value") or [None])[0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err") is None:
                logging.info("pump launch %s mint %s", sig, mint)
                return {"mint": mint, "sig": sig, "uri": uri}, ""
            return None, "a transação falhou na rede: " + str(st.get("err"))[:120]
    return None, "o lançamento não confirmou a tempo (assinatura " + str(sig) + ") — confira na Solscan"
