"""Kamino Lend (klend) - account decoding and the instructions a liquidator needs.

Layouts (byte offsets) come from Kamino's published IDL (klend-sdk/src/idl/klend.json); every account is checked for
its size and Anchor discriminator before it is read, so a program upgrade that changes the layout stops the bot
instead of producing wrong numbers.
Instruction order for a liquidation is the one Kamino's own SDK uses (KaminoAction.buildLiquidateTxns, V2 ixs):
refresh every reserve of the obligation -> refresh obligation -> liquidate_obligation_and_redeem_reserve_collateral_v2
(the V2 instruction refreshes the obligation farms itself).
"""
import hashlib
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

KLEND = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
FARMS = "FarmsPZpWu9i7Kky8tPN37rs2TpmMrAZrC7S7vJa91Hr"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
SYSVAR_IX = "Sysvar1nstructions1111111111111111111111111"
NULL_KEYS = {SYSTEM, "nu11111111111111111111111111111111111111111"}
SF = float(2 ** 60)                     # Kamino "scaled fraction": 60 fractional bits

OBLIGATION_SIZE = 3344
RESERVE_SIZE = 8624
OBLIGATION_HEAD = 2320                  # bytes read from an obligation (everything the bot uses, up to the referrer)


def _disc(kind, name):
    return hashlib.sha256((kind + ":" + name).encode()).digest()[:8]


OBLIGATION_DISC = _disc("account", "Obligation")
RESERVE_DISC = _disc("account", "Reserve")
IX_REFRESH_RESERVE = _disc("global", "refresh_reserve")
IX_REFRESH_OBLIGATION = _disc("global", "refresh_obligation")
IX_LIQUIDATE_V2 = _disc("global", "liquidate_obligation_and_redeem_reserve_collateral_v2")


def _u8(b, o):
    return b[o]


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def _u128(b, o):
    lo, hi = struct.unpack_from("<QQ", b, o)
    return lo | (hi << 64)


def _pk(b, o):
    raw = bytes(b[o:o + 32])
    if len(raw) != 32:
        raise ValueError("account data too short")       # never let the Rust library panic (kills the thread)
    return str(Pubkey.from_bytes(raw))


def _real(k):
    return k not in NULL_KEYS


# ---------------------------------------------------------------- accounts
def obligation(address, data):
    """Decoded obligation (values in USD as of its last refresh), or None if it is not a klend obligation."""
    if len(data) < OBLIGATION_HEAD or data[:8] != OBLIGATION_DISC:
        return None
    deps, bors = [], []
    for i in range(8):
        o = 96 + i * 136
        r = _pk(data, o)
        if _real(r):
            deps.append({"reserve": r, "amount": _u64(data, o + 32), "value": _u128(data, o + 40) / SF})
    for i in range(5):
        o = 1208 + i * 200
        r = _pk(data, o)
        if _real(r):
            bors.append({"reserve": r, "amount": _u128(data, o + 88) / SF, "value": _u128(data, o + 104) / SF,
                         "bf_value": _u128(data, o + 120) / SF})
    return {"address": address, "slot": _u64(data, 16), "market": _pk(data, 32), "owner": _pk(data, 64),
            "deposits": deps, "borrows": bors, "deposited": _u128(data, 1192) / SF,
            "bf_debt": _u128(data, 2208) / SF, "debt": _u128(data, 2224) / SF,
            "allowed": _u128(data, 2240) / SF, "unhealthy": _u128(data, 2256) / SF,
            "elevation": _u8(data, 2285), "has_debt": _u8(data, 2287), "referrer": _pk(data, 2288)}


