import os
import tempfile
import time
import unittest
from unittest.mock import patch

import signals as sg

MINT = "SigMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = 1_800_000_000


def pair(**kw):
    p = {"chainId": "solana", "dexId": "pumpswap", "pairAddress": "POOL1",
         "baseToken": {"address": MINT, "symbol": "SIG"}, "quoteToken": {"address": sg.SOL_MINT},
         "liquidity": {"usd": 80_000}, "pairCreatedAt": (NOW - 10 * 3600) * 1000,
         "txns": {"h1": {"buys": 300, "sells": 150}}, "volume": {"h24": 400_000},
         "priceChange": {"h6": 40, "h24": 120}, "priceUsd": "0.001"}
    p.update(kw)
    return p


def dip_candles(bounce=True):
    """Uptrend for 80 candles, a 20% dip in the last ~10 and (optionally) a green reversal candle."""
    cs, price, t = [], 1.0, NOW - 100 * 300
    for i in range(80):
        o = price
        price *= 1.008
        cs.append((t, o, price * 1.002, o * 0.998, price, 1000))
        t += 300
    top = price
    for i in range(10):                                   # correction
        o = price
        price *= 0.976
        cs.append((t, o, o * 1.001, price * 0.995, price, 800))
        t += 300
    if bounce:
        o = price
        price = o * 1.06                                   # green candle closing above the previous high
        cs.append((t, o, price * 1.001, o * 0.999, price, 1500))
    return cs, top


class PopularTests(unittest.TestCase):
    def test_liquidity_10k_to_150k(self):
        self.assertEqual(sg.popular_reason(pair(), NOW), "")
        self.assertIn("< US$ 10 mil", sg.popular_reason(pair(liquidity={"usd": 9_000}), NOW))
        self.assertIn("> US$ 150 mil", sg.popular_reason(pair(liquidity={"usd": 151_000}), NOW))

    def test_most_people_buying_and_uptrend(self):
        self.assertIn("pouca gente", sg.popular_reason(pair(txns={"h1": {"buys": 20, "sells": 5}}), NOW))
        self.assertIn("mais venda", sg.popular_reason(pair(txns={"h1": {"buys": 300, "sells": 400}}), NOW))
        self.assertIn("tendência", sg.popular_reason(pair(priceChange={"h6": -5, "h24": 30}), NOW))


class EntryTests(unittest.TestCase):
    def test_dip_in_uptrend_turning_up_gives_entry_stop_and_targets(self):
        cs, top = dip_candles()
        setup, why = sg.entry_setup(cs)
        self.assertEqual(why, "")
        self.assertLess(setup["sl"], setup["entry"])
        self.assertTrue(sg.SL_MIN - 1e-9 <= setup["risk"] <= sg.SL_MAX)
        self.assertAlmostEqual(setup["tp1"], setup["entry"] * (1 + 1.5 * setup["risk"]))
        self.assertAlmostEqual(setup["tp2"], setup["entry"] * (1 + 3 * setup["risk"]))
        self.assertGreater(setup["pullback"], 0.08)

    def test_no_signal_while_still_falling(self):
        cs, _ = dip_candles(bounce=False)
        setup, why = sg.entry_setup(cs)
        self.assertIsNone(setup)

    def test_no_signal_at_the_top(self):
        cs, price, t = [], 1.0, NOW
        for i in range(90):
            o = price
            price *= 1.01
            cs.append((t, o, price, o, price, 1000))
            t += 300
        self.assertEqual(sg.entry_setup(cs)[1], "sem correção (perto do topo)")


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        d = sg._load()
        d["open"]["s1"] = {"mint": MINT, "symbol": "SIG", "entry": 1.0, "sl": 0.9, "tp1": 1.15, "tp2": 1.30, "at": NOW}
        sg._save(d)

    def test_tp1_then_tp2(self):
        msgs = sg.follow_up({MINT: 1.16}, now=NOW + 60)
        self.assertIn("TP1 ATINGIDO", msgs[0])
        msgs = sg.follow_up({MINT: 1.31}, now=NOW + 120)
        self.assertIn("TP2 ATINGIDO", msgs[0])
        r = sg.results()
        self.assertAlmostEqual(r["avg"], (0.15 * 0.5 + 0.30 * 0.5) * 100)   # half at TP1, half at TP2

    def test_stop_loss(self):
        self.assertIn("STOP LOSS", sg.follow_up({MINT: 0.89}, now=NOW + 60)[0])
        self.assertAlmostEqual(sg.results()["avg"], -10.0)

    def test_after_tp1_the_stop_is_the_entry(self):
        sg.follow_up({MINT: 1.16}, now=NOW + 60)
        msgs = sg.follow_up({MINT: 0.99}, now=NOW + 120)
        self.assertIn("VOLTOU À ENTRADA", msgs[0])
        self.assertAlmostEqual(sg.results()["avg"], 7.5)


class CycleTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)

    def fetch(self, cs):
        def f(url):
            if "ohlcv" in url:
                return {"data": {"attributes": {"ohlcv_list": [list(c) for c in reversed(cs)]}}}
            if "/tokens/v1/solana/" in url:
                return [pair()]
            if "geckoterminal" in url:
                return {"data": []}
            return [{"chainId": "solana", "tokenAddress": MINT}]
        return f

    def test_signal_sent_once_with_entry_stop_and_targets(self):
        sg.set_enabled(True)
        cs, _ = dip_candles()
        sent = []
        with patch.object(sg, "popular_reason", return_value=""):
            sg.cycle(lambda c, t: sent.append(t), 1, fetch=self.fetch(cs), now=NOW, safety=lambda m, curve=False: "")
            sg.cycle(lambda c, t: sent.append(t), 1, fetch=self.fetch(cs), now=NOW + 120, safety=lambda m, curve=False: "")
        signals_sent = [t for t in sent if "SINAL DE ENTRADA" in t]
        self.assertEqual(len(signals_sent), 1)                    # not repeated
        for word in ("ENTRADA", "STOP LOSS", "TAKE PROFIT 1", "TAKE PROFIT 2", MINT):
            self.assertIn(word, signals_sent[0])

    def test_unsafe_coin_gives_no_signal(self):
        sg.set_enabled(True)
        cs, _ = dip_candles()
        with patch.object(sg, "popular_reason", return_value=""):
            out = sg.cycle(None, None, fetch=self.fetch(cs), now=NOW,
                           safety=lambda m, curve=False: "teste de venda devolveu 40%")
        self.assertEqual(out, [])
        self.assertIn("anti-golpe: teste de venda devolveu 40%", sg.LAST["why"])

    def test_off_sends_nothing(self):
        self.assertEqual(sg.cycle(None, None, fetch=lambda u: [], now=NOW), [])


class RemovedRobotsTests(unittest.TestCase):
    def test_open_positions_of_the_removed_robots_still_have_exits(self):
        from live_exit_monitor import position_signal
        for mode in ("sniper", "consensus"):
            pos = {"mode": mode, "opened_at": time.time()}
            self.assertEqual(position_signal(pos, 100, 59)["reason"], "stop_loss")


if __name__ == "__main__":
    unittest.main()
