"""Risk-gated Solana token scanner. Read-only by design."""
import logging
import os
import time
from wallet_monitor import wallet_public_address, rpc_balance_sol
import requests

LOG = logging.getLogger("memebot")
API = "https://api.dexscreener.com"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "SolanaMemeBot/0.2 (research-only)"
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY_USD", "50000"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MAX_FDV_LIQ = float(os.getenv("MAX_FDV_TO_LIQUIDITY", "100"))
INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "180")))
TRADING_ENABLED = False  # No signer, no swap client, no private keys.

def json_get(path):
    response = SESSION.get(API + path, timeout=15)
    response.raise_for_status()
    return response.json()

def age_hours(pair, now_ms=None):
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    created = pair.get("pairCreatedAt")
    return max(0, (now_ms - created) / 3600000) if isinstance(created, (int, float)) and created <= now_ms else None

def assess_pair(pair, now_ms=None):
    """Market-quality screen, NOT a scam certification."""
    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    volume = float((pair.get("volume") or {}).get("h24") or 0)
    fdv = float(pair.get("fdv") or 0)
    age = age_hours(pair, now_ms)
    category = "new" if age is not None and age < 168 else "established"
    reasons = []
    if pair.get("chainId") != "solana":
        reasons.append("not_solana")
    if age is None:
        reasons.append("unknown_pair_age")
    elif age < 24:
        reasons.append("pair_younger_than_24h")
    if liquidity < MIN_LIQUIDITY:
        reasons.append("insufficient_liquidity")
    if volume < MIN_VOLUME:
        reasons.append("insufficient_volume")
    if fdv <= 0 or liquidity <= 0 or fdv / liquidity > MAX_FDV_LIQ:
        reasons.append("unfavorable_or_unknown_valuation_liquidity_ratio")
    if not pair.get("baseToken", {}).get("address"):
        reasons.append("missing_token_address")
    # Fail closed: market metrics cannot prove that selling is possible or that
    # mint/freeze authority, top holders and LP custody are safe.
    reasons.append("onchain_security_not_verified")
    return {
        "token": (pair.get("baseToken") or {}).get("address"),
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "category": category,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "age_hours": round(age, 1) if age is not None else None,
        "status": "WATCH_ONLY" if len(reasons) == 1 else "REJECT",
        "reasons": reasons,
    }

def scan():
    profiles = json_get("/token-profiles/latest/v1")
    addresses = list(dict.fromkeys(
        p["tokenAddress"] for p in profiles
        if p.get("chainId") == "solana" and p.get("tokenAddress")
    ))[:12]
    results = []
    for address in addresses:
        try:
            data = json_get("/latest/dex/tokens/" + address)
            pairs = [p for p in data.get("pairs") or [] if p.get("chainId") == "solana"]
            if not pairs:
                LOG.info("No Solana pair for %s", address)
                continue
            pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            results.append(assess_pair(pair))
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            LOG.warning("Token scan failed %s: %s", address, exc)
        time.sleep(1)
    return results

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.warning("READ ONLY. No wallet connection or live trading. Trading enabled: %s", TRADING_ENABLED)
    while True:
        try:
            address = wallet_public_address()
            if address:
                try:
                    balance = rpc_balance_sol(address)
                    LOG.info("wallet_read_only_balance_sol=%.9f", balance)
                except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    LOG.warning("wallet_balance_unavailable=%s", exc)
            else:
                LOG.info("wallet_public_address_not_configured")
            assessments = scan()
            for result in assessments:
                LOG.info("token_assessment=%s", result)
            if os.getenv("MEV_AUTOTRADE_ENABLED") == "true":
                # Dry-run by default; live swaps additionally require the two
                # independent MEV/LIVE switches and existing live preflight.
                try:
                    from jito_arbitrage import scan_cycle
                    scan_cycle(assessments)
                except Exception as exc:
                    LOG.error("MEV cycle stopped: %s", type(exc).__name__)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            LOG.error("Scanner error: %s", exc)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
