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
    def test_liquidity_10k_to_300k(self):
        self.assertEqual(sg.popular_reason(pair(), NOW), "")
        self.assertIn("< US$ 10 mil", sg.popular_reason(pair(liquidity={"usd": 9_000}), NOW))
        self.assertIn("> US$ 300 mil", sg.popular_reason(pair(liquidity={"usd": 301_000}), NOW))
        self.assertEqual(sg.popular_reason(pair(liquidity={"usd": 250_000}), NOW), "")

    def test_most_people_buying_and_uptrend(self):
        self.assertIn("pouca gente", sg.popular_reason(pair(txns={"h1": {"buys": 20, "sells": 5}}), NOW))
        self.assertIn("mais venda", sg.popular_reason(pair(txns={"h1": {"buys": 300, "sells": 400}}), NOW))
        self.assertIn("tendência", sg.popular_reason(pair(priceChange={"h6": -5, "h24": -2}), NOW))
        self.assertEqual(sg.popular_reason(pair(priceChange={"h6": -5, "h24": 30}), NOW), "")   # 24 h uptrend is enough
        self.assertIn("tendência", sg.popular_reason(pair(priceChange={"h1": -40, "h6": 10, "h24": 30}), NOW))


class PumpFunTests(unittest.TestCase):
    def test_only_pump_fun_coins(self):
        self.assertEqual(sg.popular_reason(pair(dexId="pumpfun"), NOW), "")          # launch curve
        self.assertEqual(sg.popular_reason(pair(dexId="pumpswap"), NOW), "")         # graduated
        self.assertEqual(sg.popular_reason(pair(dexId="raydium", baseToken={"address": "Abc" + "x" * 37 + "pump",
                                                                          "symbol": "P"}), NOW), "")  # pump.fun mint
        self.assertIn("não é da pump.fun", sg.popular_reason(pair(dexId="raydium"), NOW))

    def test_young_coin_uses_1_minute_candles(self):
        self.assertEqual(sg.candle_minutes(pair(pairCreatedAt=(NOW - 2 * 3600) * 1000), NOW), 1)
        self.assertEqual(sg.candle_minutes(pair(pairCreatedAt=(NOW - 20 * 3600) * 1000), NOW), 5)
        urls = []
        sg.candles("POOL1", fetch=lambda u: urls.append(u) or {}, minutes=1)
        self.assertIn("aggregate=1", urls[0])

    def test_candles_fall_back_to_the_geckoterminal_pool_of_the_coin(self):
        import requests
        resp = requests.Response()
        resp.status_code = 404
        urls = []

        def f(u):
            urls.append(u)
            if "/pools/CURVE/" in u:
                raise requests.HTTPError(response=resp)
            if "/tokens/" in u:
                return {"data": [{"attributes": {"address": "GPOOL"}}]}
            return {"data": {"attributes": {"ohlcv_list": [[1, 1, 2, 0.5, 1.5, 10]]}}}
        self.assertEqual(sg.candles("CURVE", fetch=f, minutes=1, mint="MINTpump"), [(1.0, 1.0, 2.0, 0.5, 1.5, 10.0)])
        self.assertIn("/pools/GPOOL/ohlcv", urls[-1])

    def test_coin_one_hour_old_is_allowed(self):
        self.assertEqual(sg.popular_reason(pair(pairCreatedAt=(NOW - 3700) * 1000), NOW), "")
        self.assertIn("nova demais", sg.popular_reason(pair(pairCreatedAt=(NOW - 1800) * 1000), NOW))


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
        d["open"]["s1"] = {"mint": MINT, "symbol": "SIG", "entry": 1.0, "sl": 0.85, "tp1": 1.2, "tp2": 1.45,
                           "tp3": 3.0, "potential": 2, "at": NOW}
        sg._save(d)

    def test_quick_wick_under_the_stop_does_not_close(self):
        self.assertEqual(sg.follow_up({MINT: 0.84}, now=NOW + 60), [])           # dipped under the stop...
        self.assertEqual(sg.follow_up({MINT: 0.95}, now=NOW + 120), [])          # ...and came back: still open
        self.assertIn("s1", sg._load()["open"])

    def test_stop_counts_when_the_price_stays_under(self):
        sg.follow_up({MINT: 0.84}, now=NOW + 60)
        msgs = sg.follow_up({MINT: 0.83}, now=NOW + 60 + sg.SL_CONFIRM_S)
        self.assertIn("STOP LOSS", msgs[0])
        self.assertAlmostEqual(sg.results()["avg"], -15.0)

    def test_crash_through_the_stop_closes_at_once(self):
        self.assertIn("STOP LOSS", sg.follow_up({MINT: 0.70}, now=NOW + 60)[0])

    def test_tp1_tp2_then_the_potential_tp3(self):
        self.assertIn("TP1 ATINGIDO", sg.follow_up({MINT: 1.21}, now=NOW + 60)[0])
        self.assertIn("TP2 ATINGIDO", sg.follow_up({MINT: 1.46}, now=NOW + 120)[0])
        self.assertIn("TP3 ATINGIDO", sg.follow_up({MINT: 3.05}, now=NOW + 180)[0])
        self.assertAlmostEqual(sg.results()["avg"], (0.2 * 0.5 + 0.45 * 0.25 + 2.0 * 0.25) * 100)
        self.assertIn("1 bateram o potencial", sg.status_text())

    def test_after_tp1_the_stop_is_the_entry(self):
        sg.follow_up({MINT: 1.21}, now=NOW + 60)
        sg.follow_up({MINT: 0.99}, now=NOW + 120)
        msgs = sg.follow_up({MINT: 0.98}, now=NOW + 120 + sg.SL_CONFIRM_S)
        self.assertIn("VOLTOU À ENTRADA", msgs[0])
        self.assertAlmostEqual(sg.results()["avg"], 10.0)


