import base64
import struct
import unittest

from solders.keypair import Keypair
from solders.pubkey import Pubkey

import save as sv
import save_bot as sb

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLM = "So11111111111111111111111111111111111111112"


def pk():
    return str(Keypair().pubkey())


def put(b, o, k):
    b[o:o + 32] = bytes(Pubkey.from_string(k))


def wad(x):
    v = int(x * 1e18)
    return struct.pack("<QQ", v & (2 ** 64 - 1), v >> 64)


def make_reserve(market, mint, price, dec, bonus=5, threshold=85):
    b = bytearray(sv.RESERVE_SIZE)
    b[0] = 1
    put(b, 10, market)
    put(b, 42, mint)
    b[74] = dec
    for o in (75, 107, 139, 227, 267, 339):
        put(b, o, pk())
    b[211:227] = wad(price)
    b[301], b[302] = bonus, threshold
    put(b, 489, "11111111111111111111111111111111")
    return bytes(b)


def make_obligation(market, dep, dep_amt, dep_val, bor, bor_amt, bor_val, unhealthy):
    b = bytearray(sv.OBLIGATION_SIZE)
    b[0] = 1
    put(b, 10, market)
    put(b, 42, pk())
    b[74:90] = wad(dep_val)
    b[90:106] = wad(bor_val)
    b[122:138] = wad(unhealthy)
    b[202], b[203] = 1, 1
    put(b, 204, dep)
    b[236:244] = struct.pack("<Q", dep_amt)
    b[244:260] = wad(dep_val)
    o = 204 + 88
    put(b, o, bor)
    b[o + 48:o + 64] = wad(bor_amt)
    b[o + 64:o + 80] = wad(bor_val)
    return bytes(b)


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.market = pk()
        self.rsol, self.rusdc = pk(), pk()
        self.res = {self.rsol: sv.reserve(self.rsol, make_reserve(self.market, SOLM, 100.0, 9)),
                    self.rusdc: sv.reserve(self.rusdc, make_reserve(self.market, USDC, 1.0, 6))}
        self.ob = sv.obligation(pk(), make_obligation(self.market, self.rsol, 10 ** 9, 100.0, self.rusdc, 80e6, 80.0, 76.5))

    def test_decode(self):
        self.assertEqual(self.res[self.rsol]["mint"], SOLM)
        self.assertAlmostEqual(self.res[self.rsol]["price"], 100.0)
        self.assertEqual(len(self.ob["deposits"]), 1)
        self.assertAlmostEqual(self.ob["borrows"][0]["amount"], 80e6)
        self.assertAlmostEqual(sv.ratio(self.ob), 80 / 76.5)

    def test_plan_and_instructions(self):
        p, why = sb.plan(self.ob, self.res, 100 * 10 ** 6)
        self.assertEqual(p["repay"]["mint"], USDC)
        me = pk()
        import kamino as k
        ixs = sv.refresh_ixs(self.ob, self.res) + [sv.ix_liquidate(me, self.ob, p["repay"], p["withdraw"], p["amount"], k.ata)]
        self.assertEqual([bytes(i.data[:1]) for i in ixs], [b"\x03", b"\x03", b"\x07", b"\x11"])
        self.assertEqual(len(ixs[0].accounts), 3)                    # no extra oracle
        liq = ixs[-1]
        self.assertEqual(len(liq.accounts), 15)
        self.assertEqual(struct.unpack("<Q", bytes(liq.data[1:]))[0], p["amount"])
        self.assertTrue(liq.accounts[13].is_signer)

    def test_exact_check_reads_simulated_obligation(self):
        refreshed = make_obligation(self.market, self.rsol, 10 ** 9, 90.0, self.rusdc, 80e6, 80.0, 76.5)
        rpc = lambda m, a: {"value": {"err": None, "accounts": [{"data": [base64.b64encode(refreshed).decode(), "base64"]}]}}
        ltv, why = sb.exact_ltv(rpc, Keypair().pubkey(), self.ob, self.res)
        self.assertAlmostEqual(ltv, 80 / 76.5, places=4)


if __name__ == "__main__":
    unittest.main()
