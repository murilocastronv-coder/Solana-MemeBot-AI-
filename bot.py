"""Solana token risk monitor. No signing or trading is enabled."""
import os
import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
API = "https://api.dexscreener.com/token-profiles/latest/v1"
PAUSE = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "180")))
MIN_LIQ = float(os.getenv("MIN_LIQUIDITY_USD", "25000"))
MIN_AGE_H = float(os.getenv("MIN_PAIR_AGE_HOURS", "24"))
session = requests.Session()
session.headers.update({"User-Agent": "SolanaMemeBotRiskMonitor/0.1"})

def get_json(url):
    response = session.get(url, timeout=15)
    response.raise_for_status()
    return response.json()

def check_token(address):
    pairs = get_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}").get("pairs") or []
    pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not pairs:
        return {"address": address, "status": "REJECT", "reasons": ["No Solana market"]}
    pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    volume = float((pair.get("volume") or {}).get("h24") or 0)
    created = pair.get("pairCreatedAt")
    age_hours = (time.time() * 1000 - created) / 3600000 if created else 0
    reasons = []
    if liquidity < MIN_LIQ: reasons.append("Low liquidity")
    if age_hours < MIN_AGE_H: reasons.append("Pair too new or age unknown")
    if volume <= 0: reasons.append("No reported 24h volume")
    # Market data alone cannot establish mint/freeze authority, holder distribution,
    # transfer restrictions, or whether the token is a scam.
    reasons.append("On-chain safety checks not implemented: NOT SAFE TO TRADE")
    return {"address": address, "status": "WATCH_ONLY", "liquidity_usd": liquidity,
            "volume_24h_usd": volume, "age_hours": round(age_hours, 1), "reasons": reasons}

def main():
    logging.warning("READ-ONLY MODE: trading and private-key access are disabled.")
    while True:
        try:
            profiles = get_json(API)
            addresses = list(dict.fromkeys(
                p["tokenAddress"] for p in profiles
                if p.get("chainId") == "solana" and p.get("tokenAddress")
            ))[:12]
            for address in addresses:
                try:
                    logging.info("risk_check=%s", check_token(address))
                except (requests.RequestException, ValueError, TypeError) as exc:
                    logging.warning("token_check_failed address=%s error=%s", address, exc)
                time.sleep(1)
        except (requests.RequestException, ValueError, TypeError) as exc:
            logging.error("scan_failed=%s", exc)
        time.sleep(PAUSE)

if __name__ == "__main__":
    main()
