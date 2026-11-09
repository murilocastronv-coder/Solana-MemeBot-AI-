"""Create one Solana keypair encrypted on a persistent disk.
Never reveal private key via Telegram, logs or GitHub.
"""
import os,json,base64,threading
from pathlib import Path
from solders.keypair import Keypair
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
_LOCK=threading.Lock()

def wallet_path():
    raw=os.getenv("BOT_WALLET_FILE","").strip()
    if not raw or not Path(raw).is_absolute():
        raise RuntimeError("Configure BOT_WALLET_FILE as an absolute path on a persistent disk")
    return Path(raw)

def create_or_get():
    password=os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE","")
    if len(password)<24:
        raise RuntimeError("Configure BOT_WALLET_ENCRYPTION_PASSPHRASE (24+ characters) in Render secrets")
    path=wallet_path()
    with _LOCK:
        if path.exists():
            data=json.loads(path.read_text(encoding="utf-8"))
            if data.get("version")!=1 or not data.get("public_address"):
                raise RuntimeError("Wallet file invalid; creation blocked")
            return data["public_address"],False
        if not path.parent.is_dir():
            raise RuntimeError("Persistent wallet directory missing")
        salt=os.urandom(16)
        key=Scrypt(salt=salt,length=32,n=2**15,r=8,p=1).derive(password.encode())
        wallet=Keypair()
        address=str(wallet.pubkey())
        payload={"version":1,"salt":base64.b64encode(salt).decode(),
                 "ciphertext":Fernet(base64.urlsafe_b64encode(key)).encrypt(bytes(wallet)).decode(),
                 "public_address":address}
        fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(payload,f)
            f.flush()
            os.fsync(f.fileno())
        return address,True

def get_address():
    path=wallet_path()
    if not path.exists():return None
    data=json.loads(path.read_text(encoding="utf-8"))
    return data["public_address"] if data.get("version")==1 else None

def export_private_key(password):
    """Decrypt only after passphrase validation; caller must never log output."""
    from cryptography.fernet import InvalidToken
    data=json.loads(wallet_path().read_text(encoding="utf-8"))
    if data.get("version") != 1: raise ValueError("Invalid wallet")
    salt=base64.b64decode(data["salt"],validate=True)
    derived=Scrypt(salt=salt,length=32,n=2**15,r=8,p=1).derive(password.encode())
    try:
        secret=Fernet(base64.urlsafe_b64encode(derived)).decrypt(data["ciphertext"].encode())
    except InvalidToken:
        raise ValueError("Invalid password or wallet")
    wallet=Keypair.from_bytes(secret)
    if str(wallet.pubkey()) != data["public_address"]:
        raise ValueError("Wallet address mismatch")
    return str(wallet.pubkey()), str(wallet)
