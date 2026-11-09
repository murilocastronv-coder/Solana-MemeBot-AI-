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


ORDER_USD = _env("COPY_ORDER_USD", 20)   # US$ per copied position
MAX_POSITIONS = int(_env("COPY_MAX_POSITIONS", 3))
# Simulation: copy + strategy robot together never hold more than this many open orders.
MAX_OPEN_TOTAL = int(_env("MAX_OPEN_ORDERS", 3))
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
MAX_AUTO_WALLETS = int(_env("COPY_AUTO_MAX_WALLETS", 15))
# A new candidate replaces an unproven followed wallet only if its score is clearly better.
REPLACE_MARGIN = _env("COPY_REPLACE_MARGIN", 1.5)
# Only follow wallets holding at least this much (SOL balance in USD); remove them below it.
MIN_WALLET_USD = _env("COPY_MIN_WALLET_USD", 40)
BALANCE_MAX_AGE = 600
# Only copy buys where the wallet put at least this much in the coin (and only follow
# wallets whose average buy is at least this much).
MIN_BUY_USD = _env("COPY_MIN_BUY_USD", 40)
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
AUDIT_MAX_TX = int(_env("COPY_AUDIT_MAX_TX", 150))

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
        d["wallets"].setdefault(address, {"last_sig": None, "copies": 0, "wins": 0, "pnl_usd": 0.0, "paused": False,
                                          "open_buys": {}, "holds": []})
        d["wallets"][address]["paused"] = False
        _save(d)


def clone(address, summary, closed):
    """Follow a wallet chosen by the owner and exit like it does: its own measured TP, SL and
    max hold (from wallet_profile). Its entries are copied even if they are small or quick."""
    import wallet_profile
    follow(address)
    exit_cfg = {
        "tp": round(min(max(float(summary.get("suggest_tp") or TAKE_PROFIT), 0.05), 10.0), 4),
        "sl": round(min(max(float(summary.get("suggest_sl") or STOP_LOSS), 0.03), 0.9), 4),
        "max_hold_s": int(summary["suggest_max_hold_min"] * 60) if summary.get("suggest_max_hold_min") else None,
    }
    with _LOCK:
        d = _load()
        w = d["wallets"][address]
        w["clone"] = True
        w["exit"] = exit_cfg
        w["holds"] = (wallet_profile.holds_seconds(closed) or w.get("holds") or [])[-30:]
        w["profile"] = {k: summary.get(k) for k in ("positions", "win_rate", "pnl_usd", "tp_median", "sl_median",
                                                    "hold_median", "size_median")}
        _save(d)
    _event("clonou " + address[:6] + "…: TP +" + format(exit_cfg["tp"] * 100, ".0f") + "% · SL -" +
           format(exit_cfg["sl"] * 100, ".0f") + "%")
    return exit_cfg


def open_sim_total():
    """Simulated open orders of the copy trade + the strategy robot."""
    n = sum(1 for p in _load()["positions"].values() if not p.get("real"))
    try:
        import clone_strategy
        n += len(clone_strategy._load()["positions"])
    except Exception:
        pass
    return n


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
    with _LOCK:
        d = _load()
        if address in d["wallets"]:
            return False
        auto_count = sum(bool(w.get("auto_discovered")) for w in d["wallets"].values())
        if auto_count >= MAX_AUTO_WALLETS or len(d["wallets"]) >= MAX_WALLETS:
            return False
        d["wallets"][address] = {
            "last_sig": None, "copies": 0, "wins": 0, "pnl_usd": 0.0,
            "paused": False, "open_buys": {}, "holds": holds[-30:],
            "auto_discovered": True,
        }
        _record_stats(d["wallets"][address], candidate)
        usd = wallet_usd(address)
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
        busy = {p.get("wallet") for p in d["positions"].values()}
        auto = [a for a, w in d["wallets"].items() if w.get("auto_discovered") and a not in busy]
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
            if (usd < MIN_WALLET_USD or small) and a not in {p.get("wallet") for p in d["positions"].values()}:
                d["wallets"].pop(a)
                removed.append(a)
            _save(d)
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
        busy = {p.get("wallet") for p in d["positions"].values()}
        bad = [(w.get("pnl_usd", 0.0), a) for a, w in d["wallets"].items()
               if _bad_auto_wallet(w) and a not in busy]
        if bad:
            _, worst = min(bad)
        else:
            # Self-analysis: a wallet that has not proven itself with our own copies yet
            # (fewer than MIN_COPIES_TO_JUDGE) gives its place to a clearly better candidate.
            new_score = score(candidate)
            weak = [(w.get("score", 0.0), a) for a, w in d["wallets"].items()
                    if w.get("auto_discovered") and a not in busy and w.get("copies", 0) < MIN_COPIES_TO_JUDGE]
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
    with _LOCK:
        w = _load()["wallets"].get(address)
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
    old = REPLACED.pop(address, None)
    if old:
        lines.append("↔️ Entrou no lugar de " + old[:6] + "…" + old[-4:] + " (nota menor ou resultado ruim)")
    return "\n".join(lines)


