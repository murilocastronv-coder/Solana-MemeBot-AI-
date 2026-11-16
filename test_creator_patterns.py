import struct
import time
import unittest

import creator_patterns as cp
import memecoin_ai as mc
import pump_launch as pl


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = cp.B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def s(t):
    return struct.pack("<I", len(t)) + t.encode()


class CreatorTest(unittest.TestCase):
    def test_b58_roundtrip(self):
        for raw in (b"\x00\x01abc", bytes(range(40))):
            self.assertEqual(cp.b58decode(b58encode(raw)), raw)

    def test_parse_launch(self):
        from solders.keypair import Keypair
        payer, mint, other = (str(Keypair().pubkey()) for _ in range(3))
        curve = pl.pda([b"bonding-curve", mint], pl.PUMP)
        from solders.pubkey import Pubkey
        data = pl.DISC_CREATE_V2 + s("Frog Lab") + s("FLAB") + s("https://ipfs/x") + bytes(Pubkey.from_string(payer)) + b"\x00"
        tx = {"blockTime": 1_800_000_000, "transaction": {"message": {"accountKeys": [payer, mint, pl.PUMP],
              "instructions": [{"programIdIndex": 2, "accounts": [1], "data": b58encode(data)}]}},
              "meta": {"err": None, "preBalances": [int(2e9)], "postBalances": [int(1.48e9)],
                       "postTokenBalances": [{"mint": mint, "owner": curve, "uiTokenAmount": {"amount": str(9 * 10 ** 14)}},
                                             {"mint": mint, "owner": payer, "uiTokenAmount": {"amount": str(5 * 10 ** 13)}},
                                             {"mint": mint, "owner": other, "uiTokenAmount": {"amount": str(10 ** 13)}}]}}
        r = cp.parse_launch(tx)
        self.assertEqual((r["name"], r["symbol"], r["mint"], r["creator"]), ("Frog Lab", "FLAB", mint, payer))
        self.assertAlmostEqual(r["buy_sol"], 0.5, places=3)
        self.assertEqual(r["bundled"], 1)                      # another wallet bought inside the creation
        self.assertAlmostEqual(r["creator_pct"], 5.0)

    def test_labels_analysis_and_text(self):
        self.assertEqual(cp.label(None), "fail")
        self.assertEqual(cp.label({"grad": True, "mcap": 50_000}), "win")
        self.assertEqual(cp.label({"grad": True, "mcap": 5_000}), "pumped_and_fell")
        base = 1_800_000_000 - time.gmtime(1_800_000_000).tm_hour * 3600
        rows = []
        for i in range(400):
            h = (i % 8) * 3
            win = h == 15 and (i // 8) % 2 == 0
            rows.append({"mint": "m" + str(i), "name": "Frog Lab" if win else "Random Thing", "symbol": "X", "creator": "c" + str(i % 50),
                         "t": base + h * 3600, "buy_sol": 0.3, "bundled": 0, "creator_pct": 2, "x": win, "tg": False, "web": False,
                         "label": "win" if win else "fail"})
        rows.append(dict(rows[0], mint="bund", bundled=3, label="win"))
        rep = cp.analyse(rows, {"c0": {"launches": 4, "wins": 3}, "c1": {"launches": 200, "wins": 9}})
        self.assertEqual(rep["best_hours"][0], 15)
        self.assertEqual(rep["bundled"], 1)
        self.assertIn("frog", [w for w, k, n in rep["win_words"]])
        self.assertEqual([c for c, w, n in rep["creators"]], ["c0"])     # serial launcher left out
        txt = cp.text(rep)
        self.assertIn("12h–15h", txt)                                   # 15h UTC = 12h Brasília
        self.assertIn("não copiamos", txt)

    def test_auto_proposals_follow_best_hours(self):
        cp._MEM.clear()
        cp._MEM.update({"best_hours": [0, 6, 12, 18], "at": time.time()})
        try:
            t = 1_800_000_000 - time.gmtime(1_800_000_000).tm_hour * 3600 - time.gmtime(1_800_000_000).tm_min * 60
            self.assertTrue(mc.auto_due(t + 13 * 3600, 0))            # 13h UTC is inside the 12h block
            self.assertFalse(mc.auto_due(t + 13 * 3600, t + 12 * 3600 + 60))   # one per block
            self.assertFalse(mc.auto_due(t + 10 * 3600, 0))           # 9h block is not a best block
        finally:
            cp._MEM.clear()


if __name__ == "__main__":
    unittest.main()
