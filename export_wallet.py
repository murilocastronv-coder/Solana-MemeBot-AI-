"""Export a dedicated Solana wallet key ONLY on a trusted local device.
Never run in a remote shell with session logging or on shared hardware.
"""
import base64
import getpass
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

def main():
    source = Path(os.environ.get("BOT_WALLET_FILE", "bot_wallet.enc"))
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise SystemExit("Unsupported wallet file")
    password = getpass.getpass("Wallet encryption passphrase: ")
    salt = base64.b64decode(data["salt"], validate=True)
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    try:
        raw = Fernet(key).decrypt(data["ciphertext"].encode())
    except InvalidToken:
        raise SystemExit("Wrong passphrase or damaged encrypted wallet")
    wallet = Keypair.from_bytes(raw)
    address = str(wallet.pubkey())
    if address != data["public_address"]:
        raise SystemExit("Public address mismatch. Export aborted.")
    print("Verified Solana deposit address:", address)
    print("To import into Phantom, use Import Private Key on a trusted device.")
    answer = input("Reveal private key locally now? Type EXPORT to confirm: ")
    if answer != "EXPORT":
        print("Cancelled")
        return
    # Do not send to Telegram, network, files, or logs.
    print("PRIVATE KEY (keep secret):", str(wallet))
    print("Clear your terminal history/screen and never share this key.")

if __name__ == "__main__":
    main()
