"""Persistent private Telegram owner binding.

First claim is a bootstrap trust-on-first-use operation. For funded wallets,
prefer TELEGRAM_CHAT_ID set explicitly before first launch.
"""
import json, os, threading
from pathlib import Path

_LOCK = threading.RLock()

def _path():
    wallet = os.getenv("BOT_WALLET_FILE", "")
    if not wallet or not Path(wallet).is_absolute():
        raise RuntimeError("BOT_WALLET_FILE must point to a persistent absolute path")
    return Path(wallet).with_name("telegram_owner.json")

def get_owner():
    with _LOCK:
        p = _path()
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        owner = str(data.get("chat_id", ""))
        if not owner.isdigit():
            raise RuntimeError("Invalid persistent owner file")
        return owner

def claim(chat_id):
    chat_id = str(chat_id)
    if not chat_id.isdigit():
        raise ValueError("Private chat ID must be numeric")
    with _LOCK:
        existing = get_owner()
        if existing:
            return existing == chat_id
        p = _path()
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"version": 1, "chat_id": chat_id}, file)
            file.flush()
            os.fsync(file.fileno())
        return True
