"""How much people are talking about a coin on X (Twitter): mentions of its mint address or $TICKER.

Uses the official X API v2 'recent tweet counts' endpoint. It needs a paid X API plan: put the
bearer token in X_BEARER_TOKEN (Render secret). Without it this module returns None and the signals
bot works without Twitter. Results are cached for 15 minutes per coin to save API calls.
"""
import os
import re
import time

import requests

API = "https://api.twitter.com/2/tweets/counts/recent"
CACHE_S = 15 * 60
_cache = {}
_http = requests.Session()
STATUS = {"last_error": "", "calls": 0}


def configured():
    return bool(os.getenv("X_BEARER_TOKEN", "").strip())


def query_for(symbol, mint):
    parts = ['"' + mint + '"']
    sym = re.sub(r"[^A-Za-z0-9]", "", symbol or "")
    if 2 <= len(sym) <= 10:
        parts.append('"$' + sym + '"')          # ticker as text (cashtag) - common way coins are shilled
    return "(" + " OR ".join(parts) + ") -is:retweet"


def buzz(symbol, mint, get=None, now=None):
    """{'h1': mentions last hour, 'h24': last 24 h, 'growth': last 3 h vs the 21 h before (per hour)}
    or None (no token / error)."""
    if not configured() and get is None:
        return None
    now = now or time.time()
    hit = _cache.get(mint)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    try:
        if get is None:
            r = _http.get(API, params={"query": query_for(symbol, mint), "granularity": "hour"}, timeout=15,
                          headers={"Authorization": "Bearer " + os.getenv("X_BEARER_TOKEN", "").strip()})
            STATUS["calls"] += 1
            if r.status_code != 200:
                STATUS["last_error"] = "HTTP " + str(r.status_code)
                return None
            data = r.json()
        else:
            data = get(query_for(symbol, mint))
        rows = sorted((data or {}).get("data") or [], key=lambda x: x.get("start", ""))
        counts = [int(x.get("tweet_count") or 0) for x in rows][-24:]
    except Exception as exc:
        STATUS["last_error"] = type(exc).__name__
        return None
    if not counts:
        out = {"h1": 0, "h24": 0, "growth": 0.0}
    else:
        last3 = sum(counts[-3:]) / max(len(counts[-3:]), 1)
        before = counts[:-3]
        base = sum(before) / len(before) if before else 0
        out = {"h1": counts[-1], "h24": sum(counts), "growth": (last3 / base) if base else (3.0 if last3 else 0.0)}
    _cache[mint] = (now, out)
    return out
