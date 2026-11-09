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


def _rpc_factory(rpc_url):
    session = requests.Session()

    def rpc(method, params):
        r = session.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError("Solana RPC " + method + " failed")
        return payload.get("result")
    return rpc


def scan(rpc_url=None, get=_get_json, rpc=None, max_tokens=MAX_TOKENS, per_token=TX_PER_TOKEN):
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
        scanned = 0
        for p in pairs:
            params = {"limit": per_token}
            if cursors.get(p["pair"]):
                params["until"] = cursors[p["pair"]]
            try:
                sigs = rpc("getSignaturesForAddress", [p["pair"], params]) or []
            except Exception:
                continue
            if not sigs:
                continue
            for s in reversed(sigs):          # oldest first, so buy comes before sell
                if s.get("err"):
                    continue
                try:
                    tx = rpc("getTransaction", [s["signature"], {"encoding": "json", "commitment": "confirmed",
                                                                 "maxSupportedTransactionVersion": 0}])
                except Exception:
                    continue
                if not isinstance(tx, dict):
                    continue
                block = {"blockTime": tx.get("blockTime") or s.get("blockTime"), "transactions": [tx]}
                wallet_copy.process_confirmed_block(state, block, slot=tx.get("slot"))
                scanned += 1
            cursors[p["pair"]] = sigs[0]["signature"]
        if len(cursors) > 300:
            keep = {p["pair"] for p in pairs}
            state["dex_cursors"] = {k: v for k, v in list(cursors.items())[-200:]} | \
                {k: cursors[k] for k in keep if k in cursors}
        state["dex_tokens"] = [{"symbol": p["symbol"], "mint": p["mint"], "volume24h": p["volume24h"]} for p in pairs]
        state["dex_transactions_scanned"] = int(state.get("dex_transactions_scanned", 0)) + scanned
        state["updated_at"] = int(time.time())
        wallet_copy._save_state(path, state)
    return scanned