def wallets_text():
    """Followed wallets ranked by score, with their history and our copy results."""
    with _LOCK:
        d = _load()
    rows = sorted(d["wallets"].items(), key=lambda kv: kv[1].get("score", 0.0), reverse=True)
    active = sum(1 for _, w in rows if not w.get("paused"))
    copies = sum(w.get("copies", 0) for _, w in rows)
    pnl = sum(w.get("pnl_usd", 0.0) for _, w in rows)
    lines = ["👛 CARTEIRAS COPIADAS: " + str(active) + " ativas de " + str(len(rows)) +
             " (limite automático " + str(MAX_AUTO_WALLETS) + ")",
             "Cópias feitas: " + str(copies) + " · resultado das cópias: " + format(pnl, "+.2f") + " USD",
             "Ordenadas pela nota (lucro × acerto × experiência). Carteira sem resultado é trocada "
             "por outra com nota " + format(REPLACE_MARGIN, "g") + "× maior; com " + str(MIN_COPIES_TO_JUDGE) +
             " cópias no prejuízo, é pausada.",
             "Saldo mínimo da carteira copiada: US$ " + format(MIN_WALLET_USD, ".0f") + " em SOL (abaixo disso é removida).",
             "Compra mínima: só copia quando a carteira coloca ≥ US$ " + format(MIN_BUY_USD, ".0f") + " na moeda.",
             "Idade mínima: carteira operando há ≥ " + format(MIN_WALLET_DAYS, ".0f") + " dias.",
             "Ativa: operou em ≥ " + str(MIN_ACTIVE_DAYS) + " dos últimos 7 dias."]
    for i, (addr, w) in enumerate(rows, 1):
        st = w.get("stats") or {}
        status, med = holder_status(w)
        icon = "⏸️" if w.get("paused") else ("✅" if status == "ok" else ("👀" if status == "watching" else "⚡"))
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


def unfollow(address):
    with _LOCK:
        d = _load()
        d["wallets"].pop(address, None)
        _save(d)


# ---------------------------------------------------------------- chain reading
def _rpc(method, params):
    from live_swap_executor import rpc
    return rpc(method, params)


def _order(input_mint, output_mint, amount):
    from jupiter_v2_executor import order
    return order(input_mint, output_mint, int(amount))


def _sol_usd():
    import live_order_bridge
    return float(live_order_bridge.sol_usd_price())


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
            for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
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
    sigs = _rpc("getSignaturesForAddress", [wallet, {"limit": 15}]) or []
    if not sigs:
        return []
    newest = sigs[0]["signature"]
    if info.get("last_sig") is None:
        info["last_sig"] = newest    # first look: start from now
        return []
    fresh = []
    for s in sigs:
        if s["signature"] == info["last_sig"]:
            break
        if s.get("err") is None:
            fresh.append(s["signature"])
    info["last_sig"] = newest
    out = []
    for sig in reversed(fresh):
        tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": int(os.getenv("SOLANA_MAX_TX_VERSION", "1")),
                                           "commitment": "confirmed"}])
        signal = signal_from_tx(tx, wallet)
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
def _score(d, wallet, pnl):
    w = d["wallets"].get(wallet)
    if not w:
        return None
    w["copies"] += 1
    w["wins"] += 1 if pnl > 0 else 0
    w["pnl_usd"] = round(w["pnl_usd"] + pnl, 4)
    if w["copies"] >= MIN_COPIES_TO_JUDGE and w["pnl_usd"] < 0 and not w["paused"]:
        w["paused"] = True
        return "⏸️ Parei de copiar " + wallet[:6] + "…: " + str(w["copies"]) + " cópias, P/L " + format(w["pnl_usd"], "+.2f") + " USD"
    return None


