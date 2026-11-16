"""DEV LAUNCH SNIPER backtest (owner's prompt, Nov 16), 5 months, REAL on-chain trades through Helius.

Devs approved on each date by their trades CLOSED BEFORE it (prompt: hit rate >= 80%, >= 30 trades, profit,
PF >= 2, >= MIN_TOKENS coins). For every buy of such a dev the real trades are read once and cached:
the first seconds (entry price and how many OTHER wallets bought = momentum), the price path of the next
15 minutes (TP / SL / time) and the trades after the dev's sales. Then the prompt's variants are simulated on the
same data (US$ 5, max 5 open, daily loss US$ 15, costs per side, entry DELAY s after the dev, skip if the price
is already +15% above the dev's or the coin is worth < US$ 1,000):
  1. imediato: vende a mesma parte que o DEV vende (copy sell);
  2. venda total no primeiro sinal: sai tudo na 1ª venda do DEV;
  3. TP +20% / SL -8% / 15 min, e também sai se o DEV vender tudo (regras do prompt);
  4. com filtro de momentum: como o 3, só se >= 3 outras carteiras compraram nos primeiros segundos;
  5. com Smart Money: NÃO CALCULADO (precisaria do histórico de cada comprador).
Honest limits: entry/exit at real trade prices of other wallets (median of 3); the bot's own buy moving the curve is
in the cost; failed transactions not modelled; the 'same wallet' / 'funds' checks cannot be rebuilt in the past.
"""
import logging
import threading
import time

import dev_bot as db
import dev_strategy_test as dst
import launch_test as lt
import snipe_backtest as sbt

DAYS = db.E("LS_TEST_DAYS", 150)
MIN_WIN = db.E("LS_TEST_MIN_WIN", 0.80)
MIN_TRADES = int(db.E("LS_TEST_MIN_TRADES", 30))
USD = db.E("LS_TEST_USD", 5)
MAX_OPEN = int(db.E("LS_TEST_MAX_OPEN", 5))
DAILY_LOSS = db.E("LS_TEST_DAILY_LOSS", 15)
TP, SL, MAX_HOLD = 0.20, 0.08, 15 * 60
MOMENTUM_BUYERS = 3
DELAYS = (3, 10)
CAPITAL = 500.0
MAX_S = db.E("LS_TEST_MINUTES", 3) * 60
VARIANTS = (("1. Imediato + vende igual ao DEV (proporcional)", "mirror"),
            ("2. Imediato + venda TOTAL na 1ª venda do DEV", "first"),
            ("3. TP +20% / SL −8% / 15 min (+ sai se o DEV vender tudo)", "tpsl"),
            ("4. Igual ao 3 + filtro de MOMENTUM (≥ 3 compradores)", "momentum"))
_RUNNING = threading.Lock()
_STOP = threading.Event()


def _key(dev, h):
    return dev + h["mint"] + str(int(float(h["opened"])))


def fetch(rpc, dev, h, sol_usd):
    """Compact real data of one dev buy (cached)."""
    opened = float(h["opened"])
    sells = h.get("sells") or [[h["closed"], 1.0]]
    first = lt._trades(rpc, h["mint"], int(opened) - 2, opened + 40, sol_usd, pages=2)
    path = lt._trades(rpc, h["mint"], int(opened) + 2, opened + MAX_HOLD + 60, sol_usd, pages=3)
    sales = [[float(t), float(f)] for t, f in (sells[:1] if len(sells) == 1 else [sells[0], sells[-1]])]
    after = [lt._trades(rpc, h["mint"], int(t), int(t) + 40, sol_usd, pages=1) for t, _ in sales]
    keep = lambda rows: [[int(t), p, 1 if who == dev else 0] for t, p, who in rows][:300]   # noqa: E731
    return {"opened": opened, "dev_px": float(h.get("entry_usd") or 0), "first": keep(first), "path": keep(path),
            "sales": sales, "after": [keep(a) for a in after]}


def _price_after(rows, t):
    return lt._median3([p for tt, p, dv in rows if tt >= t and not dv])


