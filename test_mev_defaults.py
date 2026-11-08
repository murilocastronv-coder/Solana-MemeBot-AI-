import os
import tempfile
import unittest
from unittest.mock import patch

import live_trade_state as s


class MevDefaultsTests(unittest.TestCase):
    def test_old_defaults_move_once_and_owner_choice_kept(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            st = s._load(); st["mev_settings"] = {"input_lamports": 10_000_000, "jito_tip_lamports": 1_000}; s._save(st)
            cfg = s.mev_config()
            self.assertEqual((cfg["input_lamports"], cfg["jito_tip_lamports"]), (50_000_000, 10_000))
            s.update_mev_setting("input_lamports", 10_000_000)   # owner goes back to 0.01 SOL
            self.assertEqual(s.mev_config()["input_lamports"], 10_000_000)


    def test_mev_only_applied_once(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            s.start(); s.set_sniper(True); s.set_flag("launch_enabled", True); s.set_flag("candle_enabled", True)
            self.assertTrue(s.apply_mev_only_once())
            st = s.status()
            self.assertFalse(st["enabled"])
            self.assertFalse(s.sniper_enabled() or s.flag("launch_enabled") or s.flag("candle_enabled") or s.flag("pumpfun_enabled"))
            self.assertTrue(s.flag("hot_disabled") and s.flag("mev_enabled"))
            s.set_flag("launch_enabled", True)          # owner turns one back on later
            self.assertFalse(s.apply_mev_only_once())   # not undone again
            self.assertTrue(s.flag("launch_enabled"))


if __name__ == "__main__":
    unittest.main()
