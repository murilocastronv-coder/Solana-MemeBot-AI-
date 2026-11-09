import unittest

import jupiter_v2_executor as j


class ExecuteErrorTests(unittest.TestCase):
    def test_reason_is_shown(self):
        text = j._execute_error({"status": "Failed", "code": -2000, "error": "Slippage tolerance exceeded"})
        self.assertIn("slippage", text)
        self.assertIn("-2000", text)

    def test_unknown_failure_keeps_code(self):
        self.assertEqual(j._execute_error({"status": "Failed", "code": -1004}), " — código -1004")
        self.assertEqual(j._execute_error({"status": "Failed"}), "")


if __name__ == "__main__":
    unittest.main()
