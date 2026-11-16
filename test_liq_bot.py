import struct
import time
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
    b[4873] = 85                                   # liquidation threshold %
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
        self.res[self.rsol].update(price=90.0, price_ref=100.0)   # live oracle 10% under the reserve: limit 76.5 < debt 80
        self.assertGreater(k.ltv_now(self.ob, self.res), 1.0)

    def test_plan_and_instructions(self):
        p, why = lb.plan(self.ob, self.res, 200 * 10 ** 6)
        repay, withdraw, amount, min_receive, usd = p["repay"], p["withdraw"], p["amount"], p["min_receive"], p["usd"]
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

    def test_sol_debt_and_unknown_debt(self):
        ob = dict(self.ob, borrows=[dict(self.ob["borrows"][0], reserve=self.rsol, amount=10 ** 9)],
                  deposits=[dict(self.ob["deposits"][0], reserve=self.rusdc)])
        self.res[self.rusdc]["min_bonus_bps"] = 500
        p, why = lb.plan(ob, self.res, 200 * 10 ** 6)
        self.assertEqual(p["repay"]["mint"], SOLM)
        self.assertEqual(p["amount"], int(lb.LIQ_USD * 0.98 / 100.0 * 1e9))
        self.res[self.rusdc]["min_bonus_bps"] = 100                      # bonus too small to pay two swaps
        self.assertIsNone(lb.plan(ob, self.res, 200 * 10 ** 6)[0])
        other = str(pk())
        self.res[other] = dict(self.res[self.rsol], mint=str(pk()), address=other)
        ob2 = dict(self.ob, borrows=[dict(self.ob["borrows"][0], reserve=other)])
        self.res[self.rusdc]["min_bonus_bps"] = 500
        ob2 = dict(ob2, deposits=[dict(self.ob["deposits"][0], reserve=self.rusdc)])
        p, why = lb.plan(ob2, self.res, 200 * 10 ** 6)          # owner: any debt token is accepted now
        self.assertEqual(p["repay"]["address"], other)

    def test_scope_price_chain(self):
        data = struct.pack("<QQ", 150_000_000, 6) + bytes(40) + struct.pack("<QQ", 2_000_000, 6) + bytes(40)
        prices = k.scope_slice(data, 7)
        self.assertEqual(k.scope_price(prices, [7, 65535, 65535, 65535]), 150.0)
        self.assertAlmostEqual(k.scope_price(prices, [7, 8, 65535, 65535]), 300.0)
        self.assertIsNone(k.scope_price(prices, [0, 0, 0, 0]))
        self.assertIsNone(k.scope_price(prices, [9, 65535, 65535, 65535]))

    def test_build_tx_with_sol_wrap_fits(self):
        ob = dict(self.ob, borrows=[dict(self.ob["borrows"][0], reserve=self.rsol, amount=10 ** 9)],
                  deposits=[dict(self.ob["deposits"][0], reserve=self.rusdc)])
        self.res[self.rusdc]["min_bonus_bps"] = 500
        p, _ = lb.plan(ob, self.res, 200 * 10 ** 6)
        me = pk()
        lb._TIPS.update(at=time.time(), list=[str(pk())])
        rpc = lambda m, a: {"value": [None, None]}
        msg = lb.build_tx(rpc, me, ob, self.res, p, blockhash=str(__import__("solders.hash", fromlist=["Hash"]).Hash.default()))
        self.assertLessEqual(len(lb._raw(msg)), 1232)
        progs = [str(msg.account_keys[ix.program_id_index]) for ix in msg.instructions]
        self.assertEqual(progs.count(k.KLEND), 4)
        self.assertEqual(progs[-1] if len(lb._raw(msg)) else None, progs[-1])

    def test_discriminators(self):
        import hashlib
        self.assertEqual(k.IX_LIQUIDATE_V2, bytes([162, 161, 35, 143, 30, 187, 185, 103]))   # Kamino SDK constant
        self.assertEqual(k.OBLIGATION_DISC, hashlib.sha256(b"account:Obligation").digest()[:8])


if __name__ == "__main__":
    unittest.main()


class HeadSliceTest(unittest.TestCase):
    def test_scan_slice_reads_the_referrer(self):
        c = KaminoTest()
        c.setUp()
        market = c.market
        full = make_obligation(market, c.rsol, 10 ** 9, 100.0, c.rusdc, 80e6, 80.0, 85.0)
        head = full[:k.OBLIGATION_HEAD]
        ob = k.obligation(str(pk()), head)
        self.assertEqual(ob["referrer"], k.SYSTEM)
        with self.assertRaises(ValueError):
            k._pk(full[:2300], 2288)


