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


if __name__ == "__main__":
    unittest.main()
