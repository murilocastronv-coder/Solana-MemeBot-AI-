import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import btc_fullmargin as bfm

T0 = 1_700_000_000 // 300 * 300
NOW = T0 + 900 * 300


def candles(n=800, trend=-1):
    """Steady trend (down by default), 5-minute candles, oldest first; the last one is still forming."""
    out, p = [], 60000.0
    for i in range(n):
        o = p
        p *= (1 + 0.0008 * trend) if i % 4 else (1 - 0.0004 * trend)
        out.append({"t": T0 + i * 300, "o": o, "h": max(o, p) * 1.0003, "l": min(o, p) * 0.9997, "c": p})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = patch.object(bfm, "_path", side_effect=lambda mode="demo": Path(self.dir) / (mode + ".json"))
        self.p.start()
        self.addCleanup(self.p.stop)
        self.msgs = []

    def send(self, chat, text, kb=None):
        self.msgs.append(text)

    def turn_on(self, mode="demo", **kw):
        d = bfm._load(mode)
        d.update(dict(dict(on=True, day=bfm.today(NOW), capital=100.0), **kw))
        bfm._save(d)

    def cyc(self, cs, mode="demo", now=NOW):
        return bfm.cycle(mode, self.send, "1", now=now, klines=lambda: cs)


class ReadChart(Base):
    def test_downtrend_scores_negative(self):
        sc, a, text = bfm.read_chart(candles()[:-1])
        self.assertLessEqual(sc, -4)
        self.assertGreater(a, 0)
        self.assertIn("leitura", text)

    def test_uptrend_scores_positive(self):
        self.assertGreaterEqual(bfm.read_chart(candles(trend=1)[:-1])[0], 4)

    def test_strong_fall_and_rise(self):
        down, up = candles()[:-1], candles(trend=1)[:-1]
        self.assertEqual(bfm.strong_move(down)[0], -1)
        self.assertEqual(bfm.strong_move(up)[0], 1)


class Basket(unittest.TestCase):
    def short(self, **kw):
        return dict({"side": "short", "entry": 100.0, "size_usd": 500.0, "collateral_usd": 100.0, "n": 1, "last": 100.0}, **kw)

    def test_first_order_30pct_and_risk_cut_from_500(self):
        self.assertEqual(bfm.first_collateral(100.0), 30.0)
        self.assertEqual(bfm.first_collateral(600.0), 90.0)                 # 30% x 0.5
        self.assertEqual(bfm.risk_cut(499.0), 1.0)
        self.assertEqual(bfm.risk_cut(500.0), 0.5)

    def test_close_leaves_profit_after_fees(self):
        pos = self.short()
        win = bfm.pnl_usd(pos, bfm.tp_price(pos))
        self.assertGreater(win, 0)                                         # +0.25% beats the ~0.12% fees
        self.assertAlmostEqual(bfm.tp_price(pos), 99.75)
        self.assertAlmostEqual(bfm.tp_price(self.short(side="long")), 100.25)

    def test_recovery_orders_get_further_apart(self):
        self.assertAlmostEqual(bfm.next_grid_price(self.short()), 100.5)
        self.assertAlmostEqual(bfm.next_grid_price(self.short(loss_adds=1, last=100.5)), 100.5 * 1.0075)
        self.assertAlmostEqual(bfm.next_grid_price(self.short(side="long", loss_adds=2)), 100.0 * (1 - 0.01))

    def test_pnl_long_and_short(self):
        self.assertAlmostEqual(bfm.pnl_usd(self.short(), 99.0), 5.0 - 0.6)
        self.assertAlmostEqual(bfm.pnl_usd(self.short(side="long"), 101.0), 5.0 - 0.6)

    def test_adding_moves_the_average_price(self):
        d = {"mode": "demo", "leverage": 5, "capital": 100.0, "trades": 0, "pos": None}
        bfm._open(d, 100.0, "short", "x")
        self.assertEqual(d["pos"]["collateral_usd"], 30.0)
        self.assertAlmostEqual(bfm.free_capital(d), 70.0)
        bfm._open(d, 100.5, "short", "vai voltar", 17.5)
        pos = d["pos"]
        self.assertEqual(pos["n"], 2)
        self.assertEqual(pos["collateral_usd"], 47.5)
        self.assertTrue(100.0 < pos["entry"] < 100.5)