def trade_result(e, variant, delay):
    """(return, exit_time) or reason string for one cached dev buy."""
    t_in = e["opened"] + delay
    entry = _price_after(e["first"], t_in)
    if not entry:
        return "sem preço na entrada"
    if entry * sbt.SUPPLY < db.CURVE_MIN_MCAP:
        return "moeda abaixo de US$ " + format(db.CURVE_MIN_MCAP, ",.0f")
    if e["dev_px"] and entry > e["dev_px"] * (1 + db.MAX_RUNUP_DEV):
        return "preço já +" + format(db.MAX_RUNUP_DEV * 100, ".0f") + "% acima do DEV"
    if variant == "momentum":
        buyers = sum(1 for t, p, dv in e["first"] if e["opened"] < t <= t_in and not dv)
        if buyers < MOMENTUM_BUYERS:
            return "sem momentum (< " + str(MOMENTUM_BUYERS) + " compradores)"
    sales = e["sales"]
    if variant in ("mirror", "first"):
        left, got = 1.0, 0.0
        use = sales[:1] if variant == "first" else sales
        for i, (t_sale, frac) in enumerate(use):
            px = _price_after(e["after"][i], t_sale + delay)
            part = left if (variant == "first" or i == len(use) - 1) else left * min(frac, 1.0)
            got += part * ((px or 0.0) / entry)
            left -= part
        return got * (1 - sbt.COST) ** 2 - 1, use[-1][0] + delay
    # tpsl / momentum: walk the real path; first exit signal wins
    dev_all_out = next((t for t, f in sales if f >= 0.95), sales[-1][0])
    for t, p, dv in e["path"]:
        if t < t_in or dv:
            continue
        r = p / entry
        if r >= 1 + TP:
            return (1 + TP) * (1 - sbt.COST) ** 2 - 1, t
        if r <= 1 - SL:
            return r * (1 - sbt.COST) ** 2 - 1, t            # a stop fills at the real (worse) price
        if t >= dev_all_out + delay:
            return r * (1 - sbt.COST) ** 2 - 1, t
        if t - t_in >= MAX_HOLD:
            return r * (1 - sbt.COST) ** 2 - 1, t
    last = [p for t, p, dv in e["path"] if t >= t_in and not dv]
    if not last:
        px = _price_after(e["after"][-1], dev_all_out + delay) if e["after"] else None
        return (px or 0.0) / entry * (1 - sbt.COST) ** 2 - 1, dev_all_out + delay
    return last[-1] / entry * (1 - sbt.COST) ** 2 - 1, e["path"][-1][0]


def simulate(evs, c, variant, delay):
    open_until, day_pnl, trades, rejected, months = [], {}, [], {}, {}
    for t, dev, h in evs:
        e = c.get(_key(dev, h))
        if not e:
            continue
        r = trade_result(e, variant, delay)
        if isinstance(r, str):
            rejected[r] = rejected.get(r, 0) + 1
            continue
        open_until = [x for x in open_until if x > t]
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        if len(open_until) >= MAX_OPEN:
            rejected["máx. posições"] = rejected.get("máx. posições", 0) + 1
            continue
        if day_pnl.get(day, 0) <= -DAILY_LOSS:
            rejected["limite de perda do dia"] = rejected.get("limite de perda do dia", 0) + 1
            continue
        pnl = USD * r[0] - 2 * sbt.NET_FEE
        open_until.append(r[1])
        day_pnl[day] = day_pnl.get(day, 0) + pnl
        trades.append(pnl)
        m = time.strftime("%Y-%m", time.gmtime(t))
        months[m] = months.get(m, 0) + pnl
    eq, peak, dd = CAPITAL, CAPITAL, 0.0
    for x in trades:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
    gw, gl = sum(x for x in trades if x > 0), -sum(x for x in trades if x < 0)
    return {"n": len(trades), "pnl": sum(trades), "win": sum(1 for x in trades if x > 0) / len(trades) if trades else 0,
            "pf": gw / gl if gl > 0 else None, "dd": dd, "months": months, "rejected": rejected,
            "per_day": len(trades) / max(1.0, DAYS)}


