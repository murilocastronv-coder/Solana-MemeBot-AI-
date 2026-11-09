"""Shared Jupiter API pacing and bounded HTTP 429 backoff.

All API calls in the bot share one process-wide request slot so token feeds,
quotes, transaction builds, and MEV quotes cannot burst past the API quota.
POST execution calls are never retried automatically.
"""
import logging
import os
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

LOG = logging.getLogger("jupiter_rate_limiter")
_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_COOLDOWN_UNTIL = 0.0

try:
    _CONFIGURED_INTERVAL = float(os.getenv("JUPITER_MIN_REQUEST_INTERVAL_SECONDS", "1.25"))
except ValueError:
    _CONFIGURED_INTERVAL = 1.25
_DEFAULT_INTERVAL = 1.25 if os.getenv("JUPITER_API_KEY", "").strip() else 2.1
_MIN_INTERVAL = max(_DEFAULT_INTERVAL, _CONFIGURED_INTERVAL)


def _retry_after(response, attempt):
    value = (response.headers or {}).get("Retry-After", "").strip()
    if value:
        try:
            return min(120.0, max(0.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return min(120.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(30.0, 2.5 * (2 ** attempt))


def request(method, url, **kwargs):
    """Issue one serialized Jupiter API call; retry only safe GETs on HTTP 429."""
    global _NEXT_REQUEST_AT, _COOLDOWN_UNTIL
    method = str(method).upper()
    attempts = 3 if method == "GET" else 1
    response = None
    for attempt in range(attempts):
        with _LOCK:
            now = time.monotonic()
            ready_at = max(_NEXT_REQUEST_AT, _COOLDOWN_UNTIL)
            if ready_at > now:
                time.sleep(ready_at - now)
            _NEXT_REQUEST_AT = time.monotonic() + _MIN_INTERVAL
            response = requests.request(method, url, **kwargs)
            if response.status_code == 429:
                delay = _retry_after(response, attempt)
                _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + delay)
                LOG.warning("Jupiter rate limit HTTP 429 path=%s cooldown=%.1fs attempt=%s",
                            urlsplit(url).path, delay, attempt + 1)
        if response.status_code != 429 or attempt + 1 >= attempts:
            return response
    return response


def get(url, **kwargs):
    return request("GET", url, **kwargs)


def post(url, **kwargs):
    return request("POST", url, **kwargs)
