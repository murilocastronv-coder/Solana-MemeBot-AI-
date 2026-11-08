"""Jupiter Swap V2 order/sign/execute adapter.

Not wired to autonomous trading. Caller MUST reserve a persistent order,
validate the route and transaction, and reconcile balances before settlement.
Never log wallet secrets or signed transaction payloads.
"""
import base64
import os
import requests
import jupiter_rate_limiter
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from bot_wallet_service import export_private_key, get_address

BASE = "https://api.jup.ag/swap/v2"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

class OrderError(RuntimeError):
    pass

def headers():
    key = os.getenv("JUPITER_API_KEY", "").strip()
    return {"x-api-key": key, "Content-Type": "application/json"} if key else {"Content-Type": "application/json"}

def order(input_mint, output_mint, amount, taker=None):
    if input_mint == output_mint or int(amount) <= 0:
        raise ValueError("Invalid swap amount or pair")
    Pubkey.from_string(input_mint)
    Pubkey.from_string(output_mint)
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount))}
    if taker:
        Pubkey.from_string(taker)
        params["taker"] = taker
    response = jupiter_rate_limiter.get(BASE + "/order", params=params, headers=headers(), timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("requestId") or int(data.get("outAmount", 0)) <= 0:
        raise OrderError("Jupiter returned no valid route")
    if taker and not data.get("transaction"):
        raise OrderError("Jupiter returned no executable transaction")
    return data

def sign_order(order_data, wallet_passphrase):
    """Partial-sign for routes that may require a market-maker co-signature."""
    address = get_address()
    if not address:
        raise OrderError("Wallet missing")
    raw = base64.b64decode(order_data["transaction"], validate=True)
    unsigned = VersionedTransaction.from_bytes(raw)
    keys = unsigned.message.account_keys
    required = int(unsigned.message.header.num_required_signatures)
    if required < 1:
        raise OrderError("Transaction has no signers")
    # RFQ routes (JupiterZ market makers) put the market maker as fee payer and
    # ask the taker for a partial signature. We only require that our wallet is
    # exactly one of the required signers; we never sign as fee payer for others.
    signer_indices = [i for i in range(required) if str(keys[i]) == address]
    if len(signer_indices) != 1:
        raise OrderError("Wallet must be exactly one required signer")
    _, secret = export_private_key(wallet_passphrase)
    wallet = Keypair.from_base58_string(secret)
    if str(wallet.pubkey()) != address:
        raise OrderError("Wallet signer mismatch")
    signature = wallet.sign_message(to_bytes_versioned(unsigned.message))
    signatures = list(unsigned.signatures)
    if len(signatures) != required:
        raise OrderError("Invalid signer count")
    signatures[signer_indices[0]] = signature
    signed = VersionedTransaction.populate(unsigned.message, signatures)
    return base64.b64encode(bytes(signed)).decode("ascii")

def execute_signed(signed_transaction, request_id):
    if os.getenv("LIVE_TRADING_ENABLED") != "true":
        raise OrderError("Real swaps are disabled")
    if not request_id or not signed_transaction:
        raise OrderError("Missing signed order")
    response = jupiter_rate_limiter.post(BASE + "/execute", headers=headers(), timeout=45,
                             json={"signedTransaction": signed_transaction, "requestId": request_id})
    response.raise_for_status()
    result = response.json()
    if result.get("status") != "Success" or not result.get("signature"):
        raise OrderError("Swap execution uncertain or failed; reconcile requestId before retry")
    return {"signature": result["signature"],
            "total_input_amount": int(result["totalInputAmount"]),
            "total_output_amount": int(result["totalOutputAmount"])}