class ExactHealthTest(unittest.TestCase):
    def test_exact_ltv_reads_the_simulated_obligation(self):
        import base64
        c = KaminoTest()
        c.setUp()
        refreshed = make_obligation(c.market, c.rsol, 10 ** 9, 90.0, c.rusdc, 80e6, 80.0, 76.5)
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            return {"value": {"err": None, "accounts": [{"data": [base64.b64encode(refreshed).decode(), "base64"]}]}}
        ltv, why = lb.exact_ltv(rpc, pk(), c.ob, c.res)
        self.assertAlmostEqual(ltv, 80 / 76.5, places=4)
        self.assertEqual(calls[0][1][1]["accounts"]["addresses"], [c.ob["address"]])

    def test_healthy_in_kamino_stops_before_any_swap(self):
        c = KaminoTest()
        c.setUp()
        lb._TRIED.clear()
        healthy = make_obligation(c.market, c.rsol, 10 ** 9, 100.0, c.rusdc, 80e6, 80.0, 85.0)
        import base64

        def rpc(method, params):
            if method == "simulateTransaction":
                return {"value": {"err": None, "accounts": [{"data": [base64.b64encode(healthy).decode(), "base64"]}]}}
            if method == "getTokenAccountBalance":
                return {"value": {"amount": str(60 * 10 ** 6)}}
            raise AssertionError("no swap or send for a healthy obligation: " + method)
        kp = Keypair()
        self.assertIsNone(lb.attempt(rpc, c.ob, c.res, 1.2, signer=kp))

    def test_zero_threshold_collateral_is_not_chosen(self):
        c = KaminoTest()
        c.setUp()
        c.res[c.rsol]["threshold_pct"] = 0
        p, why = lb.plan(c.ob, c.res, 100 * 10 ** 6)
        self.assertIsNone(p)


class CalibrationTest(unittest.TestCase):
    def test_gap_is_learned_and_drops_from_watch(self):
        import base64
        c = KaminoTest()
        c.setUp()
        lb._TRIED.clear()
        lb._CAL.clear()
        lb._WATCH[c.ob["address"]] = c.ob
        refreshed = make_obligation(c.market, c.rsol, 10 ** 9, 100.0, c.rusdc, 80e6, 80.0, 100.0)    # Kamino: 0.80

        def rpc(method, params):
            if method == "simulateTransaction":
                return {"value": {"err": None, "accounts": [{"data": [base64.b64encode(refreshed).decode(), "base64"]}]}}
            raise AssertionError(method)
        self.assertIsNone(lb.attempt(rpc, c.ob, c.res, 1.0, signer=Keypair()))
        self.assertAlmostEqual(lb._CAL[c.ob["address"]], 0.8, places=3)
        self.assertNotIn(c.ob["address"], lb._WATCH)


class VolatileWatchTest(unittest.TestCase):
    def test_volatile_get_slots_before_correlated_loops(self):
        lb._WATCH.clear()
        for i in range(400):
            lb._WATCH["loop%d" % i] = {"elevation": 1, "ltv": 0.998}
        for i in range(50):
            lb._WATCH["vol%d" % i] = {"elevation": 0, "ltv": 0.92}
        lb._trim_watch()
        self.assertEqual(len(lb._WATCH), lb.MAX_WATCH)
        self.assertEqual(sum(1 for k in lb._WATCH if k.startswith("vol")), 50)
        self.assertEqual(lb.watch_from({"elevation": 0}), lb.VOLATILE_WATCH_LTV)
        self.assertEqual(lb.watch_from({"elevation": 2}), lb.WATCH_LTV)
        lb._WATCH.clear()


class AllMarketsTest(unittest.TestCase):
    def test_all_kamino_markets_main_first(self):
        import snipe_backtest as sbt
        old = sbt._get
        sbt._get = lambda key, d: {"markets": [("Kamino", "xStocks", "X1")], "liqs": {"X1": [1, 2]}}
        try:
            api = [{"lendingMarket": "A%d" % i, "name": "m%d" % i} for i in range(39)] + \
                  [{"lendingMarket": "MAIN", "name": "Main", "isPrimary": True}]
            lb.SCAN_MAIN = True
            got = lb.markets(fetch=lambda url: api)
            lb.SCAN_MAIN = False
            small = lb.markets(fetch=lambda url: api)
        finally:
            sbt._get = old
            lb.SCAN_MAIN = False
        self.assertNotIn(("Main", "MAIN"), small)                 # 512 MB server: main market off by default
        self.assertEqual(got[0], ("Main", "MAIN"))
        self.assertEqual(got[1], ("xStocks", "X1"))
        self.assertEqual(len(got), 41)
