"""Market making SOL/USDC — SIMULATION with real prices.

What it does
- Reads the real wallet value (SOL + USDC, read-only) to size the book:
  capital = min(wallet value, MM_CAPITAL_USD); 40% USDC for bids, 40% SOL for
  asks, 10% for fees, 10% safety reserve that is never quoted.
- Every tick (real SOL/USDC price from Jupiter) it keeps up to 4 limit orders:
  2 bids below and 2 asks above the mid. The spread adapts to volatility
  (0.4%..0.8%), the quotes lean against the inventory, and quoting pauses
  when volatility or a sharp drop makes it too risky.
- Orders are re-priced every MM_REPRICE_SECONDS if the mid moved; every
  place/cancel is charged a network fee, so re-pricing costs are visible.
- A resting order fills only when the price trades THROUGH it (conservative);
  a touch fills half (partial fill). Maker fee is applied on fills.
- P/L is the total book value (USDC + SOL at mid) against the start, so it
  includes inventory moves, not just "captured spread". Daily loss limit is
  checked on that total (realized + unrealized).

Nothing is signed or sent. A real executor (Phoenix/OpenBook limit orders)
is not wired: real mode stays off until it is built and validated.
"""
import json
import os
import statistics
import tempfile
import threading
import time
from collections import deque


def _env(name, default):
    return float(os.getenv(name, str(default)))


CAPITAL_USD = _env("MM_CAPITAL_USD", 50)
MIN_ORDER_USD = _env("MM_MIN_ORDER_USD", 5)
SPREAD_MIN = _env("MM_SPREAD_MIN", 0.004)
SPREAD_MAX = _env("MM_SPREAD_MAX", 0.008)
LEVEL_STEP = _env("MM_LEVEL_STEP", 0.002)          # 2nd order 0.2% further out
REPRICE_SECONDS = _env("MM_REPRICE_SECONDS", 10)
REPRICE_MOVE = _env("MM_REPRICE_MOVE", 0.0015)     # re-price only if mid moved 0.15%
TX_FEE_USD = _env("MM_TX_FEE_USD", 0.001)          # per place or cancel
MAKER_FEE = _env("MM_MAKER_FEE", 0.0)              # fraction of filled notional
DAILY_LOSS_USD = _env("MM_DAILY_LOSS_USD", 1)
VOL_PAUSE = _env("MM_VOL_PAUSE", 0.004)            # stdev of tick returns that pauses quoting
DROP_PAUSE = _env("MM_DROP_PAUSE", 0.02)           # 2% drop over the window pauses quoting
MAX_REPRICES_PER_HOUR = int(_env("MM_MAX_REPRICES_PER_HOUR", 120))

_LOCK = threading.RLock()
_prices = deque(maxlen=60)
_reprices = deque(maxlen=1000)


# ---------------------------------------------------------------- storage
def _path():
    import live_trade_state
    return live_trade_state._path().with_name("mm_sim.json")


def _default():
    return {"enabled": False, "usdc": 0.0, "sol": 0.0, "reserve_usd": 0.0, "fee_budget_usd": 0.0,
            "start_value": 0.0, "orders": [], "fills": [], "fees_usd": 0.0, "day": "", "day_start_value": 0.0,
            "paused": "", "last_reprice": 0.0, "last_mid": 0.0, "captured_usd": 0.0}


def _load():
    path = _path()
    if not path.exists():
        return _default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default()
    base = _default()
    base.update(data)
    return base


def _save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mm_sim.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


# ---------------------------------------------------------------- market data
def _mid():
    import live_order_bridge
    return float(live_order_bridge.sol_usd_price())


def _wallet_value(mid):
    """Real wallet value in USD (SOL + USDC), read-only."""
    from bot_wallet_service import get_address
    from solana_settlement import get_sol_balance, get_token_balance
    from jupiter_v2_executor import USDC_MINT
    owner = get_address()
    if not owner:
        raise RuntimeError("carteira do bot não configurada")
    sol = get_sol_balance(owner) / 1e9
    usdc = get_token_balance(owner, USDC_MINT) / 1e6
    return sol * mid + usdc


def volatility():
    if len(_prices) < 5:
        return 0.0
    p = list(_prices)
    returns = [p[i] / p[i - 1] - 1 for i in range(1, len(p))]
    return statistics.pstdev(returns)


