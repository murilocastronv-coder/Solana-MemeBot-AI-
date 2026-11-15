"""PostgreSQL storage (Render, DATABASE_URL). Small key -> JSON documents table, pure-Python driver (pg8000).

Used by the DEV robot: every save goes to PostgreSQL (and to the disk as a backup); the first load moves
the disk copy into the database. Without DATABASE_URL (or if the database is unreachable) the disk is used.
"""
import json
import logging
import os
import threading
from urllib.parse import unquote, urlparse

_LOCK = threading.RLock()
_CONN = {"c": None, "failed_at": 0.0}


def enabled():
    return bool(os.getenv("DATABASE_URL", "").strip())


def _connect():
    import pg8000.native
    import ssl
    u = urlparse(os.getenv("DATABASE_URL", "").strip())
    host = u.hostname or ""
    ctx = None
    if "." in host:                      # external hostname: TLS; Render's internal hostname: plain
        ctx = ssl.create_default_context()
    c = pg8000.native.Connection(user=unquote(u.username or ""), password=unquote(u.password or ""), host=host,
                                 port=u.port or 5432, database=(u.path or "/").lstrip("/"), ssl_context=ctx, timeout=15)
    c.run("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v JSONB NOT NULL, updated TIMESTAMPTZ DEFAULT now())")
    c.run("CREATE TABLE IF NOT EXISTS events (id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ DEFAULT now(), kind TEXT, data JSONB)")
    return c


def _conn():
    import time
    if _CONN["c"] is None:
        if time.time() - _CONN["failed_at"] < 60:
            return None
        try:
            _CONN["c"] = _connect()
            logging.info("postgres connected")
        except Exception as exc:
            _CONN["failed_at"] = time.time()
            logging.warning("postgres unavailable: %s", type(exc).__name__)
            return None
    return _CONN["c"]


def _run(sql, **params):
    with _LOCK:
        c = _conn()
        if c is None:
            raise RuntimeError("postgres unavailable")
        try:
            return c.run(sql, **params)
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            _CONN["c"] = None
            raise


def get(key):
    """Stored JSON for key, None if missing. Raises if the database is unreachable."""
    rows = _run("SELECT v FROM kv WHERE k = :k", k=key)
    if not rows:
        return None
    v = rows[0][0]
    return json.loads(v) if isinstance(v, str) else v


def put(key, value):
    _run("INSERT INTO kv (k, v, updated) VALUES (:k, CAST(:v AS JSONB), now()) "
         "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated = now()", k=key, v=json.dumps(value))


def event(kind, data):
    try:
        _run("INSERT INTO events (kind, data) VALUES (:kind, CAST(:d AS JSONB))", kind=kind, d=json.dumps(data))
    except Exception:
        pass