class Cycle(Base):
    def test_off_does_nothing(self):
        self.assertEqual(self.cyc(candles()), "desligado")

    def test_first_order_of_the_day_right_away_to_the_side_of_the_reading(self):
        self.turn_on()
        self.assertEqual(self.cyc(candles(trend=1)), "abriu")
        d = bfm._load("demo")
        self.assertEqual(d["pos"]["side"], "long")
        self.assertEqual(d["pos"]["collateral_usd"], 30.0)               # 1st order: 30% of the account
        self.assertIn("COMPRA (long) aberta", self.msgs[-1])
        bfm._save(dict(d, pos=None))
        self.turn_on(trades=0, last_bar=0)
        self.assertEqual(self.cyc(candles()), "abriu")
        self.assertEqual(bfm._load("demo")["pos"]["side"], "short")

    def test_strong_move_trades_more_with_full_lot(self):
        self.turn_on(trades=1)
        self.assertEqual(self.cyc(candles()), "abriu")                   # strong fall: sells with score >= 3
        d = bfm._load("demo")
        self.assertEqual(d["pos"]["side"], "short")
        self.assertEqual(d["pos"]["collateral_usd"], 30.0)
        self.assertIn("QUEDA FORTE", self.msgs[-1])

    def test_always_in_opens_the_next_right_after_closing(self):
        self.turn_on(trades=3, last_entry=NOW - 60, last_bar=candles()[-2]["t"])     # same candle, entered 1 min ago
        with patch.object(bfm, "strong_move", return_value=(0, 0.2)), \
                patch.object(bfm, "read_chart", return_value=(-1, 10.0, "leitura -1")):
            self.assertEqual(self.cyc(candles()), "abriu")
        pos = bfm._load("demo")["pos"]
        self.assertEqual(pos["side"], "short")
        self.assertEqual(pos["collateral_usd"], 30.0)                    # 30% of the account

    def test_no_strong_move_trades_much_less_when_always_in_is_off(self):
        self.turn_on(trades=1, last_entry=NOW - 600)
        with patch.object(bfm, "ALWAYS_IN", False), patch.object(bfm, "strong_move", return_value=(0, 0.2)):
            self.assertEqual(self.cyc(candles()), "sem movimento forte: 1 entrada por hora")
            d = bfm._load("demo")
            d.update(last_entry=0, last_bar=0)
            bfm._save(d)
            with patch.object(bfm, "read_chart", return_value=(-8, 10.0, "leitura -8")):
                self.assertEqual(self.cyc(candles()), "abriu")
        self.assertEqual(bfm._load("demo")["pos"]["collateral_usd"], 15.0)   # half of the 30%

    def _open_short(self, cs):
        self.turn_on(trades=1)
        bfm.cycle("demo", self.send, "1", now=NOW, klines=lambda: cs)
        return bfm._load("demo")["pos"]

    def _next_candle(self, cs, close, open_=None):
        """A new closed 5-minute candle (open_ -> close) and the current price = close."""
        o = open_ if open_ is not None else close
        last = dict(cs[-2], t=cs[-1]["t"], o=o, c=close, h=max(close, o), l=min(close, o))
        return cs[:-1] + [last, dict(last, t=last["t"] + 300)]

    def test_it_will_come_back_adds_and_closes_all_in_profit(self):
        cs = candles()
        pos = self._open_short(cs)
        up = pos["entry"] * 1.006                                         # 0.6% against the short
        later = self._next_candle(cs, up, open_=up * 1.001)                # the last candle already turned down
        with patch.object(bfm, "read_chart", return_value=(-5, 10.0, "leitura -5")):
            self.assertEqual(self.cyc(later), "reforço")
        pos = bfm._load("demo")["pos"]
        self.assertEqual((pos["n"], pos["loss_adds"]), (2, 1))
        self.assertAlmostEqual(pos["collateral_usd"], 30.0 + 70.0 / 4)     # the rest of the capital split in 4
        self.assertIn("VAI VOLTAR 1/4", self.msgs[-1])
        back = cs[:-1] + [dict(cs[-1], c=bfm.tp_price(pos) * 0.999)]
        self.assertEqual(self.cyc(back), "fechou")
        d = bfm._load("demo")
        self.assertIsNone(d["pos"])
        self.assertGreater(d["capital"], 100.0)                           # profit after the fees
        self.assertIn("2 ordem(ns)", self.msgs[-1])

    def test_no_recovery_while_the_market_keeps_going_against(self):
        cs = candles()
        pos = self._open_short(cs)
        up = pos["entry"] * 1.006
        later = self._next_candle(cs, up, open_=up * 0.999)                # last candle still going up (against)
        with patch.object(bfm, "read_chart", return_value=(-5, 10.0, "leitura -5")):
            self.assertEqual(self.cyc(later), "posição aberta")
        self.assertEqual(bfm._load("demo")["pos"]["n"], 1)

    def test_it_will_win_adds_50pct_once(self):
        cs = candles()
        pos = self._open_short(cs)
        down = pos["entry"] * 0.9985                                      # +0.15% for the short
        later = self._next_candle(cs, down)
        with patch.object(bfm, "read_chart", return_value=(-5, 10.0, "leitura -5")):
            self.assertEqual(self.cyc(later), "reforço")
            pos = bfm._load("demo")["pos"]
            self.assertAlmostEqual(pos["collateral_usd"], 30.0 + 70.0 * 0.5)
            self.assertIn("VAI GANHAR", self.msgs[-1])
            again = self._next_candle(later, down)
            self.assertEqual(self.cyc(again), "posição aberta")          # only once
        self.assertEqual(bfm._load("demo")["pos"]["n"], 2)

    def test_liquidation_guard(self):
        self.turn_on(trades=5)
        cs = candles()
        price = cs[-1]["c"]
        d = bfm._load("demo")
        d["pos"] = {"side": "short", "entry": price / 1.2, "size_usd": 100.0, "collateral_usd": 20.0, "n": 5, "last": price}
        d["capital"] = 200.0                                              # so the daily limit does not fire first
        bfm._save(d)
        self.assertEqual(self.cyc(cs), "fechou")
        self.assertIn("PROTEÇÃO", self.msgs[-1])

    def test_daily_target_stops_until_tomorrow(self):
        self.turn_on(capital=bfm.TARGET_USD + 2, trades=3)
        self.assertEqual(self.cyc(candles()), "META DO DIA BATIDA")
        self.assertEqual(self.cyc(candles()), "parado até amanhã")
        self.assertEqual(self.cyc(candles(), now=NOW + 86400), "abriu")
        d = bfm._load("demo")
        self.assertEqual(d["capital"], 100.0)
        self.assertEqual(d["days"][-1]["result"], bfm.TARGET_USD + 2 - 100)

    def test_daily_loss_limit(self):
        self.turn_on(capital=49.0, trades=2)
        self.assertEqual(self.cyc(candles()), "LIMITE DE PERDA DO DIA")

    def test_no_more_stop_after_2_losses(self):
        self.turn_on(trades=2, losses=2)
        self.assertEqual(self.cyc(candles()), "abriu")

    def test_accounts_are_separate(self):
        self.turn_on("demo")
        self.cyc(candles())
        self.assertIsNotNone(bfm._load("demo")["pos"])
        self.assertIsNone(bfm._load("real")["pos"])

    def test_real_opens_and_adds_on_jupiter(self):
        self.turn_on("real")
        calls = []

        class JP:
            @staticmethod
            def ensure_usdc(usd, sol):
                calls.append(("usdc", usd))

            @staticmethod
            def open_position(side, usd, lev, price, min_size=0.0):
                calls.append((side, usd, lev, min_size))
                return {"price": price, "signature": "SIG"}

        with patch.dict("sys.modules", {"jupiter_perps": JP}), patch.object(bfm, "sol_usd", return_value=150.0):
            self.assertEqual(self.cyc(candles(), mode="real"), "abriu")
            d = bfm._load("real")
            bfm._open(d, d["pos"]["entry"] * 1.006, "short", "vai voltar", 17.5)
        self.assertEqual(calls[:2], [("usdc", 30.0), ("short", 30.0, bfm.DEFAULT_LEVERAGE, 0.0)])
        self.assertEqual(calls[3][3], 30.0 * bfm.DEFAULT_LEVERAGE)       # the add waits for the bigger position
        self.assertIn("solscan.io/tx/SIG", self.msgs[-1])


