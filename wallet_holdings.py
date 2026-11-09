"""What the bot's own wallet really holds on chain, before buying anything.

Counts the memecoins in the wallet (SPL Token and Token-2022 accounts), ignoring
SOL / USDC / USDT and worthless leftovers (worth less than DUST_USD or with no
route to sell). The buy guard refuses a new coin when the wallet already holds
MAX_OPEN_ORDERS coins or already holds that coin.
"""
import os
import time

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SKIP = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
DUST_USD = float(os.getenv("HOLDINGS_DUST_USD", "1"))
CACHE_SECONDS = 20
_cache = {"at": 0.0, "wallet": None, "coins": None}


def _rpc(method, params):
    from live_swap_executor import rpc
    return rpc(method, params)


def raw_balances(wallet, rpc=None):
    """{mint: raw amount} of every non-zero token account of the wallet."""
    rpc = rpc or _rpc
    out = {}
    for program in TOKEN_PROGRAMS:
        res = rpc("getTokenAccountsByOwner", [wallet, {"programId": program}, {"encoding": "jsonParsed"}]) or {}
        for acc in res.get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            mint = info.get("mint")
            amount = int(((info.get("tokenAmount") or {}).get("amount")) or 0)
            if mint and amount > 0 and mint not in SKIP:
                out[mint] = out.get(mint, 0) + amount
    return out


def value_usd(mint, amount_raw):
    """USD value of a token amount by a real sell quote; None when it cannot be sold."""
    import copy_trader as ct
    try:
        lamports = int(ct._order(mint, ct.SOL_MINT, int(amount_raw))["outAmount"])
    except Exception:
        return None
    return lamports / 1e9 * ct._sol_usd()


def coins_held(wallet, rpc=None, valuer=value_usd, fresh=False):
    """{mint: usd} of the memecoins that really count (worth >= DUST_USD)."""
    now = time.monotonic()
    if not fresh and _cache["wallet"] == wallet and now - _cache["at"] < CACHE_SECONDS:
        return dict(_cache["coins"])
    coins = {}
    for mint, amount in raw_balances(wallet, rpc).items():
        usd = valuer(mint, amount)
        if usd is not None and usd >= DUST_USD:
            coins[mint] = usd
    _cache.update(at=now, wallet=wallet, coins=coins)
    return dict(coins)


def can_buy(mint, wallet, limit=None, rpc=None, valuer=value_usd):
    """(True, n_held) when a new coin may be bought, else (False, reason)."""
    import copy_trader as ct
    limit = limit or ct.MAX_OPEN_TOTAL
    try:
        coins = coins_held(wallet, rpc, valuer, fresh=True)
    except Exception as exc:
        return False, "não consegui ler a carteira (" + type(exc).__name__ + ")"
    if mint in coins:
        return False, "a carteira já tem essa moeda"
    if len(coins) >= limit:
        return False, "a carteira já tem " + str(len(coins)) + " moedas (máx. " + str(limit) + ")"
    return True, len(coins)


def refresh(wallet):
    """Background refresh (never call from the Telegram thread)."""
    coins_held(wallet, fresh=True)


def status_line(wallet):
    """Last known holdings, without touching the network (buttons must answer at once)."""
    import copy_trader as ct
    if _cache["wallet"] != wallet or _cache["coins"] is None:
        return "Moedas na carteira agora: conferindo…"
    coins = dict(_cache["coins"])
    age = int(time.monotonic() - _cache["at"])
    if age > 120:
        return "Moedas na carteira agora: " + str(len(coins)) + "/" + str(ct.MAX_OPEN_TOTAL) + " (há " + str(age // 60) + " min)"
    return ("Moedas na carteira agora: " + str(len(coins)) + "/" + str(ct.MAX_OPEN_TOTAL) +
            (" (" + ", ".join(m[:4] + "… US$ " + format(v, ".0f") for m, v in coins.items()) + ")" if coins else ""))
