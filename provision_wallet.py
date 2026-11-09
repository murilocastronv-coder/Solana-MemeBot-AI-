"""Provision a dedicated Solana mainnet wallet on a trusted local machine.
NEVER run this in CI, GitHub Actions, or an ephemeral Render container.
No transactions or withdrawals are performed.
"""
import json
import os
from pathlib import Path
from solders.keypair import Keypair
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
import base64
import getpass

def main():
    destination = Path(os.environ.get("BOT_WALLET_FILE", "bot_wallet.enc"))
    if destination.exists():
        raise SystemExit("Wallet file already exists; refusing to overwrite")
    password = getpass.getpass("New wallet encryption passphrase (not your Phantom seed): ")
    confirm = getpass.getpass("Confirm passphrase: ")
    if len(password) < 16 or password != confirm:
        raise SystemExit("Passphrase must have at least 16 characters and match")
    salt = os.urandom(16)
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    cipher = Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode())))
    wallet = Keypair()
    encrypted = cipher.encrypt(bytes(wallet))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"version": 1, "salt": base64.b64encode(salt).decode(),
                       "ciphertext": encrypted.decode(), "public_address": str(wallet.pubkey())}, out)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print("Public Solana address:", wallet.pubkey())
    print("Encrypted wallet file:", destination)
    print("BACK UP this encrypted file AND the passphrase separately before depositing.")
    print("This tool does not activate real trading or automated withdrawals.")

if __name__ == "__main__":
    main()
