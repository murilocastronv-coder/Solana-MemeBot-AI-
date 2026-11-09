import base64, hashlib, json, os, struct, tempfile, time, unittest
from unittest.mock import patch
from solders.pubkey import Pubkey
from solders.keypair import Keypair
import jupiter_perps as jp

OWNER = Keypair()


def position_bytes(side=1, price=65000, size=10, collateral=5):
    raw = b"\0" * 8 + bytes(OWNER.pubkey()) + bytes(jp.POOL) + bytes(jp.BTC_CUSTODY) + bytes(jp.BTC_CUSTODY)
    raw += struct.pack("<qq", 1, 2) + bytes([side]) + struct.pack("<QQQq", int(price * 1e6), int(size * 1e6), int(collateral * 1e6), 0)
    return raw + b"\0" * 40


class EncodingTests(unittest.TestCase):
    def test_discriminator_is_anchor_global_hash(self):
        self.assertEqual(jp._disc("createIncreasePositionMarketRequest"),
                         hashlib.sha256(b"global:create_increase_position_market_request").digest()[:8])

    def test_increase_matches_idl_layout(self):
        ix = jp.increase_ix(OWNER.pubkey(), "short", 5_000_000, 10_000_000, 64_000_000_000, None, 7)
        self.assertEqual(len(ix.accounts), 16)
        self.assertEqual(ix.accounts[0].pubkey, OWNER.pubkey())
        self.assertTrue(ix.accounts[0].is_signer)
        self.assertEqual(ix.accounts[8].pubkey, jp.USDC_CUSTODY)    # short -> USDC collateral
        self.assertEqual(ix.accounts[10].pubkey, jp.PROGRAM_ID)     # referral None
        data = bytes(ix.data)[8:]
        size, coll = struct.unpack_from("<QQ", data, 0)
        self.assertEqual((size, coll, data[16]), (10_000_000, 5_000_000, 2))
        self.assertEqual(struct.unpack_from("<Q", data, 17)[0], 64_000_000_000)
        self.assertEqual(data[25], 0)                                 # jupiterMinimumOut None
        self.assertEqual(struct.unpack_from("<Q", data, 26)[0], 7)

    def test_long_uses_btc_collateral_and_min_out(self):
        ix = jp.increase_ix(OWNER.pubkey(), "long", 5_000_000, 10_000_000, 1, 7000, 9)
        self.assertEqual(ix.accounts[8].pubkey, jp.BTC_CUSTODY)
        data = bytes(ix.data)[8:]
        self.assertEqual((data[25], struct.unpack_from("<Q", data, 26)[0]), (1, 7000))

    def test_decrease_closes_entire_position(self):
        data = bytes(jp.decrease_ix(OWNER.pubkey(), "long", 1, 5).data)[8:]
        self.assertEqual(data[24:27], b"\x00\x01\x01")               # minOut None, entirePosition Some(true)

    def test_read_position(self):
        enc = base64.b64encode(position_bytes()).decode()
        with patch("jupiter_perps.rpc", return_value={"value": {"data": [enc, "base64"]}}):
            pos = jp.read_position(OWNER.pubkey(), "long")
        self.assertEqual((pos["side"], pos["price"], pos["size_usd"], pos["collateral_usd"]), ("long", 65000, 10, 5))


class SendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true", "BOT_WALLET_ENCRYPTION_PASSPHRASE": "x" * 24})
        self.env.start(); self.addCleanup(self.env.stop)
        for p in (patch("jupiter_perps._keypair", return_value=OWNER),
                  patch("jupiter_perps.get_address", return_value=str(OWNER.pubkey()))):
            p.start(); self.addCleanup(p.stop)
        self.calls = []

    def fake_rpc(self, sim_err=None):
        def rpc(method, params):
            self.calls.append(method)
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": "11111111111111111111111111111111"}}
            if method == "simulateTransaction":
                return {"value": {"err": sim_err, "logs": ["Program log: Error: MaxPriceSlippage"]}}
            if method == "sendTransaction":
                return "SIG"
        return rpc

    def test_failed_simulation_never_sends(self):
        with patch("jupiter_perps.rpc", side_effect=self.fake_rpc({"InstructionError": [2, {"Custom": 6001}]})):
            with self.assertRaises(jp.PerpsError) as ctx:
                jp._send([jp.decrease_ix(OWNER.pubkey(), "long", 1, 5)])
        self.assertNotIn("sendTransaction", self.calls)
        self.assertIn("nada foi enviado", str(ctx.exception))

    def test_simulation_ok_then_send(self):
        with patch("jupiter_perps.rpc", side_effect=self.fake_rpc()):
            self.assertEqual(jp._send([jp.decrease_ix(OWNER.pubkey(), "long", 1, 5)]), "SIG")
        self.assertEqual(self.calls, ["getLatestBlockhash", "simulateTransaction", "sendTransaction"])

    def test_disabled_without_live_switch(self):
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "false"}):
            with self.assertRaises(jp.PerpsError):
                jp._send([])



if __name__ == "__main__":
    unittest.main()