def reserve(address, data):
    if len(data) != RESERVE_SIZE or data[:8] != RESERVE_DISC:
        return None
    dec = _u64(data, 272)
    fees = (_u128(data, 344) + _u128(data, 360) + _u128(data, 376)) / SF
    total = _u64(data, 224) + _u128(data, 232) / SF - fees
    csup = _u64(data, 2592)
    return {"address": address, "market": _pk(data, 32), "farm_coll": _pk(data, 64), "farm_debt": _pk(data, 96),
            "mint": _pk(data, 128), "supply": _pk(data, 160), "fee_vault": _pk(data, 192),
            "available": _u64(data, 224), "borrowed": _u128(data, 232) / SF, "price": _u128(data, 248) / SF,
            "price_ts": _u64(data, 264), "decimals": dec, "token_program": _pk(data, 408),
            "c_mint": _pk(data, 2560), "c_supply": csup, "c_vault": _pk(data, 2600),
            "rate": (total / csup) if csup else 1.0,                      # liquidity per collateral token
            "status": _u8(data, 4856), "take_rate_pct": _u8(data, 4870), "threshold_pct": _u8(data, 4873),
            "min_bonus_bps": _u16(data, 4874), "max_bonus_bps": _u16(data, 4876),
            "name": bytes(data[5032:5064]).split(b"\0")[0].decode("utf-8", "ignore"),
            "scope": _pk(data, 5112), "chain": list(struct.unpack_from("<4H", data, 5144)), "sb_price": _pk(data, 5160), "sb_twap": _pk(data, 5192), "pyth": _pk(data, 5224)}


SCOPE_ENTRY = 56                        # DatedPrice: value u64, exp u64, slot u64, ts u64, 24 bytes
SCOPE_HEAD = 8 + 32                     # discriminator + oracle_mappings
SCOPE_MAX = 512


def scope_entries(chain):
    """The Scope price indexes a reserve multiplies (Kamino stops at the first index outside the table)."""
    out = []
    for i in chain:
        if i >= SCOPE_MAX:
            break
        out.append(i)
    return out if chain != [0, 0, 0, 0] else []


def scope_price(prices, chain):
    """USD price from a Scope chain, prices = {index: (value, exp)}; None when an index is missing."""
    idx = scope_entries(chain)
    if not idx:
        return None
    px = 1.0
    for i in idx:
        if i not in prices or prices[i][0] == 0:
            return None
        v, e = prices[i]
        px *= v / 10 ** e
    return px


def scope_slice(data, first):
    """{index: (value, exp)} from a Scope account slice that starts at entry `first`."""
    out = {}
    for n in range(len(data) // SCOPE_ENTRY):
        v, e = struct.unpack_from("<QQ", data, n * SCOPE_ENTRY)
        out[first + n] = (v, e)
    return out


def ltv_now(ob, reserves):
    """Debt / liquidation limit with LIVE prices (>= 1.0 means it can be liquidated), or None.
    Each position's own refreshed value is moved by how much its price changed since the reserve's last refresh
    (live oracle price / reserve price), so Kamino's exchange rates, elevation-group thresholds and borrow factors are
    kept exactly; only the price move is estimated. Kamino's exact number (liq_bot.exact_ltv) decides in the end."""
    if not ob["has_debt"] or ob["unhealthy"] <= 0 or ob["deposited"] <= 0 or ob["debt"] <= 0:
        return None

    def move(r):
        ref = r.get("price_ref") or r["price"]
        return (r["price"] / ref) if ref > 0 else 1.0
    dep = debt = 0.0
    for d in ob["deposits"]:
        r = reserves.get(d["reserve"])
        if not r:
            return None
        dep += d["value"] * move(r)
    for b in ob["borrows"]:
        r = reserves.get(b["reserve"])
        if not r:
            return None
        debt += b["value"] * move(r)
    if dep <= 0 or debt <= 0:
        return ob["bf_debt"] / ob["unhealthy"]
    return (ob["bf_debt"] * debt / ob["debt"]) / (ob["unhealthy"] * dep / ob["deposited"])


# ---------------------------------------------------------------- addresses
def market_authority(market):
    return str(Pubkey.find_program_address([b"lma", bytes(Pubkey.from_string(market))], Pubkey.from_string(KLEND))[0])


def farm_user_state(farm, obligation_address, farms_program=FARMS):
    return str(Pubkey.find_program_address([b"user", bytes(Pubkey.from_string(farm)), bytes(Pubkey.from_string(obligation_address))],
                                           Pubkey.from_string(farms_program))[0])


def ata(owner, mint, token_program=TOKEN):
    return str(Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
                                            bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))[0])


# ---------------------------------------------------------------- instructions
def _m(k, signer=False, writable=False):
    return AccountMeta(Pubkey.from_string(k), signer, writable)


def _opt(k):
    """Anchor optional account: the program id stands for 'none'."""
    return _m(k if _real(k) else KLEND)


def ix_refresh_reserve(r):
    return Instruction(Pubkey.from_string(KLEND), IX_REFRESH_RESERVE, [
        _m(r["address"], writable=True), _m(r["market"]), _opt(r["pyth"]), _opt(r["sb_price"]), _opt(r["sb_twap"]), _opt(r["scope"])])