def drop():
    return (1 - _prices[-1] / max(_prices)) if _prices else 0.0


def target_spread(vol):
    """0.4% when calm, widening with volatility up to 0.8%."""
    return max(SPREAD_MIN, min(SPREAD_MAX, SPREAD_MIN + 2 * vol))


# ---------------------------------------------------------------- engine
def start(mid=None, wallet_value=None):
    """Size the simulated book from the real wallet value and switch it on."""
    mid = _mid() if mid is None else mid
    value = _wallet_value(mid) if wallet_value is None else wallet_value
    capital = min(value, CAPITAL_USD)
    if capital < 4 * MIN_ORDER_USD / 0.8:
        raise RuntimeError("saldo da carteira (US$ " + format(value, ".2f") + ") pequeno demais para 4 ordens de US$ " +
                           format(MIN_ORDER_USD, ".0f"))
    with _LOCK:
        data = _default()
        data.update(enabled=True, usdc=capital * 0.4, sol=capital * 0.4 / mid, fee_budget_usd=capital * 0.1,
                    reserve_usd=capital * 0.1, start_value=capital, last_mid=mid)
        _save(data)
    _prices.clear()
    return capital


def stop():
    with _LOCK:
        data = _load()
        data["enabled"] = False
        data["orders"] = []
        _save(data)


def enabled():
    with _LOCK:
        return bool(_load()["enabled"])


def book_value(data, mid):
    return data["usdc"] + data["sol"] * mid + data["fee_budget_usd"] + data["reserve_usd"]


def _quotes(data, mid, spread):
    """Up to 2 bids and 2 asks, leaning against the inventory, sized from the balances."""
    sol_usd = data["sol"] * mid
    total = data["usdc"] + sol_usd
    skew = ((sol_usd - data["usdc"]) / total) if total else 0.0   # >0: too much SOL -> lower prices
    centre = mid * (1 - skew * spread / 2)
    orders = []
    bid_size = data["usdc"] / 2
    ask_size = sol_usd / 2
    for level in range(2):
        offset = spread / 2 + level * LEVEL_STEP
        if bid_size >= MIN_ORDER_USD:
            orders.append({"side": "buy", "price": centre * (1 - offset), "usd": bid_size})
        if ask_size >= MIN_ORDER_USD:
            orders.append({"side": "sell", "price": centre * (1 + offset), "usd": ask_size})
    return orders


def _fill(data, order, fraction, mid):
    usd = order["usd"] * fraction
    fee = usd * MAKER_FEE
    if order["side"] == "buy":
        if data["usdc"] < usd:
            return None
        data["usdc"] -= usd
        data["sol"] += (usd - fee) / order["price"]
    else:
        sol = usd / order["price"]
        if data["sol"] < sol:
            return None
        data["sol"] -= sol
        data["usdc"] += usd - fee
    data["fees_usd"] += fee
    data["captured_usd"] += abs(mid - order["price"]) / mid * usd
    order["usd"] -= usd
    fill = {"at": time.time(), "side": order["side"], "price": round(order["price"], 4), "usd": round(usd, 2),
            "partial": fraction < 1}
    data["fills"] = (data["fills"] + [fill])[-500:]
    return fill


