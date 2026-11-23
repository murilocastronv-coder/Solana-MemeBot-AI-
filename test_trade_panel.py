import unittest
from unittest import mock

import trade_panel as tp

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
PAIR = "Bzc9NZfMqkXR6fz1DBph7BDf9BroyEf6pnzESP7v5iiw"


def pool(mint=MINT, liq=50_000, sym="POP"):
    return {"chainId": "solana", "dexId": "raydium", "pairAddress": PAIR, "priceUsd": "0.00001234",
            "baseToken": {"address": mint, "symbol": sym, "name": "Popcat"}, "liquidity": {"usd": liq},
            "marketCap": 1_200_000, "volume": {"h24": 300_000}, "priceChange": {"m5": 1, "h1": -2.5, "h6": 4, "h24": 10},
            "pairCreatedAt": 1_700_000_000_000}


class FindCoinTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        tp._MEM.update({"tp": 0.0, "sl": 0.0, "trail": 0.0})

    def test_contract_and_links(self):
        self.assertEqual(tp.candidates("olha " + MINT), [MINT])
        self.assertEqual(tp.candidates("https://pump.fun/coin/" + MINT), [MINT])
        self.assertEqual(tp.candidates("So11111111111111111111111111111111111111112"), [])
        self.assertEqual(tp.candidates("oi tudo bem"), [])

    def test_mint_lookup_picks_most_liquid_pool(self):
        get = lambda url: [pool(liq=10), pool(liq=90_000)] if "/tokens/v1/" in url else {}
        c = tp.info(MINT, get=get)
        self.assertEqual((c["mint"], c["liq"], c["symbol"]), (MINT, 90_000, "POP"))

    def test_dexscreener_pair_link_resolves_to_the_coin(self):
        get = lambda url: [] if "/tokens/v1/" in url else {"pairs": [pool()]}
        c = tp.info("https://dexscreener.com/solana/" + PAIR, get=get)
        self.assertEqual(c["mint"], MINT)

    def test_card_has_buy_sell_and_settings_buttons(self):
        c = tp._row(pool())
        text = tp.card_text(c, None, None, "", ("W", 0.5, 80.0), now=1_700_000_000 + 3 * 86400)
        self.assertIn("Popcat (POP)", text)
        self.assertIn("✅ OK", text)
        self.assertIn("criada há 3 dias", text)
        s = dict(tp.ut.default())
        kb = tp.card_keyboard(MINT, {"mode": "trade", "cost_usd": 10, "tp": 0.5}, s)
        data = [b.get("callback_data") for row in kb for b in row]
        self.assertIn("tb:0.1:" + MINT, data)
        self.assertIn("ts:50:" + MINT, data)
        self.assertIn("tdo:" + MINT, data)
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))
        self.assertIn("🎯 Alvo +50%", [b["text"] for row in kb for b in row])


class ExitTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        tp._MEM.update({"tp": 0.0, "sl": 0.0, "trail": 0.0})

    def test_rules(self):
        p = {"trade_tp": 0.5, "trade_sl": 0.3, "trade_trail": 0.2}
        self.assertEqual(tp.exit_signal(p, 1.6, 1.6), ("sell", "trade_tp"))
        self.assertEqual(tp.exit_signal(p, 0.69, 1.0), ("sell", "trade_sl"))
        self.assertEqual(tp.exit_signal(p, 1.2, 1.3), ("hold", "trailing"))
        self.assertEqual(tp.exit_signal(p, 1.03, 1.3), ("sell", "trade_trail"))
        self.assertEqual(tp.exit_signal({}, 0.1, 5.0), ("hold", "trade_hold"))   # nothing set: never sells by itself

    def test_monitor_uses_panel_rules(self):
        import live_exit_monitor as mon
        sig = mon.position_signal({"mode": "trade", "trade_sl": 0.3}, 10.0, 6.0)
        self.assertEqual((sig["action"], sig["reason"]), ("sell", "trade_sl"))

    def test_cycle_settings(self):
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "position", return_value=None), \
                mock.patch.object(tp, "_save"):
            self.assertEqual(tp.set_option(1, MINT, "tp"), (0.25, False))
            self.assertEqual(tp.set_option(1, MINT, "tp"), (0.5, False))
            for _ in range(3):
                tp.set_option(1, MINT, "tp")
            self.assertEqual(tp._MEM["tp"], 0.0)                 # back to off after +200%


