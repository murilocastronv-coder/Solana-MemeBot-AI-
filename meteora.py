"""Meteora DLMM - the instructions the pool robot needs (open a position, add liquidity, claim fees, remove, close).

From Meteora's public IDL (MeteoraAg/dlmm-sdk idls/dlmm.json): discriminators and account order are copied from it,
LbPair offsets computed from its types (size 904). Only pools whose two tokens use the classic SPL token program
(flags 0) are supported; positions are 69 bins centred on the active bin (fits one position and at most 2 bin arrays).
"""
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"
RENT = "SysvarRent111111111111111111111111111111111"
LB_PAIR_SIZE = 904
POSITION_SIZE = 8120
BINS_PER_ARRAY = 70
HALF = 34                                       # 34 bins each side of the active bin -> 69 bins
SPOT_BALANCED = 3
IX_INIT_POSITION = bytes([219, 192, 234, 71, 190, 191, 102, 80])
IX_ADD_BY_STRATEGY = bytes([7, 3, 150, 127, 148, 40, 61, 200])
IX_REMOVE_ALL = bytes([10, 51, 61, 35, 112, 105, 24, 85])
IX_CLAIM_FEE = bytes([169, 32, 79, 137, 136, 232, 70, 137])
IX_CLOSE_POSITION = bytes([123, 134, 81, 0, 49, 68, 98, 98])


def _pk(b, o):
    raw = bytes(b[o:o + 32])
    if len(raw) != 32:
        raise ValueError("account data too short")
    return str(Pubkey.from_bytes(raw))


def lb_pair(address, data):
    if len(data) < LB_PAIR_SIZE:
        return None
    return {"address": address, "active_id": struct.unpack_from("<i", data, 76)[0], "bin_step": struct.unpack_from("<H", data, 80)[0],
            "status": data[82], "mint_x": _pk(data, 88), "mint_y": _pk(data, 120), "reserve_x": _pk(data, 152),
            "reserve_y": _pk(data, 184), "flag_x": data[880], "flag_y": data[881]}


def _pda(seeds):
    return str(Pubkey.find_program_address(seeds, Pubkey.from_string(DLMM))[0])


def event_authority():
    return _pda([b"__event_authority"])


def bitmap_extension(pair):
    return _pda([b"bitmap", bytes(Pubkey.from_string(pair))])


def bin_array_index(bin_id):
    return bin_id // BINS_PER_ARRAY                 # floor division also for negative ids


def bin_array(pair, index):
    return _pda([b"bin_array", bytes(Pubkey.from_string(pair)), struct.pack("<q", index)])


def position_range(active_id):
    lo = active_id - HALF
    return lo, 2 * HALF + 1, lo + 2 * HALF       # lower bin, width, upper bin


def _m(k, signer=False, writable=False):
    return AccountMeta(Pubkey.from_string(k), signer, writable)


def ix_initialize_position(payer, position, pair, lower, width):
    return Instruction(Pubkey.from_string(DLMM), IX_INIT_POSITION + struct.pack("<ii", lower, width), [
        _m(payer, True, True), _m(position, True, True), _m(pair), _m(payer, True, False), _m(SYSTEM), _m(RENT),
        _m(event_authority()), _m(DLMM)])


def _liq_accounts(position, pair, ext, user_x, user_y, p, lower_arr, upper_arr, sender):
    return [_m(position, writable=True), _m(pair, writable=True), _m(ext, writable=ext != DLMM),
            _m(user_x, writable=True), _m(user_y, writable=True), _m(p["reserve_x"], writable=True), _m(p["reserve_y"], writable=True),
            _m(p["mint_x"]), _m(p["mint_y"]), _m(lower_arr, writable=True), _m(upper_arr, writable=True),
            _m(sender, True, False), _m(TOKEN), _m(TOKEN), _m(event_authority()), _m(DLMM)]


def ix_add_liquidity(position, p, ext, user_x, user_y, lower_arr, upper_arr, sender, amount_x, amount_y, lower, upper, slippage_bins=5):
    data = (IX_ADD_BY_STRATEGY + struct.pack("<QQii", int(amount_x), int(amount_y), p["active_id"], slippage_bins) +
            struct.pack("<ii", lower, upper) + bytes([SPOT_BALANCED]) + bytes(64))
    return Instruction(Pubkey.from_string(DLMM), data,
                       _liq_accounts(position, p["address"], ext, user_x, user_y, p, lower_arr, upper_arr, sender))


def ix_remove_all(position, p, ext, user_x, user_y, lower_arr, upper_arr, sender):
    return Instruction(Pubkey.from_string(DLMM), IX_REMOVE_ALL,
                       _liq_accounts(position, p["address"], ext, user_x, user_y, p, lower_arr, upper_arr, sender))


def ix_claim_fee(position, p, user_x, user_y, lower_arr, upper_arr, sender):
    return Instruction(Pubkey.from_string(DLMM), IX_CLAIM_FEE, [
        _m(p["address"], writable=True), _m(position, writable=True), _m(lower_arr, writable=True), _m(upper_arr, writable=True),
        _m(sender, True, False), _m(p["reserve_x"], writable=True), _m(p["reserve_y"], writable=True),
        _m(user_x, writable=True), _m(user_y, writable=True), _m(p["mint_x"]), _m(p["mint_y"]), _m(TOKEN),
        _m(event_authority()), _m(DLMM)])


def ix_close_position(position, p, lower_arr, upper_arr, sender):
    return Instruction(Pubkey.from_string(DLMM), IX_CLOSE_POSITION, [
        _m(position, writable=True), _m(p["address"], writable=True), _m(lower_arr, writable=True), _m(upper_arr, writable=True),
        _m(sender, True, False), _m(sender, writable=True), _m(event_authority()), _m(DLMM)])
