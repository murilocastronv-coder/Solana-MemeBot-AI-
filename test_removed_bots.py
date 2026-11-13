import time
import unittest

import live_exit_monitor as lem
import telegram_bot as tb


class RemovedBotsTests(unittest.TestCase):
    def test_open_positions_of_removed_bots_still_have_an_exit(self):
        for mode in ("signal", "dip"):
            pos = {"mode": mode, "peak_ratio": 0, "opened_at": time.time()}
            self.assertEqual(lem.position_signal(pos, 10, 5.9)["reason"], "stop_loss")
            self.assertEqual(lem.position_signal(pos, 10, 9)["action"], "hold")
            old = dict(pos, opened_at=time.time() - 7 * 3600)
            self.assertEqual(lem.position_signal(old, 10, 10.5)["reason"], "time_stop")

    def test_menu_has_only_copy_trade(self):
        data = {b["callback_data"] for row in tb.keyboard() for b in row}
        self.assertFalse(data & {"sig_on", "dip_on"})


if __name__ == "__main__":
    unittest.main()
