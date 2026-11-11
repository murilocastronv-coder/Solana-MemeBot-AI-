"""Copy trading: follow chosen memecoin wallets, copy their buys, exit with them.

- Wallets are added by the owner (/seguir ADDRESS). On the first look at a
  wallet only its latest signature is stored, so old trades are never copied.
- Every few seconds each wallet's new transactions are read from the RPC and
  turned into BUY/SELL signals from the wallet's own balance changes (a token
  goes up while SOL/USDC goes down = buy; the opposite = sell).
- BUY: if the token passes the anti-scam checks (mint/freeze revoked, top 10
  holders < 25%, sell test >= 92%), copy with a fixed size (US$ 10).
- SELL: when the copied wallet sells, our position is sold too; before that,
  stop -15% / take profit +50%.
- Scoring: every closed copy is credited to the wallet that triggered it. A
  wallet with >= 5 copies and a negative result for US is paused automatically.

Simulation prices everything with real Jupiter quotes and touches nothing.
Real mode (owner authorised) buys/sells through the live engine (quote,
balance, signed, sent, confirmed on-chain); its exits run in the existing
exit monitor (mode "copy").
"""
import json
import logging
import os
import tempfile
import threading
import time
from collections import deque

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINTS = {SOL_MINT, USDC_MINT, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def _env(name, default):
    return float(os.getenv(name, str(default)))


ORDER_USD = _env("COPY_ORDER_USD", 32)   # US$ per copied position
MAX_POSITIONS = int(_env("COPY_MAX_POSITIONS", 12))   # at most 12 copied operations open
# Simulation: copy + strategy robot together never hold more than this many open orders.
MAX_OPEN_TOTAL = int(_env("MAX_OPEN_ORDERS", 12))
STOP_LOSS = _env("COPY_STOP_LOSS", 0.15)
# "wallet" (default): no fixed TP/SL, exit only when the copied wallet sells (same movement).
# "tp_sl": also use COPY_TAKE_PROFIT / COPY_STOP_LOSS.
EXIT_MODE = os.getenv("COPY_EXIT_MODE", "wallet").strip().lower()
TAKE_PROFIT = _env("COPY_TAKE_PROFIT", 0.50)
MIN_COPIES_TO_JUDGE = int(_env("COPY_MIN_TRADES_TO_JUDGE", 5))
MAX_TOP_HOLDERS = 0.25
MIN_ROUNDTRIP = 0.92
NETWORK_FEE_USD = 0.003
START_USD = 100.0
MAX_WALLETS = int(_env("COPY_MAX_WALLETS", 30))
MAX_AUTO_WALLETS = int(_env("COPY_AUTO_MAX_WALLETS", 20))
# A new candidate replaces an unproven followed wallet only if its score is clearly better.
REPLACE_MARGIN = _env("COPY_REPLACE_MARGIN", 1.5)
# Only follow wallets holding at least this much (SOL balance in USD); remove them below it.
MIN_WALLET_USD = _env("COPY_MIN_WALLET_USD", 40)
BALANCE_MAX_AGE = 600
# Only copy buys where the wallet put at least this much in the coin (and only follow
# wallets whose average buy is at least this much).
MIN_BUY_USD = _env("COPY_MIN_BUY_USD", 40)
# Conviction filter: copy only buys of at least this multiple of what the wallet usually puts in
# a coin (median of its recent buys). 1.0 = its normal size or more; 0 = off.
CONVICTION_MULT = _env("COPY_CONVICTION_MULT", 1.0)
# Only follow wallets that have been trading for at least this many days.
MIN_WALLET_DAYS = _env("COPY_MIN_WALLET_DAYS", 7)   # created at least 7 days ago
# Only follow wallets that trade (almost) every day: active in >= this many of the last 7 days.
MIN_ACTIVE_DAYS = int(_env("COPY_MIN_ACTIVE_DAYS", 6))
_balance_cache = {}
# Only copy wallets that HOLD: we first watch each wallet's own buy->sell
# round trips and copy it only if the median hold is at least this long.
MIN_HOLD_SECONDS = _env("COPY_MIN_HOLD_MINUTES", 5) * 60
MIN_ROUND_TRIPS = int(_env("COPY_MIN_ROUND_TRIPS", 3))
# ... and at least this share of its positions must last 5+ min (blocks frequent 1-minute flippers).
MIN_LONG_SHARE = _env("COPY_MIN_LONG_HOLD_SHARE", 0.6)
AUDIT_MAX_TX = int(_env("COPY_AUDIT_MAX_TX", 300))
# Recent results: only copy wallets that made money in the last RESULT_DAYS days
# (>= RESULT_MIN_TRADES closed trades, win rate >= RESULT_MIN_WIN, profit > RESULT_MIN_USD).
RESULT_DAYS = _env("COPY_RESULT_DAYS", 9)
RESULT_MIN_TRADES = int(_env("COPY_RESULT_MIN_TRADES", 40))   # at least 40 trades in the period
# Keep only the TOP_WALLETS wallets with the highest profit in the period; the rest are removed.
TOP_WALLETS = int(_env("COPY_TOP_WALLETS", 20))
RESULT_MIN_WIN = _env("COPY_RESULT_MIN_WIN", 0.5)
RESULT_MIN_USD = _env("COPY_RESULT_MIN_USD", 0)
AUDIT_VALID_S = _env("COPY_AUDIT_VALID_HOURS", 24) * 3600
# Stable list: candidates are audited BEFORE being followed (a few per round), rejected wallets
# can't come back for REJECT_DAYS, and an approved wallet is only replaced by one whose profit in
# the period is REPLACE_PNL_MARGIN x higher.
PREAUDIT_PER_CYCLE = int(_env("COPY_PREAUDIT_PER_CYCLE", 1))
# While the list still has free slots (below MAX_AUTO_WALLETS), audit more candidates per round so the
# automatic search fills the list up to the limit faster.
PREAUDIT_FILL_PER_CYCLE = int(_env("COPY_PREAUDIT_FILL_PER_CYCLE", 3))


def free_auto_slots():
    """How many more discovered wallets fit in the copy list (limit MAX_AUTO_WALLETS / MAX_WALLETS)."""
    with _LOCK:
        d = _load()
    auto = sum(bool(w.get("auto_discovered")) for w in d["wallets"].values())
    return max(0, min(MAX_AUTO_WALLETS - auto, MAX_WALLETS - len(d["wallets"])))


def preaudit_budget():
    """Candidates to audit this round: more while there is room in the list, 1 when it is full."""
    free = free_auto_slots()
    return max(PREAUDIT_PER_CYCLE, min(free, PREAUDIT_FILL_PER_CYCLE)) if free else PREAUDIT_PER_CYCLE
REJECT_DAYS = _env("COPY_REJECT_DAYS", 3)
REPLACE_PNL_MARGIN = _env("COPY_REPLACE_PNL_MARGIN", 1.2)
# Test period: a wallet that enters is copied for at least TEST_DAYS before any automatic removal
# or replacement (only /deixar removes it earlier), so its real copy results can be seen.
TEST_DAYS = _env("COPY_TEST_DAYS", 3)
# During the test a wallet is only paused if its copies lost this much (US$) in total
TEST_MAX_LOSS_USD = _env("COPY_TEST_MAX_LOSS_USD", 100)
# Pause a copied wallet that makes this many trades shorter than MIN_HOLD (5 min) in its last 5 seen trades
FAST_TRADES_TO_PAUSE = int(_env("COPY_FAST_TRADES_TO_PAUSE", 2))
# Pause a copied wallet after this many losing copies in a row (also during the test)
LOSS_STREAK_TO_PAUSE = int(_env("COPY_LOSS_STREAK_TO_PAUSE", 6))
# Proportional wallets: copy each buy with the SAME % of our free balance that the wallet used of its own
# (10% -> 10%, 30% -> 30%), capped. List in COPY_PROPORTIONAL_WALLETS (comma) or toggle with /proporcional.
# each copy: between 10% and 30% of OUR wallet (free SOL + open positions), same % as the wallet inside that range
PROPORTIONAL_MIN_FRACTION = _env("COPY_PROPORTIONAL_MIN_FRACTION", 0.10)
PROPORTIONAL_MAX_FRACTION = _env("COPY_PROPORTIONAL_MAX_FRACTION", 0.30)
# mirror wallet (4x5Uti...zTJ): each copied buy between 5% and 6% of our account
MIRROR_MIN_FRACTION = _env("COPY_MIRROR_MIN_FRACTION", 0.05)
MIRROR_MAX_FRACTION = _env("COPY_MIRROR_MAX_FRACTION", 0.06)
# ... and the whole position in one coin (all its buys, rebuys included) at most 12% of our account
MIRROR_MAX_COIN_FRACTION = _env("COPY_MIRROR_MAX_COIN_FRACTION", 0.12)
PROPORTIONAL_MAX_USD = _env("COPY_PROPORTIONAL_MAX_USD", 0)      # 0 = no dollar cap (only the % limits)
PROPORTIONAL_RESERVE_USD = _env("COPY_PROPORTIONAL_RESERVE_USD", 2)   # left in SOL for network fees
# buying the same coin again: the whole position in that coin can't pass 30% of our wallet
PROPORTIONAL_MAX_COIN_FRACTION = _env("COPY_PROPORTIONAL_MAX_COIN_FRACTION", 0.30)


def coin_cost_usd(mint, real):
    """What we already put in this coin (all buys of the copy)."""
    if real:
        import live_trade_state as st
        return float((st._load()["positions"].get(mint) or {}).get("cost_usd") or 0)
    return float((_load()["positions"].get(mint) or {}).get("cost_usd") or 0)


def add_on_room(size, already_usd, wallet_value_usd, max_coin=None):
    """Buying the same coin again: cut the buy so the coin stays <= max_coin of the wallet. None = no room."""
    if not already_usd:
        return size
    cap = PROPORTIONAL_MAX_COIN_FRACTION if max_coin is None else max_coin
    room = cap * float(wallet_value_usd) - float(already_usd)
    size = min(float(size), room)
    return round(size, 2) if size >= PROPORTIONAL_MIN_USD else None
PROPORTIONAL_MIN_USD = _env("COPY_PROPORTIONAL_MIN_USD", 3)
BUY_FRACTION = {}
# The copied wallet buys the same coin again: buy again too, up to this many buys per coin
MAX_BUYS_PER_COIN = int(_env("COPY_MAX_BUYS_PER_COIN", 4))   # (wallet, mint) -> share of its free balance the wallet used in its last buy


def mirror(address, w=None):
    """Mirror wallet (COPY_PROPORTIONAL_WALLETS or flagged): copy EVERYTHING it does - no style/result/size
    filters, never paused or removed automatically. Always proportional too."""
    env = {a.strip() for a in os.getenv("COPY_PROPORTIONAL_WALLETS", "").split(",") if a.strip()}
    if w is None:
        w = _load()["wallets"].get(address) or {}
    return bool(w.get("mirror")) or address in env


def proportional(address, w=None):
    """Sizing only: copy each buy with the same % the wallet puts in the coin (10%-30% of our account)."""
    if w is None:
        w = _load()["wallets"].get(address) or {}
    return bool(w.get("proportional")) or mirror(address, w)


def set_proportional(address, on):
    with _LOCK:
        d = _load()
        w = d["wallets"].get(address)
        if w is None:
            return None
        w["proportional"] = bool(on)
        _save(d)
    return True


def buy_fraction(tx, wallet):
    """Share of its free balance (SOL + WSOL + USDC, before the buy) the wallet put in this buy."""
    paid = buy_value_usd(tx, wallet)
    if not paid:
        return None
    meta = (tx or {}).get("meta") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    pre_sol = 0
    if wallet in keys:
        i = keys.index(wallet)
        pre = meta.get("preBalances") or []
        pre_sol = int(pre[i]) if i < len(pre) else 0
    pre_wsol = pre_usdc = 0
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") != wallet:
            continue
        amount = int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
        if b.get("mint") == SOL_MINT:
            pre_wsol += amount
        elif b.get("mint") == USDC_MINT:
            pre_usdc += amount
    try:
        price = _sol_usd()
    except Exception:
        return None
    total = (pre_sol + pre_wsol) / 1e9 * price + pre_usdc / 1e6
    return min(1.0, paid / total) if total > 0 else None


def fraction_limits(address, w=None):
    """(min, max) share of our account per buy: mirror wallets 5%-22%, other proportional ones 10%-30%."""
    if mirror(address, w):
        return MIRROR_MIN_FRACTION, MIRROR_MAX_FRACTION
    return PROPORTIONAL_MIN_FRACTION, PROPORTIONAL_MAX_FRACTION


def proportional_usd(fraction, wallet_value_usd, free_usd=None, limits=None):
    """Our order for a proportional wallet: the wallet's % of its balance, kept between the min and max
    share of our wallet value (free SOL + open positions); never more than the free SOL (minus fees reserve).
    None = too small or nothing free."""
    if not fraction or wallet_value_usd is None:
        return None
    lo, hi = limits or (PROPORTIONAL_MIN_FRACTION, PROPORTIONAL_MAX_FRACTION)
    share = min(max(float(fraction), lo), hi)
    usd = share * float(wallet_value_usd)
    if PROPORTIONAL_MAX_USD > 0:
        usd = min(usd, PROPORTIONAL_MAX_USD)
    if free_usd is not None:
        usd = min(usd, float(free_usd) - PROPORTIONAL_RESERVE_USD)
    return round(usd, 2) if usd >= PROPORTIONAL_MIN_USD else None


def _busy(d):
    """Copied wallets with an open position (owner's or any user's): they stay watched until it is sold."""
    out = {p.get("wallet") for p in d["positions"].values()}
    try:
        import user_copy
        out |= user_copy.busy_wallets()
    except Exception:
        pass
    return out


def drop_finished_leaving():
    """A wallet marked to leave goes away once nobody (owner or user) holds a copy from it."""
    with _LOCK:
        d = _load()
        busy = _busy(d)
        gone = [a for a, w in d["wallets"].items() if w.get("leaving") and a not in busy]
        for a in gone:
            d["wallets"].pop(a)
        if gone:
            _save(d)


def users_active():
    try:
        import user_copy
        return user_copy.any_active()
    except Exception:
        return False


def in_test(w, now=None):
    fa = w.get("followed_at")
    return fa is None or (now or time.time()) - float(fa) < TEST_DAYS * 86400


def ensure_test_start():
    """Wallets followed before the test period existed start their 3-day test now."""
    with _LOCK:
        d = _load()
        missing = [a for a, w in d["wallets"].items() if w.get("followed_at") is None]
        for a in missing:
            d["wallets"][a]["followed_at"] = time.time()
        if missing:
            _save(d)

_LOCK = threading.RLock()
_safety_cache = {}
EVENTS = deque(maxlen=50)
LAST_ERROR = {"text": ""}
REPLACED = {}   # new wallet -> wallet it replaced (for the Telegram notice)


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("copy_trader.json")


def _default():
    return {"enabled": False, "real": False, "wallets": {}, "balance": START_USD, "start": START_USD,
            "positions": {}, "history": []}


def _load():
    path = _path()
    if not path.exists():
        return _default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default()
    d = _default()
    d.update(data)
    return d


def _save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".copy_trader.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _event(text):
    EVENTS.append((time.time(), text))


def set_mode(on, real=False):
    with _LOCK:
        d = _load()
        d["enabled"], d["real"] = bool(on), bool(on and real)
        _save(d)


def enabled():
    with _LOCK:
        return bool(_load()["enabled"])


def real_enabled():
    with _LOCK:
        d = _load()
        return bool(d["enabled"] and d["real"])


def follow(address):
    from solders.pubkey import Pubkey
    Pubkey.from_string(address)  # raises on an invalid address
    with _LOCK:
        d = _load()
        if address not in d["wallets"] and len(d["wallets"]) >= MAX_WALLETS:
            raise ValueError("máximo de " + str(MAX_WALLETS) + " carteiras")
        d["wallets"].setdefault(address, {"followed_at": time.time(),
                                          "last_sig": None, "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False,
                                          "open_buys": {}, "holds": []})
        d["wallets"][address]["paused"] = False
        _save(d)


def open_sim_total():
    """Simulated open copy orders."""
    return sum(1 for p in _load()["positions"].values() if not p.get("real"))


def _exit_for(wallet):
    with _LOCK:
        w = _load()["wallets"].get(wallet) or {}
    e = w.get("exit") or {}
    return {"tp": float(e.get("tp") or TAKE_PROFIT), "sl": float(e.get("sl") or STOP_LOSS),
            "max_hold_s": e.get("max_hold_s")}


def auto_follow(candidate):
    """Add a discovered wallet only after its observed history passes filters."""
    from solders.pubkey import Pubkey
    address = str(candidate.get("wallet") or "")
    Pubkey.from_string(address)
    holds = [max(0, int(x)) for x in candidate.get("hold_samples", [])]
    if len(holds) < MIN_ROUND_TRIPS or _median(holds) < MIN_HOLD_SECONDS or long_share(holds) < MIN_LONG_SHARE:
        return False
    if int(candidate.get("closed_trades", 0)) < MIN_ROUND_TRIPS:
        return False
    if int(candidate.get("realized_lamports", 0)) <= 0:
        return False
    if is_rejected(address):
        return False
    with _LOCK:
        d = _load()
        if address in d["wallets"]:
            return False
        auto_count = sum(bool(w.get("auto_discovered")) for w in d["wallets"].values())
        if auto_count >= MAX_AUTO_WALLETS or len(d["wallets"]) >= MAX_WALLETS:
            return False
    if (not old_enough(candidate) or not active_daily(candidate) or not big_enough_buyer(candidate)
            or not has_min_balance(address)):
        return False
    usd = wallet_usd(address)          # network: outside the lock (cached by has_min_balance)
    with _LOCK:
        d = _load()
        if address in d["wallets"]:
            return False
        auto_count = sum(bool(w.get("auto_discovered")) for w in d["wallets"].values())
        if auto_count >= MAX_AUTO_WALLETS or len(d["wallets"]) >= MAX_WALLETS:
            return False
        d["wallets"][address] = {
            "followed_at": time.time(),
            "last_sig": None, "copies": 0, "wins": 0, "pnl_usd": 0.0,
            "paused": False, "open_buys": {}, "holds": holds[-30:],
            "auto_discovered": True,
        }
        _record_stats(d["wallets"][address], candidate)
        if usd is not None:
            d["wallets"][address]["balance_usd"] = round(usd, 2)
        _save(d)
    _event("descoberta automática: " + address[:6] + "… passou os filtros de retenção e histórico")
    return True


def wallet_usd(address, max_age=BALANCE_MAX_AGE):
    """SOL balance of a wallet in USD (cached 10 min). None if it cannot be read now."""
    now = time.monotonic()
    hit = _balance_cache.get(address)
    if hit and now - hit[0] < max_age:
        return hit[1]
    try:
        res = _rpc("getBalance", [address, {"commitment": "confirmed"}])
        lamports = int(res["value"] if isinstance(res, dict) else res)
        usd = lamports / 1e9 * _sol_usd()
    except Exception:
        return None
    _balance_cache[address] = (now, usd)
    return usd


def big_enough_buyer(stats):
    """Average buy in the wallet's history >= MIN_BUY_USD (None/unknown counts as no)."""
    if MIN_BUY_USD <= 0:
        return True
    lamports = stats.get("average_buy_lamports")
    if lamports is None:
        return False
    try:
        return int(lamports) / 1e9 * _sol_usd() >= MIN_BUY_USD
    except Exception:
        return False


def old_enough(stats):
    """Wallet active for >= MIN_WALLET_DAYS (unknown age counts as no)."""
    if MIN_WALLET_DAYS <= 0:
        return True
    age = stats.get("age_days")
    return age is not None and float(age) >= MIN_WALLET_DAYS


def active_daily(stats):
    """Active on >= MIN_ACTIVE_DAYS of the last 7 days (unknown counts as no)."""
    if MIN_ACTIVE_DAYS <= 0:
        return True
    n = stats.get("active_days_7")
    return n is not None and int(n) >= MIN_ACTIVE_DAYS


def has_min_balance(address):
    if MIN_WALLET_USD <= 0:
        return True
    usd = wallet_usd(address)
    return usd is not None and usd >= MIN_WALLET_USD


def prune_low_balance():
    """Remove auto-discovered wallets whose balance fell below MIN_WALLET_USD.
    A wallet with an open copy stays until that copy is closed. Returns removed addresses."""
    if MIN_WALLET_USD <= 0:
        return []
    with _LOCK:
        d = _load()
        busy = _busy(d)
        auto = [a for a, w in d["wallets"].items() if w.get("auto_discovered") and a not in busy and not in_test(w)
                and not mirror(a, w)]
    removed = []
    for a in auto:
        usd = wallet_usd(a)
        if usd is None:
            continue
        with _LOCK:
            d = _load()
            w = d["wallets"].get(a)
            if not w:
                continue
            w["balance_usd"] = round(usd, 2)
            st = w.get("stats") or {}
            small = st.get("average_buy_lamports") is not None and not big_enough_buyer(st)
            small = small or (st.get("age_days") is not None and not old_enough(st))
            small = small or (st.get("active_days_7") is not None and not active_daily(st))
            if (usd < MIN_WALLET_USD or small) and a not in _busy(d):
                d["wallets"].pop(a)
                removed.append(a)
            _save(d)
    _reject(removed)
    for a in removed:
        _event("removi " + a[:6] + "…: saldo < US$ " + format(MIN_WALLET_USD, ".0f") +
               ", compras < US$ " + format(MIN_BUY_USD, ".0f") + ", menos de " + format(MIN_WALLET_DAYS, ".0f") +
               " dias ou sem operar quase todo dia")
    return removed


def score(stats):
    """How good a wallet looks from its history: profit (SOL) x win rate x experience."""
    trades = int(stats.get("closed_trades", 0))
    if trades <= 0:
        return 0.0
    profit = max(0.0, int(stats.get("realized_lamports", 0)) / 1e9)
    win = float(stats.get("win_rate", 0.0))
    return round(profit * win * min(trades, 25) ** 0.5, 6)


def _record_stats(w, candidate):
    w["stats"] = {"closed_trades": int(candidate.get("closed_trades", 0)),
                  "win_rate": float(candidate.get("win_rate", 0.0)),
                  "realized_lamports": int(candidate.get("realized_lamports", 0)),
                  "average_hold_seconds": int(candidate.get("average_hold_seconds", 0)),
                  "average_buy_lamports": candidate.get("average_buy_lamports"),
                  "age_days": candidate.get("age_days"),
                  "active_days_7": candidate.get("active_days_7"),
                  "updated_at": int(time.time())}
    w["score"] = score(candidate)


def _bad_auto_wallet(w):
    """An auto-discovered wallet that should give its place to a better one."""
    status, _ = holder_status(w)
    judged = w.get("copies", 0) >= MIN_COPIES_TO_JUDGE and w.get("pnl_usd", 0.0) <= 0
    return bool(w.get("auto_discovered")) and (w.get("paused") or status == "fast" or judged)


def replace_worst(candidate):
    """When all auto slots are taken, swap the worst auto wallet (paused, fast holder or losing
    for us) for a new candidate that passes the same filters. Wallets with an open copy stay."""
    address = str(candidate.get("wallet") or "")
    holds = [max(0, int(x)) for x in candidate.get("hold_samples", [])]
    if (len(holds) < MIN_ROUND_TRIPS or _median(holds) < MIN_HOLD_SECONDS or long_share(holds) < MIN_LONG_SHARE
            or int(candidate.get("closed_trades", 0)) < MIN_ROUND_TRIPS
            or int(candidate.get("realized_lamports", 0)) <= 0):
        return False
    if (not old_enough(candidate) or not active_daily(candidate) or not big_enough_buyer(candidate)
            or not has_min_balance(address)):
        return False
    with _LOCK:
        d = _load()
        if address in d["wallets"]:
            return False
        busy = _busy(d)
        bad = [(w.get("pnl_usd", 0.0), a) for a, w in d["wallets"].items()
               if _bad_auto_wallet(w) and a not in busy and not in_test(w) and not mirror(a, w)]
        if bad:
            _, worst = min(bad)
        else:
            # Self-analysis: a wallet that has not proven itself with our own copies yet
            # (fewer than MIN_COPIES_TO_JUDGE) gives its place to a clearly better candidate.
            new_score = score(candidate)
            weak = [(w.get("score", 0.0), a) for a, w in d["wallets"].items()
                    if w.get("auto_discovered") and a not in busy and w.get("copies", 0) < MIN_COPIES_TO_JUDGE
                    and not in_test(w) and not mirror(a, w)]
            if not weak:
                return False
            low, worst = min(weak)
            if new_score <= max(low, 1e-9) * REPLACE_MARGIN:
                return False
        d["wallets"].pop(worst)
        _save(d)
    REPLACED[address] = worst
    if not auto_follow(candidate):
        return False
    _event("troquei " + worst[:6] + "… por " + address[:6] + "… (melhor nota)")
    return True


def refresh_stats(candidate):
    """Update the stored history/score of a wallet we already follow."""
    address = str(candidate.get("wallet") or "")
    with _LOCK:
        d = _load()
        w = d["wallets"].get(address)
        if not w or not w.get("auto_discovered"):
            return False
        _record_stats(w, candidate)
        _save(d)
    return True


def wallet_card(address):
    """Full description of a followed wallet, for the 'new wallet' notice."""
    w = _load()["wallets"].get(address)       # panel: atomic file, no need to wait the lock
    if not w:
        return "solscan.io/account/" + address
    st = w.get("stats") or {}
    lines = ["👛 solscan.io/account/" + address,
             "Nota " + format(w.get("score", 0.0), ".3f")]
    if st:
        lines.append("Lucro no histórico: " + format(st.get("realized_lamports", 0) / 1e9, "+.3f") + " SOL · acerto " +
                     format(st.get("win_rate", 0) * 100, ".0f") + "% · " + str(st.get("closed_trades", 0)) + " operações")
        hold = st.get("average_hold_seconds")
        extra = []
        if hold:
            extra.append("segura em média " + format(hold / 60, ".0f") + " min")
        if st.get("average_buy_lamports") is not None:
            extra.append("compra média " + format(int(st["average_buy_lamports"]) / 1e9, ".2f") + " SOL")
        if extra:
            lines.append(" · ".join(extra).capitalize())
        extra = []
        if st.get("age_days") is not None:
            extra.append("opera há " + format(float(st["age_days"]), ".0f") + "+ dias")
        if st.get("active_days_7") is not None:
            extra.append("ativa " + str(st["active_days_7"]) + "/7 dias")
        if w.get("balance_usd") is not None:
            extra.append("saldo US$ " + format(w["balance_usd"], ".0f"))
        if extra:
            lines.append(" · ".join(extra).capitalize())
    au = w.get("audit") or {}
    if "pnl_recent" in au:
        lines.append("Últimos " + format(RESULT_DAYS, ".0f") + " dias: " + format(au["pnl_recent"], "+.0f") + " USD em " +
                     str(au["trades_recent"]) + " operações · acerto " + format(au["win_recent"] * 100, ".0f") + "%")
    old = REPLACED.pop(address, None)
    if old:
        lines.append("↔️ Entrou no lugar de " + old[:6] + "…" + old[-4:] + " (nota menor ou resultado ruim)")
    return "\n".join(lines)


def wallets_text():
    """Followed wallets ranked by score, with their history and our copy results."""
    d = _load()       # panel: atomic file, no need to wait the lock
    rows = sorted(d["wallets"].items(), key=lambda kv: kv[1].get("score", 0.0), reverse=True)
    active = sum(1 for _, w in rows if not w.get("paused"))
    copies = sum(w.get("copies", 0) for _, w in rows)
    pnl = sum(w.get("pnl_usd", 0.0) for _, w in rows)
    lines = ["👛 CARTEIRAS COPIADAS: " + str(active) + " ativas de " + str(len(rows)) +
             " (limite automático " + str(MAX_AUTO_WALLETS) + ")",
             "Cópias feitas: " + str(copies) + " · resultado das cópias: " + format(pnl, "+.2f") + " USD",
             "Ordenadas pela nota (lucro × acerto × experiência). Carteira sem resultado é trocada "
             "por outra com nota " + format(REPLACE_MARGIN, "g") + "× maior.",
             "Pausa automática: " + str(FAST_TRADES_TO_PAUSE) + " trades de menos de " + format(MIN_HOLD_SECONDS / 60, ".0f") +
             " min, " + str(LOSS_STREAK_TO_PAUSE) + " cópias seguidas no prejuízo, ou −US$ " + format(TEST_MAX_LOSS_USD, ".0f") +
             " no teste. ▶️ Retomar volta a copiar na hora (📐 espelho nunca pausa sozinha).",
             "Saldo mínimo da carteira copiada: US$ " + format(MIN_WALLET_USD, ".0f") + " em SOL (abaixo disso é removida).",
             "Compra mínima: só copia quando a carteira coloca ≥ US$ " + format(MIN_BUY_USD, ".0f") + " na moeda.",
             ("Convicção: só copia quando ela coloca pelo menos " + format(CONVICTION_MULT, "g") +
              "× o que costuma colocar numa moeda." if CONVICTION_MULT > 0 else "Convicção: desligada."),
             "Idade mínima: carteira operando há ≥ " + format(MIN_WALLET_DAYS, ".0f") + " dias.",
             "Ativa: operou em ≥ " + str(MIN_ACTIVE_DAYS) + " dos últimos 7 dias."]
    for i, (addr, w) in enumerate(rows, 1):
        st = w.get("stats") or {}
        status, med = holder_status(w)
        icon = "⏸️" if w.get("paused") else ("✅ copiando" if status == "ok" or mirror(addr, w) or in_test(w)
                                              else ("👀 observando" if status == "watching" else "⚡ vende rápido"))
        if mirror(addr, w):
            icon += (" 📐 espelho: copia tudo, mesma % (" + format(MIRROR_MIN_FRACTION * 100, ".0f") + "%–" +
                     format(MIRROR_MAX_FRACTION * 100, ".0f") + "% da conta, máx. " +
                     format(MIRROR_MAX_COIN_FRACTION * 100, ".0f") + "% por posição)")
        elif proportional(addr, w):
            icon += " 📐 copia a % dela"
        if w.get("paused"):
            icon += " pausada (" + {"fast": "trades de menos de " + format(MIN_HOLD_SECONDS / 60, ".0f") + " min",
                                    "losses": str(w.get("loss_streak", LOSS_STREAK_TO_PAUSE)) + " perdas seguidas",
                                    "manual": "por você"
                                    }.get(w.get("pause_reason"), "prejuízo nas cópias") + ")"
        hist = ""
        if st:
            hist = (" · histórico " + format(st.get("realized_lamports", 0) / 1e9, "+.3f") + " SOL, acerto " +
                    format(st.get("win_rate", 0) * 100, ".0f") + "%, " + str(st.get("closed_trades", 0)) + " ops")
            if st.get("age_days") is not None:
                hist += ", ativa há " + format(float(st["age_days"]), ".0f") + "+ dias"
            if st.get("active_days_7") is not None:
                hist += ", operou " + str(st["active_days_7"]) + "/7 dias"
            if st.get("average_buy_lamports") is not None:
                hist += ", compra média " + format(int(st["average_buy_lamports"]) / 1e9, ".2f") + " SOL"
        bal = (" · saldo US$ " + format(w["balance_usd"], ".0f")) if w.get("balance_usd") is not None else ""
        typical = typical_buy_usd(w, offline=True)   # panel: never wait on the network
        if typical:
            bal += " · costuma colocar US$ " + format(typical, ".0f")
        if proven(w) and not in_test(w):
            bal += " · ✅ aprovada no teste"
        if in_test(w) and w.get("followed_at"):
            left = TEST_DAYS * 86400 - (time.time() - float(w["followed_at"]))
            bal += " · 🧪 em teste (faltam " + (format(left / 3600, ".0f") + " h" if left < 2 * 86400 else
                                              format(left / 86400, ".1f") + " dias") + ")"
        if w.get("clone"):
            ex = w.get("exit") or {}
            bal += (" · 🧬 CLONE: TP +" + format(float(ex.get("tp") or TAKE_PROFIT) * 100, ".0f") + "%, SL -" +
                    format(float(ex.get("sl") or STOP_LOSS) * 100, ".0f") + "%" +
                    (", máx. " + format(ex["max_hold_s"] / 60, ".0f") + " min" if ex.get("max_hold_s") else ""))
        lines.append(str(i) + ". " + icon + " nota " + format(w.get("score", 0.0), ".3f") + bal + hist +
                     " · segura " + format(med / 60, ".0f") + " min · cópias " + str(w.get("copies", 0)) +
                     " (" + str(w.get("wins", 0)) + " no lucro) · P/L " + format(w.get("pnl_usd", 0.0), "+.2f") +
                     " USD\n   solscan.io/account/" + addr)
    if not rows:
        lines.append("Nenhuma carteira ainda. A busca automática adiciona as melhores sozinha.")
    return "\n".join(lines)


def adopt_orphans():
    """An open copy whose wallet is no longer followed (removed before it sold) would never be sold:
    put that wallet back as watch-only (no new buys) so we sell together with it; it leaves again
    when the copy closes. Returns the wallets put back."""
    back, orphans = [], []
    with _LOCK:
        d = _load()
        for mint, p in d["positions"].items():
            w = p.get("wallet")
            if w and w not in d["wallets"]:
                d["wallets"][w] = {"last_sig": None, "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False,
                                   "open_buys": {}, "holds": [], "leaving": True}
                back.append(w)
                orphans.append((mint, w))
        if back:
            _save(d)
    for w in back:
        _event("voltei a vigiar " + w[:6] + "… só para vender junto (posição aberta sem dono)")
    ADOPTED.extend(orphans)
    return back


ADOPTED = []   # (mint, wallet) put back by adopt_orphans: check once whether the wallet already sold


SELL_RETRY_S = 30
# the copied wallet "closed" the coin when it keeps 5% or less of the most it held (leftover dust)
LEADER_CLOSED_SHARE = _env("COPY_LEADER_CLOSED_SHARE", 0.05)
WATCH_EVERY_S = 60
_WATCH = {"at": 0.0}


def _want_sell(mint, reason, why=""):
    """Mark a copied position to be sold (the copied wallet already sold). True the first time."""
    with _LOCK:
        d = _load()
        p = d["positions"].get(mint)
        if not p:
            return False
        first = not p.get("sell_wanted")
        p.setdefault("sell_wanted", time.time())
        p["sell_reason"] = reason
        p["sell_tried"] = time.time()
        if why:
            p["sell_error"] = why
        _save(d)
        return first


def retry_wanted_sells(send=None, chat=None):
    """Positions whose copied wallet already sold but our sale did not go through: try again."""
    msgs = []
    with _LOCK:
        wanted = [(m, p) for m, p in _load()["positions"].items() if p.get("sell_wanted")]
    for mint, p in wanted:
        if time.time() - float(p.get("sell_tried") or 0) < SELL_RETRY_S:
            continue
        with _LOCK:
            d = _load()
            if mint in d["positions"]:
                d["positions"][mint]["sell_tried"] = time.time()
                _save(d)
        msgs += _close(mint, p.get("sell_reason") or "copy_follow", send, chat)
    return msgs


def watch_copied_balances(send=None, chat=None, now=None):
    """Every minute: if the copied wallet no longer holds the coin (sold in a way we missed, sold to
    USDT, many transactions at once, or moved the coins away), sell ours too."""
    now = now or time.time()
    if now - _WATCH["at"] < WATCH_EVERY_S:
        return []
    _WATCH["at"] = now
    msgs = []
    with _LOCK:
        positions = {m: p for m, p in _load()["positions"].items() if not p.get("sell_wanted") and not p.get("pending_buy")
                     and now - float(p.get("selling_since") or 0) > 180}     # a sale being confirmed: don't send another
    for mint, p in positions.items():
        w = p.get("wallet")
        if not w or now - float(p.get("opened_at") or now) < 90:      # give a fresh copy time to settle
            continue
        try:
            held = wallet_token_balance(w, mint)
        except Exception:
            continue
        top = max(int(p.get("leader_max") or 0), held)
        with _LOCK:
            d = _load()
            if mint in d["positions"]:
                d["positions"][mint].update(leader_max=top, leader_held=held, leader_checked=now)   # shown in the panel
                _save(d)
        if held == 0 or (top > 0 and held <= LEADER_CLOSED_SHARE * top):
            _event(w[:6] + "… fechou " + mint[:6] + " (tem " + format(held / top * 100 if top else 0, ".0f") +
                   "% do que tinha): vendendo a cópia")
            logging.info("copied wallet %s no longer holds %s: selling", w[:6], mint[:8])
            _want_sell(mint, "copy_follow")
            msgs += _close(mint, "copy_follow", send, chat)
    return msgs


def wallet_token_balance(wallet, mint):
    from solana_settlement import get_token_balance
    return get_token_balance(wallet, mint)


def sell_if_already_sold(send=None, chat=None):
    """For positions whose wallet was just put back: if that wallet holds none of the coin anymore,
    it sold while we were not watching, so sell now."""
    msgs = []
    while ADOPTED:
        mint, w = ADOPTED.pop()
        try:
            held = wallet_token_balance(w, mint)
        except Exception:
            continue
        if held == 0:
            _event(w[:6] + "… já tinha vendido " + mint[:6] + ": vendi também")
            msgs += _close(mint, "copy_follow", send, chat)
    return msgs


_RECOVER = {"at": 0.0}


def recover_real_orphans(force=False):
    """Real coins the live engine holds but the copy trader lost track of (e.g. a buy that was
    confirmed late). Find which followed wallet still holds the coin and sell together with it;
    if none does, give the position a safety exit (target / stop) so it never stays open forever.
    Runs at most every 5 minutes. Returns messages."""
    if not force and time.time() - _RECOVER["at"] < 300:
        return []
    _RECOVER["at"] = time.time()
    import live_trade_state as st
    msgs = []
    # coins the bot wallet really holds but no record knows about: register them so they can be sold
    try:
        import live_engine
        import wallet_holdings
        owner = live_engine.get_address()
        held = wallet_holdings.raw_balances(owner) if owner else {}
        worth = wallet_holdings.coins_held(owner, fresh=True) if owner else {}
    except Exception:
        held, worth = {}, {}
    live = st._load()["positions"]
    with _LOCK:
        known_copy = set(_load()["positions"])
    for mint, usd in worth.items():
        if mint not in live and mint not in known_copy and held.get(mint):
            st.adopt_position(mint, held[mint], usd, mint[:6], mode="copy")
            msgs.append("📥 " + mint[:6] + "… (US$ " + format(usd, ".0f") + ") estava na carteira sem registro: "
                        "registrei para o bot poder vender.")
    live = st._load()["positions"]
    with _LOCK:
        d = _load()
        known = set(d["positions"])
        wallets = list(d["wallets"])
    for mint, lp in live.items():
        if mint in known or lp.get("mode") == "clone" or lp.get("copy_recovered"):
            continue
        holder = lp.get("copy_wallet")
        if not holder:
            for w in wallets:
                try:
                    if wallet_token_balance(w, mint) > 0:
                        holder = w
                        break
                except Exception:
                    continue
        if holder:
            with _LOCK:
                d = _load()
                d["positions"][mint] = {"wallet": holder, "real": True, "opened_at": float(lp.get("opened_at") or time.time())}
                if holder not in d["wallets"]:
                    d["wallets"][holder] = {"last_sig": None, "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False,
                                            "open_buys": {}, "holds": [], "leaving": True}
                _save(d)
            st.set_position_fields(mint, mode="copy", copy_wallet=holder, copy_recovered=True)
            msgs.append("🔁 Recuperei " + str(lp.get("symbol") or mint[:6]) + ": vendo junto quando " + holder[:6] + "…" +
                        holder[-4:] + " vender.")
        else:
            st.set_position_fields(mint, mode="copy", copy_orphan=True, copy_recovered=True)
            msgs.append("⚠️ " + str(lp.get("symbol") or mint[:6]) + " está na carteira sem carteira de origem conhecida "
                        "(nenhuma carteira seguida ainda tem essa moeda). Saída de segurança: " + orphan_exit_text() + ".")
    return msgs


def orphan_exit_text():
    from live_exit_monitor import ORPHAN_EXIT
    return ("stop −" + format(float(ORPHAN_EXIT["stop_loss"]) * 100, ".0f") + "% · a partir de +" +
            format((float(ORPHAN_EXIT["arm"]) - 1) * 100, ".0f") + "% stop móvel (vende " +
            format(float(ORPHAN_EXIT["drop"]) * 100, ".0f") + "% abaixo do topo)")


def positions_text():
    """Open copies, where each came from and whether that wallet is being watched."""
    d = _load()       # panel: atomic file, no need to wait the lock
    try:
        import live_trade_state as st
        live = st._load()["positions"]
    except Exception:
        live = {}
    extra = [(m, lp) for m, lp in live.items() if m not in d["positions"] and lp.get("mode") != "clone"]
    if not d["positions"] and not extra:
        return "📦 Nenhuma posição copiada aberta."
    lines = ["📦 POSIÇÕES COPIADAS ABERTAS: " + str(len(d["positions"]) + len(extra))]
    for m, lp in extra:
        how = ("🛟 saída de segurança: " + orphan_exit_text() +
               (" · 🚀 stop móvel ATIVO" if lp.get("trail_armed") else "") if lp.get("copy_orphan") else
               "🔎 procurando a carteira de origem (até 5 min)")
        lines.append("• " + str(lp.get("symbol") or m[:6]) + " 💵 REAL · sem carteira de origem registrada\n  " + how +
                     "\n  solscan.io/token/" + m)
    for mint, p in d["positions"].items():
        w = p.get("wallet") or "?"
        info = d["wallets"].get(w)
        watch = ("👀 vigiando a carteira — vende quando ela vender" if info else
                 "⚠️ carteira fora da lista — volta a ser vigiada na próxima rodada")
        if info and info.get("leaving"):
            watch += " (só até esta venda)"
        if p.get("leader_checked") and p.get("leader_max"):
            share = int(p.get("leader_held") or 0) / int(p["leader_max"]) * 100
            watch += "\n  📊 a carteira ainda tem " + format(share, ".0f") + "% do que tinha (conferido há " + \
                     str(int((time.time() - float(p["leader_checked"])) // 60)) + " min) · vendo quando cair a ≤ " + \
                     format(LEADER_CLOSED_SHARE * 100, ".0f") + "%"
        if p.get("selling_since") and time.time() - float(p["selling_since"]) < 600:
            watch = "⏳ venda enviada, confirmando na blockchain"
        if p.get("sell_wanted"):
            watch = "⏳ a carteira já vendeu — tentando vender a cada " + str(SELL_RETRY_S) + " s" + \
                    (" (último erro: " + str(p.get("sell_error"))[:80] + ")" if p.get("sell_error") else "")
        held = (time.time() - float(p.get("opened_at") or time.time())) / 60
        lines.append("• " + mint[:6] + "… " + ("💵 REAL" if p.get("real") else "🧪 SIM") +
                     (" · " + str(p["buys"]) + " compras" if int(p.get("buys") or 1) > 1 else "") + " · aberta há " +
                     (format(held, ".0f") + " min" if held < 120 else format(held / 60, ".1f") + " h") +
                     " · de " + w[:6] + "…" + w[-4:] + "\n  " + watch +
                     "\n  👛 Carteira copiada (toque para copiar):\n  " + w +
                     "\n  solscan.io/token/" + mint)
    return "\n".join(lines)


def wallet_buttons(addresses):
    """One row per copied wallet: copy its address with one tap, or open it on Solscan."""
    rows, seen = [], set()
    for w in addresses:
        if not w or w == "?" or w in seen:
            continue
        seen.add(w)
        short = w[:4] + "…" + w[-4:]
        rows.append([{"text": "📋 Copiar " + short, "copy_text": {"text": w}},
                     {"text": "🔎 Ver " + short, "url": "https://solscan.io/account/" + w}])
    return rows[:10]


def manage_buttons():
    """Copied wallets list: copy / view / pause-or-resume, one row per wallet (in list order)."""
    d = _load()
    rows = sorted(d["wallets"].items(), key=lambda kv: kv[1].get("score", 0.0), reverse=True)
    out = []
    for i, (w, info) in enumerate(rows[:30], 1):
        # the first button says the state AND what a tap does: paused -> "▶️ COPIAR", active -> "✅ copiando · pausar"
        toggle = ({"text": str(i) + ". ▶️ COPIAR", "callback_data": "cw_on:" + w} if info.get("paused")
                  else {"text": str(i) + ". ✅ copiando · pausar", "callback_data": "cw_off:" + w})
        out.append([toggle, {"text": "📋 endereço", "copy_text": {"text": w}},
                    {"text": "🔎 Ver", "url": "https://solscan.io/account/" + w}])
    return out


def set_paused(address, paused):
    """Owner's choice: pause (no new copies; open copies still sell with it) or resume a wallet.
    A manual pause is never undone automatically; resuming clears the automatic counters."""
    with _LOCK:
        d = _load()
        w = d["wallets"].get(address)
        if w is None:
            return None
        if paused:
            w["paused"], w["pause_reason"] = True, "manual"
        else:
            w["paused"] = False
            w.pop("pause_reason", None)
            w["proportional"] = True            # a wallet the owner resumes copies the same % it puts in each coin
            w["loss_streak"], w["live_holds"] = 0, []      # don't re-pause at once for old reasons
        _save(d)
    _event(("⏸️ pausei " if paused else "▶️ retomei ") + address[:6] + "… (escolha do dono)")
    return True


def positions_wallets():
    return [p.get("wallet") for p in _load()["positions"].values()]


def unfollow(address):
    """Stop following. With an open copy, the wallet stays only to sell together with it
    (no new buys) and leaves when that copy closes. Returns True if removed now."""
    with _LOCK:
        d = _load()
        if address in _busy(d) and address in d["wallets"]:
            d["wallets"][address]["leaving"] = True
            _save(d)
            return False
        d["wallets"].pop(address, None)
        _save(d)
        return True


# ---------------------------------------------------------------- chain reading
def _rpc(method, params):
    from live_swap_executor import rpc
    return rpc(method, params)


def _order(input_mint, output_mint, amount):
    from jupiter_v2_executor import order
    return order(input_mint, output_mint, int(amount))


_SOL_PRICE = {"at": 0.0, "usd": None}


def _sol_usd():
    """SOL price in USD, cached 60 s (one network call, not one per wallet / per tap).
    If the price service fails, the last price from the past hour is used."""
    now = time.monotonic()
    if _SOL_PRICE["usd"] and now - _SOL_PRICE["at"] < 60:
        return _SOL_PRICE["usd"]
    import live_order_bridge
    try:
        usd = float(live_order_bridge.sol_usd_price())
    except Exception:
        if _SOL_PRICE["usd"] and now - _SOL_PRICE["at"] < 3600:
            return _SOL_PRICE["usd"]
        raise
    _SOL_PRICE.update(at=now, usd=usd)
    return usd


def sol_usd_cached():
    """Last known SOL price without any network call (None if never fetched)."""
    return _SOL_PRICE["usd"]


def signal_from_tx(tx, wallet):
    """('buy'|'sell', mint) from the wallet's own balance changes, or None."""
    parsed = _parse_tx(tx, wallet)
    return parsed[:2] if parsed else None


def sell_fraction(tx, wallet):
    """Share of its holding that the wallet sold in this transaction (1.0 = everything)."""
    parsed = _parse_tx(tx, wallet)
    if not parsed or parsed[0] != "sell":
        return None
    pre = getattr(_parse_tx, "last_pre", 0)
    sold = -parsed[4] if len(parsed) > 4 else 0
    return 1.0 if pre <= 0 else max(0.0, min(1.0, sold / pre))


def buy_value_usd(tx, wallet):
    """USD the wallet paid for the token in a buy transaction (SOL/WSOL + USDC), or None."""
    parsed = _parse_tx(tx, wallet)
    if not parsed or parsed[0] != "buy":
        return None
    _, _, sol_change, usdc_change, _ = parsed
    try:
        price = _sol_usd()
    except Exception:
        return None
    return max(0, -sol_change) / 1e9 * price + max(0, -usdc_change) / 1e6


def _parse_tx(tx, wallet):
    """(side, mint, SOL+WSOL change in lamports, USDC change in raw units) or None."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return None
    deltas = {}
    pre_amounts = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            amount = int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
            deltas[b.get("mint")] = deltas.get(b.get("mint"), 0) + sign * amount
            if sign < 0:
                pre_amounts[b.get("mint")] = pre_amounts.get(b.get("mint"), 0) + amount
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    sol_delta = 0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol_delta = post[i] - pre[i]
    quote_change = sol_delta + deltas.get(SOL_MINT, 0) + deltas.get(USDC_MINT, 0) * 1000
    tokens = {m: v for m, v in deltas.items() if m and m not in QUOTE_MINTS and v != 0}
    if len(tokens) != 1:
        return None
    mint, change = next(iter(tokens.items()))
    _parse_tx.last_pre = pre_amounts.get(mint, 0)
    sol_change = sol_delta + deltas.get(SOL_MINT, 0)
    usdc_change = deltas.get(USDC_MINT, 0)
    if change > 0 and quote_change < 0:
        return "buy", mint, sol_change, usdc_change, change
    if change < 0 and quote_change > 0:
        return "sell", mint, sol_change, usdc_change, change
    return None


def new_signals(wallet, info):
    """New BUY/SELL signals of one wallet since the last look (oldest first)."""
    sigs = _rpc("getSignaturesForAddress", [wallet, {"limit": 50}]) or []
    if not sigs:
        return []
    newest = sigs[0]["signature"]
    if info.get("last_sig") is None:
        info["last_sig"] = newest    # first look: start from now
        return []
    fresh, found = [], False
    for _page in range(4):           # up to 200 transactions since the last look: busy wallets don't lose sells
        for s in sigs:
            if s["signature"] == info["last_sig"]:
                found = True
                break
            if s.get("err") is None:
                fresh.append(s["signature"])
        if found or len(sigs) < 50:
            break
        sigs = _rpc("getSignaturesForAddress", [wallet, {"limit": 50, "before": sigs[-1]["signature"]}]) or []
        if not sigs:
            break
    info["last_sig"] = newest
    out = []
    for sig in reversed(fresh):
        tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": int(os.getenv("SOLANA_MAX_TX_VERSION", "1")),
                                           "commitment": "confirmed"}])
        if not tx:
            # not available on the node yet: read this wallet again next round (last_sig is not saved)
            raise RuntimeError("transaction not available yet")
        signal = signal_from_tx(tx, wallet)
        if signal and signal[0] == "buy":
            BUY_FRACTION[(wallet, signal[1])] = buy_fraction(tx, wallet)
        if signal:
            out.append(signal + (buy_value_usd(tx, wallet) if signal[0] == "buy" else sell_fraction(tx, wallet),))
    return out


def safety_reason(mint):
    hit = _safety_cache.get(mint)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        info = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        parsed = (((info or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
        data = parsed.get("info") or {}
        if parsed.get("type") != "mint":
            reason = "não é um token válido"
        elif data.get("mintAuthority"):
            reason = "criador ainda pode criar moedas"
        elif data.get("freezeAuthority"):
            reason = "criador pode congelar carteiras"
        else:
            supply = int(_rpc("getTokenSupply", [mint])["value"]["amount"])
            largest = sorted((int(a["amount"]) for a in _rpc("getTokenLargestAccounts", [mint])["value"]), reverse=True)
            share = sum(largest[1:11]) / supply if supply else 1.0
            reason = ("10 maiores donos têm " + format(share * 100, ".0f") + "%") if share >= MAX_TOP_HOLDERS else ""
            if not reason:
                lamports = int(ORDER_USD / _sol_usd() * 1e9)
                tokens = int(_order(SOL_MINT, mint, lamports)["outAmount"])
                back = int(_order(mint, SOL_MINT, tokens)["outAmount"])
                ratio = back / lamports if lamports else 0
                reason = ("teste de venda devolveu " + format(ratio * 100, ".0f") + "%") if ratio < MIN_ROUNDTRIP else ""
    except Exception as exc:
        return "verificação indisponível (" + type(exc).__name__ + ")"
    _safety_cache[mint] = (time.monotonic(), reason)
    return reason


# ---------------------------------------------------------------- trading
def _real_result(mint, opened_at):
    """Profit of THIS real copy: the live engine's sale of the coin closed after the copy was opened.
    None if there is no such record (never credit an old trade of the same coin)."""
    import live_trade_state as st
    since = float(opened_at or 0) - 5
    sales = [h for h in st.history() if h.get("mint") == mint and float(h.get("closed_at") or 0) >= since]
    if not sales or any(h.get("pnl_usd") is None for h in sales):
        return None
    return sum(float(h["pnl_usd"]) for h in sales)       # partial sales of this copy + the final one


def reset_bad_scores_once():
    """Copy results counted before the fix were wrong (failed buys and old trades counted as losing
    copies): start every wallet's count again, unpause them and give them a new 3-day test. Once."""
    with _LOCK:
        d = _load()
        if d.get("score_v2"):
            return False
        for w in d["wallets"].values():
            w.update(copies=0, wins=0, pnl_usd=0.0, paused=False, followed_at=time.time())
            w.pop("proven", None)
        d["score_v2"] = True
        _save(d)
    _event("contagem das cópias corrigida: carteiras zeradas e em novo teste de 3 dias")
    return True


def _score(d, wallet, pnl):
    w = d["wallets"].get(wallet)
    if not w:
        return None
    w["copies"] += 1
    w["wins"] += 1 if pnl > 0 else 0
    w["pnl_usd"] = round(w["pnl_usd"] + pnl, 4)
    w["loss_streak"] = int(w.get("loss_streak", 0)) + 1 if pnl < 0 else 0
    if w["paused"] or mirror(wallet, w):       # mirror wallet: never paused automatically
        return None
    if w["loss_streak"] >= LOSS_STREAK_TO_PAUSE:
        # too many losing copies in a row: pause, also during the 3-day test
        w["paused"], w["pause_reason"] = True, "losses"
        return ("⏸️ Pausei " + wallet[:6] + "…" + wallet[-4:] + ": " + str(w["loss_streak"]) + " cópias seguidas no "
                "prejuízo (P/L total " + format(w["pnl_usd"], "+.2f") + " USD). Posições abertas dela vendem quando ela vender.")
    if in_test(w):
        # 3-day test: keep copying; pause only if its copies lost a lot
        if w["pnl_usd"] <= -TEST_MAX_LOSS_USD:
            w["paused"] = True
            return ("⏸️ Parei de copiar " + wallet[:6] + "… no teste: perdeu muito (" + str(w["copies"]) +
                    " cópias, P/L " + format(w["pnl_usd"], "+.2f") + " USD, limite −US$ " + format(TEST_MAX_LOSS_USD, ".0f") + ")")
        return None
    if w["copies"] >= MIN_COPIES_TO_JUDGE and w["pnl_usd"] < 0:
        w["paused"] = True
        return "⏸️ Parei de copiar " + wallet[:6] + "…: " + str(w["copies"]) + " cópias, P/L " + format(w["pnl_usd"], "+.2f") + " USD"
    return None


def unpause_test_wallets():
    """Wallets in the 3-day test that were paused without losing a lot go back to being copied."""
    back = []
    with _LOCK:
        d = _load()
        for a, w in d["wallets"].items():
            if w.get("paused") and w.get("pause_reason") != "manual" and mirror(a, w):
                w["paused"] = False
                w.pop("pause_reason", None)
                back.append(a)
            elif (w.get("paused") and w.get("pause_reason") not in ("fast", "losses", "manual") and in_test(w)
                    and float(w.get("pnl_usd", 0.0)) > -TEST_MAX_LOSS_USD):
                w["paused"] = False
                back.append(a)
        if back:
            _save(d)
    for a in back:
        _event("▶️ voltei a copiar " + a[:6] + "… (em teste, sem prejuízo grande)")
    return back


QUEUE_MAX_AGE_S = _env("COPY_QUEUE_MAX_SECONDS", 180)
_BUY_Q = deque()
_BUY_QLOCK = threading.Lock()
_BUY_EVENT = threading.Event()
_WORKER = {"thread": None}


def queue_real_buy(mint, wallet, send, chat, usd=None, now=None):
    """Real copy buys run one at a time in a worker thread, so reading the wallets never stops while a
    buy is being confirmed; a buy waits in the queue while another order is in progress."""
    with _BUY_QLOCK:
        if any(e[0] == mint and e[1] == wallet for e in _BUY_Q):
            return False
        _BUY_Q.append((mint, wallet, send, chat, usd, now or time.time()))
    if _WORKER["thread"] is None:
        process_buy_queue(wait=False)      # no worker running (tests / not started): buy right away
    else:
        _BUY_EVENT.set()
    return True


def start_buy_worker():
    """Started once by the Telegram bot: real copy buys then run off the wallet-reading loop."""
    if _WORKER["thread"] is None or not _WORKER["thread"].is_alive():
        _WORKER["thread"] = threading.Thread(target=_buy_worker, daemon=True)
        _WORKER["thread"].start()


def process_buy_queue(sleep=time.sleep, max_steps=None, wait=True):
    """Buy what is queued, oldest first. Waits while an order is pending; drops a buy that is too old
    or whose wallet already sold the coin."""
    import live_trade_state as st
    steps = 0
    while max_steps is None or steps < max_steps:
        steps += 1
        with _BUY_QLOCK:
            if not _BUY_Q:
                return
            mint, wallet, send, chat, usd, at = _BUY_Q[0]
        age = time.time() - at
        if age > QUEUE_MAX_AGE_S:
            with _BUY_QLOCK:
                _BUY_Q.popleft()
            _event("não deu tempo de copiar " + mint[:6] + " de " + wallet[:6] + "… (fila: ordem anterior demorou)")
            if send and chat:
                send(chat, "⚠️ Não consegui copiar a compra de " + wallet[:6] + "…" + wallet[-4:] + " em " + mint[:6] +
                     "… a tempo: outra ordem demorou mais de " + format(QUEUE_MAX_AGE_S / 60, ".0f") + " min para confirmar.")
            continue
        try:
            busy = bool(st.pending_orders())
        except Exception:
            busy = False
        if busy:
            if not wait:
                return                     # the worker / next call buys it once the order is released
            sleep(2)                       # another order is being confirmed: wait for it
            continue
        if age > 20:
            try:
                held = wallet_token_balance(wallet, mint)
            except Exception:
                held = None
            if held == 0:                  # it already sold the coin while we waited: don't buy
                with _BUY_QLOCK:
                    _BUY_Q.popleft()
                continue
        with _BUY_QLOCK:
            _BUY_Q.popleft()
        try:
            m = _open(mint, wallet, send, chat, usd=usd, direct=True)
            if m and send and chat:
                send(chat, m)
        except Exception as exc:
            logging.warning("queued copy buy failed for %s: %s", mint[:8], type(exc).__name__)


def _buy_worker():
    while True:
        _BUY_EVENT.wait(5)
        _BUY_EVENT.clear()
        try:
            process_buy_queue()
        except Exception as exc:
            logging.warning("copy buy worker: %s", type(exc).__name__)


MIRROR_INFO = {}   # mirror wallet -> {"read": time, "signal": text, "skip": text} (shown in the status)


def _skip(mint, wallet, reason, send=None, chat=None):
    """A buy of a copied wallet was not copied: log it and, for a mirror wallet, tell the owner why."""
    text = "não copiei a compra de " + wallet[:6] + "…" + wallet[-4:] + " em " + mint[:6] + "…: " + reason
    _event(text)
    logging.warning("copy skipped %s from %s: %s", mint[:8], wallet[:6], reason)
    if mirror(wallet):
        MIRROR_INFO.setdefault(wallet, {})["skip"] = time.strftime("%H:%M") + " " + mint[:6] + "…: " + reason
        if send and chat:
            try:
                send(chat, "⚠️ NÃO COPIEI uma compra da carteira espelho " + wallet[:6] + "…" + wallet[-4:] +
                     "\nMoeda: " + mint + "\nMotivo: " + reason)
            except Exception:
                pass
    return None


def _open(mint, wallet, send, chat, usd=None, direct=False):
    """Copy a buy (simulated or real). Returns a message or None. usd: custom size (proportional wallet).
    Real buys are queued for the buy worker unless direct=True (the worker itself)."""
    size = float(usd or ORDER_USD)
    real = real_enabled() and send is not None and chat is not None
    if real and not direct:
        queue_real_buy(mint, wallet, send, chat, usd)
        return None
    with _LOCK:
        mine = _load()["positions"].get(mint)
    # same wallet buying the same coin again: buy again (add to our copy), up to MAX_BUYS_PER_COIN buys
    add_on = bool(mine) and mine.get("wallet") == wallet and bool(mine.get("real")) == real
    if mine and not add_on:
        return _skip(mint, wallet, "essa moeda já está aberta copiada de outra carteira", send, chat)
    if add_on and int(mine.get("buys") or 1) >= MAX_BUYS_PER_COIN:
        return _skip(mint, wallet, "já fiz " + str(MAX_BUYS_PER_COIN) + " compras dessa moeda (máximo)", send, chat)
    if add_on and (mine.get("pending_buy") or mine.get("sell_wanted") or mine.get("selling_since")):
        return _skip(mint, wallet, "a compra/venda anterior dessa moeda ainda está confirmando", send, chat)
    if real:
        import live_engine
        import live_trade_state as st
        if not live_engine.ready():
            return _skip(mint, wallet, "robô real parado: " + str(live_engine.why_stopped() or "?"), send, chat)
        allowed, why_not = st.can_open(mint if add_on else None)
        live = st._load()["positions"]
        open_now = len(live)
        if not allowed:
            return _skip(mint, wallet, "robô real não pode abrir agora (" + str(why_not) + ")", send, chat)
        if add_on:
            if mint not in live:
                return _skip(mint, wallet, "a posição dessa moeda não está mais aberta no robô", send, chat)
        elif open_now >= MAX_OPEN_TOTAL or st.positions_by_mode("copy") >= MAX_POSITIONS:
            return _skip(mint, wallet, "limite de " + str(MAX_OPEN_TOTAL) + " posições abertas atingido (" +
                         str(open_now) + " abertas)", send, chat)
        elif mint in live:
            return _skip(mint, wallet, "essa moeda já está aberta por outro robô (sniper/consenso)", send, chat)
        import wallet_holdings
        ok, why = wallet_holdings.can_buy(mint, live_engine.get_address(), add_on=add_on)
        if not ok:
            return _skip(mint, wallet, why, send, chat)
    elif not add_on:
        with _LOCK:
            d = _load()
            if len(d["positions"]) >= MAX_POSITIONS or open_sim_total() >= MAX_OPEN_TOTAL:
                return _skip(mint, wallet, "limite de posições da simulação", send, chat)
    why = safety_reason(mint)
    if why:
        return _skip(mint, wallet, "anti-golpe: " + why, send, chat)
    if real:
        import live_engine
        pair = {"baseToken": {"address": mint, "symbol": mint[:6]}}
        settled = live_engine._execute_buy(pair, mint, mint[:6], send, chat, live_engine.get_address(), None, "copy",
                                           usd=size, add_on=add_on)
        if not settled:
            return _skip(mint, wallet, "a compra na Jupiter falhou (veja a mensagem de erro da compra)", send, chat)
        pending = settled.get("status") != "settled_buy"
        with _LOCK:
            d = _load()
            if add_on and mint in d["positions"]:
                d["positions"][mint]["buys"] = int(d["positions"][mint].get("buys") or 1) + 1
            else:
                # also when the buy is still being confirmed: the live engine finishes it later, and we must
                # remember which wallet to sell together with
                d["positions"][mint] = {"wallet": wallet, "real": True, "opened_at": time.time(), "pending_buy": pending}
            _save(d)
        ex = _exit_for(wallet)
        try:
            import live_trade_state as st
            st.set_position_fields(mint, mode="copy", copy_wallet=wallet, copy_tp=ex["tp"], copy_sl=ex["sl"],
                                   copy_max_hold_s=ex["max_hold_s"])
        except Exception:
            pass
        _event("REAL copiou " + ("nova compra (" + str(int(_load()["positions"].get(mint, {}).get("buys") or 1)) +
               "ª) " if add_on else "compra ") + "de " + wallet[:6] + "… em " + mint[:6])
        return None   # the live engine already reported the buy
    try:
        lamports = int(size / _sol_usd() * 1e9)
        tokens = int(_order(SOL_MINT, mint, lamports)["outAmount"])
    except Exception as exc:
        LAST_ERROR["text"] = "cotação de compra falhou: " + type(exc).__name__
        return None
    cost = size + NETWORK_FEE_USD
    with _LOCK:
        d = _load()
        if d["balance"] < cost:
            return None
        d["balance"] -= cost
        ex = d["wallets"].get(wallet, {}).get("exit") or {}
        q = d["positions"].get(mint)
        if add_on and q:
            q["tokens"] = int(q["tokens"]) + tokens
            q["cost_usd"] = float(q["cost_usd"]) + cost
            q["buys"] = int(q.get("buys") or 1) + 1
            n = q["buys"]
        else:
            d["positions"][mint] = {"wallet": wallet, "real": False, "tokens": tokens, "cost_usd": cost,
                                    "opened_at": time.time(), "tp": ex.get("tp"), "sl": ex.get("sl"),
                                    "max_hold_s": ex.get("max_hold_s")}
            n = 1
        _save(d)
    _event("SIM copiou " + (str(n) + "ª compra" if n > 1 else "compra") + " de " + wallet[:6] + "… em " + mint[:6])
    return ("👥 SIM copiou " + (str(n) + "ª compra da mesma moeda" if n > 1 else "compra") + " de " + wallet[:6] + "…: " +
            mint + " · US$ " + format(cost, ".2f"))


def _close(mint, reason, send=None, chat=None):
    """Close one copied position (wallet sold, stop or target). Returns messages."""
    msgs = []
    with _LOCK:
        pos = _load()["positions"].get(mint)
    if not pos:
        return msgs
    if pos.get("real"):
        import live_engine
        import live_trade_state as st
        try:
            result = live_engine.sell_now(mint, "copy_follow" if reason == "copy_follow" else reason, send, chat)
        except Exception as exc:
            why = type(exc).__name__ + ": " + str(exc)[:160]
            LAST_ERROR["text"] = "venda real falhou: " + why
            logging.warning("copy sell failed for %s: %s", mint[:8], why)
            first = _want_sell(mint, reason, why)
            if first:
                msgs.append("⚠️ A carteira copiada vendeu " + mint[:6] + "…, mas a minha venda falhou (" + why[:120] +
                            "). Vou tentar de novo a cada " + str(SELL_RETRY_S) + " s até vender.")
            return msgs
        if result == "pending":
            # sent, still being confirmed: keep the copy; sync_real_closed credits the real result later
            with _LOCK:
                d = _load()
                if mint in d["positions"]:
                    d["positions"][mint].pop("sell_wanted", None)
                    d["positions"][mint]["selling_since"] = time.time()
                    _save(d)
            return msgs
        pnl = _real_result(mint, pos.get("opened_at"))      # this position's own sale; None = unknown
    else:
        try:
            value = _order(mint, SOL_MINT, int(pos["tokens"]))
            net = int(value["outAmount"]) / 1e9 * _sol_usd() - NETWORK_FEE_USD
        except Exception as exc:
            LAST_ERROR["text"] = "cotação de venda falhou: " + type(exc).__name__
            return msgs
        pnl = net - float(pos["cost_usd"]) + float(pos.get("realized_usd", 0.0))
    with _LOCK:
        d = _load()
        if mint not in d["positions"]:
            return msgs
        d["positions"].pop(mint)
        if not pos.get("real"):
            d["balance"] += pnl + float(pos["cost_usd"]) - float(pos.get("realized_usd", 0.0))
        d["history"] = (d["history"] + [{"mint": mint, "wallet": pos["wallet"], "reason": reason,
                                         "pnl_usd": None if pnl is None else round(pnl, 4), "real": bool(pos.get("real")),
                                         "closed_at": time.time()}])[-300:]
        paused = _score(d, pos["wallet"], pnl) if pnl is not None else None
        w = d["wallets"].get(pos["wallet"])
        if w and w.get("leaving") and pos["wallet"] not in _busy(d):
            d["wallets"].pop(pos["wallet"])
        _save(d)
    if not pos.get("real"):
        msgs.append("👥 SIM " + ("💰" if pnl > 0 else "🔻") + " vendeu " + mint[:6] + " (" +
                    {"copy_follow": "a carteira vendeu", "take_profit": "alvo", "stop_loss": "stop"}.get(reason, reason) +
                    "): " + format(pnl, "+.2f") + " USD")
    if paused:
        msgs.append(paused)
    return msgs


def _sell_part_real(mint, fraction, send=None, chat=None):
    """Real copy: the copied wallet sold part of the coin -> sell the same part (the rest stays)."""
    import live_engine
    try:
        result = live_engine.sell_part(mint, fraction, "copy_follow", send, chat)
    except Exception as exc:
        why = type(exc).__name__ + ": " + str(exc)[:160]
        LAST_ERROR["text"] = "venda parcial falhou: " + why
        logging.warning("copy partial sell failed for %s: %s", mint[:8], why)
        return ["⚠️ A carteira copiada vendeu " + format(fraction * 100, ".0f") + "% de " + mint[:6] +
                "…, mas a minha venda parcial falhou (" + why[:120] + "). O resto vende junto quando ela vender."]
    if result == "gone":
        return _close(mint, "copy_follow", send, chat)
    _event("vendeu " + format(fraction * 100, ".0f") + "% de " + mint[:6] + " junto com a carteira")
    return []


def _sell_part(mint, fraction):
    """Simulation: sell the same share the copied wallet sold."""
    with _LOCK:
        pos = _load()["positions"].get(mint)
    if not pos or fraction <= 0:
        return []
    part = int(int(pos["tokens"]) * fraction)
    if part <= 0:
        return []
    try:
        net = int(_order(mint, SOL_MINT, part)["outAmount"]) / 1e9 * _sol_usd() - NETWORK_FEE_USD
    except Exception as exc:
        LAST_ERROR["text"] = "cotação de venda falhou: " + type(exc).__name__
        return []
    with _LOCK:
        d = _load()
        p = d["positions"].get(mint)
        if not p:
            return []
        cost_part = float(p["cost_usd"]) * fraction
        p["tokens"] = int(p["tokens"]) - part
        p["cost_usd"] = float(p["cost_usd"]) - cost_part
        p["realized_usd"] = float(p.get("realized_usd", 0.0)) + net - cost_part
        d["balance"] += net
        _save(d)
    return ["👥 SIM vendeu " + format(fraction * 100, ".0f") + "% de " + mint[:6] + " junto com a carteira: " +
            format(net - cost_part, "+.2f") + " USD"]


def sim_exits():
    """Stop / target for simulated copies (real ones use the live exit monitor)."""
    msgs = []
    if EXIT_MODE == "wallet":
        return msgs           # exit only when the copied wallet sells
    with _LOCK:
        positions = {m: p for m, p in _load()["positions"].items() if not p.get("real")}
    for mint, pos in positions.items():
        try:
            value = int(_order(mint, SOL_MINT, int(pos["tokens"]))["outAmount"]) / 1e9 * _sol_usd()
        except Exception:
            continue
        change = (value - NETWORK_FEE_USD) / float(pos["cost_usd"]) - 1
        tp, sl = float(pos.get("tp") or TAKE_PROFIT), float(pos.get("sl") or STOP_LOSS)
        max_hold = pos.get("max_hold_s")
        if change >= tp:
            msgs += _close(mint, "take_profit")
        elif change <= -sl:
            msgs += _close(mint, "stop_loss")
        elif max_hold and time.time() - float(pos.get("opened_at", time.time())) > float(max_hold):
            msgs += _close(mint, "time_stop")
    return msgs


def sync_real_closed():
    """Credit real copies the exit monitor already sold (stop/target) to their wallet."""
    import live_trade_state as st
    held = set(st._load()["positions"])
    msgs = []
    with _LOCK:
        d = _load()
        for m, p in d["positions"].items():
            if p.get("pending_buy") and m in held:
                p["pending_buy"] = False                     # the pending buy got confirmed
                st.set_position_fields(m, mode="copy", copy_wallet=p["wallet"])
        gone = [m for m, p in d["positions"].items() if p.get("real") and m not in held
                and not (p.get("pending_buy") and time.time() - float(p.get("opened_at", 0)) < 900)]
        for mint in gone:
            pos = d["positions"].pop(mint)
            if pos.get("pending_buy"):
                continue                       # the buy never went through: not a copy, nothing to credit
            pnl = _real_result(mint, pos.get("opened_at"))
            d["history"] = (d["history"] + [{"mint": mint, "wallet": pos["wallet"], "reason": "stop/alvo",
                                             "pnl_usd": None if pnl is None else round(pnl, 4), "real": True,
                                             "closed_at": time.time()}])[-300:]
            paused = _score(d, pos["wallet"], pnl) if pnl is not None else None
            if paused:
                msgs.append(paused)
        if gone:
            _save(d)
    return msgs


def _median(values):
    v = sorted(values)
    return v[len(v) // 2] if v else 0.0


def long_share(holds):
    """Fraction of positions held at least MIN_HOLD_SECONDS."""
    return sum(1 for h in holds if h >= MIN_HOLD_SECONDS) / len(holds) if holds else 0.0


def holder_status(w):
    """('ok'|'watching'|'fast', median hold seconds) from the wallet's round trips.
    ok = median >= 5 min AND at least MIN_LONG_SHARE of its positions held >= 5 min."""
    holds = w.get("holds") or []
    if len(holds) < MIN_ROUND_TRIPS:
        return "watching", _median(holds)
    med = _median(holds)
    ok = med >= MIN_HOLD_SECONDS and long_share(holds) >= MIN_LONG_SHARE
    return ("ok" if ok else "fast"), med


def measure(address, rpc, sol_usd, max_tx=None):
    """Read the wallet's history: (hold seconds per closed position, audit dict)."""
    import wallet_profile
    _, closed, _ = wallet_profile.analyze(address, rpc, sol_usd, max_tx=max_tx or AUDIT_MAX_TX)
    holds = wallet_profile.holds_seconds(closed)
    now = time.time()
    recent = [p for p in closed if p.get("closed", 0) >= now - RESULT_DAYS * 86400]
    audit = {"at": int(now), "positions": len(closed), "median_s": int(_median(holds)),
             "long_share": round(long_share(holds), 3),
             "pnl_recent": round(sum(p.get("pnl_usd", 0.0) for p in recent), 2),
             "trades_recent": len(recent),
             "win_recent": round(sum(1 for p in recent if p.get("pnl_usd", 0) > 0) / len(recent), 3) if recent else 0.0}
    return holds, audit


def audit_wallet(address, rpc, sol_usd, max_tx=None):
    """Measure the wallet's real hold time per coin and recent results, and store them.
    Returns (status, median_seconds, share_long, positions)."""
    holds, audit = measure(address, rpc, sol_usd, max_tx)
    with _LOCK:
        d = _load()
        w = d["wallets"].get(address)
        if w is None:
            return None
        if holds:
            w["holds"] = holds
        w["audit"] = audit
        _save(d)
        status, med = holder_status(w)
    return status, med, long_share(holds), audit["positions"]


def _reject(addresses, now=None):
    if not addresses:
        return
    until = (now or time.time()) + REJECT_DAYS * 86400
    with _LOCK:
        d = _load()
        rej = {a: t for a, t in (d.get("rejected") or {}).items() if t > time.time()}
        for a in addresses:
            rej[a] = until
        d["rejected"] = dict(list(rej.items())[-3000:])
        _save(d)


def is_rejected(address, now=None):
    with _LOCK:
        return (_load().get("rejected") or {}).get(address, 0) > (now or time.time())


def prefilter(candidate):
    """Cheap checks from the discovery data (no network)."""
    holds = [max(0, int(x)) for x in candidate.get("hold_samples", [])]
    return (len(holds) >= MIN_ROUND_TRIPS and _median(holds) >= MIN_HOLD_SECONDS and
            long_share(holds) >= MIN_LONG_SHARE and int(candidate.get("closed_trades", 0)) >= MIN_ROUND_TRIPS and
            int(candidate.get("realized_lamports", 0)) > 0 and old_enough(candidate) and
            active_daily(candidate) and big_enough_buyer(candidate))


def preaudit_and_follow(candidate, rpc, sol_usd):
    """Audit a discovered wallet BEFORE following it. Returns 'added', 'replaced', 'rejected' or 'full'.
    Rejected wallets cannot come back for REJECT_DAYS."""
    address = str(candidate.get("wallet") or "")
    if not has_min_balance(address):
        _reject([address])
        return "rejected"
    holds, audit = measure(address, rpc, sol_usd)
    probe = {"holds": holds, "audit": audit}
    if holder_status(probe)[0] != "ok" or result_status(probe) != "ok":
        _reject([address])
        return "rejected"
    cand = dict(candidate, hold_samples=holds or candidate.get("hold_samples", []))
    outcome = "added"
    with _LOCK:
        d = _load()
        auto = [(((w.get("audit") or {}).get("pnl_recent", 0.0)), a) for a, w in d["wallets"].items()
                if w.get("auto_discovered")]
        busy = _busy(d)
        full = len(auto) >= MAX_AUTO_WALLETS or len(d["wallets"]) >= MAX_WALLETS
        if full:
            free = sorted((pnl, a) for pnl, a in auto if a not in busy and not in_test(d["wallets"][a])
                          and not mirror(a, d["wallets"][a])
                          and not proven(d["wallets"][a]))
            if not free or audit["pnl_recent"] < REPLACE_PNL_MARGIN * max(free[0][0], 0.01):
                return "full"            # good, but not clearly better than the weakest approved wallet
            d["wallets"].pop(free[0][1])
            REPLACED[address] = free[0][1]
            _save(d)
            outcome = "replaced"
    if not auto_follow(cand):
        return "full"
    with _LOCK:
        d = _load()
        w = d["wallets"].get(address)
        if w is not None:
            w["holds"] = holds or w.get("holds", [])
            w["audit"] = audit
            _save(d)
    if outcome == "replaced":
        _reject([REPLACED[address]])
    return outcome


def result_status(w, now=None):
    """'ok' | 'bad' | 'unknown' for the wallet's own results in the last RESULT_DAYS days
    (from the latest audit; an audit older than AUDIT_VALID_S counts as unknown)."""
    a = w.get("audit") or {}
    now = now or time.time()
    if not a or now - a.get("at", 0) > AUDIT_VALID_S or "pnl_recent" not in a:
        return "unknown"
    ok = (a["trades_recent"] >= RESULT_MIN_TRADES and a["win_recent"] >= RESULT_MIN_WIN
          and a["pnl_recent"] > RESULT_MIN_USD)
    return "ok" if ok else "bad"


REMOVAL_REASON = {}   # address -> why it was removed (for the Telegram notice)


def proven(w):
    """Profitable for US: at least one copy and the copies' total result above zero."""
    return int(w.get("copies", 0)) >= 1 and float(w.get("pnl_usd", 0.0)) > 0


def remove_fast_wallets():
    """After its TEST_DAYS test, a wallet stays only if our copies of it made money ("aprovada no
    teste"); otherwise it leaves (loss, zero result or no copy at all) and can't come back for
    REJECT_DAYS, so the automatic search puts a new one in its place. A wallet with an open copy
    stays until that copy closes. Returns removed addresses."""
    ensure_test_start()
    removed = []
    with _LOCK:
        d = _load()
        busy = _busy(d)
        for a, w in list(d["wallets"].items()):
            if a in busy or in_test(w) or w.get("leaving") or mirror(a, w):
                continue
            if proven(w):
                if not w.get("proven"):
                    w["proven"] = True
                    _event("✅ " + a[:6] + "… aprovada no teste: cópias " + format(w["pnl_usd"], "+.2f") + " USD")
                continue
            d["wallets"].pop(a)
            removed.append(a)
            REMOVAL_REASON[a] = ("nenhuma cópia em " + format(TEST_DAYS, ".0f") + " dias" if not w.get("copies") else
                                 "cópias com resultado " + format(float(w.get("pnl_usd", 0.0)), "+.2f") + " USD")
        if removed or any(w.get("proven") for w in d["wallets"].values()):
            _save(d)
    _reject(removed)
    for a in removed:
        _event("removi " + a[:6] + "…: " + REMOVAL_REASON.get(a, "sem lucro"))
    return removed


def audit_line(address, res):
    """One report line for an audited wallet."""
    status, med, share, n = res
    with _LOCK:
        w = _load()["wallets"].get(address) or {}
    a = w.get("audit") or {}
    result = result_status(w) if w else "unknown"
    if w and in_test(w):
        icon = "🧪 EM TESTE (copiando)"
    elif w and proven(w):
        icon = "✅ APROVADA NO TESTE (cópias " + format(float(w.get("pnl_usd", 0.0)), "+.2f") + " USD)"
    elif w:
        icon = "🗑️ SAI (sem lucro nas cópias)"
    else:
        icon = "🗑️ REMOVIDA"
    return (icon + " " + address[:6] + "…" + address[-4:] + " · " + format(RESULT_DAYS, ".0f") + " dias: " +
            format(a.get("pnl_recent", 0.0), "+.0f") + " USD em " + str(a.get("trades_recent", 0)) + " ops, acerto " +
            format(a.get("win_recent", 0.0) * 100, ".0f") + "% · segura " + format(med / 60, ".1f") + " min · " +
            format(share * 100, ".0f") + "% ≥ " + format(MIN_HOLD_SECONDS / 60, ".0f") + " min")


def audit_all(rpc, sol_usd, max_tx=None):
    """Audit every followed wallet. Returns a Telegram report."""
    with _LOCK:
        wallets = list(_load()["wallets"])
    rows = []
    for a in wallets:
        try:
            res = audit_wallet(a, rpc, sol_usd, max_tx)
        except Exception as exc:
            rows.append("⚠️ " + a[:6] + "…" + a[-4:] + ": não consegui ler (" + type(exc).__name__ + ")")
            continue
        if res is None:
            continue
        rows.append(audit_line(a, res))
    remove_fast_wallets()
    ok = sum(1 for r in rows if r.startswith("✅") or r.startswith("🧪"))
    head = ("🔍 AUDITORIA DAS CARTEIRAS: " + str(ok) + " de " + str(len(rows)) + " sendo copiadas\n"
            "Regras: carteira nova entra só se passar nos últimos " + format(RESULT_DAYS, ".0f") + " dias (lucro, ≥ " +
            str(RESULT_MIN_TRADES) + " operações, acerto ≥ " + format(RESULT_MIN_WIN * 100, ".0f") + "%, segura ≥ " +
            format(MIN_HOLD_SECONDS / 60, ".0f") + " min); fica " + format(TEST_DAYS, ".0f") + " dias em teste; depois "
            "fica só se as cópias derem lucro.")
    return head + ("\n\n" + "\n".join(rows) if rows else "\nNenhuma carteira na lista.")


def typical_buy_usd(w, offline=False):
    """What this wallet usually puts in one coin (USD): median of its last observed buys,
    or its historical average buy while there are fewer than 3 observations."""
    sizes = [float(x) for x in (w.get("buy_sizes") or []) if x]
    if len(sizes) >= 3:
        return _median(sizes)
    lamports = (w.get("stats") or {}).get("average_buy_lamports")
    if lamports:
        try:
            price = sol_usd_cached() if offline else _sol_usd()
            return int(lamports) / 1e9 * price if price else None
        except Exception:
            return None
    return _median(sizes) if sizes else None


def _record_buy_size(wallet, usd):
    with _LOCK:
        d = _load()
        w = d["wallets"].get(wallet)
        if w is None:
            return
        w["buy_sizes"] = ((w.get("buy_sizes") or []) + [round(float(usd), 2)])[-30:]
        _save(d)


def _track_hold(wallet, side, mint, now):
    """Record the copied wallet's own buy time / hold duration."""
    with _LOCK:
        d = _load()
        w = d["wallets"].get(wallet)
        if not w:
            return
        w.setdefault("open_buys", {})
        w.setdefault("holds", [])
        msg = None
        if side == "buy":
            w["open_buys"].setdefault(mint, now)
        elif mint in w["open_buys"]:
            held = now - w["open_buys"].pop(mint)
            w["holds"] = (w["holds"] + [held])[-30:]
            w["live_holds"] = ((w.get("live_holds") or []) + [round(held)])[-5:]      # trades seen live
            fast = [h for h in w["live_holds"] if h < MIN_HOLD_SECONDS]
            if len(fast) >= FAST_TRADES_TO_PAUSE and not w.get("paused") and not mirror(wallet, w):
                w["paused"], w["pause_reason"] = True, "fast"
                msg = ("⏸️ Pausei " + wallet[:6] + "…" + wallet[-4:] + ": fez " + str(len(fast)) + " trades de menos de " +
                       format(MIN_HOLD_SECONDS / 60, ".0f") + " min (último: " + format(held / 60, ".1f") +
                       " min). Não copio mais as compras dela; posições abertas dela vendem quando ela vender.")
        if len(w["open_buys"]) > 200:
            w["open_buys"] = dict(list(w["open_buys"].items())[-100:])
        _save(d)
    if msg:
        _event(msg)
    return msg


def cycle(send=None, chat=None):
    """One pass over every followed wallet. Returns Telegram messages.
    With copying switched off, wallets that still have an open copy are watched anyway, only to
    sell together with them (there is no fixed TP/SL)."""
    owner_on = enabled()
    users_on = users_active()
    try:
        import consensus
        consensus_on = consensus.enabled()
    except Exception:
        consensus_on = False
    active = owner_on or users_on or consensus_on    # signals are read while anything uses them
    msgs = []
    reset_bad_scores_once()
    ensure_test_start()
    unpause_test_wallets()
    adopt_orphans()
    drop_finished_leaving()
    msgs += sell_if_already_sold(send, chat)
    msgs += retry_wanted_sells(send, chat)
    msgs += watch_copied_balances(send, chat)
    try:
        msgs += recover_real_orphans()
    except Exception as exc:
        LAST_ERROR["text"] = "recuperação de posições falhou: " + type(exc).__name__
    with _LOCK:
        d = _load()
        busy = _busy(d)
        wallets = {a: w for a, w in d["wallets"].items() if active or a in busy}
        # mirror wallets first: their buys are copied as fast as possible
        wallets = dict(sorted(wallets.items(), key=lambda kv: not mirror(kv[0], kv[1])))
    if not wallets:
        return msgs
    for wallet, info in wallets.items():
        try:
            signals = new_signals(wallet, info)
        except Exception as exc:
            LAST_ERROR["text"] = "leitura da carteira " + wallet[:6] + "… falhou: " + type(exc).__name__
            continue
        with _LOCK:
            d = _load()
            if wallet in d["wallets"]:
                d["wallets"][wallet]["last_sig"] = info.get("last_sig")
                _save(d)
        if mirror(wallet, info):
            MIRROR_INFO.setdefault(wallet, {})["read"] = time.time()
        for side, mint, extra in signals:
            paid_usd = extra if side == "buy" else None
            if mirror(wallet, info):
                MIRROR_INFO.setdefault(wallet, {})["signal"] = (time.strftime("%H:%M") + " " +
                                                               ("comprou " if side == "buy" else "vendeu ") + mint[:6] + "…")
                logging.info("mirror %s %s %s", wallet[:6], side, mint[:8])
            fast_msg = _track_hold(wallet, side, mint, time.time())
            if fast_msg:
                msgs.append(fast_msg)
            typical = None
            if side == "buy" and paid_usd:
                typical = typical_buy_usd(_load()["wallets"].get(wallet, {}))   # usual size BEFORE this buy (no lock: may hit the network)
                _record_buy_size(wallet, paid_usd)
            if (side == "buy" and consensus_on and not info.get("paused") and not info.get("leaving")
                    and (paid_usd or 0) >= MIN_BUY_USD):
                try:
                    consensus.record_buy(mint, wallet)          # consensus robot: how many wallets buy this coin
                except Exception as exc:
                    LAST_ERROR["text"] = "consenso falhou: " + type(exc).__name__
            if side == "sell":
                try:
                    import user_copy
                    user_copy.on_sell(mint, wallet, extra, send)      # users holding it sell too
                except Exception as exc:
                    LAST_ERROR["text"] = "venda dos usuários falhou: " + type(exc).__name__
                with _LOCK:
                    pos = _load()["positions"].get(mint)
                if pos and pos["wallet"] == wallet:
                    frac = 1.0 if extra is None else float(extra)
                    if not pos.get("real") and frac < 0.95:
                        msgs += _sell_part(mint, frac)        # same movement: sold part -> sell the same part
                    elif pos.get("real") and frac < 0.95:
                        msgs += _sell_part_real(mint, frac, send, chat)   # took part of the profit: same part
                    else:
                        msgs += _close(mint, "copy_follow", send, chat)
            elif (mirror(wallet, info) and not owner_on and not info.get("paused")):
                _skip(mint, wallet, "o copy trade está DESLIGADO (toque em 💵 Copiar (REAL))", send, chat)
            elif (owner_on or users_on) and not info.get("paused") and not info.get("leaving"):
                is_mirror = mirror(wallet, info)      # mirror wallet: copy EVERYTHING it does (anti-scam stays)
                with _LOCK:
                    status, med = holder_status(_load()["wallets"].get(wallet, {}))
                if status != "ok" and not is_mirror:
                    _event(("observando " if status == "watching" else "não copia (vende rápido) ") + wallet[:6] + "…")
                    continue
                with _LOCK:
                    wi = _load()["wallets"].get(wallet, {})
                    result = "ok" if in_test(wi) or is_mirror else result_status(wi)   # test period: keep copying
                if result != "ok":
                    _event("não copia " + wallet[:6] + "…: " + ("aguardando auditoria dos últimos " if result == "unknown"
                           else "sem bom resultado nos últimos ") + format(RESULT_DAYS, ".0f") + " dias")
                    continue
                prop = proportional(wallet, wi)
                if not is_mirror and MIN_BUY_USD > 0 and (paid_usd is None or paid_usd < MIN_BUY_USD):
                    _event("ignorou compra pequena de " + wallet[:6] + "…: " +
                           ("valor desconhecido" if paid_usd is None else "US$ " + format(paid_usd, ".0f")) +
                           " < US$ " + format(MIN_BUY_USD, ".0f"))
                    continue
                if CONVICTION_MULT > 0 and not is_mirror:
                    if typical and paid_usd < CONVICTION_MULT * typical:
                        _event("ignorou compra baixa de " + wallet[:6] + "…: US$ " + format(paid_usd, ".0f") +
                               " (ela costuma colocar US$ " + format(typical, ".0f") + ")")
                        continue
                if users_on and not safety_reason(mint):
                    try:
                        import user_copy
                        user_copy.on_buy(mint, wallet, send)          # every copying user buys too
                    except Exception as exc:
                        LAST_ERROR["text"] = "compra dos usuários falhou: " + type(exc).__name__
                if not owner_on:
                    continue
                size = None
                if prop:
                    frac = BUY_FRACTION.pop((wallet, mint), None)
                    free = value = None
                    try:
                        if real_enabled():
                            import live_engine
                            import live_trade_state as st
                            free = wallet_usd(live_engine.get_address(), max_age=0)
                            value = None if free is None else free + sum(
                                float(p.get("cost_usd") or 0) for p in st._load()["positions"].values())
                        else:
                            dd = _load()
                            free = dd["balance"]
                            value = free + sum(float(p.get("cost_usd") or 0) for p in dd["positions"].values()
                                               if not p.get("real"))
                    except Exception:
                        free = value = None
                    size = proportional_usd(frac, value, free, limits=fraction_limits(wallet, wi))
                    already = coin_cost_usd(mint, real_enabled()) if size else 0
                    coin_cap = MIRROR_MAX_COIN_FRACTION if is_mirror else PROPORTIONAL_MAX_COIN_FRACTION
                    if size and already:
                        size = add_on_room(size, already, value, coin_cap)
                        if size is None:
                            _skip(mint, wallet, "recompra: já tenho " + format(already / value * 100, ".0f") +
                                  "% da conta nessa moeda (máx. " + format(coin_cap * 100, ".0f") + "%)", send, chat)
                            continue
                    if size is None:
                        _skip(mint, wallet, ("não consegui medir quanto ela pagou" if not frac else
                                             "sem saldo livre (o tamanho da compra deu menos de US$ " +
                                             format(PROPORTIONAL_MIN_USD, ".0f") + ")"), send, chat)
                        continue
                    _event("proporcional " + wallet[:6] + "…: ela usou " + format(frac * 100, ".0f") + "% → compro US$ " +
                           format(size, ".2f") + " (" + format(size / value * 100, ".0f") + "% da carteira)")
                m = _open(mint, wallet, send, chat, usd=size)
                if m:
                    msgs.append(m)
    msgs += sim_exits()
    if real_enabled():
        try:
            msgs += sync_real_closed()
        except Exception:
            pass
    return msgs


# ---------------------------------------------------------------- panel
def status_text():
    d = _load()       # panel: atomic file, never waits for a background job
    mode = "REAL" if d["enabled"] and d["real"] else ("SIMULAÇÃO" if d["enabled"] else "DESLIGADO")
    lines = ["👥 COPY TRADE — " + mode,
             "US$ " + format(ORDER_USD, ".0f") + " por cópia · máx. " + str(MAX_POSITIONS) + " posições · " +
             ("sai só quando a carteira vender (mesmo movimento, sem TP/SL fixo)" if EXIT_MODE == "wallet" else
              "vende quando a carteira vender, stop −" + format(STOP_LOSS * 100, ".0f") + "%, alvo +" +
              format(TAKE_PROFIT * 100, ".0f") + "%"),
             "Só copia carteiras que seguram as moedas ≥ " + format(MIN_HOLD_SECONDS / 60, ".0f") +
             " min (mediana de " + str(MIN_ROUND_TRIPS) + "+ operações observadas)",
             "Simulação: saldo US$ " + format(d["balance"], ".2f") + " · posições abertas: " + str(len(d["positions"]))]
    if not d["wallets"]:
        lines.append("Nenhuma carteira seguida ainda: a busca automática adiciona as melhores sozinha (ou use /seguir ENDEREÇO).")
    for addr, w in d["wallets"].items():
        status, med = holder_status(w)
        hold = {"watching": "observando (" + str(len(w.get("holds") or [])) + "/" + str(MIN_ROUND_TRIPS) + " operações)",
                "fast": "segura só " + format(med / 60, ".1f") + " min → não copia",
                "ok": "segura " + format(med / 60, ".0f") + " min"}[status]
        icon = "⏸️ " if w["paused"] else ("✅ " if status == "ok" else ("👀 " if status == "watching" else "⚡ "))
        origin = " auto" if w.get("auto_discovered") else " manual"
        lines.append(icon + addr[:4] + "…" + addr[-4:] + origin + " · " + hold + " · cópias " + str(w["copies"]) +
                     " (" + str(w["wins"]) + " no lucro) · P/L " + format(w["pnl_usd"], "+.2f") + " USD")
    for addr, w in d["wallets"].items():
        if mirror(addr, w):
            info = MIRROR_INFO.get(addr, {})
            ago = int(time.time() - info["read"]) if info.get("read") else None
            lines.append("📐 Espelho " + addr[:4] + "…" + addr[-4:] + ": " +
                         ("lida há " + str(ago) + " s" if ago is not None else "ainda não lida desde o reinício") +
                         (" · último sinal " + info["signal"] if info.get("signal") else "") +
                         (" · última recusa " + info["skip"] if info.get("skip") else "") +
                         ("" if d["enabled"] else " · ⚠️ COPY DESLIGADO"))
    for t, text in list(EVENTS)[-3:]:
        lines.append("• " + text)
    if LAST_ERROR["text"]:
        lines.append("Último erro: " + LAST_ERROR["text"])
    return "\n".join(lines)
