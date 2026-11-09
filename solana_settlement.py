"""Solana transaction finality and balance reconciliation helpers.

Read-only RPC calls. A finalized signature is not proof of received tokens;
compare pre/post balances and inspect transaction metadata before settlement.
"""
import time
from live_swap_executor import rpc, SwapError

def wait_for_finalized(signature, timeout_seconds=90, interval_seconds=3):
    if not isinstance(signature, str) or len(signature) < 70:
        raise ValueError("Invalid transaction signature")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        values = response.get("value", []) if isinstance(response, dict) else []
        status = values[0] if values else None
        if status:
            if status.get("err") is not None:
                raise SwapError("Transaction finalized with an on-chain error")
            if status.get("confirmationStatus") == "finalized":
                return {"signature": signature, "finalized": True, "slot": status.get("slot")}
        time.sleep(interval_seconds)
    raise TimeoutError("Transaction status uncertain: do not retry until reconciled")

def get_token_balance(owner, mint):
    result = rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "finalized"}])
    if not isinstance(result, dict):
        raise SwapError("Malformed token balance response")
    return sum(int(item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
               for item in result.get("value", []))

def get_sol_balance(owner):
    result = rpc("getBalance", [owner, {"commitment": "finalized"}])
    if not isinstance(result, dict) or "value" not in result:
        raise SwapError("Malformed SOL balance response")
    return int(result["value"])