class Results(Base):
    def close_one(self, exit_price, mode="demo"):
        d = bfm._load(mode)
        d["pos"] = {"entry": 100.0, "size_usd": 500.0, "collateral_usd": 100.0, "risk": 1.0, "stop": 101.0}
        bfm._close(d, exit_price, "🎯 ALVO")
        bfm._save(d)
        return d

    def test_fees_are_taken_out(self):
        gross, fees, net = bfm.breakdown({"entry": 100.0, "size_usd": 500.0, "opened": NOW - 3600}, 99.0, now=NOW)
        self.assertAlmostEqual(gross, 5.0)
        self.assertAlmostEqual(fees, 500 * (0.0012 + 0.0001))
        self.assertAlmostEqual(net, gross - fees)

    def test_close_notification_shows_gross_fees_net_and_balance(self):
        self.turn_on()
        d = self.close_one(99.0)
        t = bfm.close_text(d, "🎯 ALVO", 99.0, 100.0)
        self.assertIn("✅ LUCRO +4.40 USD (depois das taxas)", t)
        self.assertIn("Bruto +5.00 · taxas −0.60 · líquido +4.40", t)
        self.assertIn("Saldo da conta: US$ 104.40", t)
        self.assertIn("Total desde o início: +4.40", t)

    def test_loss_notification(self):
        self.turn_on()
        d = self.close_one(101.0)
        self.assertIn("❌ PREJUÍZO -5.60", bfm.close_text(d, "🛑 STOP", 101.0, 100.0))

    def test_lifetime_totals_survive_a_new_day(self):
        self.turn_on()
        self.close_one(99.0)
        self.close_one(101.0)
        d = bfm._load("demo")
        bfm.new_day(d, NOW + 86400)
        tt = d["total"]
        self.assertEqual((tt["trades"], tt["wins"], tt["losses"]), (2, 1, 1))
        self.assertAlmostEqual(tt["net"], 4.4 - 5.6)
        self.assertEqual(d["capital"], 100.0)

    def test_results_say_if_it_is_making_money(self):
        self.turn_on()
        self.close_one(99.0)
        t = bfm.results_text()
        self.assertIn("LÍQUIDO +4.40", t)
        self.assertIn("LUCRANDO", t)
        self.assertIn("Ainda sem operações", t)          # the real account
        self.assertIn("Total desde o início", bfm.account_text("demo"))

    def test_results_chart(self):
        self.turn_on()
        self.close_one(99.0)
        png = bfm.results_png()
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_real_result_is_measured_on_chain(self):
        self.turn_on("real")
        bal = [10.0]

        class JP:
            @staticmethod
            def usdc_balance(owner):
                return bal[0]

            @staticmethod
            def close_position(side, price):
                bal[0] += 103.0                         # collateral 100 came back with +3 net
                return {"signature": "S"}

            @staticmethod
            def usdc_to_sol():
                return None

        with patch.dict("sys.modules", {"jupiter_perps": JP}), \
                patch("bot_wallet_service.get_address", return_value="11111111111111111111111111111111"):
            d = self.close_one(99.0, mode="real")
        self.assertAlmostEqual(d["last_close"]["net"], 3.0)
        self.assertAlmostEqual(d["capital"], 103.0)


