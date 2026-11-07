import os, tempfile, time, unittest
from unittest.mock import patch
import btc_perps as b


def m5(n=80, drift=8.0, last_body=90.0, last_close_top=True, start=60000.0, t_end=None):
    """Uptrending 5m candles; the last one is a strong bullish candle closing near its high."""
    t_end = t_end or (int(time.time()) // 300 * 300 - 300)
    out, price = [], start
    for i in range(n - 1):
        if i < n - 16:
            step = drift * 1.6 if i % 2 else -drift          # uptrend
        else:
            step = drift * 1.3 if i % 2 else -drift * 1.9    # pause before the breakout candle (RSI cools)
        o = price; c = price + step
        out.append({"t": t_end - (n - 1 - i) * 300, "o": o, "h": max(o, c) + 70, "l": min(o, c) - 70, "c": c, "v": 100.0}); price = c
    o = price; c = price + last_body
    out.append({"t": t_end, "o": o, "h": c + (2 if last_close_top else 90), "l": o - 5, "c": c, "v": 250.0})
    return out


def flip(cands):
    base = cands[0]["o"] * 2
    return [{"t": x["t"], "o": base - x["o"], "h": base - x["l"], "l": base - x["h"], "c": base - x["c"], "v": x["v"]} for x in cands]


class ScalpTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.sent = []
        b.set_enabled(True); b.set_leverage(3); b.set_option("strategy", "scalp")

    def send(self, chat, text, *a):
        self.sent.append(text)

    def test_signals(self):
        self.assertEqual(b.scalp_signal(m5())[0], "long")
        self.assertEqual(b.scalp_signal(flip(m5()))[0], "short")
        low_vol = m5(); low_vol[-1]["v"] = 50.0
        side, _, why = b.scalp_signal(low_vol)
        self.assertIsNone(side); self.assertIn("volume", why)
        quiet = m5(drift=1, last_body=10)
        for c in quiet: c["h"] = max(c["o"], c["c"]) + 3; c["l"] = min(c["o"], c["c"]) - 3
        side, _, why = b.scalp_signal(quiet)
        self.assertIsNone(side); self.assertIn("volatilidade", why)

    def test_opens_and_closes_at_candle_end(self):
        c1 = m5()
        b.cycle(self.send, "1", candles=c1, price=c1[-1]["c"])
        pos = b.summary()["position"]
        self.assertEqual((pos["side"], pos["strategy"], pos["exit_at"]), ("long", "scalp", c1[-1]["t"] + 600))
        self.assertIn("Fecha no fim da vela", self.sent[-1])
        # next candle closes without a signal -> time exit
        c2 = c1 + [{"t": c1[-1]["t"] + 300, "o": c1[-1]["c"], "h": c1[-1]["c"] + 30, "l": c1[-1]["c"] - 30, "c": c1[-1]["c"] + 5}]
        with patch("btc_perps.time.time", return_value=pos["exit_at"] + 1):
            b.cycle(self.send, "1", candles=c2, price=c1[-1]["c"] + 5)
        s = b.summary()
        self.assertIsNone(s["position"])
        self.assertEqual(s["history"][-1]["reason"], "candle_close")
        self.assertIn("fim da vela", self.sent[-1])

    def test_never_carried_to_next_candle(self):
        c1 = m5()
        b.cycle(self.send, "1", candles=c1, price=c1[-1]["c"])
        pos = b.summary()["position"]
        c2 = m5(n=81, t_end=c1[-1]["t"] + 300)              # next candle signals long again
        with patch("btc_perps.time.time", return_value=pos["exit_at"] + 1):
            b.cycle(self.send, "1", candles=c2, price=c1[-1]["c"])
        s = b.summary()
        self.assertEqual(s["history"][-1]["reason"], "candle_close")   # closed at candle end
        self.assertEqual(s["position"]["exit_at"], c2[-1]["t"] + 600)  # fresh trade for the new candle

    def test_take_profit_and_sizing(self):
        c1 = m5()
        b.cycle(self.send, "1", candles=c1, price=c1[-1]["c"])
        pos = b.summary()["position"]
        self.assertAlmostEqual(pos["collateral"], 10.0)                 # 10% of $100
        self.assertAlmostEqual(pos["stop"], pos["entry"] * 0.9975)
        self.assertAlmostEqual(pos["tp"], pos["entry"] * 1.004)
        b.cycle(self.send, "1", candles=c1, price=pos["tp"] + 1)
        self.assertEqual(b.summary()["history"][-1]["reason"], "take_profit")

    def test_pause_after_three_losses(self):
        c = m5()
        for k in range(3):
            ck = m5(n=80 + k, t_end=c[-1]["t"] + 300 * k)
            b.cycle(self.send, "1", candles=ck, price=ck[-1]["c"])
            pos = b.summary()["position"]
            b.cycle(self.send, "1", candles=ck, price=pos["stop"] - 1)  # stopped out
        self.assertIn("3 perdas seguidas", "\n".join(self.sent))
        c4 = m5(n=84, t_end=c[-1]["t"] + 900)
        b.cycle(self.send, "1", candles=c4, price=c4[-1]["c"])
        self.assertIsNone(b.summary()["position"])

    def test_protective_stop_inside_candle(self):
        c1 = m5()
        b.cycle(self.send, "1", candles=c1, price=c1[-1]["c"])
        pos = b.summary()["position"]
        b.cycle(self.send, "1", candles=c1, price=pos["stop"] - 1)
        self.assertEqual(b.summary()["history"][-1]["reason"], "stop_loss")

    def test_daily_loss_pause(self):
        data = b._load()
        data.update(scalp_day=time.strftime("%Y-%m-%d", time.gmtime()), scalp_day_start=120.0)
        b._save(data)                       # account is 100 -> lost > 10% today
        c1 = m5()
        b.cycle(self.send, "1", candles=c1, price=c1[-1]["c"])
        self.assertIsNone(b.summary()["position"])
        self.assertIn("pausado até amanhã", self.sent[-1])


class TelegramScalpTests(unittest.TestCase):
    def test_real_mode_requires_confirmation(self):
        d = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")}):
            import telegram_bot as t, telegram_owner
            telegram_owner.claim(5)
            data = b._load(); data.update(mode="real"); b._save(data)
            out = []
            with patch.object(t, "send", side_effect=lambda c, x, k=None: out.append((x, k))), \
                 patch.object(t, "request_api", return_value={}), patch("btc_perps.real_equity", return_value=150.0):
                cb = lambda a: t.handle({"callback_query": {"id": "1", "data": a, "message": {"chat": {"id": 5, "type": "private"}}}})
                cb("strat_scalp")
                self.assertIn("DINHEIRO REAL", out[-1][0])
                self.assertEqual(b._load()["strategy"], "trend")          # not switched yet
                cb("strat_scalp_ok")
                self.assertEqual(b._load()["strategy"], "scalp")
                self.assertIn("SCALPING M5 ATIVO", out[-1][0])


if __name__ == "__main__":
    unittest.main()
