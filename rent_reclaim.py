"""Close the bot wallet's empty token accounts and get their rent (~0.002 SOL each) back.

Every new token the MEV cycle trades opens a token account that keeps ~0.002 SOL
as rent. After the atomic buy+sell the account is usually left empty; closing it
returns that SOL to the wallet. Only accounts with a zero balance are touched.
"""
import base64
import logging

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

LOG = logging.getLogger("rent_reclaim")
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
MAX_PER_TX = 8


def empty_accounts(rpc, owner):
    found = []
    for program in TOKEN_PROGRAMS:
        payload = rpc("getTokenAccountsByOwner", [owner, {"programId": program},
                                                  {"encoding": "jsonParsed", "commitment": "confirmed"}])
        for item in ((payload or {}).get("result") or {}).get("value") or []:
            try:
                info = item["account"]["data"]["parsed"]["info"]
                amount = info["tokenAmount"]["amount"]
                lamports = int(item["account"]["lamports"])
            except (KeyError, TypeError, ValueError):
                continue
            if amount == "0" and info.get("owner") == owner and info.get("state") == "initialized":
                found.append((item["pubkey"], program, lamports))
    return found


def close_ix(account, program, owner):
    # SPL Token CloseAccount (instruction 9): rent goes back to the owner.
    return Instruction(Pubkey.from_string(program), bytes([9]), [
        AccountMeta(Pubkey.from_string(account), False, True),
        AccountMeta(owner, False, True),
        AccountMeta(owner, True, False),
    ])


def reclaim(rpc, signer):
    """Close up to MAX_PER_TX empty accounts. Returns (closed_count, lamports, signature)."""
    owner = signer.pubkey()
    accounts = empty_accounts(rpc, str(owner))[:MAX_PER_TX]
    if not accounts:
        return 0, 0, None
    blockhash = ((rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("result") or {}).get("value", {}).get("blockhash")
    if not blockhash:
        raise RuntimeError("RPC getLatestBlockhash sem resposta")
    message = Message.new_with_blockhash([close_ix(a, p, owner) for a, p, _ in accounts], owner,
                                         Hash.from_string(blockhash))
    tx = Transaction([signer], message, Hash.from_string(blockhash))
    encoded = base64.b64encode(bytes(tx)).decode("ascii")
    result = rpc("sendTransaction", [encoded, {"encoding": "base64", "preflightCommitment": "confirmed"}])
    if not isinstance(result, dict) or result.get("error") or not result.get("result"):
        raise RuntimeError("RPC sendTransaction recusou o fechamento")
    return len(accounts), sum(l for _, _, l in accounts), result["result"]
