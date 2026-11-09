"""Find copy-trade wallets from the memecoins trending on DexScreener.

DexScreener's public API gives the hot Solana tokens (boosts + new profiles) and
their main pair. Its "Top traders" tab is not part of the public API, so the
trades of each hot pair are read on the blockchain (Solana RPC) instead, and fed
into wallet_copy's per-wallet, per-coin history (profit + how long each coin
was held). wallet_copy.best_wallets() then ranks them as usual.

Analytics only: this module never signs or sends anything.
"""
import os
import time

import requests

import wallet_copy

DEX = "https://api.dexscreener.com"
FEEDS = ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1")
SKIP_MINTS = {"So11111111111111111111111111111111111111112",
              "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
              "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def _env(name, default):
    return float(os.getenv(name, str(default)))


MIN_LIQUIDITY = _env("COPY_DEX_MIN_LIQUIDITY_USD", 10000)
MIN_VOLUME_24H = _env("COPY_DEX_MIN_VOLUME_24H_USD", 50000)
MAX_TOKENS = int(_env("COPY_DEX_MAX_TOKENS", 8))
TX_PER_TOKEN = int(_env("COPY_DEX_TX_PER_TOKEN", 15))
# Speed-up: wallets seen buying a hot coin get their own recent history read right away.
WALLETS_PER_CYCLE = int(_env("COPY_DEX_WALLETS_PER_CYCLE", 4))
WALLET_HISTORY = int(_env("COPY_DEX_WALLET_HISTORY", 50))
REANALYZE_SECONDS = _env("COPY_DEX_REANALYZE_HOURS", 12) * 3600
# Free RPC plans refuse bursts (HTTP 429). Stay under the limit and retry refused calls.
MAX_RPS = max(0.5, _env("COPY_RPC_MAX_RPS", 6))
RPC_ERRORS = {"count": 0, "last": ""}

_http = requests.Session()


def _get_json(path):
    r = _http.get(DEX + path, timeout=15, headers={"Accept": "application/json", "User-Agent": "SolanaCopyTrade/1.0"})
    r.raise_for_status()
    return r.json()


def trending_pairs(get=_get_json, max_tokens=MAX_TOKENS):
    """Hot Solana memecoins on DexScreener: best pair per token, by 24h volume."""
    mints = []
    for feed in FEEDS:
        try:
            rows = get(feed)
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("chainId") == "solana" and row.get("tokenAddress"):
                mints.append(row["tokenAddress"])
    mints = [m for m in dict.fromkeys(mints) if m not in SKIP_MINTS][:90]
    best = {}
    for i in range(0, len(mints), 30):
        try:
            pairs = get("/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
        except Exception:
            continue
        for p in pairs if isinstance(pairs, list) else []:
            if not isinstance(p, dict) or p.get("chainId") != "solana" or not p.get("pairAddress"):
                continue
            mint = (p.get("baseToken") or {}).get("address")
            if not mint or mint in SKIP_MINTS:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            vol = float((p.get("volume") or {}).get("h24") or 0)
            if liq < MIN_LIQUIDITY or vol < MIN_VOLUME_24H:
                continue
            if mint not in best or vol > best[mint]["volume24h"]:
                best[mint] = {"mint": mint, "symbol": (p.get("baseToken") or {}).get("symbol") or "?",
                              "pair": p["pairAddress"], "liquidity": liq, "volume24h": vol}
    return sorted(best.values(), key=lambda x: x["volume24h"], reverse=True)[:max_tokens]


def _rpc_factory(rpc_url, sleep=time.sleep, clock=time.monotonic, session=None):
    """JSON-RPC caller limited to MAX_RPS, retrying rate-limited / temporary failures."""
    session = session or requests.Session()
    gap = 1.0 / MAX_RPS
    last = {"at": 0.0}

    def rpc(method, params):
        for attempt in range(4):
            wait = last["at"] + gap - clock()
            if wait > 0:
                sleep(wait)
            last["at"] = clock()
            try:
                r = session.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                 timeout=20)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError("HTTP " + str(r.status_code))
                r.raise_for_status()
                payload = r.json()
                err = payload.get("error")
                if err:
                    text = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                    if "limit" in text.lower() or "too many" in text.lower():
                        raise RuntimeError("rate limit: " + text[:80])
                    raise ValueError(method + ": " + text[:120])   # real error: no retry
                return payload.get("result")
            except ValueError as exc:
                RPC_ERRORS["count"] += 1
                RPC_ERRORS["last"] = str(exc)[:120]
                raise
            except Exception as exc:
                RPC_ERRORS["count"] += 1
                RPC_ERRORS["last"] = (type(exc).__name__ + ": " + str(exc))[:120]
                if attempt == 3:
                    raise
                sleep(0.5 * 2 ** attempt)
    return rpc


def _buyers(tx):
    """Wallets that paid SOL for a token in this transaction."""
    tokens, sol = wallet_copy._tx_deltas(tx)
    out = []
    for owners in tokens.values():
        for owner, (delta, _) in owners.items():
            if delta > 0 and int(sol.get(owner, 0)) < 0:
                out.append(owner)
    return out


def _get_tx(rpc, signature):
    return rpc("getTransaction", [signature, {"encoding": "json", "commitment": "confirmed",
                                              "maxSupportedTransactionVersion": 0}])


def analyze_wallet(wallet, rpc, limit=WALLET_HISTORY):
    """Replay the wallet's latest transactions (oldest first) on a clean state.
    Returns its stats (closed trades, wins, profit, hold time per coin) or None."""
    sigs = rpc("getSignaturesForAddress", [wallet, {"limit": limit}]) or []
    tmp = wallet_copy.empty_state()
    for s in reversed(sigs):
        if s.get("err"):
            continue
        try:
            tx = _get_tx(rpc, s["signature"])
        except Exception:
            continue
        if isinstance(tx, dict):
            wallet_copy.process_confirmed_block(tmp, {"blockTime": tx.get("blockTime") or s.get("blockTime"),
                                                      "transactions": [tx]}, slot=tx.get("slot"))
    return tmp["wallets"].get(wallet)


def scan(rpc_url=None, get=_get_json, rpc=None, max_tokens=MAX_TOKENS, per_token=TX_PER_TOKEN,
         wallets_per_cycle=WALLETS_PER_CYCLE, history=WALLET_HISTORY, now=None):
    """Read the newest trades of each hot pair and update wallet history. Returns tx count."""
    if rpc is None:
        rpc_url = (rpc_url or os.getenv("SOLANA_RPC_URL", "")).strip()
        if not rpc_url:
            raise RuntimeError("Dedicated Solana RPC missing")
        rpc = _rpc_factory(rpc_url)
    pairs = trending_pairs(get, max_tokens)
    with wallet_copy._LOCK:
        path = wallet_copy._state_path()
        state = wallet_copy._load_state(path)
        cursors = state.setdefault("dex_cursors", {})
        seen = set(state.get("dex_seen_sigs", []))
        buyers = []
        scanned = failed = 0
        errors_before = RPC_ERRORS["count"]
        for p in pairs:
            params = {"limit": per_token}
            if cursors.get(p["pair"]):
                params["until"] = cursors[p["pair"]]
            try:
                sigs = rpc("getSignaturesForAddress", [p["pair"], params]) or []
            except Exception:
                failed += 1
                continue
            if not sigs:
                continue
            for s in reversed(sigs):          # oldest first, so buy comes before sell
                if s.get("err") or s["signature"] in seen:
                    continue
                try:
                    tx = _get_tx(rpc, s["signature"])
                except Exception:
                    failed += 1
                    continue
                if not isinstance(tx, dict):
                    continue
                seen.add(s["signature"])
                buyers += _buyers(tx)
                block = {"blockTime": tx.get("blockTime") or s.get("blockTime"), "transactions": [tx]}
                wallet_copy.process_confirmed_block(state, block, slot=tx.get("slot"))
                scanned += 1
            cursors[p["pair"]] = sigs[0]["signature"]
        if len(cursors) > 300:
            keep = {p["pair"] for p in pairs}
            state["dex_cursors"] = {k: v for k, v in list(cursors.items())[-200:]} | \
                {k: cursors[k] for k in keep if k in cursors}
        # Speed-up: read the recent history of new buyers instead of waiting to see them sell.
        analyzed = state.setdefault("dex_analyzed", {})
        now = int(now or time.time())
        fresh = [w for w in dict.fromkeys(buyers) if now - int(analyzed.get(w, 0)) >= REANALYZE_SECONDS]
        done = with_trades = 0
        for w in fresh[:max(0, wallets_per_cycle)]:
            try:
                stats = analyze_wallet(w, rpc, history)
            except Exception:
                failed += 1
                continue
            analyzed[w] = now
            done += 1
            if stats:
                state["wallets"][w] = stats
                with_trades += 1
        if len(analyzed) > 5000:
            state["dex_analyzed"] = dict(sorted(analyzed.items(), key=lambda kv: kv[1])[-3000:])
        state["dex_seen_sigs"] = list(seen)[-20000:]
        state["dex_last_cycle"] = {"at": now, "tokens": len(pairs), "tx_read": scanned, "failed": failed,
                                   "rpc_errors": RPC_ERRORS["count"] - errors_before,
                                   "last_error": RPC_ERRORS["last"] if RPC_ERRORS["count"] > errors_before else "",
                                   "buyers": len(set(buyers)), "new_buyers": len(fresh),
                                   "histories": done, "histories_with_trades": with_trades}
        state["dex_tokens"] = [{"symbol": p["symbol"], "mint": p["mint"], "volume24h": p["volume24h"]} for p in pairs]
        state["dex_transactions_scanned"] = int(state.get("dex_transactions_scanned", 0)) + scanned
        state["updated_at"] = int(time.time())
        wallet_copy._save_state(path, state)
    return scanned
