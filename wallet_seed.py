"""Connect a Phantom wallet from its 12-word recovery phrase.

The phrase is read ONLY from the SOLANA_BOT_SEED_PHRASE environment variable
(a Render secret). It is never accepted through Telegram, never logged and
never stored. Only the public address is kept in memory; the private key is
discarded right after the address is derived. Live trading stays disabled.

Derivation matches Phantom: BIP39 seed -> SLIP-0010 ed25519 m/44'/501'/ACCOUNT'/0'.
"""
import hashlib
import hmac
import os
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mnemonic import Mnemonic

ENV_VAR = "SOLANA_BOT_SEED_PHRASE"
ACCOUNT_ENV_VAR = "SOLANA_BOT_ACCOUNT_INDEX"  # 0 = first Phantom account, 1 = second...
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_HARDENED = 0x80000000


def _normalize(phrase):
    return " ".join(unicodedata.normalize("NFKD", phrase).lower().split())


def validate_phrase(phrase):
    words = _normalize(phrase or "")
    if len(words.split()) not in (12, 24) or not Mnemonic("english").check(words):
        raise ValueError("Frase de recuperação inválida (confira as 12 palavras e a ordem)")
    return words


def looks_like_seed_phrase(text):
    """True if a chat message looks like a recovery phrase (used to warn/delete)."""
    words = (text or "").lower().split()
    if len(words) not in (12, 15, 18, 21, 24):
        return False
    wordlist = set(Mnemonic("english").wordlist)
    return sum(w in wordlist for w in words) >= len(words) - 1


def _b58encode(data):
    num = int.from_bytes(data, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _derive_secret(seed, path):
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for index in path:
        data = b"\0" + key + (index | _HARDENED).to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def address_from_phrase(phrase, account=0):
    words = validate_phrase(phrase)
    seed = hashlib.pbkdf2_hmac("sha512", words.encode(), b"mnemonic", 2048)
    secret = _derive_secret(seed, [44, 501, int(account), 0])
    public = Ed25519PrivateKey.from_private_bytes(secret).public_key()
    return _b58encode(public.public_bytes(Encoding.Raw, PublicFormat.Raw))


def connected_address():
    """Public address of the configured Phantom wallet, or None if not set."""
    phrase = os.getenv(ENV_VAR, "")
    if not phrase.strip():
        return None
    account = int(os.getenv(ACCOUNT_ENV_VAR, "0") or 0)
    return address_from_phrase(phrase, account)