def referrer_token_state(referrer, reserve_address):
    return str(Pubkey.find_program_address([b"referrer_acc", bytes(Pubkey.from_string(referrer)), bytes(Pubkey.from_string(reserve_address))],
                                           Pubkey.from_string(KLEND))[0])


def ix_refresh_obligation(ob, reserves=None):
    metas = [_m(ob["market"]), _m(ob["address"], writable=True)]
    metas += [_m(d["reserve"], writable=True) for d in ob["deposits"]]
    metas += [_m(b["reserve"], writable=True) for b in ob["borrows"]]
    if _real(ob.get("referrer") or SYSTEM):
        # one referrer token state per borrow; the reserves that take protocol fees first (Kamino SDK order)
        bs = [b["reserve"] for b in ob["borrows"]]
        taking = [r for r in bs if (reserves or {}).get(r) is None or (reserves or {})[r].get("take_rate_pct", 1) > 0]
        order = taking + [r for r in bs if r not in taking]
        metas += [_m(referrer_token_state(ob["referrer"], r), writable=True) for r in order]
    return Instruction(Pubkey.from_string(KLEND), IX_REFRESH_OBLIGATION, metas)


def ix_create_ata(payer, owner, mint, token_program):
    """Associated token account, idempotent (does nothing when it exists)."""
    return Instruction(Pubkey.from_string(ATA_PROGRAM), bytes([1]), [
        _m(payer, True, True), _m(ata(owner, mint, token_program), writable=True), _m(owner), _m(mint), _m(SYSTEM), _m(token_program)])


def ix_liquidate(liquidator, ob, repay, withdraw, amount, min_receive, farms_program=FARMS):
    """liquidate_obligation_and_redeem_reserve_collateral_v2: repays `amount` (base units of the repay token) of the
    obligation's debt and receives the withdraw reserve's collateral, redeemed to its token, with the bonus."""
    market = ob["market"]
    metas = [_m(liquidator, True, False), _m(ob["address"], writable=True), _m(market), _m(market_authority(market)),
             _m(repay["address"], writable=True), _m(repay["mint"]), _m(repay["supply"], writable=True),
             _m(withdraw["address"], writable=True), _m(withdraw["mint"]), _m(withdraw["c_mint"], writable=True),
             _m(withdraw["c_vault"], writable=True), _m(withdraw["supply"], writable=True), _m(withdraw["fee_vault"], writable=True),
             _m(ata(liquidator, repay["mint"], repay["token_program"]), writable=True),
             _m(ata(liquidator, withdraw["c_mint"], TOKEN), writable=True),
             _m(ata(liquidator, withdraw["mint"], withdraw["token_program"]), writable=True),
             _m(TOKEN), _m(repay["token_program"]), _m(withdraw["token_program"]), _m(SYSVAR_IX)]
    for farm in (withdraw["farm_coll"], repay["farm_debt"]):            # collateral farm, then debt farm
        if _real(farm):
            metas += [_m(farm_user_state(farm, ob["address"], farms_program), writable=True), _m(farm, writable=True)]
        else:
            metas += [_m(KLEND), _m(KLEND)]
    metas.append(_m(farms_program))
    if ob["elevation"] > 0:
        metas += [_m(d["reserve"], writable=True) for d in ob["deposits"]]
    data = IX_LIQUIDATE_V2 + struct.pack("<QQQ", int(amount), int(min_receive), 0)
    return Instruction(Pubkey.from_string(KLEND), data, metas)


def liquidation_ixs(liquidator, ob, reserves, repay, withdraw, amount, min_receive, farms_program=FARMS):
    """Kamino SDK order: refresh the other reserves, then the two of the action, refresh obligation, liquidate."""
    involved = [d["reserve"] for d in ob["deposits"]] + [b["reserve"] for b in ob["borrows"]]
    current = [withdraw["address"], repay["address"]] if withdraw["address"] != repay["address"] else [repay["address"]]
    others = [a for a in dict.fromkeys(involved) if a not in current]
    ixs = [ix_refresh_reserve(reserves[a]) for a in others + current]
    ixs.append(ix_refresh_obligation(ob, reserves))
    ixs.append(ix_liquidate(liquidator, ob, repay, withdraw, amount, min_receive, farms_program))
    return ixs