class Live(Base):
    def open_pos(self, mode="demo"):
        self.turn_on(mode)
        d = bfm._load(mode)
        d["pos"] = {"entry": 100.0, "size_usd": 500.0, "collateral_usd": 100.0, "risk": 1.0, "stop": 101.0, "opened": NOW - 600}
        bfm._save(d)

    def test_floating_positive(self):
        self.open_pos()
        t = bfm.live_text("demo", 99.5, now=NOW, track={})
        self.assertIn("🟢 FLUTUAÇÃO: +1.89 USD", t)
        self.assertIn("já com as taxas", t)
        self.assertIn("●", t)
        self.assertIn("máx +1.89", t)

    def test_floating_negative(self):
        self.open_pos()
        self.assertIn("🔴 FLUTUAÇÃO: -3.11", bfm.live_text("demo", 100.5, now=NOW))

    def test_no_position(self):
        self.turn_on()
        self.assertIn("Nenhuma ordem aberta", bfm.live_text("demo", 90000.0))

    def test_live_edits_the_same_message_and_stops(self):
        self.open_pos()
        edits, prices = [], iter([99.5, 99.0, 98.8, 98.9, 99.1])
        sent = []

        def send(chat, text, kb=None):
            sent.append(text)
            return {"result": {"message_id": 7}}

        with patch.object(bfm, "price_now", lambda: next(prices)), patch.object(bfm, "LIVE_EVERY_S", 0), \
                patch.object(bfm, "LIVE_FOR_S", 0.05), patch.object(bfm, "EDIT", lambda c, m, t, kb=None: edits.append((m, t))):
            real_sleep = __import__("time").sleep
            with patch("time.sleep", lambda s: real_sleep(0.02)):
                bfm.start_live("demo", send, "1")
        self.assertEqual(len(sent), 1)
        self.assertTrue(edits)
        self.assertTrue(all(m == 7 for m, _ in edits))
        self.assertIn("Ao vivo encerrado", edits[-1][1])
        self.assertNotIn("1", bfm.LIVE)

    def test_buttons(self):
        cbs = [b["callback_data"] for row in bfm.keyboard() for b in row]
        self.assertIn("bfm_live_demo", cbs)
        bfm.LIVE["1"] = {"mode": "demo"}
        bfm.handle("bfm_live_stop", self.send, "1", lambda fn: fn())
        self.assertNotIn("1", bfm.LIVE)


