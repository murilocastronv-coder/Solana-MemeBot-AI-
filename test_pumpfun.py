import time, unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, Mock
import live_engine, live_trade_state as state, pumpfun, sim_engine
from test_live_engine import LiveEngineTests

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def token(**over):
    t = {"id": MINT, "symbol": "PEPE2", "launchpad": "pump.fun", "holderCount": 400, "twitter": "https://x.com/pepe2",
         "liquidity": 30000, "mcap": 250000,
         "firstPool": {"createdAt": (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()},
         "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True,
                   "topHoldersPercentage": 18.0, "devBalancePercentage": 1.0},
         "stats5m": {"priceChange": 9.0, "numBuys": 80, "numSells": 40, "buyVolume": 9000, "sellVolume": 5000},
         "stats1h": {"priceChange": 120.0, "numBuys": 600}}
    t.update(over)
    return t


class CandidateTests(unittest.TestCase):
    def test_good_token_passes(self):
        self.assertTrue(pumpfun.candidate(token()))

    def test_rejections(self):
        bad = [token(holderCount=40),
               token(audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True, "topHoldersPercentage": 10}),
               token(audit={"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 40}),
               token(stats5m={"priceChange": 80, "numBuys": 80, "numSells": 40, "buyVolume": 9000, "sellVolume": 5000}),
               token(liquidity=8000),
               token(firstPool={"createdAt": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}),
               token(firstPool={"createdAt": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()})]
        for t in bad:
            self.assertFalse(pumpfun.candidate(t))

    def test_only_pumpfun_tokens(self):
        self.assertTrue(pumpfun.is_pumpfun({"id": "abcpump", "launchpad": ""}))
        self.assertFalse(pumpfun.is_pumpfun({"id": MINT, "launchpad": "bonk.fun"}))

    def test_feed_error_is_reported(self):
        pumpfun._cache[:] = [0.0, []]
        with patch("pumpfun.requests.get", side_effect=RuntimeError("401 Unauthorized")):
            self.assertEqual(pumpfun.fetch(), [])
        self.assertIn("401", pumpfun.last_scan["error"])

    def test_feed_is_parsed(self):
        pumpfun._cache[:] = [0.0, []]
        resp = Mock(); resp.json.return_value = [token(), {"id": "other", "launchpad": "raydium"}]
        resp.raise_for_status.return_value = None
        with patch("pumpfun.requests.get", return_value=resp):
            rows = pumpfun.fetch()
        self.assertEqual([t["id"] for t in rows], [MINT])


class PumpfunTradingTests(LiveEngineTests):
    def setUp(self):
        super().setUp()
        pumpfun._seen.clear()
        for p in (patch("pumpfun.fetch", return_value=[token()]),
                  patch("live_engine.holders_ok", return_value=(True, "")),
                  patch("sim_engine.order", return_value={"outAmount": "1000"})):
            p.start(); self.addCleanup(p.stop)

    def test_off_by_default(self):
        live_engine.start()
        self.assertIsNone(pumpfun.cycle(self.send, "1"))

    def test_real_buy_15pct_marked_pumpfun(self):
        live_engine.start(); state.set_flag("pumpfun_enabled", True); self.side = "buy"
        result = pumpfun.cycle(self.send, "1")
        self.assertEqual(result["status"], "settled_buy")
        pos = state._load()["positions"][MINT]
        self.assertEqual((pos["mode"], pos["cost_usd"]), ("pumpfun", 30.0))
        self.assertIn("PUMP.FUN COMPROU PEPE2", self.sent[-1])
        self.assertIn("Stop -12%", self.sent[-1])

    def test_simulation_runs_pumpfun(self):
        sim_engine.set_enabled(True)
        self.assertEqual(sim_engine.pumpfun_cycle(self.send, "1"), 1)
        pos = list(sim_engine._load()["positions"].values())[0]
        self.assertEqual((pos["mode"], pos["cost_usd"]), ("pumpfun", 15.0))
        self.assertEqual(state.status()["positions"], 0)


if __name__ == "__main__":
    unittest.main()
