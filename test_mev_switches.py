import os
import unittest
from unittest.mock import patch

import live_trade_state as s


class MevSwitchTests(unittest.TestCase):
    def test_live_permission_on_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEV_AUTOTRADE_ENABLED", None)
            os.environ.pop("MEV_LIVE_ENABLED", None)
            self.assertTrue(s.mev_autotrade_allowed())
            self.assertTrue(s.mev_live_allowed())

    def test_false_turns_it_off(self):
        with patch.dict(os.environ, {"MEV_LIVE_ENABLED": "false"}):
            self.assertFalse(s.mev_live_allowed())
        with patch.dict(os.environ, {"MEV_AUTOTRADE_ENABLED": "FALSE", "MEV_LIVE_ENABLED": "true"}):
            self.assertFalse(s.mev_autotrade_allowed())
            self.assertFalse(s.mev_live_allowed())


if __name__ == "__main__":
    unittest.main()
