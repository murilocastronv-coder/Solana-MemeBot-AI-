"""Real BTC long/short on Jupiter Perpetuals with the bot's own wallet.

Built from the program's public Anchor IDL (instructions
createIncreasePositionMarketRequest / createDecreasePositionMarketRequest).
Jupiter Perps uses a request model: we submit a request transaction and a
Jupiter keeper executes it seconds later; we then confirm by reading the
Position account on-chain.

Safety:
- every transaction is SIMULATED on-chain first; if the simulation fails
  nothing is sent and the exact error is returned;
- collateral is USDC (shorts: USDC collateral; longs: swapped to BTC by the
  program with a minimum-out guard); results are read back on-chain;
- requires LIVE_TRADING_ENABLED=true and the encrypted bot wallet.
"""
import base64
import hashlib
import os
import random
import struct
import time

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from bot_wallet_service import export_private_key, get_address
from live_swap_executor import rpc

PROGRAM_ID = Pubkey.from_string("PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu")
POOL = Pubkey.from_string("5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq")
EVENT_AUTHORITY = Pubkey.from_string("37hJBDnntwqhGbK7L6M1bLyvccj4u55CCUiLPdYkiqBN")
BTC_CUSTODY = Pubkey.from_string("5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm")
USDC_CUSTODY = Pubkey.from_string("G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
WBTC_MINT = Pubkey.from_string("3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
PERPETUALS = Pubkey.find_program_address([b"perpetuals"], PROGRAM_ID)[0]
SIDE = {"long": 1, "short": 2}
SLIPPAGE = 0.01
CONFIRM_TIMEOUT = 90


class PerpsError(RuntimeError):
    pass


# ------------------------------------------------------------------ encoding helpers
def _disc(name):
    snake = "".join("_" + c.lower() if c.isupper() else c for c in name)
    return hashlib.sha256(("global:" + snake).encode()).digest()[:8]


def _u64(v):
    return struct.pack("<Q", int(v))


def _opt_u64(v):
    return b"\x00" if v is None else b"\x01" + _u64(v)


def _opt_bool(v):
    return b"\x00" if v is None else b"\x01" + (b"\x01" if v else b"\x00")


def ata(owner, mint):
    return Pubkey.find_program_address([bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)], ATA_PROGRAM)[0]


def collateral_custody(side):
    return BTC_CUSTODY if side == "long" else USDC_CUSTODY


def position_pda(owner, side):
    return Pubkey.find_program_address(
        [b"position", bytes(owner), bytes(POOL), bytes(BTC_CUSTODY), bytes(collateral_custody(side)),
         bytes([SIDE[side]])], PROGRAM_ID)[0]


def request_pda(position, counter, increase):
    return Pubkey.find_program_address(
        [b"position_request", bytes(position), _u64(counter), bytes([1 if increase else 2])], PROGRAM_ID)[0]


def _meta(pubkey, writable=False, signer=False):
    return AccountMeta(pubkey, is_signer=signer, is_writable=writable)


def _tail():
    # referral = PROGRAM_ID means "None" for an Anchor optional account
    return [_meta(PROGRAM_ID), _meta(TOKEN_PROGRAM), _meta(ATA_PROGRAM), _meta(SYSTEM_PROGRAM),
            _meta(EVENT_AUTHORITY), _meta(PROGRAM_ID)]


def increase_ix(owner, side, collateral_usdc_raw, size_usd_raw, price_limit_raw, jupiter_min_out, counter):
    position = position_pda(owner, side)
    request = request_pda(position, counter, True)
    data = (_disc("createIncreasePositionMarketRequest") + _u64(size_usd_raw) + _u64(collateral_usdc_raw) +
            bytes([SIDE[side]]) + _u64(price_limit_raw) + _opt_u64(jupiter_min_out) + _u64(counter))
    accounts = [_meta(owner, True, True), _meta(ata(owner, USDC_MINT), True), _meta(PERPETUALS), _meta(POOL),
                _meta(position, True), _meta(request, True), _meta(ata(request, USDC_MINT), True),
                _meta(BTC_CUSTODY), _meta(collateral_custody(side)), _meta(USDC_MINT)] + _tail()
    return Instruction(PROGRAM_ID, data, accounts)


def decrease_ix(owner, side, price_limit_raw, counter):
    position = position_pda(owner, side)
    request = request_pda(position, counter, False)
    data = (_disc("createDecreasePositionMarketRequest") + _u64(0) + _u64(0) + _u64(price_limit_raw) +
            _opt_u64(None) + _opt_bool(True) + _u64(counter))
    accounts = [_meta(owner, True, True), _meta(ata(owner, USDC_MINT), True), _meta(PERPETUALS), _meta(POOL),
                _meta(position), _meta(request, True), _meta(ata(request, USDC_MINT), True),
                _meta(BTC_CUSTODY), _meta(collateral_custody(side)), _meta(USDC_MINT)] + _tail()
    return Instruction(PROGRAM_ID, data, accounts)


# ------------------------------------------------------------------ on-chain reads
def read_position(owner, side):
    """Decoded Position account (sizes/prices in USD), or None if it does not exist."""
    info = rpc("getAccountInfo", [str(position_pda(owner, side)), {"encoding": "base64", "commitment": "confirmed"}])
    value = (info or {}).get("value")
    if not value:
        return None
    raw = base64.b64decode(value["data"][0])
    off = 8 + 32 * 4
    open_time, update_time = struct.unpack_from("<qq", raw, off); off += 16
    side_byte = raw[off]; off += 1
    price, size, collateral = struct.unpack_from("<QQQ", raw, off); off += 24
    realised = struct.unpack_from("<q", raw, off)[0]
    return {"side": {1: "long", 2: "short"}.get(side_byte, "none"), "price": price / 1e6, "size_usd": size / 1e6,
            "collateral_usd": collateral / 1e6, "realised_pnl_usd": realised / 1e6, "update_time": update_time}


def usdc_balance(owner):
    result = rpc("getTokenAccountsByOwner", [str(owner), {"mint": str(USDC_MINT)}, {"encoding": "jsonParsed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
               for a in (result or {}).get("value", [])) / 1e6


# ------------------------------------------------------------------ sending
def _keypair():
    _, secret = export_private_key(os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"])
    return Keypair.from_base58_string(secret)


def _send(instructions):
    if os.getenv("LIVE_TRADING_ENABLED", "true").strip().lower() == "false":
        raise PerpsError("LIVE_TRADING_ENABLED está desligado")
    kp = _keypair()
    if str(kp.pubkey()) != get_address():
        raise PerpsError("Carteira não confere")
    ixs = [set_compute_unit_limit(400_000), set_compute_unit_price(100_000)] + instructions
    blockhash = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"])
    tx = VersionedTransaction(MessageV0.try_compile(kp.pubkey(), ixs, [], blockhash), [kp])
    encoded = base64.b64encode(bytes(tx)).decode()
    sim = rpc("simulateTransaction", [encoded, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}])
    err = (sim or {}).get("value", {}).get("err")
    if err is not None:
        logs = [l for l in (sim["value"].get("logs") or []) if "Error" in l or "error" in l][-2:]
        raise PerpsError("Simulação recusou a ordem (nada foi enviado): " + str(err) + " " + " | ".join(logs)[:300])
    return rpc("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])


def _wait(owner, side, opened, min_size=0.0):
    deadline = time.monotonic() + CONFIRM_TIMEOUT
    while time.monotonic() < deadline:
        pos = read_position(owner, side)
        size = (pos or {}).get("size_usd", 0)
        if opened and size > min_size + 0.01:
            return pos
        if not opened and size == 0:
            return pos or {"size_usd": 0}
        time.sleep(3)
    raise PerpsError("A Jupiter não executou o pedido em " + str(CONFIRM_TIMEOUT) + " s (o pedido expira e o "
                     "dinheiro volta para a carteira)")


def open_position(side, collateral_usd, leverage, btc_price, min_size=0.0):
    """Open (or add to) a BTC position with USDC collateral. Returns the on-chain position.
    For an add, pass the current size as min_size so we wait until the keeper increased it."""
    owner = Pubkey.from_string(get_address())
    if usdc_balance(owner) < collateral_usd:
        raise PerpsError("USDC insuficiente na carteira do bot para a garantia de $" + format(collateral_usd, ".2f"))
    counter = random.getrandbits(63)
    collateral_raw = int(collateral_usd * 1e6)
    size_raw = int(collateral_usd * leverage * 1e6)
    limit = btc_price * (1 + SLIPPAGE if side == "long" else 1 - SLIPPAGE)
    min_out = None
    if side == "long":  # the program swaps USDC -> BTC collateral; guard the swap
        from jupiter_v2_executor import order
        out = int(order(str(USDC_MINT), str(WBTC_MINT), collateral_raw)["outAmount"])
        min_out = int(out * (1 - SLIPPAGE))
    sig = _send([increase_ix(owner, side, collateral_raw, size_raw, int(limit * 1e6), min_out, counter)])
    pos = _wait(owner, side, opened=True, min_size=min_size)
    pos["signature"] = sig
    return pos


def close_position(side, btc_price):
    """Close the whole position back to USDC. Returns the final on-chain state."""
    owner = Pubkey.from_string(get_address())
    current = read_position(owner, side)
    if not current or current["size_usd"] == 0:
        return {"size_usd": 0, "already_closed": True}
    counter = random.getrandbits(63)
    limit = btc_price * (1 - SLIPPAGE if side == "long" else 1 + SLIPPAGE)
    sig = _send([decrease_ix(owner, side, int(limit * 1e6), counter)])
    pos = _wait(owner, side, opened=False)
    pos["signature"] = sig
    return pos


# ------------------------------------------------------------------ USDC for the collateral
SOL_MINT = "So11111111111111111111111111111111111111112"


def _swap(input_mint, output_mint, amount_raw):
    from jupiter_v2_executor import execute_signed, order, sign_order
    data = order(input_mint, output_mint, int(amount_raw), taker=get_address())
    signed = sign_order(data, os.environ["BOT_WALLET_ENCRYPTION_PASSPHRASE"])
    return execute_signed(signed, data["requestId"])


def ensure_usdc(usd, sol_usd):
    """Swap SOL -> USDC so the wallet holds at least `usd` USDC for the collateral (+1% margin)."""
    owner = Pubkey.from_string(get_address())
    have = usdc_balance(owner)
    if have >= usd:
        return have
    need = (usd - have) * 1.01 + 0.05
    lamports = int(need / float(sol_usd) * 1e9)
    sol = int((rpc("getBalance", [str(owner), {"commitment": "confirmed"}]) or {}).get("value", 0))
    if lamports > sol - 20_000_000:          # keep ~0.02 SOL for network fees
        raise PerpsError("SOL insuficiente para a garantia de US$ " + format(usd, ".2f"))
    _swap(SOL_MINT, str(USDC_MINT), lamports)
    for _ in range(10):
        time.sleep(2)
        have = usdc_balance(owner)
        if have >= usd:
            return have
    raise PerpsError("USDC não chegou na carteira depois da troca")


def usdc_to_sol(keep_usd=0.0):
    """After closing: swap the USDC back to SOL (the bot keeps its balance in SOL)."""
    owner = Pubkey.from_string(get_address())
    amount = usdc_balance(owner) - keep_usd
    if amount < 1:
        return None
    return _swap(str(USDC_MINT), SOL_MINT, int(amount * 1e6))