class HandleTest(unittest.TestCase):
    def setUp(self):
        tp._MEM.clear()
        tp.INPUT.clear()
        self.ao = mock.patch.object(tp, "AIRDROP_ONLY", False)
        self.ao.start()

    def tearDown(self):
        self.ao.stop()

    def test_typed_amount_buys(self):
        sent, bought = [], []
        run = lambda fn: fn()
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save"), \
                mock.patch.object(tp, "buy", side_effect=lambda c, m, sol, s, *a: bought.append((m, sol))):
            self.assertTrue(tp.handle("tbx:" + MINT, None, lambda c, t, k=None: sent.append(t), 1, run_async=run))
            self.assertTrue(tp.handle("0.3", "0.3", lambda c, t, k=None: sent.append(t), 1, run_async=run))
            self.assertTrue(tp.handle("tb:0.25:" + MINT, None, lambda c, t, k=None: sent.append(t), 1, run_async=run))
            self.assertFalse(tp.handle("oi", "oi", lambda *a, **k: None, 1, run_async=run))
        self.assertEqual(bought, [(MINT, 0.3), (MINT, 0.25)])

    def test_confirm_trades_asks_first(self):
        sent, bought = [], []
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save"), \
                mock.patch.object(tp, "buy", side_effect=lambda c, m, sol, s, *a: bought.append(sol)):
            tp._load()["confirm"] = True
            tp.handle("tb:0.5:" + MINT, None, lambda c, t, k=None: sent.append((t, k)), 1, run_async=lambda fn: fn())
            self.assertEqual(bought, [])
            self.assertIn("Confirma", sent[-1][0])
            tp.handle(sent[-1][1][0][0]["callback_data"], None, lambda c, t, k=None: None, 1, run_async=lambda fn: fn())
        self.assertEqual(bought, [0.5])

    def test_home_has_all_menu_buttons(self):
        with mock.patch.object(tp, "is_owner", return_value=False), mock.patch.object(tp, "USER_TRADING", False):
            data = {b.get("callback_data") for row in tp.home_keyboard(5) for b in row}
        self.assertTrue({"tbuy", "tsellm", "tpl", "tlim", "tdl", "tcopy", "tscan", "twl", "wallet_withdraw", "tset", "thelp", "thome"} <= data)

    def test_sol_robot_bot_has_the_airdrop_hunter_for_the_owner(self):
        """owner, Nov 23: airdrops back next to the SOL robot; replies prepared, the owner posts them on X."""
        import btc_fullmargin
        import airdrop_bot
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "AIRDROP_ONLY", True), \
                mock.patch.object(tp, "HOME", "btc"), mock.patch.object(btc_fullmargin, "keyboard", return_value=[]), \
                mock.patch.object(tp, "settings", return_value={}), \
                mock.patch.object(airdrop_bot, "screen", return_value=("🪂 AIRDROPS DA SOLANA", [])):
            data = {b.get("callback_data") for row in tp.home_keyboard(1) for b in row}
            self.assertIn("tad", data)
            sent = []
            tp.handle("tad", None, lambda c, t, k=None: sent.append(t), 1, run_async=lambda fn: fn())
            self.assertIn("AIRDROPS", sent[-1])
            sent.clear()
            tp.handle("tcopy", None, lambda c, t, k=None: sent.append(t), 1, run_async=lambda fn: fn())
            self.assertIn("Full Margin SOL", sent[-1])                    # the rest stays off for the owner

    def test_sol_robot_bot_lets_other_accounts_buy_and_sell_with_the_fee(self):
        """owner, Nov 23: in the SOL robot bot the other accounts buy / sell coins and pay 2% per buy and per sell."""
        with mock.patch.object(tp, "is_owner", return_value=False), mock.patch.object(tp, "USER_TRADING", True), \
                mock.patch.object(tp, "AIRDROP_ONLY", True), mock.patch.object(tp, "HOME", "btc"):
            data = {b.get("callback_data") for row in tp.home_keyboard(5) for b in row}
            self.assertTrue({"tbuy", "tsellm", "tpl", "wallet_deposit", "wallet_withdraw"} <= data)
            self.assertFalse({"tcopy", "tscan", "trob", "tlim"} & data)
            self.assertFalse(tp.airdrop_only(5))
            self.assertTrue(tp.user_blocked(5, "tcopy"))
            self.assertTrue(tp.user_blocked(5, "tlo:" + MINT))
            self.assertFalse(tp.user_blocked(5, "tb:0.5:" + MINT))
            sent = []
            with mock.patch.object(tp, "settings", return_value={}):
                tp.handle("tcopy", None, lambda c, t, k=None: sent.append(t), 5, run_async=lambda fn: fn())
            self.assertIn("2% por compra e por venda", sent[-1])
        self.assertEqual(tp.ut.FEE_RATE, 0.02)
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "AIRDROP_ONLY", True), \
                mock.patch.object(tp, "HOME", "btc"):
            self.assertTrue(tp.airdrop_only(1))                          # the owner keeps only the SOL robot

    def test_dca_runs_and_counts_down(self):
        bought = []
        with mock.patch.object(tp, "is_owner", return_value=True), mock.patch.object(tp, "_save"), \
                mock.patch.object(tp, "buy", side_effect=lambda c, m, sol, s, *a: bought.append(sol)):
            tp._load()
            oid = tp.add_dca(1, MINT, "POP", 0.1, 2, 1, now=1000)
            tp.run_dca(1, lambda *a, **k: None, now=1000)
            tp.run_dca(1, lambda *a, **k: None, now=1500)          # not yet (every 1 h)
            self.assertEqual(tp.dcas(1)[oid]["left"], 1)
            tp.run_dca(1, lambda *a, **k: None, now=1000 + 3600)
            self.assertEqual(tp.dcas(1), {})
        self.assertEqual(bought, [0.1, 0.1])


if __name__ == "__main__":
    unittest.main()
