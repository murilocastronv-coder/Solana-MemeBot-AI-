import struct
import unittest

from solders.keypair import Keypair
from solders.pubkey import Pubkey

import meteora as mt

SOL = "So11111111111111111111111111111111111111112"


def pk():
    return str(Keypair().pubkey())


def make_pair(active=-1234):
    b = bytearray(mt.LB_PAIR_SIZE)
    struct.pack_into("<i", b, 76, active)
    struct.pack_into("<H", b, 80, 25)
    for o, k in ((88, pk()), (120, SOL), (152, pk()), (184, pk())):
        b[o:o + 32] = bytes(Pubkey.from_string(k))
    return bytes(b)


class MeteoraTest(unittest.TestCase):
    def test_pair_ranges_and_instructions(self):
        addr = pk()
        p = mt.lb_pair(addr, make_pair())
        self.assertEqual(p["active_id"], -1234)
        self.assertEqual(p["mint_y"], SOL)
        lower, width, upper = mt.position_range(p["active_id"])
        self.assertEqual((width, upper - lower + 1), (69, 69))
        self.assertEqual(mt.bin_array_index(-1), -1)                  # floor for negative bins
        self.assertEqual(mt.bin_array_index(-70), -1)
        self.assertEqual(mt.bin_array_index(-71), -2)
        self.assertLessEqual(mt.bin_array_index(upper) - mt.bin_array_index(lower), 1)
        me, pos = pk(), pk()
        la, ua = mt.bin_array(addr, mt.bin_array_index(lower)), mt.bin_array(addr, mt.bin_array_index(upper))
        add = mt.ix_add_liquidity(pos, p, mt.DLMM, pk(), pk(), la, ua, me, 1000, 2000, lower, upper)
        self.assertEqual(bytes(add.data[:8]), mt.IX_ADD_BY_STRATEGY)
        self.assertEqual(len(add.data), 8 + 8 + 8 + 4 + 4 + 4 + 4 + 1 + 64)
        self.assertEqual(struct.unpack_from("<QQii", bytes(add.data), 8), (1000, 2000, -1234, 5))
        self.assertEqual(len(add.accounts), 16)
        self.assertFalse(add.accounts[2].is_writable)                 # no bitmap extension -> program id, read-only
        init = mt.ix_initialize_position(me, pos, addr, lower, width)
        self.assertEqual(struct.unpack_from("<ii", bytes(init.data), 8), (lower, 69))
        self.assertEqual(len(mt.ix_claim_fee(pos, p, pk(), pk(), la, ua, me).accounts), 14)
        self.assertEqual(len(mt.ix_close_position(pos, p, la, ua, me).accounts), 8)
        from solders.compute_budget import set_compute_unit_limit
        from solders.hash import Hash
        from solders.message import MessageV0
        import kamino as k
        import liq_bot as lb
        ixs = [set_compute_unit_limit(1), set_compute_unit_limit(1), k.ix_create_ata(me, me, p["mint_x"], k.TOKEN),
               k.ix_create_ata(me, me, SOL, k.TOKEN), lb._ix_transfer(me, k.ata(me, SOL), 5), lb._ix_token([17], [(k.ata(me, SOL), False, True)]),
               init, add]
        msg = MessageV0.try_compile(Pubkey.from_string(me), ixs, [], Hash.default())
        self.assertLessEqual(len(lb._raw(msg)) + 64, 1232)            # + the position's signature


if __name__ == "__main__":
    unittest.main()
