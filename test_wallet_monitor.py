import unittest
from unittest.mock import patch, Mock
import wallet_monitor

ADDRESS = "11111111111111111111111111111111"
class WalletMonitorTests(unittest.TestCase):
    def test_valid_public_address(self):
        self.assertEqual(wallet_monitor.validate_public_address(ADDRESS), ADDRESS)
    def test_reject_secret_phrase(self):
        with self.assertRaises(ValueError):
            wallet_monitor.validate_public_address("one two three four five six seven eight nine ten eleven twelve")
    @patch("wallet_monitor.requests.post")
    def test_rpc_balance(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"jsonrpc":"2.0","id":1,"result":{"value":1230000000}}
        self.assertEqual(wallet_monitor.rpc_balance_sol(ADDRESS), 1.23)
    def test_no_address(self):
        with patch.dict("os.environ", {"SOLANA_BOT_PUBLIC_ADDRESS":""}):
            self.assertIn("Configure", wallet_monitor.deposit_instructions())
if __name__ == "__main__":
    unittest.main()
