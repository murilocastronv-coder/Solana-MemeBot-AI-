import os
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import sniper_bot as s

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def token(minutes=25, **over):
    t = {"id": MINT, "symbol": "NEWPUMP", "launchpad": "pump.fun", "graduatedPool": "Pool1",
         "firstPool": {"createdAt": (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()},
         "liquidity": 40_000, "mcap": 300_000, "holderCount": 450, "twitter": "https://x.com/new",
         "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True,
                   "topHoldersPercentage": 15.0, "devBalancePercentage": 1.0},
         "organicScoreLabel": "medium",
         "stats5m": {"priceChange": 9.0, "numBuys": 80, "numSells": 40, "buyVolume": 9000, "sellVolume": 5000},
         "stats1h": {"priceChange": 120.0, "numBuys": 600}}
    t.update(over)
    return t


class SniperFilterTests(unittest.TestCase):
    def test_good_token_passes(self):
        self.assertEqual(s.reasons(token()), [])

    def test_age_window_10_to_60(self):
        self.assertIn("fora de 10–60 min de vida", s.reasons(token(minutes=5)))
        self.assertIn("fora de 10–60 min de vida", s.reasons(token(minutes=75)))

    def test_scam_signals_rejected(self):
        bad = {"ainda na curva do pump.fun (liquidez não queimada)": token(graduatedPool=None),
               "criador pode criar/congelar": token(audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True,
                                                           "topHoldersPercentage": 10, "devBalancePercentage": 1}),
               "maiores donos concentrados": token(audit={"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True,
                                                         "topHoldersPercentage": 40, "devBalancePercentage": 1}),
               "dev com muitas moedas": token(audit={"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True,
                                                    "topHoldersPercentage": 10, "devBalancePercentage": 9}),
               "marcada como suspeita": token(isSus=True),
               "volume falso (organic baixo)": token(organicScoreLabel="low"),
               "sem site/redes": token(twitter=None),
               "poucos donos": token(holderCount=50)}
        for reason, t in bad.items():
            self.assertIn(reason, s.reasons(t))


class SniperCycleTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start(); self.addCleanup(p.stop)
        s._tried.clear(); s._RECENT.clear()
        self.sell_sol = 0
        for x in [patch.object(s.base, "_sol_usd", return_value=200.0),
                  patch.object(s.base, "safety_reason", return_value=""),
                  patch.object(s.base, "_order", side_effect=lambda i, o, a: {"outAmount": "1000" if i == s.SOL_MINT else str(self.sell_sol)})]:
            x.start(); self.addCleanup(x.stop)

    def test_off_by_default(self):
        self.assertEqual(s.buy_cycle([token()]), [])

    def test_sim_buy_then_trailing_exit(self):
        s.set_mode(True)
        self.assertEqual(len(s.buy_cycle([token()])), 1)
        self.sell_sol = int(14 / 200 * 1e9)          # +40%: trailing armed, hold
        self.assertEqual(s.exit_cycle(), [])
        self.sell_sol = int(11.5 / 200 * 1e9)        # >12% below the peak -> sell in profit
        closed = s.exit_cycle()
        self.assertEqual(closed[0]["reason"], "take_profit")
        self.assertGreater(closed[0]["pnl_usd"], 1)

    def test_sim_stop_and_time_stop(self):
        s.set_mode(True)
        s.buy_cycle([token()])
        self.sell_sol = int(8.5 / 200 * 1e9)
        self.assertEqual(s.exit_cycle()[0]["reason"], "stop_loss")
        self.assertEqual(s.exit_decision(10, 10.2, 1.02, 31 * 60)["reason"], "time_stop")

    def test_max_three_positions(self):
        s.set_mode(True)
        from solders.keypair import Keypair
        s.buy_cycle([token(id=str(Keypair().pubkey())) for _ in range(5)])
        self.assertEqual(len(s._load()["positions"]), 3)

    def test_real_mode_uses_live_engine_with_fixed_10_usd(self):
        s.set_mode(True, real=True)
        with patch.object(s, "_real_room", return_value=""), \
             patch.object(s, "_real_buy", return_value={"status": "settled_buy"}) as buy:
            out = s.buy_cycle([token()], send=lambda *a: None, chat="1")
        self.assertTrue(out[0]["real"])
        buy.assert_called_once()
        self.assertEqual(s._load()["positions"], {})
        self.assertIn("REAL", s.status_text())


if __name__ == "__main__":
    unittest.main()
