import struct
import unittest

from solders.keypair import Keypair
from solders.pubkey import Pubkey

import kamino as k
import liq_bot as lb

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLM = "So11111111111111111111111111111111111111112"


def pk():
    return Keypair().pubkey()


def put_pk(b, o, key):
    b[o:o + 32] = bytes(Pubkey.from_string(str(key)))


def sf(x):
    v = int(x * 2 ** 60)
    return struct.pack("<QQ", v & (2 ** 64 - 1), v >> 64)


def make_reserve(market, mint, price, decimals, rate=1.0):
    b = bytearray(k.RESERVE_SIZE)
    b[:8] = k.RESERVE_DISC
    put_pk(b, 32, market)
    for o in (64, 96):
        put_pk(b, o, k.SYSTEM)
    put_pk(b, 128, mint)
    put_pk(b, 160, pk())
    put_pk(b, 192, pk())
    b[224:232] = struct.pack("<Q", int(1000 * 10 ** decimals * rate))
    b[248:264] = sf(price)
    b[272:280] = struct.pack("<Q", decimals)
    put_pk(b, 408, k.TOKEN)
    put_pk(b, 2560, pk())
    b[2592:2600] = struct.pack("<Q", int(1000 * 10 ** decimals))
    put_pk(b, 2600, pk())
    put_pk(b, 5112, pk())
    for o in (5160, 5192, 5224):
        put_pk(b, o, k.SYSTEM)
    b[5032:5036] = b"SOLX"
    return bytes(b)


def make_obligation(market, dep_res, dep_amount, dep_value, bor_res, bor_amount, bor_value, unhealthy):
    b = bytearray(k.OBLIGATION_SIZE)
    b[:8] = k.OBLIGATION_DISC
    put_pk(b, 32, market)
    put_pk(b, 64, pk())
    for i in range(8):
        put_pk(b, 96 + i * 136, k.SYSTEM)
    for i in range(5):
        put_pk(b, 1208 + i * 200, k.SYSTEM)
    put_pk(b, 96, dep_res)
    b[128:136] = struct.pack("<Q", dep_amount)
    b[136:152] = sf(dep_value)
    put_pk(b, 1208, bor_res)
    b[1296:1312] = sf(bor_amount)
    b[1312:1328] = sf(bor_value)
    b[1328:1344] = sf(bor_value)
    b[1192:1208] = sf(dep_value)
    b[2208:2224] = sf(bor_value)
    b[2224:2240] = sf(bor_value)
    b[2256:2272] = sf(unhealthy)
    b[2287] = 1
    put_pk(b, 2288, k.SYSTEM)
    return bytes(b)


class KaminoTest(unittest.TestCase):
    def setUp(self):
        self.market = str(pk())
        self.rsol, self.rusdc = str(pk()), str(pk())
        self.res = {self.rsol: k.reserve(self.rsol, make_reserve(self.market, SOLM, 100.0, 9)),
                    self.rusdc: k.reserve(self.rusdc, make_reserve(self.market, USDC, 1.0, 6))}
        # 1 SOL collateral (was worth 100), 80 USDC debt, liquidation limit 85
        self.ob = k.obligation(str(pk()), make_obligation(self.market, self.rsol, 10 ** 9, 100.0, self.rusdc, 80e6, 80.0, 85.0))

    def test_decode_and_ltv(self):
        self.assertEqual(self.res[self.rsol]["mint"], SOLM)
        self.assertAlmostEqual(self.res[self.rsol]["price"], 100.0)
        self.assertAlmostEqual(self.res[self.rsol]["rate"], 1.0, places=6)
        self.assertEqual(len(self.ob["deposits"]), 1)
        self.assertAlmostEqual(k.ltv_now(self.ob, self.res), 80 / 85, places=4)
        self.res[self.rsol]["price"] = 90.0                       # SOL fell 10%: limit 76.5 < debt 80
        self.assertGreater(k.ltv_now(self.ob, self.res), 1.0)

    def test_plan_and_instructions(self):
        p, why = lb.plan(self.ob, self.res, 200 * 10 ** 6)
        repay, withdraw, amount, min_receive, usd = p
        self.assertEqual(repay["mint"], USDC)
        self.assertEqual(amount, int(lb.LIQ_USD * 1e6))
        self.assertEqual(min_receive, int(usd / 100.0 * 1e9 * lb.MIN_RECEIVE))
        me = str(pk())
        ixs = k.liquidation_ixs(me, self.ob, self.res, repay, withdraw, amount, min_receive)
        self.assertEqual([bytes(i.data[:8]) for i in ixs],
                         [k.IX_REFRESH_RESERVE, k.IX_REFRESH_RESERVE, k.IX_REFRESH_OBLIGATION, k.IX_LIQUIDATE_V2])
        self.assertEqual(str(ixs[0].accounts[0].pubkey), withdraw["address"])     # SDK order: withdraw, then repay
        liq = ixs[-1]
        self.assertEqual(len(liq.accounts), 25)
        self.assertTrue(liq.accounts[0].is_signer)
        self.assertEqual(struct.unpack("<QQQ", bytes(liq.data[8:])), (amount, min_receive, 0))
        self.assertEqual(str(liq.accounts[13].pubkey), k.ata(me, USDC))
        from solders.hash import Hash
        from solders.message import MessageV0
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        msg = MessageV0.try_compile(Pubkey.from_string(me), [set_compute_unit_limit(1), set_compute_unit_price(1)] + ixs, [], Hash.default())
        from solders.signature import Signature
        from solders.transaction import VersionedTransaction
        size = len(bytes(VersionedTransaction.populate(msg, [Signature.default()])))
        self.assertLess(size, 1232)

    def test_no_usdc_debt_is_skipped(self):
        ob = dict(self.ob, borrows=[dict(self.ob["borrows"][0], reserve=self.rsol)])
        p, why = lb.plan(ob, self.res, 200 * 10 ** 6)
        self.assertIsNone(p)
        self.assertIn("USDC", why)

    def test_discriminators(self):
        import hashlib
        self.assertEqual(k.IX_LIQUIDATE_V2, bytes([162, 161, 35, 143, 30, 187, 185, 103]))   # Kamino SDK constant
        self.assertEqual(k.OBLIGATION_DISC, hashlib.sha256(b"account:Obligation").digest()[:8])


if __name__ == "__main__":
    unittest.main()
