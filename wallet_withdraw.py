"""Owner's withdrawal: send SOL from the bot wallet to an address the owner typed (after confirming).

Keeps a little SOL for network fees on "tudo" (everything). Waits for the on-chain confirmation.
"""
import base64
import os
import time

from bot_wallet_service import export_private_key, get_address
from live_swap_executor import rpc

LAMPORTS = 1_000_000_000
FEE_LAMPORTS = 5_000
KEEP_ON_ALL = 3_000_000          # 0.003 SOL left in the wallet when withdrawing everything


class WithdrawError(RuntimeError):
    pass


def valid_address(text):
    from solders.pubkey import Pubkey
    try:
        pk = Pubkey.from_string(text.strip())
    except Exception:
        return None
    s = str(pk)
    if s == get_address():
        return None
    return s


def balance_lamports(address=None):
    return int((rpc("getBalance", [address or get_address(), {"commitment": "confirmed"}]) or {}).get("value", 0))


def amount_lamports(text, balance):
    """Lamports to send for '0.5', '0,5' or 'tudo'; raises WithdrawError if it does not fit."""
    t = text.strip().lower().replace(",", ".")
    if t in ("tudo", "all", "max"):
        lam = balance - KEEP_ON_ALL
    else:
        try:
            lam = int(round(float(t) * LAMPORTS))
        except ValueError:
            raise WithdrawError("valor inválido: mande o número em SOL (ex.: 0.5) ou 'tudo'")
    if lam <= 0:
        raise WithdrawError("saldo insuficiente")
    if lam + FEE_LAMPORTS > balance:
        raise WithdrawError("saldo insuficiente: a carteira tem " + format(balance / LAMPORTS, ".4f") + " SOL")
    return lam


def send_sol(to_address, lamports, wait_s=60):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    _, secret = export_private_key(os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"])
    kp = Keypair.from_base58_string(secret)
    if str(kp.pubkey()) != get_address():
        raise WithdrawError("carteira não confere")
    blockhash = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to_address), lamports=int(lamports)))
    tx = Transaction([kp], Message.new_with_blockhash([ix], kp.pubkey(), blockhash), blockhash)
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        st = (rpc("getSignatureStatuses", [[sig]]) or {}).get("value", [None])[0]
        if st:
            if st.get("err") is not None:
                raise WithdrawError("a rede recusou a transferência")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
        time.sleep(2)
    raise WithdrawError("enviado mas ainda sem confirmação; confira em https://solscan.io/tx/" + str(sig))
