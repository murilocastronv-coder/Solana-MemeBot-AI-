"""Save (ex-Solend) lending program - account decoding and the instructions a liquidator needs.

Layouts and instruction tags from Save's public program (solendprotocol/solana-program-library, token-lending):
Obligation 1300 bytes, Reserve 619 bytes, values are Decimal (u128 scaled by 1e18, WAD).
Liquidation (sdk liquidate_obligation_and_redeem_reserve_collateral, tag 17): every reserve of the obligation is
refreshed (tag 3: reserve, pyth, switchboard [, extra oracle]), then the obligation (tag 7: obligation, deposit
reserves, borrow reserves), then the liquidation itself (15 accounts). Liquidatable when borrowed_value >=
unhealthy_borrow_value (processor: ObligationHealthy otherwise).
"""
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

SAVE = "So1endDq2YkqhipRh3WViPa8hdiSpxWy6z3Z6tMCpAo"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
NULL_KEYS = {"11111111111111111111111111111111", "nu11111111111111111111111111111111111111111"}
WAD = 1e18
OBLIGATION_SIZE = 1300
RESERVE_SIZE = 619
COLL_LEN, LIQ_LEN = 88, 112


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def _u128(b, o):
    lo, hi = struct.unpack_from("<QQ", b, o)
    return lo | (hi << 64)


def _pk(b, o):
    raw = bytes(b[o:o + 32])
    if len(raw) != 32:
        raise ValueError("account data too short")
    return str(Pubkey.from_bytes(raw))


def real(k):
    return k not in NULL_KEYS


def obligation(address, data):
    if len(data) != OBLIGATION_SIZE or data[0] == 0:
        return None
    nd, nb = data[202], data[203]
    if nd + nb > 10:
        return None
    deps, bors, o = [], [], 204
    for _ in range(nd):
        deps.append({"reserve": _pk(data, o), "amount": _u64(data, o + 32), "value": _u128(data, o + 40) / WAD})
        o += COLL_LEN
    for _ in range(nb):
        bors.append({"reserve": _pk(data, o), "amount": _u128(data, o + 48) / WAD, "value": _u128(data, o + 64) / WAD})
        o += LIQ_LEN
    return {"address": address, "market": _pk(data, 10), "owner": _pk(data, 42), "deposits": deps, "borrows": bors,
            "deposited": _u128(data, 74) / WAD, "debt": _u128(data, 90) / WAD, "allowed": _u128(data, 106) / WAD,
            "unhealthy": _u128(data, 122) / WAD, "closeable": data[187], "has_debt": 1 if bors else 0}


def ratio(ob):
    """Debt / liquidation limit (>= 1 can be liquidated)."""
    if ob["unhealthy"] <= 0:
        return None
    return ob["debt"] / ob["unhealthy"]


def reserve(address, data):
    if len(data) != RESERVE_SIZE or data[0] == 0:
        return None
    return {"address": address, "market": _pk(data, 10), "mint": _pk(data, 42), "decimals": data[74],
            "supply": _pk(data, 75), "pyth": _pk(data, 107), "switchboard": _pk(data, 139),
            "available": _u64(data, 171), "borrowed": _u128(data, 179) / WAD, "price": _u128(data, 211) / WAD,
            "c_mint": _pk(data, 227), "c_supply_total": _u64(data, 259), "c_vault": _pk(data, 267),
            "ltv_pct": data[300], "bonus_pct": data[301], "threshold_pct": data[302], "fee_receiver": _pk(data, 339),
            "extra_oracle": _pk(data, 489), "token_program": TOKEN,
            # fields the shared liquidation helpers read
            "min_bonus_bps": data[301] * 100, "name": ""}


def market_authority(market):
    return str(Pubkey.find_program_address([bytes(Pubkey.from_string(market))], Pubkey.from_string(SAVE))[0])


def _m(k, signer=False, writable=False):
    return AccountMeta(Pubkey.from_string(k), signer, writable)


def ix_refresh_reserve(r):
    metas = [_m(r["address"], writable=True), _m(r["pyth"]), _m(r["switchboard"])]
    if real(r.get("extra_oracle") or "11111111111111111111111111111111"):
        metas.append(_m(r["extra_oracle"]))
    return Instruction(Pubkey.from_string(SAVE), bytes([3]), metas)


def ix_refresh_obligation(ob):
    metas = [_m(ob["address"], writable=True)]
    metas += [_m(d["reserve"], writable=True) for d in ob["deposits"]]
    metas += [_m(b["reserve"], writable=True) for b in ob["borrows"]]
    return Instruction(Pubkey.from_string(SAVE), bytes([7]), metas)


def ix_liquidate(liquidator, ob, repay, withdraw, amount, ata):
    """ata(owner, mint, program) -> address. Repays `amount` of the repay reserve's token, receives redeemed collateral."""
    metas = [_m(ata(liquidator, repay["mint"], TOKEN), writable=True),
             _m(ata(liquidator, withdraw["c_mint"], TOKEN), writable=True),
             _m(ata(liquidator, withdraw["mint"], TOKEN), writable=True),
             _m(repay["address"], writable=True), _m(repay["supply"], writable=True),
             _m(withdraw["address"], writable=True), _m(withdraw["c_mint"], writable=True),
             _m(withdraw["c_vault"], writable=True), _m(withdraw["supply"], writable=True),
             _m(withdraw["fee_receiver"], writable=True), _m(ob["address"], writable=True),
             _m(ob["market"], writable=True), _m(market_authority(ob["market"])), _m(liquidator, True, False), _m(TOKEN)]
    return Instruction(Pubkey.from_string(SAVE), bytes([17]) + struct.pack("<Q", int(amount)), metas)


def refresh_ixs(ob, reserves):
    involved = list(dict.fromkeys([d["reserve"] for d in ob["deposits"]] + [b["reserve"] for b in ob["borrows"]]))
    return [ix_refresh_reserve(reserves[a]) for a in involved] + [ix_refresh_obligation(ob)]