class PotentialTests(unittest.TestCase):
    def test_smaller_market_cap_and_more_activity_mean_more_room(self):
        self.assertEqual(sg.potential(pair(marketCap=200_000, volume={"h24": 50_000}), 75), 3)
        self.assertEqual(sg.potential(pair(marketCap=600_000, volume={"h24": 100_000}), 75), 2)
        self.assertEqual(sg.potential(pair(marketCap=3_000_000, volume={"h24": 100_000}), 75), 1)
        self.assertEqual(sg.potential(pair(marketCap=3_000_000, volume={"h24": 4_000_000}), 75), 2)   # volume > mcap
        self.assertEqual(sg.potential(pair(marketCap=600_000, volume={"h24": 100_000},
                                           priceChange={"h6": 50, "h24": 400}), 75), 1)             # already pumped

    def test_fair_stop_has_room_for_the_coins_swings(self):
        cs, _ = dip_candles()
        setup, _ = sg.entry_setup(cs)
        bottom = min(c[3] for c in cs[-6:])
        self.assertLessEqual(setup["sl"], bottom * 0.97)                         # below the bottom, with room
        self.assertGreaterEqual(setup["risk"], 0.10)


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


class StrengthTests(unittest.TestCase):
    def test_levels_70_80_90(self):
        self.assertEqual(sg.level(93), 90)
        self.assertEqual(sg.level(85), 80)
        self.assertEqual(sg.level(72), 70)
        self.assertIsNone(sg.level(65))

    def test_stronger_setup_and_twitter_buzz_raise_the_score(self):
        cs, _ = dip_candles()
        setup, _ = sg.entry_setup(cs)
        weak = pair(txns={"h1": {"buys": 90, "sells": 70}}, priceChange={"h6": 2, "h24": 5})
        strong = pair(txns={"h1": {"buys": 700, "sells": 100}}, priceChange={"h6": 40, "h24": 150})
        s_weak, _ = sg.strength(weak, setup)
        s_strong, _ = sg.strength(strong, setup)
        s_buzz, parts = sg.strength(strong, setup, {"h1": 80, "h24": 900, "growth": 3.0})
        self.assertLess(s_weak, s_strong)
        self.assertLess(s_strong, s_buzz)
        self.assertIn("Twitter (menções)", parts)
        self.assertLessEqual(s_buzz, 97)

    def test_signal_message_and_results_by_level(self):
        d = tempfile.mkdtemp()
        p = patch.dict(os.environ, {"BOT_WALLET_FILE": os.path.join(d, "w.json")})
        p.start()
        self.addCleanup(p.stop)
        sg.set_enabled(True)
        cs, _ = dip_candles()

        def f(url):
            if "ohlcv" in url:
                return {"data": {"attributes": {"ohlcv_list": [list(c) for c in reversed(cs)]}}}
            if "/tokens/v1/solana/" in url:
                return [pair(txns={"h1": {"buys": 700, "sells": 100}})]
            if "geckoterminal" in url:
                return {"data": []}
            return [{"chainId": "solana", "tokenAddress": MINT}]
        sent = []
        with patch.object(sg, "popular_reason", return_value=""):
            sg.cycle(lambda c, t: sent.append(t), 1, fetch=f, now=NOW, safety=lambda m, curve=False: "",
                     buzz=lambda sym, mint: {"h1": 60, "h24": 700, "growth": 2.5})
        msg = [t for t in sent if "SINAL DE ENTRADA" in t][0]
        self.assertIn("Força do sinal:", msg)
        self.assertIn("🐦 Twitter: 60 menções na última hora", msg)
        lv = list(sg._load()["open"].values())[0]["level"]
        self.assertIn(lv, (70, 80, 90))
        sig = list(sg._load()["open"].values())[0]
        sg.follow_up({MINT: sig["tp3"] * 1.01}, now=NOW + 60)
        self.assertIn("nível " + str(lv) + "%: 1 sinais", sg.status_text())


class TwitterTests(unittest.TestCase):
    def test_mentions_and_growth_from_x_counts(self):
        import twitter_buzz as tb
        tb._cache.clear()
        rows = [{"start": "2026-11-12T%02d:00:00Z" % h, "tweet_count": (2 if h < 21 else 20)} for h in range(24)]
        out = tb.buzz("SIG", MINT, get=lambda q: {"data": rows})
        self.assertEqual(out["h1"], 20)
        self.assertEqual(out["h24"], 21 * 2 + 3 * 20)
        self.assertAlmostEqual(out["growth"], 10.0)

    def test_without_token_twitter_is_off(self):
        import twitter_buzz as tb
        with patch.dict(os.environ, {"X_BEARER_TOKEN": ""}):
            self.assertIsNone(tb.buzz("SIG", MINT))

    def test_query_uses_mint_and_ticker(self):
        import twitter_buzz as tb
        q = tb.query_for("SIG", MINT)
        self.assertIn(MINT, q)
        self.assertIn('"$SIG"', q)


class RemovedRobotsTests(unittest.TestCase):
    def test_open_positions_of_the_removed_robots_still_have_exits(self):
        from live_exit_monitor import position_signal
        for mode in ("sniper", "consensus"):
            pos = {"mode": mode, "opened_at": time.time()}
            self.assertEqual(position_signal(pos, 100, 59)["reason"], "stop_loss")


if __name__ == "__main__":
    unittest.main()
