"""Compatibility entry point for the existing Render service (start command: python telegram_bot.py).

The old memecoin/Telegram bot was removed. This file only starts BTC PAPER TRADING
(4 simulated accounts). Real trading does not exist in this repository; the flag is
forced to false here so an old "true" value on the server cannot block or enable anything.
"""
import os
import sys

if os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() != "false":
    print("Aviso: LIVE_TRADING_ENABLED estava '%s'; forçado para false (só paper trading)."
          % os.environ["LIVE_TRADING_ENABLED"], flush=True)
os.environ["LIVE_TRADING_ENABLED"] = "false"
if "BTC_PAPER_DIR" not in os.environ and os.path.isdir("/var/data"):
    os.environ["BTC_PAPER_DIR"] = "/var/data"

from btc_trend_pullback import paper  # noqa: E402  (env must be set first)

if __name__ == "__main__":
    sys.exit(paper.main(["--strategy", "ambas", "--profile", "both"] + sys.argv[1:]))
