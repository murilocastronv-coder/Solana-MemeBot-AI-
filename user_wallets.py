"""One unique Solana wallet per Telegram account, plus a strong export password per account.

- The bot owner (TELEGRAM_CHAT_ID / registered owner) keeps the main bot wallet
  (bot_wallet_service, BOT_WALLET_FILE): it is the one the copy trade uses.
- Every other Telegram account gets its own new wallet the first time it uses the bot,
  stored encrypted on the persistent disk (user_wallets/<chat_id>.json, key derived from
  BOT_WALLET_ENCRYPTION_PASSPHRASE with scrypt). A wallet is never shared or reused.
- Export (web page): the first time, the person creates a strong export password; after
  that the password is required to reveal the private key (to import into Phantom).
  Only a scrypt hash of that password is stored.
Never log, print or send a private key or a password.
"""
import base64
import hmac
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

import bot_wallet_service
import telegram_owner

_LOCK = threading.RLock()
MIN_PASSWORD_LEN = 12
MAX_FAILS = 5                 # wrong export passwords per account ...
LOCK_SECONDS = 3600           # ... before export is blocked for 1 hour
_FAILS = {}                   # chat -> [timestamps of wrong passwords]


def _chat(chat):
    chat = str(chat)
    if not re.fullmatch(r"-?[0-9]{1,20}", chat):
        raise ValueError("Invalid Telegram chat id")
    return chat


def _base_dir():
    return bot_wallet_service.wallet_path().parent


def _wallet_file(chat):
    return _base_dir() / "user_wallets" / (_chat(chat) + ".json")


def _passwords_file():
    return _base_dir() / "export_passwords.json"


def _server_secret():
    password = os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE", "")
    if len(password) < 24:
        raise RuntimeError("Configure BOT_WALLET_ENCRYPTION_PASSPHRASE (24+ characters) in Render secrets")
    return password


def _kdf(salt, secret):
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(secret.encode())


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def is_owner(chat):
    try:
        owner = os.getenv("TELEGRAM_CHAT_ID", "").strip() or telegram_owner.get_owner()
    except Exception:
        return False
    return bool(owner) and str(owner) == str(chat)


# ---------------------------------------------------------------- wallets
def create_or_get(chat):
    """The wallet of this Telegram account: (address, created). Owner -> main bot wallet."""
    if is_owner(chat):
        return bot_wallet_service.create_or_get()
    path = _wallet_file(chat)
    with _LOCK:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not data.get("public_address"):
                raise RuntimeError("Wallet file invalid; creation blocked")
            return data["public_address"], False
        secret = _server_secret()
        salt = os.urandom(16)
        wallet = Keypair()                       # brand-new keypair: unique for this account
        address = str(wallet.pubkey())
        payload = {"version": 1, "chat_id": _chat(chat), "created_at": int(time.time()),
                   "salt": base64.b64encode(salt).decode(),
                   "ciphertext": Fernet(base64.urlsafe_b64encode(_kdf(salt, secret))).encrypt(bytes(wallet)).decode(),
                   "public_address": address}
        if address in all_addresses():
            raise RuntimeError("Address collision; creation blocked")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never overwrite a wallet
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        return address, True


def get_address(chat):
    if is_owner(chat):
        return bot_wallet_service.get_address()
    path = _wallet_file(chat)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("public_address") if data.get("version") == 1 else None


def all_addresses():
    folder = _base_dir() / "user_wallets"
    out = set()
    if folder.is_dir():
        for f in folder.glob("*.json"):
            try:
                out.add(json.loads(f.read_text(encoding="utf-8")).get("public_address"))
            except (OSError, ValueError):
                pass
    try:
        main = bot_wallet_service.get_address()
        if main:
            out.add(main)
    except Exception:
        pass
    out.discard(None)
    return out


def _private_key(chat):
    """(address, private key) — only after the export password was checked."""
    if is_owner(chat):
        return bot_wallet_service.export_private_key(_server_secret())
    data = json.loads(_wallet_file(chat).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Invalid wallet")
    salt = base64.b64decode(data["salt"], validate=True)
    try:
        secret = Fernet(base64.urlsafe_b64encode(_kdf(salt, _server_secret()))).decrypt(data["ciphertext"].encode())
    except InvalidToken:
        raise ValueError("Wallet cannot be decrypted")
    wallet = Keypair.from_bytes(secret)
    if str(wallet.pubkey()) != data["public_address"]:
        raise ValueError("Wallet address mismatch")
    return str(wallet.pubkey()), str(wallet)


# ---------------------------------------------------------------- export password
def password_problem(password, confirm=None):
    """None if the password is strong, else what is missing (Portuguese, for the page)."""
    if confirm is not None and password != confirm:
        return "As duas senhas não são iguais."
    if len(password) < MIN_PASSWORD_LEN:
        return "Use pelo menos " + str(MIN_PASSWORD_LEN) + " caracteres."
    if len(password) > 256:
        return "Senha longa demais."
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password):
        return "Use letras maiúsculas e minúsculas."
    if not re.search(r"[0-9]", password):
        return "Use pelo menos um número."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Use pelo menos um símbolo (ex.: ! @ # $ %)."
    if len(set(password)) < 6:
        return "Senha repetitiva demais."
    return None


def _passwords():
    path = _passwords_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def has_export_password(chat):
    return _chat(chat) in _passwords()


def set_export_password(chat, password, confirm):
    """First export: store a scrypt hash of the strong password. Returns a problem text or None."""
    why = password_problem(password, confirm)
    if why:
        return why
    with _LOCK:
        data = _passwords()
        if _chat(chat) in data:
            return "Esta conta já tem senha de exportação."
        salt = os.urandom(16)
        data[_chat(chat)] = {"salt": base64.b64encode(salt).decode(),
                             "hash": base64.b64encode(_kdf(salt, password)).decode(),
                             "created_at": int(time.time())}
        _write_atomic(_passwords_file(), data)
    return None


def locked(chat, now=None):
    now = now or time.time()
    recent = [t for t in _FAILS.get(_chat(chat), []) if now - t < LOCK_SECONDS]
    _FAILS[_chat(chat)] = recent
    return len(recent) >= MAX_FAILS


def check_export_password(chat, password):
    if locked(chat):
        return False
    entry = _passwords().get(_chat(chat))
    if not entry or len(password) > 256:
        return False
    derived = _kdf(base64.b64decode(entry["salt"]), password)
    ok = hmac.compare_digest(derived, base64.b64decode(entry["hash"]))
    if not ok:
        _FAILS.setdefault(_chat(chat), []).append(time.time())
    return ok


def export_with_password(chat, password, confirm=None, create=False):
    """('ok', address, private_key), ('created', message, None) or ('error', message, None).
    Creating the password does NOT reveal the key: the person then logs in with it."""
    if create:
        why = set_export_password(chat, password, confirm)
        if why:
            return "error", why, None
        return "created", "Senha criada! Agora entre com a sua senha para ver a chave privada.", None
    if locked(chat):
        return "error", "Muitas senhas erradas. Tente de novo em 1 hora.", None
    elif not check_export_password(chat, password):
        return "error", "Senha incorreta.", None
    address, private_key = _private_key(chat)
    return "ok", address, private_key