class Backtest(Base):
    def test_runs_the_robot_over_history(self):
        import math
        import random
        random.seed(3)
        cs, p = [], 60000.0
        for i in range(800 + 3 * 288):
            o = p
            p *= 1 + random.gauss(0, 0.001) + 0.0004 * math.sin(i / 40)
            cs.append({"t": T0 + i * 300, "o": o, "h": max(o, p) * 1.0004, "l": min(o, p) * 0.9996, "c": p})
        d = bfm.backtest(cs, 5)
        self.assertIsNone(d.get("pos"))
        self.assertGreater(d["total"]["trades"], 0)
        t = bfm.backtest_text(d, 5, 3)
        self.assertIn("BACKTEST 3 DIAS", t)
        self.assertIn("taxas pagas", t)
        self.assertFalse(bfm._load("demo")["on"])                      # the demo account is untouched
        self.assertTrue(bfm.backtest_png(d).startswith(b"\x89PNG"))

    def test_button(self):
        self.assertIn("bfm_bt", [b["callback_data"] for row in bfm.keyboard() for b in row])
        self.assertIn("bfm_bt", bfm.ACTIONS)


class Telegram(Base):
    def test_keyboard_callbacks_are_routed(self):
        cbs = [b["callback_data"] for row in bfm.keyboard() for b in row]
        self.assertIn("bfm_on_demo", cbs)
        self.assertIn("bfm_on_real", cbs)
        self.assertIn("wallet_deposit", cbs)
        self.assertIn("wallet_withdraw", cbs)
        self.assertIn("bfm_res", cbs)
        for cb in cbs:
            self.assertIn(cb, bfm.ACTIONS | {"wallet_deposit", "wallet_withdraw", "wallet_balance"})

    def test_leverage_applies_to_both(self):
        bfm.handle("bfm_lev10", self.send, "1", lambda fn: fn())
        self.assertEqual(bfm._load("demo")["leverage"], 10)
        self.assertEqual(bfm._load("real")["leverage"], 10)

    def test_max_leverage_50(self):
        self.assertEqual(max(bfm.LEVERAGES), 50)
        bfm.handle("bfm_lev50", self.send, "1", lambda fn: fn())
        self.assertEqual(bfm._load("real")["leverage"], 50)
        self.assertAlmostEqual(bfm.LIQ_GUARD / 50, 0.012)              # protection closes at -1.2% at 50x
        import telegram_bot
        self.assertTrue(bfm.ACTIONS <= telegram_bot.TRADE_ACTIONS)

    def test_real_asks_confirmation(self):
        bfm.handle("bfm_on_real", self.send, "1", lambda fn: fn())
        self.assertFalse(bfm._load("real")["on"])
        self.assertIn("CONTA REAL", self.msgs[-1])

    def test_demo_start_opens(self):
        with patch.object(bfm, "klines_5m", candles):
            bfm.handle("bfm_on_demo", self.send, "1", lambda fn: fn())
        d = bfm._load("demo")
        self.assertTrue(d["on"])
        self.assertEqual(d["capital"], 100.0)
        self.assertIsNotNone(d["pos"])
        self.assertIn("CONTA DEMO", bfm.status_text())


if __name__ == "__main__":
    unittest.main()