def text(evs, devs_ok, c, now):
    done = sum(1 for t, dev, h in evs if _key(dev, h) in c)
    L = ["🎯 BACKTEST DEV LAUNCH SNIPER — " + format(DAYS / 30, ".0f") + " meses (dados REAIS da blockchain)",
         "DEV aprovado pelo histórico ANTERIOR (acerto ≥ " + format(MIN_WIN * 100, ".0f") + "%, ≥ " + str(MIN_TRADES) +
         " ops, PF ≥ 2, lucro) · US$ " + format(USD, "g") + " · máx. " + str(MAX_OPEN) + " abertas · perda máx. US$ " +
         format(DAILY_LOSS, "g") + "/dia · não compra se +" + format(db.MAX_RUNUP_DEV * 100, ".0f") + "% acima do DEV · custos " +
         format(sbt.COST * 100, ".0f") + "% por lado",
         "📊 Compras de DEVs aprovados: " + str(len(evs)) + " (" + str(len(devs_ok)) + " DEVs) · refeitas com dados reais: " + str(done)]
    best = None
    for delay in DELAYS:
        L.append("⏱️ Entrando " + str(delay) + " s depois do DEV" + (" (velocidade do robô hoje)" if delay == 10 else " (precisaria de conexão em tempo real)") + ":")
        for name, v in VARIANTS:
            s = simulate(evs, c, v, delay)
            if not s["n"]:
                L.append("  • " + name + ": nenhuma operação")
                continue
            L.append("  • " + name + ": " + str(s["n"]) + " ops · acerto " + format(s["win"] * 100, ".0f") + "% · " +
                     format(s["pnl"], "+.2f") + " USD · PF " + ("—" if s["pf"] is None else format(s["pf"], ".2f")) +
                     " · queda máx " + format(min(s["dd"], 1) * 100, ".0f") + "%")
            if s["n"] >= 30 and (best is None or s["pnl"] > best[2]):
                best = (name, delay, s["pnl"], s)
    L.append("  • 5. Com confirmação de SMART MONEY: NÃO CALCULADO — precisaria do histórico de cada comprador de cada moeda.")
    if best and best[2] > 0 and (best[3]["pf"] or 0) >= 1.3:
        L.append("✅ Melhor: " + best[0] + " entrando " + str(best[1]) + " s depois: " + format(best[2], "+.2f") + " USD em " +
                 str(best[3]["n"]) + " ops. 📅 " + " · ".join(k + " " + format(v, "+.2f") for k, v in sorted(best[3]["months"].items())))
    elif best:
        L.append("❌ Nenhuma variação deu lucro confiável (≥ 30 ops, lucro e PF ≥ 1,3). Melhor: " + best[0] + " (" +
                 format(best[2], "+.2f") + " USD).")
    else:
        L.append("⚠️ Nenhuma variação com 30+ operações ainda.")
    if done < len(evs):
        L.append("⏳ PARCIAL: faltam " + str(len(evs) - done) + " compras — toque de novo (continua de onde parou, ~3 min cada).")
    first = min((t for t, _, _ in evs), default=now)
    if now - first < DAYS * 86400 * 0.9:
        L.append("⚠️ O histórico baixado cobre " + format((now - first) / 86400, ".0f") + " de " + format(DAYS, ".0f") +
                 " dias pedidos (o robô guarda as últimas transações de cada DEV).")
    L.append("⚠️ Passado não garante futuro; falhas de transação e o efeito da sua própria compra na curva estão só nos custos.")
    return "\n".join(L)


def cancel():
    _STOP.set()


def start(rpc, sol_usd, send=None, chat=None):
    if rpc is None:
        return "precisa do SOLANA_RPC_URL (Helius) no Render"
    if not _RUNNING.acquire(blocking=False):
        return "o teste já está rodando"
    _STOP.clear()
    now = time.time()
    evs, devs_ok = dst.events(now=now, days=DAYS, min_win=MIN_WIN, min_trades=MIN_TRADES)
    if not evs:
        _RUNNING.release()
        return "ainda não há compras de DEVs com acerto ≥ 80% e 30+ operações no histórico baixado"

    def work():
        try:
            c = sbt._get("ls_test_cache", {})
            began = time.time()
            sbt.DEADLINE[0] = began + MAX_S
            todo = [x for x in evs if _key(x[1], x[2]) not in c]
            step = max(1, len(todo) // 150)
            for i, (t, dev, h) in enumerate(todo[::step] + [x for j, x in enumerate(todo) if j % step]):
                if _STOP.is_set() or time.time() > began + MAX_S:
                    break
                try:
                    c[_key(dev, h)] = fetch(rpc, dev, h, sol_usd)
                except TimeoutError:
                    break
                except Exception as exc:
                    logging.warning("launch sniper test %s: %s: %s", h["mint"][:8], type(exc).__name__, str(exc)[:120])
                if i % 10 == 9:
                    sbt._put("ls_test_cache", c)
            sbt.DEADLINE[0] = 0.0
            sbt._put("ls_test_cache", c)
            txt = text(evs, devs_ok, c, now)
            db.note("backtest launch sniper: " + txt.split("\n")[2][:150])
            if send and chat:
                send(chat, txt)
        except Exception as exc:
            logging.exception("launch sniper test failed")
            if send and chat:
                send(chat, "⛔ Backtest DEV Launch Sniper falhou: " + type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            sbt.DEADLINE[0] = 0.0
            _RUNNING.release()

    threading.Thread(target=work, daemon=True).start()
    return ""
