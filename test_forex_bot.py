import json
import math
import unittest
from unittest import mock

import forex_bot as fx


def trend_candles(n=260, up=True):
    """A clean trend with a pullback to the EMA 20 at the end (RSI dips then turns)."""
    cs, p = [], 1.1000
    for i in range(n):
        step = 0.0006 if up else -0.0006
        if n - 12 <= i < n - 2:
            step = -0.0012 if up else 0.0012          # pullback
        if i == n - 1:
            step = 0.0008 if up else -0.0008          # turns back
        o = p
        p = p + step
        cs.append({"time": "2026-11-%02dT%02d:00:00Z" % (1 + i // 24, i % 24), "open": o, "high": max(o, p) + 0.0003,
                   "low": min(o, p) - 0.0003, "close": p})
    return cs


class Resp:
    def __init__(self, data, code=200):
        self.status_code, self.text, self._d = code, json.dumps(data), data

    def json(self):
        return self._d


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url, json.loads(data) if data else None))
        assert headers["auth-token"] == "TOK"
        for k, v in self.routes.items():
            if k in url and (not isinstance(v, tuple) or v[0] == method):
                return Resp(v[1] if isinstance(v, tuple) else v)
        return Resp({}, 404)


class ForexTest(unittest.TestCase):
    def setUp(self):
        fx._MEM.clear()
        fx._REGION.clear()
        fx._MEM.update({"on": True, "open": {}, "hist": [], "symbols": {}, "last_bar": {}, "log": []})
        self.p = mock.patch.object(fx, "_save")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_indicators(self):
        self.assertAlmostEqual(fx.ema([1, 1, 1], 3)[-1], 1)
        r = fx.rsi([1 + i * 0.01 for i in range(30)])
        self.assertGreater(r[-1], 90)
        cs = trend_candles()
        self.assertGreater(fx.atr(cs)[-1], 0)

    def test_levels_and_trailing(self):
        self.assertEqual(fx.levels("buy", 1.1, 0.001), (1.1 - 0.0015, 1.1 + 0.003))
        self.assertIsNone(fx.trail_stop("buy", 1.1, 1.1010, 1.0985, 0.001))           # not yet +1.5 ATR
        self.assertAlmostEqual(fx.trail_stop("buy", 1.1, 1.1020, 1.0985, 0.001), 1.1005)
        self.assertAlmostEqual(fx.trail_stop("sell", 1.1, 1.0980, 1.1015, 0.001), 1.0995)

    def test_cycle_opens_with_stop_and_target_and_notifies(self):
        sent = []
        routes = {"agiliumtrade.agiliumtrade.ai/users/current/accounts/ACC": {"region": "london", "state": "DEPLOYED"},
                  "/positions": [], "/candles": trend_candles(), "/symbols/EURUSD/current-price": {"bid": 1.2, "ask": 1.2001},
                  "/symbols": ["EURUSD", "GBPUSD.m"], "/trade": ("POST", {"positionId": "777"})}
        http = FakeHttp(routes)
        api = fx.Api("TOK", "ACC", http)
        with mock.patch.object(fx, "signal", side_effect=lambda cs: ("buy", 0.001)), mock.patch.object(fx, "PAIRS", ("EURUSD",)):
            r = fx.cycle(lambda c, t, *a: sent.append(t), 1, api=api)
        self.assertEqual(r, "abriu EURUSD")
        trade = [c for c in http.calls if c[1].endswith("/trade")][0][2]
        self.assertEqual(trade["actionType"], "ORDER_TYPE_BUY")
        self.assertEqual(trade["volume"], 0.01)
        self.assertAlmostEqual(trade["stopLoss"], 1.2001 - 0.0015, places=6)
        self.assertAlmostEqual(trade["takeProfit"], 1.2001 + 0.003, places=6)
        self.assertIn("london", [c for c in http.calls if c[1].endswith("/trade")][0][1])
        self.assertIn("FOREX ABRIU 🟢 COMPRA EURUSD", sent[0])
        self.assertIn("777", fx._MEM["open"])
        with mock.patch.object(fx, "signal", side_effect=lambda cs: ("buy", 0.001)), mock.patch.object(fx, "PAIRS", ("EURUSD",)):
            routes["/positions"] = [{"id": "777", "symbol": "EURUSD", "type": "POSITION_TYPE_BUY", "comment": fx.COMMENT,
                                     "openPrice": 1.2001, "currentPrice": 1.2001, "stopLoss": 1.1986}]
            self.assertEqual(fx.cycle(None, None, api=api), "sem sinal")                     # already holds EURUSD
            routes["/positions"] = []
            fx.cycle(lambda c, t, *a: sent.append(t), 1, api=api)                            # closed at stop / target
        self.assertIn("posição FECHADA", sent[-1])

    def test_off_and_not_configured(self):
        fx._MEM["on"] = False
        api = fx.Api("TOK", "ACC", FakeHttp({"/positions": [], "agiliumtrade.agiliumtrade.ai/users": {"region": "x"}}))
        self.assertEqual(fx.cycle(api=api), "desligado")
        with mock.patch.dict("os.environ", {"METAAPI_TOKEN": "", "METAAPI_ACCOUNT_ID": ""}):
            self.assertIn("Falta METAAPI_TOKEN", fx.status_text())

    def test_signal_needs_history(self):
        self.assertEqual(fx.signal(trend_candles(100))[0], None)


if __name__ == "__main__":
    unittest.main()
