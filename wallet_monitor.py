"""Read-only wallet tracking. No custody, signing, withdrawals or trades."""
import os
import requests

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

def validate_public_address(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or any(c not in BASE58 for c in value):
        raise ValueError("Invalid public Solana address format")
    return value

def wallet_public_address():
    address = os.getenv("SOLANA_BOT_PUBLIC_ADDRESS", "").strip()
    return validate_public_address(address) if address else None

def rpc_balance_sol(address, rpc_url=None):
    address = validate_public_address(address)
    rpc_url = rpc_url or os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    if not rpc_url.startswith("https://"):
        raise ValueError("RPC URL must use HTTPS")
    payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address,{"commitment":"confirmed"}]}
    response = requests.post(rpc_url, json=payload, timeout=12)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError("Solana RPC returned an error")
    lamports = data["result"]["value"]
    if not isinstance(lamports, int) or lamports < 0:
        raise ValueError("Invalid RPC balance")
    return lamports / 1_000_000_000

def deposit_instructions():
    address = wallet_public_address()
    if not address:
        return "Configure SOLANA_BOT_PUBLIC_ADDRESS with your dedicated wallet's PUBLIC address only."
    return "Transfer SOL only on Solana network to: " + address + ". Verify the address in Phantom before sending."

def withdrawal_instructions():
    return ("Withdraw from the dedicated wallet in Phantom after verifying the destination and network. "
            "The bot cannot sign or send funds; automatic withdrawals are disabled.")

def main():
    address = wallet_public_address()
    print(deposit_instructions())
    print(withdrawal_instructions())
    if address:
        print("Read-only balance (SOL):", rpc_balance_sol(address))

if __name__ == "__main__":
    main()
