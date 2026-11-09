import os
import tempfile
import time
import unittest
from unittest.mock import patch

import clone_strategy as cs
import copy_trader as ct

NOW = 1_800_000_000
SUMMARY = {"positions": 20, "win_rate": 0.65, "platforms": {"pump.fun": 15, "Raydium": 4, "outro": 1},
           "token_age_p10": 10.0, "token_age_p90": 60.0, "mcap_p10": 20_000.0, "mcap_p90": 200_000.0,
           "suggest_tp": 0.8, "suggest_sl": 0.25, "suggest_max_hold_min": 45.0}


def pair(mint, dex="pumpfun", age_min=30, mcap=50_000, liq=20_000):
    return {"chainId": "solana", "dexId": dex, "baseToken": {"address": mint, "symbol": mint[:4]},
            "pairCreatedAt": (NOW - age_min * 60) * 1000, "marketCap": mcap, "liquidity": {"usd": liq}}


def feed(pairs):
    def get(path):
        if path.startswith("/tokens/v1/solana/"):
            return pairs
        if path.startswith("/token-boosts/latest"):
            return [{"chainId": "solana", "tokenAddress": p["baseToken"]["address"]} for p in pairs]
        return []
    return get


class CloneStrategyTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        for q in (patch.object(ct, "safety_reason", return_value=""), patch.object(ct, "_sol_usd", return_value=200.0),
                  patch.object(ct, "_order", return_value={"outAmount": "1000000"})):
            q.start()
            self.addCleanup(q.stop)

    def test_config_from_profile(self):
        cfg = cs.config_from_profile("WALLET", SUMMARY)
        self.assertEqual(cfg["platforms"], ["pump.fun", "Raydium"])
        self.assertAlmostEqual(cfg["age_min"], 8.0)
        self.assertAlmostEqual(cfg["age_max"], 75.0)
        self.assertEqual((cfg["tp"], cfg["sl"], cfg["max_hold_s"]), (0.8, 0.25, 2700))

    def test_matches_profile(self):
        cfg = cs.config_from_profile("WALLET", SUMMARY)
        self.assertTrue(cs.matches(pair("A"), cfg, NOW)[0])
        self.assertIn("plataforma", cs.matches(pair("A", dex="orca"), cfg, NOW)[1])
        self.assertIn("idade", cs.matches(pair("A", age_min=600), cfg, NOW)[1])
        self.assertIn("valor de mercado", cs.matches(pair("A", mcap=5_000_000), cfg, NOW)[1])
        self.assertIn("liquidez", cs.matches(pair("A", liq=100), cfg, NOW)[1])

    def test_needs_strategy_before_turning_on(self):
        with self.assertRaises(ValueError):
            cs.set_mode(True)

    def test_sim_buys_only_matching_coins_and_respects_limit(self):
        cs.configure("WALLET", SUMMARY)
        cs.set_mode(True)
        coins = [pair("GOOD%d" % i) for i in range(5)] + [pair("BADX", dex="orca"), pair("OLDX", age_min=900)]
        msgs = cs.cycle(get=feed(coins), now=NOW)
        pos = cs._load()["positions"]
        self.assertEqual(len(pos), cs.MAX_POSITIONS)
        self.assertTrue(all(m.startswith("GOOD") for m in pos))
        self.assertEqual(len(msgs), cs.MAX_POSITIONS)
        self.assertIn("SIMULAÇÃO", cs.status_text())

    def test_sim_exit_at_target_and_time(self):
        cs.configure("WALLET", SUMMARY)
        cs.set_mode(True)
        d = cs._load()
        d["positions"] = {"UP": {"symbol": "UP", "tokens": 1, "cost_usd": 10.0, "opened_at": time.time()},
                          "SLOW": {"symbol": "SLOW", "tokens": 2, "cost_usd": 10.0, "opened_at": time.time() - 10_000}}
        cs._save(d)
        price = {"1": 0.1 * 1e9, "2": 0.05 * 1e9}       # UP worth US$ 20 (+100%), SLOW US$ 10 (flat)
        with patch.object(ct, "_order", side_effect=lambda i, o, a: {"outAmount": str(int(price[str(a)]))}):
            msgs = cs.sim_exits(cs._load()["config"])
        self.assertEqual(sorted(h["reason"] for h in cs._load()["history"]), ["alvo", "tempo"])
        self.assertTrue(any("LUCRO" in m for m in msgs))


class MenuTests(unittest.TestCase):
    def test_strategy_buttons(self):
        import telegram_bot as tb
        data = [b["callback_data"] for row in tb.keyboard() for b in row]
        for x in ("strat_sim", "strat_real", "strat_off", "strat_status", "clone_help"):
            self.assertIn(x, data)


if __name__ == "__main__":
    unittest.main()