def _open(mint, wallet, send, chat):
    """Copy a buy (simulated or real). Returns a message or None."""
    real = real_enabled() and send is not None and chat is not None
    if real:
        import live_engine
        import live_trade_state as st
        if not live_engine.ready():
            return None
        allowed, _ = st.can_open()
        open_now = len(st._load()["positions"])
        if (not allowed or open_now >= MAX_OPEN_TOTAL or st.positions_by_mode("copy") >= MAX_POSITIONS
                or mint in st._load()["positions"]):
            return None
    with _LOCK:
        d = _load()
        if mint in d["positions"] or (not real and (len(d["positions"]) >= MAX_POSITIONS or
                                                    open_sim_total() >= MAX_OPEN_TOTAL)):
            return None
    why = safety_reason(mint)
    if why:
        _event("recusou cópia de " + wallet[:6] + "…: " + why)
        return None
    if real:
        import live_engine
        pair = {"baseToken": {"address": mint, "symbol": mint[:6]}}
        settled = live_engine._execute_buy(pair, mint, mint[:6], send, chat, live_engine.get_address(), None, "copy",
                                           usd=ORDER_USD)
        if not settled or settled.get("status") != "settled_buy":
            return None
        with _LOCK:
            d = _load()
            d["positions"][mint] = {"wallet": wallet, "real": True, "opened_at": time.time()}
            _save(d)
        ex = _exit_for(wallet)
        try:
            import live_trade_state as st
            st.set_position_fields(mint, copy_tp=ex["tp"], copy_sl=ex["sl"], copy_max_hold_s=ex["max_hold_s"])
        except Exception:
            pass
        _event("REAL copiou compra de " + wallet[:6] + "… em " + mint[:6])
        return None   # the live engine already reported the buy
    try:
        lamports = int(ORDER_USD / _sol_usd() * 1e9)
        tokens = int(_order(SOL_MINT, mint, lamports)["outAmount"])
    except Exception as exc:
        LAST_ERROR["text"] = "cotação de compra falhou: " + type(exc).__name__
        return None
    cost = ORDER_USD + NETWORK_FEE_USD
    with _LOCK:
        d = _load()
        if d["balance"] < cost:
            return None
        d["balance"] -= cost
        ex = d["wallets"].get(wallet, {}).get("exit") or {}
        d["positions"][mint] = {"wallet": wallet, "real": False, "tokens": tokens, "cost_usd": cost,
                                "opened_at": time.time(), "tp": ex.get("tp"), "sl": ex.get("sl"),
                                "max_hold_s": ex.get("max_hold_s")}
        _save(d)
    _event("SIM copiou compra de " + wallet[:6] + "… em " + mint[:6])
    return "👥 SIM copiou compra de " + wallet[:6] + "…: " + mint + " · US$ " + format(cost, ".2f")


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
        before = st.pnl_summary()["total_pnl_usd"]
        try:
            live_engine.sell_now(mint, "copy_follow" if reason == "copy_follow" else reason, send, chat)
        except Exception as exc:
            LAST_ERROR["text"] = "venda real falhou: " + type(exc).__name__
            return msgs
        pnl = st.pnl_summary()["total_pnl_usd"] - before
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
                                         "pnl_usd": round(pnl, 4), "real": bool(pos.get("real")),
                                         "closed_at": time.time()}])[-300:]
        paused = _score(d, pos["wallet"], pnl)
        _save(d)
    if not pos.get("real"):
        msgs.append("👥 SIM " + ("💰" if pnl > 0 else "🔻") + " vendeu " + mint[:6] + " (" +
                    {"copy_follow": "a carteira vendeu", "take_profit": "alvo", "stop_loss": "stop"}.get(reason, reason) +
                    "): " + format(pnl, "+.2f") + " USD")
    if paused:
        msgs.append(paused)
    return msgs


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
        gone = [m for m, p in d["positions"].items() if p.get("real") and m not in held]
        for mint in gone:
            pos = d["positions"].pop(mint)
            last = next((h for h in reversed(st.history()) if h.get("mint") == mint), {}) if hasattr(st, "history") else {}
            pnl = float(last.get("pnl_usd") or 0.0)
            d["history"] = (d["history"] + [{"mint": mint, "wallet": pos["wallet"], "reason": "stop/alvo",
                                             "pnl_usd": round(pnl, 4), "real": True, "closed_at": time.time()}])[-300:]
            paused = _score(d, pos["wallet"], pnl)
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


def audit_wallet(address, rpc, sol_usd, max_tx=None):
    """Measure the wallet's real hold time per coin from its history and store it.
    Returns (status, median_seconds, share_long, positions)."""
    import wallet_profile
    max_tx = max_tx or AUDIT_MAX_TX
    _, closed, _ = wallet_profile.analyze(address, rpc, sol_usd, max_tx=max_tx)
    holds = wallet_profile.holds_seconds(closed)
    with _LOCK:
        d = _load()
        w = d["wallets"].get(address)
        if w is None:
            return None
        if holds:
            w["holds"] = holds
        w["audit"] = {"at": int(time.time()), "positions": len(closed), "median_s": int(_median(holds)),
                      "long_share": round(long_share(holds), 3)}
        _save(d)
        status, med = holder_status(w)
    return status, med, long_share(holds), len(closed)


