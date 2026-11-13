import os
import tempfile
import unittest
from unittest.mock import patch

from solders.keypair import Keypair

import copy_trader as c
import wallet_twin as wt

REF = str(Keypair().pubkey())
REF_STYLE = {"entry_age_min": 4.0, "curve_share": 0.7, "hold_min": 12.0, "trades_day": 30.0, "size_usd": 50.0, "scale_out": 0.6, "scale_in": 0.3,
             "pump_share": 0.9, "win_rate": 0.6, "positions": 200, "pnl_usd": 900.0}


def audit(style, pnl=50.0, win=0.6, trades=60):
    return {"at": 10**10, "positions": trades, "median_s": 1200, "long_share": 0.8, "pnl_recent": pnl,
            "trades_recent": trades, "win_recent": win, "style": style}


def cand():
    return {"wallet": str(Keypair().pubkey()), "closed_trades": 20, "realized_lamports": 10**9,
            "hold_samples": [60, 90, 120], "average_buy_lamports": 10**8, "age_days": 90, "active_days_7": 7}


class TwinTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        for p in (patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}),
                  patch.object(wt, "REFERENCE", REF), patch.object(c, "wallet_usd", return_value=1000.0),
                  patch.object(c, "_sol_usd", return_value=200.0),
                  patch.object(wt, "reference_style", return_value=REF_STYLE)):
            p.start()
            self.addCleanup(p.stop)

    def test_similarity(self):
        self.assertEqual(wt.similarity(REF_STYLE, REF_STYLE), 100.0)
        other = dict(REF_STYLE, hold_min=600.0, trades_day=1.0, pump_share=0.0, scale_out=0.0)
        self.assertLess(wt.similarity(REF_STYLE, other), 30)

    def test_sniper_search(self):
        snipe = dict(REF_STYLE, hold_min=12.0, entry_age_min=3.0, curve_share=0.8, trades_day=3.0, size_usd=500.0)
        self.assertTrue(wt.judge(snipe, audit(snipe), REF_STYLE)[0])            # style differs, but it snipes
        self.assertIn("segura pouco", wt.judge(dict(snipe, hold_min=2.0), audit(snipe), REF_STYLE)[2])
        self.assertIn("segura muito", wt.judge(dict(snipe, hold_min=60.0), audit(snipe), REF_STYLE)[2])
        self.assertIn("não é sniper", wt.judge(dict(snipe, entry_age_min=300.0), audit(snipe), REF_STYLE)[2])
        no_age = dict(snipe, entry_age_min=None)
        self.assertTrue(wt.judge(no_age, audit(snipe), REF_STYLE)[0])           # most buys on the launch curve
        self.assertFalse(wt.judge(dict(no_age, curve_share=0.1), audit(snipe), REF_STYLE)[0])

    def test_hit_rate_is_compared_with_the_reference_wallet(self):
        low = dict(REF_STYLE, win_rate=0.13)
        ok, _, _ = wt.judge(low, audit(low, win=0.15), low)          # wins as often as the reference: ok
        self.assertTrue(ok)
        self.assertFalse(wt.judge(low, audit(low, win=0.05), low)[0])

    def test_style_from_profile_summary(self):
        s = {"positions": 10, "hold_median": 12, "trades_per_day": 8, "size_median": 40, "scale_out": 0.5,
             "scale_in": 0.1, "platforms": {"pump.fun": 6, "PumpSwap": 2, "Raydium": 2}, "win_rate": 0.7, "pnl_usd": 30}
        self.assertAlmostEqual(wt.style(s)["pump_share"], 0.8)

    def test_only_the_reference_wallet_is_copied(self):
        c.follow("4x5Uti74CuwoM1wRq3gmuMJ3p5qmGNMEbP428Rgb7zTJ")
        other = str(Keypair().pubkey())
        c.follow(other)
        c.apply_reference_only()
        d = c._load()
        self.assertEqual(list(d["wallets"]), [REF])
        self.assertTrue(c.mirror(REF))
        self.assertEqual(c.apply_reference_only(), [])          # runs only once

    def test_same_style_and_profitable_wallet_is_followed(self):
        w = cand()
        with patch.object(c, "measure", return_value=([60] * 5, audit(dict(REF_STYLE, hold_min=15.0)))):
            self.assertEqual(c.preaudit_and_follow(w, None, 200.0), "added")
        self.assertEqual(c.holder_status(c._load()["wallets"][w["wallet"]])[0], "ok")
        self.assertTrue(c.prefilter(cand()))                   # small buyer / quick holds pass the cheap filter

    def test_different_style_or_losing_wallet_is_rejected(self):
        far = dict(REF_STYLE, hold_min=900.0, trades_day=0.5, pump_share=0.0, scale_out=0.0, scale_in=0.0)
        w = cand()
        with patch.object(c, "measure", return_value=([60] * 5, audit(far))):
            self.assertEqual(c.preaudit_and_follow(w, None, 200.0), "rejected")
        w2 = cand()
        with patch.object(c, "measure", return_value=([60] * 5, audit(REF_STYLE, pnl=-20))):
            self.assertEqual(c.preaudit_and_follow(w2, None, 200.0), "rejected")
        self.assertNotIn(w["wallet"], c._load()["wallets"])


if __name__ == "__main__":
    unittest.main()
