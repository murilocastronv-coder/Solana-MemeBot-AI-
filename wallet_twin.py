"""Find wallets that trade like the reference wallet (the one that is making the owner money).

1. Read the reference wallet's real history (wallet_profile.analyze) and keep its style:
   how long it holds a coin, how many trades per day, how much it puts per coin, how often it
   sells in parts, how much it trades on pump.fun / PumpSwap, its win rate.
2. Every wallet found by the automatic search is measured the same way and gets a similarity
   score (0-100). Only wallets with the same style (>= TWIN_MIN_SIMILARITY) that are also making
   money now (profit > 0, win rate >= TWIN_MIN_WIN, >= TWIN_MIN_TRADES trades in the period) are copied.
Analytics only: never signs or sends anything.
"""
import json
import math
import os
import tempfile
import threading
import time

# set on Render: COPY_REFERENCE_WALLET=<wallet>. Empty = old behaviour (no reference wallet).
REFERENCE = os.getenv("COPY_REFERENCE_WALLET", "").strip()
ENABLED = os.getenv("COPY_TWIN_SEARCH", "true").strip().lower() != "false"
MIN_SIMILARITY = float(os.getenv("COPY_TWIN_MIN_SIMILARITY", "60"))
MIN_WIN = float(os.getenv("COPY_TWIN_MIN_WIN", "0.45"))
MIN_TRADES = int(float(os.getenv("COPY_TWIN_MIN_TRADES", "15")))
PROFILE_MAX_AGE_S = float(os.getenv("COPY_TWIN_PROFILE_HOURS", "12")) * 3600
PUMP = ("pump.fun", "PumpSwap")
_LOCK = threading.RLock()


def style(summary):
    """The numbers that describe how a wallet trades (from wallet_profile.summarize)."""
    s = summary or {}
    if not s.get("positions"):
        return None
    plats = s.get("platforms") or {}
    total = sum(plats.values()) or 1
    return {"hold_min": float(s.get("hold_median") or 0), "trades_day": float(s.get("trades_per_day") or 0),
            "size_usd": float(s.get("size_median") or 0), "scale_out": float(s.get("scale_out") or 0),
            "scale_in": float(s.get("scale_in") or 0), "pump_share": sum(plats.get(p, 0) for p in PUMP) / total,
            "win_rate": float(s.get("win_rate") or 0), "positions": int(s.get("positions") or 0),
            "pnl_usd": float(s.get("pnl_usd") or 0)}


def _ratio_points(a, b, pts, tolerance):
    """pts when a == b, 0 when one is `tolerance` times the other (log scale)."""
    if a <= 0 or b <= 0:
        return 0.0
    return max(0.0, 1 - abs(math.log(a / b)) / math.log(tolerance)) * pts


def similarity(ref, cand):
    """0-100: how close the candidate's style is to the reference wallet's."""
    if not ref or not cand:
        return 0.0
    pts = (_ratio_points(ref["hold_min"], cand["hold_min"], 35, 6) +        # same time in the coin
           _ratio_points(ref["trades_day"], cand["trades_day"], 20, 6) +    # same rhythm
           _ratio_points(ref["size_usd"], cand["size_usd"], 15, 8) +        # similar size per coin
           max(0.0, 1 - abs(ref["pump_share"] - cand["pump_share"]) / 0.5) * 15 +   # same market (pump.fun)
           max(0.0, 1 - abs(ref["scale_out"] - cand["scale_out"]) / 0.5) * 10 +     # sells in parts like it
           max(0.0, 1 - abs(ref["scale_in"] - cand["scale_in"]) / 0.5) * 5)
    return round(pts, 1)


def _path():
    import live_trade_state
    return live_trade_state._path().with_name("wallet_twin.json")


def _load():
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".twin.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def reference_style(rpc=None, sol_usd=None, analyze=None, now=None):
    """Style of the reference wallet, measured again every PROFILE_MAX_AGE_S (cached on disk)."""
    now = now or time.time()
    with _LOCK:
        d = _load()
    ref = d.get("reference") or {}
    if ref.get("wallet") == REFERENCE and ref.get("style") and now - ref.get("at", 0) < PROFILE_MAX_AGE_S:
        return ref["style"]
    if rpc is None:
        return ref.get("style") if ref.get("wallet") == REFERENCE else None
    if analyze is None:
        import wallet_profile
        analyze = wallet_profile.analyze
    try:
        summary = analyze(REFERENCE, rpc, sol_usd, max_tx=int(os.getenv("COPY_TWIN_REFERENCE_TX", "600")))[0]
    except Exception:
        return ref.get("style") if ref.get("wallet") == REFERENCE else None
    st = style(summary)
    if st:
        with _LOCK:
            d = _load()
            d["reference"] = {"wallet": REFERENCE, "at": now, "style": st}
            _save(d)
    return st


def judge(cand_style, audit, ref):
    """(ok, similarity, why) for a measured candidate."""
    sim = similarity(ref, cand_style)
    a = audit or {}
    if not cand_style:
        return False, 0.0, "sem histórico"
    if sim < MIN_SIMILARITY:
        return False, sim, "estilo diferente (" + format(sim, ".0f") + "%)"
    if a.get("trades_recent", 0) < MIN_TRADES:
        return False, sim, "poucas operações recentes"
    if a.get("pnl_recent", 0) <= 0:
        return False, sim, "sem lucro recente"
    if a.get("win_recent", 0) < MIN_WIN:
        return False, sim, "acerta pouco"
    return True, sim, ""


def describe(st):
    if not st:
        return "ainda medindo"
    return ("segura " + format(st["hold_min"], ".0f") + " min · " + format(st["trades_day"], ".0f") + " operações/dia · US$ " +
            format(st["size_usd"], ".0f") + " por moeda · " + format(st["pump_share"] * 100, ".0f") + "% pump.fun · acerta " +
            format(st["win_rate"] * 100, ".0f") + "%")