def tick(mid=None, now=None):
    """One market update: fills, risk checks, re-pricing. Returns new fills."""
    mid = _mid() if mid is None else mid
    now = time.time() if now is None else now
    _prices.append(mid)
    fills = []
    with _LOCK:
        data = _load()
        if not data["enabled"]:
            return fills
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if data["day"] != today:
            data["day"], data["day_start_value"] = today, book_value(data, mid)
            if data["paused"].startswith("limite de perda"):
                data["paused"] = ""
        # fills: price traded through a resting order (touch = partial)
        for order in data["orders"]:
            if order["side"] == "buy" and mid <= order["price"]:
                fraction = 1.0 if mid < order["price"] * 0.9995 else 0.5
            elif order["side"] == "sell" and mid >= order["price"]:
                fraction = 1.0 if mid > order["price"] * 1.0005 else 0.5
            else:
                continue
            fill = _fill(data, order, fraction, mid)
            if fill:
                fills.append(fill)
        data["orders"] = [o for o in data["orders"] if o["usd"] >= 1]
        # risk
        loss = data["day_start_value"] - book_value(data, mid)
        vol, dd = volatility(), drop()
        reason = ""
        if loss >= DAILY_LOSS_USD:
            reason = "limite de perda diária (US$ " + format(DAILY_LOSS_USD, ".2f") + ") atingido"
        elif vol > VOL_PAUSE:
            reason = "volatilidade alta"
        elif dd > DROP_PAUSE:
            reason = "queda brusca"
        elif data["fee_budget_usd"] <= TX_FEE_USD * 4:
            reason = "verba de taxas esgotada"
        if reason:
            if data["orders"]:
                data["fee_budget_usd"] -= TX_FEE_USD * len(data["orders"])   # cancel all
                data["fees_usd"] += TX_FEE_USD * len(data["orders"])
            data["orders"], data["paused"] = [], reason
            _save(data)
            return fills
        data["paused"] = ""
        # re-price when needed (moved enough, or orders missing after fills)
        moved = abs(mid / data["last_mid"] - 1) if data["last_mid"] else 1
        recent = [t for t in _reprices if now - t < 3600]
        due = now - data["last_reprice"] >= REPRICE_SECONDS and (moved >= REPRICE_MOVE or len(data["orders"]) < 4 or fills)
        if due and len(recent) < MAX_REPRICES_PER_HOUR:
            new = _quotes(data, mid, target_spread(vol))
            actions = len(data["orders"]) + len(new)          # cancel old + place new
            cost = TX_FEE_USD * actions
            data["fee_budget_usd"] -= cost
            data["fees_usd"] += cost
            data["orders"] = new
            data["last_reprice"], data["last_mid"] = now, mid
            _reprices.append(now)
        _save(data)
    return fills


# ---------------------------------------------------------------- panel
def status_text(mid=None):
    with _LOCK:
        data = _load()
    try:
        mid = _mid() if mid is None else mid
    except Exception:
        mid = data["last_mid"] or 0.0
    value = book_value(data, mid) if mid else 0.0
    today = time.strftime("%Y-%m-%d", time.gmtime())
    fills_today = [f for f in data["fills"] if time.strftime("%Y-%m-%d", time.gmtime(f["at"])) == today]
    volume = sum(f["usd"] for f in fills_today)
    pnl = value - data["start_value"] if data["start_value"] else 0.0
    lines = ["💹 MARKET MAKING SOL/USDC — SIMULAÇÃO (preço real, dinheiro de mentira)",
             "Status: " + ("LIGADO" if data["enabled"] else "DESLIGADO") + " · Modo: SIMULAÇÃO (real desativado)",
             "Capital usado: US$ " + format(data["start_value"], ".2f") + " (do saldo real da carteira, máx. US$ " +
             format(CAPITAL_USD, ".0f") + ")",
             "Saldo SOL: " + format(data["sol"], ".4f") + " (US$ " + format(data["sol"] * mid, ".2f") + ") · USDC: " +
             format(data["usdc"], ".2f") + " · taxas: US$ " + format(data["fee_budget_usd"], ".2f") +
             " · reserva: US$ " + format(data["reserve_usd"], ".2f"),
             "Ordens abertas: " + str(sum(1 for o in data["orders"] if o["side"] == "buy")) + " compra / " +
             str(sum(1 for o in data["orders"] if o["side"] == "sell")) + " venda" +
             (" · spread alvo " + format(target_spread(volatility()) * 100, ".2f") + "%" if data["enabled"] else ""),
             "Execuções hoje: " + str(len(fills_today)) + " · volume: US$ " + format(volume, ".2f"),
             "Spread capturado (bruto): US$ " + format(data["captured_usd"], ".2f") + " · taxas pagas: US$ " +
             format(data["fees_usd"], ".2f"),
             "P/L líquido (valor total, inclui variação do SOL): " + format(pnl, "+.2f") + " USD"]
    if data["fills"]:
        f = data["fills"][-1]
        lines.append("Última execução: " + ("compra" if f["side"] == "buy" else "venda") + " US$ " +
                     format(f["usd"], ".2f") + " a " + format(f["price"], ".2f") + (" (parcial)" if f["partial"] else ""))
    if data["paused"]:
        lines.append("⏸️ Pausado: " + data["paused"])
    return "\n".join(lines)
