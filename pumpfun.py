"""Discovery of recent/trending pump.fun tokens (Jupiter Tokens API v2).

Used only to widen the list of tokens the MEV scan looks at. No trading here.
"""
import time
from datetime import datetime

import jupiter_rate_limiter

from jupiter_v2_executor import headers

FEEDS = ("https://api.jup.ag/tokens/v2/recent",
         "https://api.jup.ag/tokens/v2/toptrending/5m?limit=100")
_cache = [0.0, []]
last_scan = {"tokens": 0, "passed": 0, "error": None}


def _num(d, *path, default=0.0):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    try:
        return float(d) if d is not None else default
    except (TypeError, ValueError):
        return default


def _created_ms(t):
    raw = (t.get("firstPool") or {}).get("createdAt") or t.get("createdAt")
    if isinstance(raw, (int, float)):
        return float(raw) * (1000 if raw < 1e12 else 1)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0.0


def is_pumpfun(t):
    mint = str(t.get("id") or t.get("address") or "")
    launchpad = str(t.get("launchpad") or "").lower()
    return "pump" in launchpad or mint.endswith("pump")


def fetch():
    """Recent/trending pump.fun tokens (cached 15 s)."""
    if time.monotonic() - _cache[0] < 15:
        return _cache[1]
    tokens, error = {}, None
    for url in FEEDS:
        try:
            r = jupiter_rate_limiter.get(url, headers=headers(), timeout=12)
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("tokens") or data.get("data") or []
            for t in items:
                mint = isinstance(t, dict) and (t.get("id") or t.get("address"))
                if mint and is_pumpfun(t):
                    tokens[mint] = t
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)[:120]
    rows = list(tokens.values())
    last_scan.update(tokens=len(rows), error=error if not rows else None)
    _cache[:] = [time.monotonic(), rows]
    return rows