def remove_fast_wallets():
    """Remove every followed wallet that fails the 5-minute rule (it would never be copied),
    so the automatic search can fill the slot. A wallet with an open copy stays until it closes.
    Returns removed addresses."""
    removed = []
    with _LOCK:
        d = _load()
        busy = {p.get("wallet") for p in d["positions"].values()}
        for a, w in list(d["wallets"].items()):
            if a not in busy and holder_status(w)[0] == "fast":
                d["wallets"].pop(a)
                removed.append(a)
        if removed:
            _save(d)
    for a in removed:
        _event("removi " + a[:6] + "…: fecha posições em menos de " + format(MIN_HOLD_SECONDS / 60, ".0f") + " min")
    return removed


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
        status, med, share, n = res
        icon = {"ok": "✅ COPIA", "fast": "🗑️ REMOVIDA (rápida demais)", "watching": "👀 SEM DADOS"}[status]
        rows.append(icon + " " + a[:6] + "…" + a[-4:] + " · segura " + format(med / 60, ".1f") + " min (mediana) · " +
                    format(share * 100, ".0f") + "% das posições ≥ " + format(MIN_HOLD_SECONDS / 60, ".0f") + " min · " +
                    str(n) + " posições medidas")
    remove_fast_wallets()
    ok = sum(1 for r in rows if r.startswith("✅"))
    head = ("🔍 AUDITORIA DAS CARTEIRAS: " + str(ok) + " de " + str(len(rows)) + " liberadas para copiar\n"
            "Regra: segura ≥ " + format(MIN_HOLD_SECONDS / 60, ".0f") + " min (mediana) e ≥ " +
            format(MIN_LONG_SHARE * 100, ".0f") + "% das posições ≥ " + format(MIN_HOLD_SECONDS / 60, ".0f") +
            " min. As rápidas demais são removidas e a busca automática coloca outras no lugar.")
    return head + ("\n\n" + "\n".join(rows) if rows else "\nNenhuma carteira na lista.")


def _track_hold(wallet, side, mint, now):
    """Record the copied wallet's own buy time / hold duration."""
    with _LOCK:
        d = _load()
        w = d["wallets"].get(wallet)
        if not w:
            return
        w.setdefault("open_buys", {})
        w.setdefault("holds", [])
        if side == "buy":
            w["open_buys"].setdefault(mint, now)
        elif mint in w["open_buys"]:
            w["holds"] = (w["holds"] + [now - w["open_buys"].pop(mint)])[-30:]
        if len(w["open_buys"]) > 200:
            w["open_buys"] = dict(list(w["open_buys"].items())[-100:])
        _save(d)


def cycle(send=None, chat=None):
    """One pass over every followed wallet. Returns Telegram messages."""
    if not enabled():
        return []
    msgs = []
    with _LOCK:
        wallets = dict(_load()["wallets"])
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
        for side, mint, extra in signals:
            paid_usd = extra if side == "buy" else None
            _track_hold(wallet, side, mint, time.time())
            if side == "sell":
                with _LOCK:
                    pos = _load()["positions"].get(mint)
                if pos and pos["wallet"] == wallet:
                    frac = 1.0 if extra is None else float(extra)
                    if not pos.get("real") and frac < 0.95:
                        msgs += _sell_part(mint, frac)        # same movement: sold part -> sell the same part
                    else:
                        msgs += _close(mint, "copy_follow", send, chat)
            elif not info.get("paused"):
                with _LOCK:
                    status, med = holder_status(_load()["wallets"].get(wallet, {}))
                if status != "ok":
                    _event(("observando " if status == "watching" else "não copia (vende rápido) ") + wallet[:6] + "…")
                    continue
                if MIN_BUY_USD > 0 and (paid_usd is None or paid_usd < MIN_BUY_USD):
                    _event("ignorou compra pequena de " + wallet[:6] + "…: " +
                           ("valor desconhecido" if paid_usd is None else "US$ " + format(paid_usd, ".0f")) +
                           " < US$ " + format(MIN_BUY_USD, ".0f"))
                    continue
                m = _open(mint, wallet, send, chat)
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
    with _LOCK:
        d = _load()
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
    for t, text in list(EVENTS)[-3:]:
        lines.append("• " + text)
    if LAST_ERROR["text"]:
        lines.append("Último erro: " + LAST_ERROR["text"])
    return "\n".join(lines)
