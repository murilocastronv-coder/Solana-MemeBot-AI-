"""Trading bot screens (owner, Nov 18): paste a coin's contract (or a DexScreener / Pump.fun / Birdeye link) and get
the coin's card with buy / sell buttons, take profit, stop loss, trailing stop and limit buy orders, plus the
positions, limit orders and settings screens.

Every Telegram account trades with its own wallet:
  - the owner with the main bot wallet through live_engine (no fee; exits in live_exit_monitor, mode "trade");
  - everybody else through user_trade (FEE_RATE of each buy and sell to the fee wallet)."""
import json
import logging
import re
import threading
import time
import uuid

import os

import user_trade as ut
import user_wallets

AIRDROP_ONLY = os.getenv("AIRDROP_ONLY", "true").strip().lower() != "false"   # owner (Nov 18): trading switched off
HOME = os.getenv("BOT_HOME", "btc").strip().lower()   # owner (Nov 19, latest): only the BTC FULL MARGIN robot (demo + real)
if HOME == "forex":                                      # owner (Nov 19): forex removed, the BTC robot replaced it
    HOME = "btc"
GROUP_URL = os.getenv("TELEGRAM_GROUP_URL", "https://t.me/+fKaHJ2Dg_nIzM2U0")   # owner (Nov 18): community group

MODE = "trade"
KEY = "trade_panel"
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
TP_CHOICES = (0.0, 0.25, 0.50, 1.0, 2.0)          # take profit: off, +25%, +50%, +100%, +200%
SL_CHOICES = (0.0, 0.15, 0.30, 0.50)              # stop loss: off, -15%, -30%, -50%
TRAIL_CHOICES = (0.0, 0.10, 0.20, 0.30)           # trailing stop: off, 10 / 20 / 30% below the top
LIMIT_DROPS = (0.10, 0.20, 0.30, 0.50)            # limit buy: when the price falls 10 / 20 / 30 / 50%
TRAIL_ARM = 0.10                                  # the trailing stop starts once the coin is +10%
B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
INPUT = {}                                        # chat -> {"mint", "at"}: waiting for an "X SOL" amount
_LOCK = threading.RLock()
_MEM = {}


def is_owner(chat):
    return user_wallets.is_owner(chat)


# owner, Nov 23: in the SOL robot bot (HOME "btc") the OTHER accounts can buy and sell coins with their own wallet,
# paying ut.FEE_RATE (2%) of each buy and each sell to the fee wallet; the owner keeps only the Full Margin SOL robot
# owner, Nov 23 (latest): only the SOL robot (demo + real) - the other accounts' trading is off unless BTCFM_USER_TRADING=true
USER_TRADING = HOME == "btc" and os.getenv("BTCFM_USER_TRADING", "false").strip().lower() == "true"
USER_BLOCKED = {"tcopy", "trob", "trb_on", "trb_off", "trb_sol", "trb_bt", "/robo", "/backtest", "trpos", "/posicoes_robo",
                "tscan", "twl", "tdl", "tlim", "/ordens", "towner", "trd_on", "trd_off", "trd_reset", "trd_go", "tterms_robot"}
USER_BLOCKED_PREFIX = ("tad", "/airdrop", "sup", "vid", "/tarefas", "/videos", "tlo", "tld", "tla", "tlx", "tdo", "tdp",
                       "tdx", "tsoon", "tsn")


def airdrop_only(chat=None):
    """The old 'airdrop only' lock; off for the other accounts when they trade coins in the SOL robot bot."""
    if AIRDROP_ONLY and USER_TRADING and chat is not None and not is_owner(chat):
        return False
    return AIRDROP_ONLY


def user_blocked(chat, action):
    a = str(action or "")
    return (USER_TRADING and not is_owner(chat) and
            (a in USER_BLOCKED or a.split(":")[0] in USER_BLOCKED or a.startswith(USER_BLOCKED_PREFIX)))


# ------------------------------------------------------------------ owner settings (pg_store)
def _load():
    with _LOCK:
        if not _MEM:
            d = {k: v for k, v in ut.default().items() if k not in ("positions", "history")}
            d["terms_at"] = 1
            try:
                import pg_store
                if pg_store.enabled():
                    d.update(pg_store.get(KEY) or {})
            except Exception:
                pass
            _MEM.update(d)
        return _MEM


def _save():
    try:
        import pg_store
        if pg_store.enabled():
            pg_store.put(KEY, json.loads(json.dumps(_MEM)))
    except Exception as exc:
        logging.warning("trade panel save: %s", type(exc).__name__)


def settings(chat):
    return _load() if is_owner(chat) else ut.load(chat)


def set_setting(chat, key, value):
    if is_owner(chat):
        with _LOCK:
            _load()[key] = value
            _save()
    else:
        ut.update(chat, lambda d: d.__setitem__(key, value))


# ------------------------------------------------------------------ finding the coin
def candidates(text):
    """Base58 addresses in a message (a bare contract or one inside a link), in order, without SOL / stablecoins."""
    out = []
    for m in B58.findall(str(text or "")):
        if m not in out and m not in STABLES:
            out.append(m)
    return out


def _best(rows):
    rows = [r for r in (rows or []) if isinstance(r, dict) and (r.get("chainId") in (None, "solana"))]
    return max(rows, key=lambda r: float((r.get("liquidity") or {}).get("usd") or 0), default=None)


def info(text, get=None):
    """The coin named in `text`: DexScreener's most liquid pool (a pair address is resolved to its coin)."""
    import signals as sg
    get = get or sg._get
    for cand in candidates(text)[:3]:
        try:
            p = _best(get(sg.DEX + "/tokens/v1/solana/" + cand))
            if not p:
                d = get(sg.DEX + "/latest/dex/pairs/solana/" + cand) or {}
                p = _best(d.get("pairs") or ([d["pair"]] if d.get("pair") else []))
        except Exception as exc:
            logging.info("trade panel lookup %s: %s", cand[:6], type(exc).__name__)
            continue
        if p and (p.get("baseToken") or {}).get("address"):
            return _row(p)
    return None


def _row(p):
    base = p.get("baseToken") or {}
    chg = p.get("priceChange") or {}
    return {"mint": base.get("address"), "symbol": str(base.get("symbol") or "?")[:20], "name": str(base.get("name") or "")[:40],
            "price": float(p.get("priceUsd") or 0), "liq": float((p.get("liquidity") or {}).get("usd") or 0),
            "mcap": float(p.get("marketCap") or p.get("fdv") or 0), "vol24": float((p.get("volume") or {}).get("h24") or 0),
            "chg": {k: chg.get(k) for k in ("m5", "h1", "h6", "h24")}, "created": (p.get("pairCreatedAt") or 0) / 1000,
            "dex": str(p.get("dexId") or ""), "pair": p.get("pairAddress") or ""}


# ------------------------------------------------------------------ positions (owner: live_trade_state, others: user_trade)
def position(chat, mint):
    if is_owner(chat):
        import live_trade_state as st
        p = st._load()["positions"].get(mint)
        if not p:
            return None
        return {"symbol": p.get("symbol"), "tokens": int(p.get("token_amount_raw") or 0), "cost_usd": float(p.get("cost_usd") or 0),
                "mode": p.get("mode"), "robot": bool(p.get("trade_robot")), **settings_of(p)}
    p = ut.load(chat)["positions"].get(mint)
    return dict(p, mode=MODE) if p else None


def positions(chat):
    if is_owner(chat):
        import live_trade_state as st
        return [(m, position(chat, m)) for m in st._load()["positions"]]
    return [(m, dict(p, mode=MODE)) for m, p in ut.load(chat)["positions"].items()]


def settings_of(p):
    """tp / sl / trail of an owner position (live_trade_state fields trade_*)."""
    d = _load()
    return {k: float(p.get("trade_" + k, d.get(k) or 0) or 0) for k in ("tp", "sl", "trail")}


def exit_signal(p, ratio, peak):
    """(action, reason) for an owner position of the panel: TP, SL, trailing stop (armed at +TRAIL_ARM), else hold."""
    s = settings_of(p)
    if s["sl"] and ratio <= 1 - s["sl"]:
        return "sell", "trade_sl"
    if s["tp"] and ratio >= 1 + s["tp"]:
        return "sell", "trade_tp"
    if s["trail"] and peak >= 1 + TRAIL_ARM:
        if ratio <= peak * (1 - s["trail"]):
            return "sell", "trade_trail"
        return "hold", "trailing"
    return "hold", "trade_hold"


def adopt_robot_positions():
    """The robots were removed (owner, Nov 18): their open coins become panel trades (no TP / SL until the owner sets
    them on the card). Returns the symbols adopted."""
    import live_trade_state as st
    out = []
    for m, p in list(st._load()["positions"].items()):
        if p.get("mode") != MODE:
            st.set_position_fields(m, mode=MODE, trade_tp=0.0, trade_sl=0.0, trade_trail=0.0, trail_armed=False)
            out.append(str(p.get("symbol") or m[:6]))
    return out


def _value(mint, p):
    if not p or not p.get("tokens"):
        return None
    try:
        return ut.value_usd(mint, p["tokens"])
    except Exception:
        return None


def _wallet(chat):
    try:
        if is_owner(chat):
            import live_engine
            import live_order_bridge as bridge
            w = live_engine.get_address()
            sol = bridge.get_sol_balance(w) / 1e9
            return w, sol, sol * float(bridge.sol_usd_price())
        return ut.wallet(chat)
    except Exception:
        return None


# ------------------------------------------------------------------ text helpers
def _usd(x):
    x = float(x or 0)
    return (format(x / 1e9, ",.2f") + " bi" if x >= 1e9 else format(x / 1e6, ",.2f") + " mi" if x >= 1e6
            else format(x / 1e3, ",.1f") + " mil" if x >= 1e3 else format(x, ",.2f"))


def _price(x):
    x = float(x or 0)
    if x >= 1:
        return format(x, ",.4f")
    s = format(x, ".12f").rstrip("0")
    frac = s.split(".")[1] if "." in s else ""
    zeros = len(frac) - len(frac.lstrip("0"))
    return format(x, "." + str(min(zeros + 4, 12)) + "f")


def _pct(v):
    try:
        return format(float(v), "+.1f") + "%"
    except (TypeError, ValueError):
        return "—"


def _opt(v, sign):
    return "off" if not v else sign + format(float(v) * 100, "g") + "%"


def _sol(v):
    return format(float(v), "g") + " SOL"


# ------------------------------------------------------------------ the coin card
def card_text(c, p=None, value=None, safety=None, wallet=None, now=None, s=None):
    now = now or time.time()
    s = s or {"tp": 0, "sl": 0, "trail": 0}
    age = (now - c["created"]) / 86400 if c.get("created") else None
    L = ["🪙 " + (c["name"] + " (" + c["symbol"] + ")" if c.get("name") else c["symbol"]),
         "📄 " + c["mint"],
         "💲 Preço: US$ " + _price(c["price"]) + " · MC US$ " + _usd(c["mcap"]) + " · 💧 Liquidez US$ " + _usd(c["liq"]),
         "📈 5 min " + _pct(c["chg"].get("m5")) + " · 1 h " + _pct(c["chg"].get("h1")) + " · 6 h " + _pct(c["chg"].get("h6")) +
         " · 24 h " + _pct(c["chg"].get("h24")) + " · volume 24 h US$ " + _usd(c["vol24"]),
         "⏳ " + ("criada há " + (format(age, ".0f") + " dias" if age >= 1 else format(age * 24, ".0f") + " h") if age is not None else "idade ?") +
         (" · " + c["dex"] if c.get("dex") else ""),
         "🛡️ Anti-golpe: " + ("✅ OK" if safety == "" else "⚠️ " + str(safety) if safety else "não conferido")]
    if p:
        cost = float(p.get("cost_usd") or 0)
        L.append("💼 Sua posição: custo US$ " + format(cost, ".2f") +
                 (" → vale US$ " + format(value, ".2f") + " (" + _pct((value / cost - 1) * 100 if cost else 0) + ")" if value is not None else ""))
        L.append("⚙️ Alvo " + _opt(p.get("tp"), "+") + " · stop " + _opt(p.get("sl"), "−") + " · stop móvel " +
                 ("off" if not p.get("trail") else format(float(p["trail"]) * 100, "g") + "% abaixo do topo (liga em +" + format(TRAIL_ARM * 100, "g") + "%)"))
    else:
        L.append("⚙️ Na compra: alvo " + _opt(s["tp"], "+") + " · stop " + _opt(s["sl"], "−") + " · stop móvel " + _opt(s["trail"], ""))
    if wallet:
        L.append("👛 Sua carteira: " + format(wallet[1], ".4f") + " SOL (US$ " + format(wallet[2], ".2f") + ")")
    L.append("🔗 https://dexscreener.com/solana/" + c["mint"] + "\n🔗 https://rugcheck.xyz/tokens/" + c["mint"])
    return "\n".join(L)


def card_keyboard(mint, p=None, s=None):
    s = s or {"buy": list(ut.BUY_PRESETS[0]), "tp": 0, "sl": 0, "trail": 0}
    kb = [[{"text": "🟢 " + _sol(v), "callback_data": "tb:" + format(float(v), "g") + ":" + mint} for v in s["buy"]],
          [{"text": "🟢 Comprar X SOL", "callback_data": "tbx:" + mint}, {"text": "📋 Ordem limite", "callback_data": "tlo:" + mint},
           {"text": "🔁 DCA", "callback_data": "tdo:" + mint}]]
    if p:
        kb.append([{"text": "🔴 Vender " + str(x) + "%", "callback_data": "ts:" + str(x) + ":" + mint} for x in (25, 50, 100)])
    o = p if p else s
    kb.append([{"text": "🎯 Alvo " + _opt(o.get("tp"), "+"), "callback_data": "tt:" + mint},
               {"text": "🛑 Stop " + _opt(o.get("sl"), "−"), "callback_data": "tl:" + mint},
               {"text": "🚀 Móvel " + _opt(o.get("trail"), ""), "callback_data": "tr:" + mint}])
    watched = mint in (s.get("watch") or [])
    kb.append([{"text": "🔄 Atualizar", "callback_data": "tc:" + mint},
               {"text": ("⭐ Tirar da watchlist" if watched else "☆ Watchlist"), "callback_data": "tw:" + mint},
               {"text": "💼 Posições", "callback_data": "tpl"}])
    return kb


def show(send, chat, text, tail=None, safety=True):
    """Card of the coin named in `text`; buys right away if the account set an automatic buy. False if not found."""
    c = info(text)
    if not c:
        return False
    p = position(chat, c["mint"])
    why = None
    if safety:
        try:
            import signals as sg
            why = sg.safety_reason(c["mint"]) or ""
        except Exception:
            why = None
    s = settings(chat)
    send(chat, card_text(c, p, _value(c["mint"], p), why, _wallet(chat), s=s), card_keyboard(c["mint"], p, s) + (tail or []))
    if safety and float(s.get("auto_buy") or 0) > 0 and not p:
        send(chat, "⚡ Compra automática: " + _sol(s["auto_buy"]) + "…")
        buy(chat, c["mint"], float(s["auto_buy"]), send, c["symbol"])
    return True


# ------------------------------------------------------------------ actions
def _cycle(choices, current):
    nxt = [c for c in choices if c > float(current or 0) + 1e-9]
    return nxt[0] if nxt else choices[0]


def set_option(chat, mint, field):
    """Next value of tp / sl / trail: on the open position, else the default for the next buys."""
    choices = {"tp": TP_CHOICES, "sl": SL_CHOICES, "trail": TRAIL_CHOICES}[field]
    p = position(chat, mint)
    if p and p.get("mode") == MODE:
        v = _cycle(choices, p.get(field))
        if is_owner(chat):
            import live_trade_state as st
            st.set_position_fields(mint, **{"trade_" + field: v})
        else:
            ut.update(chat, lambda d: d["positions"].get(mint, {}).update({field: v}))
        return v, True
    v = _cycle(choices, settings(chat).get(field))
    set_setting(chat, field, v)
    return v, False


def terms_text():
    return ("📜 ANTES DE OPERAR\n"
            "• Você compra e vende com a SUA carteira do bot (só sua, criada para a sua conta).\n"
            "• TAXA: " + format(ut.FEE_RATE * 100, "g") + "% de cada compra e de cada venda, cobrada na mesma transação, em SOL.\n"
            "• Memecoins são de altíssimo risco: você pode perder todo o valor. O anti-golpe ajuda, mas não garante.\n"
            "• Deixe ~0,01 SOL a mais para as taxas da rede.")


def buy(chat, mint, sol, send, symbol=None):
    try:
        if is_owner(chat):
            import live_engine
            import live_order_bridge as bridge
            p = position(chat, mint)
            if p and p.get("mode") != MODE:
                send(chat, "⛔ Essa moeda é de um robô (" + str(p.get("mode")) + ").")
                return
            usd = round(float(sol) * float(bridge.sol_usd_price()), 2)
            settled = live_engine._execute_buy(None, mint, symbol or (info(mint) or {}).get("symbol") or mint[:6], send, chat,
                                               live_engine.get_address(), None, MODE, usd=usd, add_on=bool(p))
            if settled and settled.get("status") == "settled_buy" and not p:
                import live_trade_state as st
                d = _load()
                st.set_position_fields(mint, trade_tp=d["tp"], trade_sl=d["sl"], trade_trail=d["trail"], peak_ratio=1.0)
            return
        if not ut.load(chat).get("terms_at"):
            send(chat, terms_text(), [[{"text": "✅ Aceito a taxa de " + format(ut.FEE_RATE * 100, "g") + "%", "callback_data": "tterms"}]])
            return
        send(chat, ut.buy(chat, mint, sol, send, symbol=symbol or (info(mint) or {}).get("symbol")))
    except Exception as exc:
        send(chat, "⛔ Compra: " + type(exc).__name__ + ": " + str(exc)[:150])


def sell(chat, mint, pct, send):
    try:
        if is_owner(chat):
            import live_engine
            if not position(chat, mint):
                send(chat, "⛔ O bot não tem posição dessa moeda.")
                return
            if int(pct) >= 100:
                live_engine.sell_now(mint, "trade_manual", send, chat)
            else:
                live_engine.sell_part(mint, int(pct) / 100, "trade_manual", send, chat, note="você mandou vender " + str(int(pct)) + "%")
            return
        send(chat, ut.sell(chat, mint, pct, send))
    except Exception as exc:
        send(chat, "⛔ Venda: " + type(exc).__name__ + ": " + str(exc)[:150])


# ------------------------------------------------------------------ limit orders (owner in pg_store, others in user_trade)
def limits(chat):
    return dict(settings(chat).get("limits") or {})


def add_limit(chat, mint, symbol, price_now, drop, sol):
    if not is_owner(chat):
        return ut.add_limit(chat, mint, symbol, price_now, drop, sol)
    with _LOCK:
        d = _load()
        d.setdefault("limits", {})
        if len(d["limits"]) >= ut.MAX_LIMITS:
            raise ValueError("máximo de " + str(ut.MAX_LIMITS) + " ordens limite")
        oid = uuid.uuid4().hex[:8]
        d["limits"][oid] = {"mint": mint, "symbol": symbol, "price": float(price_now) * (1 - float(drop)), "drop": float(drop),
                            "sol": float(sol), "created": time.time()}
        _save()
        return oid


def cancel_limit(chat, oid):
    if not is_owner(chat):
        return ut.cancel_limit(chat, oid)
    with _LOCK:
        ok = _load().setdefault("limits", {}).pop(oid, None) is not None
        _save()
        return ok


def limits_text(chat):
    rows = limits(chat)
    if not rows:
        return "📋 Nenhuma ordem limite. No cartão de uma moeda, toque em 📋 Ordem limite.", []
    L, kb = ["📋 ORDENS LIMITE (compra quando o preço cair até o valor)"], []
    for oid, o in rows.items():
        L.append("• " + str(o["symbol"]) + ": compra " + _sol(o["sol"]) + " quando chegar a US$ " + _price(o["price"]) +
                 " (−" + format(o["drop"] * 100, "g") + "% do preço da ordem) · vence em " +
                 format(max(0, ut.LIMIT_TTL_S - (time.time() - o["created"])) / 86400, ".1f") + " dias")
        kb.append([{"text": "❌ Cancelar " + str(o["symbol"]), "callback_data": "tlx:" + oid}])
    return "\n".join(L), kb


def owner_upkeep(send, chat, prices=None):
    """Owner's limit orders (the users' run in user_trade.upkeep)."""
    rows = limits(chat)
    if not rows:
        return
    for oid, o in rows.items():
        if time.time() - float(o.get("created") or 0) > ut.LIMIT_TTL_S:
            cancel_limit(chat, oid)
    try:
        px = prices(set(o["mint"] for o in rows.values())) if prices else ut._dex_prices(set(o["mint"] for o in rows.values()))
    except Exception:
        return
    for oid, o in limits(chat).items():
        if ut.limit_hit(o, px.get(o["mint"], 0)) and cancel_limit(chat, oid):
            send(chat, "📋 Ordem limite atingida: " + str(o["symbol"]) + " chegou a US$ " + _price(px[o["mint"]]) + ". Comprando…")
            buy(chat, o["mint"], o["sol"], send, o["symbol"])


# ------------------------------------------------------------------ screens
def positions_text(chat):
    rows = positions(chat)
    if not rows:
        return "💼 Nenhuma posição aberta. Cole o contrato de uma moeda aqui para ver o cartão e comprar.", []
    L, kb, total_cost, total_val = ["💼 SUAS POSIÇÕES"], [], 0.0, 0.0
    for m, p in rows:
        cost = float(p.get("cost_usd") or 0)
        val = _value(m, p)
        total_cost += cost
        total_val += val if val is not None else cost
        L.append("• " + str(p.get("symbol") or m[:6]) + ": US$ " + format(cost, ".2f") +
                 (" → US$ " + format(val, ".2f") + " (" + _pct((val / cost - 1) * 100 if cost else 0) + ")" if val is not None else " → ?") +
                 " · alvo " + _opt(p.get("tp"), "+") + " · stop " + _opt(p.get("sl"), "−") + " · móvel " + _opt(p.get("trail"), ""))
        kb.append([{"text": "📊 " + str(p.get("symbol") or m[:6]), "callback_data": "tc:" + m},
                   {"text": "🔴 Vender 100%", "callback_data": "ts:100:" + m}])
    L.append("Total: US$ " + format(total_cost, ".2f") + " → US$ " + format(total_val, ".2f") + " (" + format(total_val - total_cost, "+.2f") + ")")
    return "\n".join(L), kb


SPEED_NAMES = {"eco": "🌱 Eco", "fast": "🐎 Rápido", "turbo": "🚀 Turbo"}


def _on(v):
    return "🟢" if v else "🔴"


def settings_text(chat):
    s = settings(chat)
    owner = is_owner(chat)
    sp = ut.SPEEDS.get(s.get("speed") or "fast", ut.SPEEDS["fast"])
    L = ["⚙️ CONFIGURAÇÕES",
         "⚡ Velocidade: " + SPEED_NAMES.get(s.get("speed") or "fast") + " (taxa de prioridade até " + format(sp[1] / 1e9, "g") + " SOL)",
         "💵 Compra rápida: " + " / ".join(_sol(v) for v in s["buy"]),
         "📉 Slippage máximo: " + format(int(s["slippage_bps"]) / 100, "g") + "%",
         "🤖 Compra automática ao colar o contrato: " + ("off" if not float(s.get("auto_buy") or 0) else _sol(s["auto_buy"])),
         "🤖 Venda automática (padrão de cada compra): alvo " + _opt(s["tp"], "+") + " · stop " + _opt(s["sl"], "−") +
         " · móvel " + _opt(s["trail"], ""),
         _on(s.get("confirm")) + " Confirmar trades: " + ("pergunta antes de cada compra" if s.get("confirm") else "compra no toque"),
         _on(s.get("sell_protect")) + " Proteção de venda: " + ("confirma vendas de 75% ou mais" if s.get("sell_protect") else "desligada"),
         "🛡️ Anti-golpe: sempre conferido no cartão da moeda",
         ("💸 Sem taxa (dono)" if owner else "💸 Taxa: " + format(ut.FEE_RATE * 100, "g") + "% por compra e por venda")]
    if owner:
        L.append("(Velocidade e slippage valem para os usuários; a sua carteira usa o motor principal.)")
    kb = [[{"text": ("✅ " if (s.get("speed") or "fast") == k else "") + v, "callback_data": "tsp:" + k} for k, v in SPEED_NAMES.items()],
          [{"text": "💵 Compra rápida", "callback_data": "tsb"}, {"text": "📉 Slippage", "callback_data": "tss"}],
          [{"text": _on(float(s.get("auto_buy") or 0)) + " Compra automática", "callback_data": "tsa"},
           {"text": _on(s["tp"] or s["sl"] or s["trail"]) + " Venda automática", "callback_data": "tsell"}],
          [{"text": "🎯 Alvo padrão", "callback_data": "tst"}, {"text": "🛑 Stop padrão", "callback_data": "tsl"},
           {"text": "🚀 Móvel padrão", "callback_data": "tsr"}],
          [{"text": _on(s.get("confirm")) + " Confirmar trades", "callback_data": "tsc"},
           {"text": _on(s.get("sell_protect")) + " Proteção de venda", "callback_data": "tsv"}],
          [{"text": "🔴 Proteção MEV", "callback_data": "tsoon:mev"}, {"text": "🖼️ Cartões de P/L", "callback_data": "tsoon:pnl"}],
          [{"text": "👛 Carteira", "callback_data": "twal"}, {"text": "🔐 Segurança da conta", "callback_data": "tsec"}]]
    return "\n".join(L), kb


SOON = {"mev": "🛡️ Proteção MEV (envio privado pela Jito)", "pnl": "🖼️ Cartões de lucro / prejuízo em imagem",
"rewards": "💰 Recompensas por indicação",
        "copy": "👥 Copy trade de uma carteira que você escolher"}


# ------------------------------------------------------------------ home screen
def home_text(chat):
    if airdrop_only(chat) and HOME == "btc":
        if not is_owner(chat):
            return "🔒 Este bot agora é privado. Se você tem SOL na carteira do bot, toque em 💸 Sacar SOL."
        import btc_fullmargin              # owner, Nov 19: the BTC robot (demo + real) is the home screen
        return btc_fullmargin.status_text()
    if airdrop_only(chat) and HOME == "robot":
        import big_bot
        return big_bot.status_text(chat, big_bot.cached_signals())
    if airdrop_only(chat) and HOME == "superteam":
        import superteam_bot
        return superteam_bot.screen(chat)[0] + "\n💬 Nosso grupo no Telegram: " + GROUP_URL
    if airdrop_only(chat):
        import airdrop_bot
        t = airdrop_bot.screen(chat)[0].replace("🪂 AIRDROPS DA SOLANA", "🪂 CAÇADOR DE AIRDROPS DA SOLANA", 1)
        return t + "\n💬 Nosso grupo no Telegram: " + GROUP_URL
    w = _wallet(chat)
    L = ["🟣 Solana · 👛 Sua carteira do bot (toque para copiar):", (w[0] if w else (user_wallets.get_address(chat) or "?")),
         ("💰 Saldo: " + format(w[1], ".4f") + " SOL (US$ " + format(w[2], ".2f") + ")" if w else "💰 Saldo: indisponível agora"),
         "—",
         "Cole aqui o contrato de qualquer moeda da Solana para comprar e vender. Toque em 🔄 Atualizar para ver o saldo novo.",
         "💬 Nosso grupo no Telegram: " + GROUP_URL]
    if not is_owner(chat):
        L.append("💸 Taxa: " + format(ut.FEE_RATE * 100, "g") + "% por compra e por venda.")
    return "\n".join(L)


def home_keyboard(chat):
    if airdrop_only(chat) and HOME == "btc":
        if not is_owner(chat):
            return [[{"text": "💸 Sacar SOL", "callback_data": "wallet_withdraw"}]]
        import btc_fullmargin
        return btc_fullmargin.keyboard()              # owner, Nov 23: only the SOL robot (airdrops removed again)
    if airdrop_only(chat) and HOME == "robot":
        import big_bot
        kb = [[{"text": "📊 Resultados da carteira de teste", "callback_data": "tres"}]] + big_bot.keyboard(chat)
        row = [{"text": "📥 Depositar", "callback_data": "wallet_deposit"}, {"text": "💸 Sacar", "callback_data": "wallet_withdraw"}]
        try:
            if positions(chat):
                row.append({"text": "💼 Posições", "callback_data": "tpl"})
        except Exception:
            pass
        return kb + [row]
    if airdrop_only(chat):
        import airdrop_bot
        import superteam_bot
        kb = superteam_bot.screen(chat)[1] if HOME == "superteam" else airdrop_bot.screen(chat)[1]
        extra = [{"text": "💸 Sacar SOL", "callback_data": "wallet_withdraw"}]
        try:
            if positions(chat):
                extra.append({"text": "💼 Posições", "callback_data": "tpl"})
        except Exception:
            pass
        kb = kb + [extra, [{"text": "💬 Grupo no Telegram", "url": GROUP_URL}]]
        if is_owner(chat):
            kb.append([{"text": "🎬 Vídeos automáticos", "callback_data": "vid"}])
            kb.append([{"text": "📊 Painel do dono", "callback_data": "towner"}])
        return kb
    if USER_TRADING and not is_owner(chat):                  # the other accounts: only buy / sell coins (2% fee)
        return [[{"text": "🟢 Comprar", "callback_data": "tbuy"}, {"text": "🔴 Vender", "callback_data": "tsellm"}],
                [{"text": "💼 Posições", "callback_data": "tpl"}, {"text": "⚙️ Configurações", "callback_data": "tset"}],
                [{"text": "📥 Depositar", "callback_data": "wallet_deposit"}, {"text": "💸 Sacar", "callback_data": "wallet_withdraw"}],
                [{"text": "❓ Ajuda", "callback_data": "thelp"}, {"text": "🔄 Atualizar", "callback_data": "thome"}]]
    kb = [[{"text": "🟢 Comprar", "callback_data": "tbuy"}, {"text": "🔴 Vender", "callback_data": "tsellm"}],
          [{"text": "💼 Posições", "callback_data": "tpl"}, {"text": "📋 Ordens limite", "callback_data": "tlim"},
           {"text": "🔁 Ordens DCA", "callback_data": "tdl"}],
          [{"text": "👥 Copy trade", "callback_data": "tcopy"}, {"text": "🤖 Robô moedas grandes", "callback_data": "trob"}],
          [{"text": "🔎 Scanner", "callback_data": "tscan"}, {"text": "🪂 Airdrops", "callback_data": "tad"},
           {"text": "⭐ Watchlist", "callback_data": "twl"}],
          [{"text": "📥 Depositar", "callback_data": "wallet_deposit"}, {"text": "💸 Sacar", "callback_data": "wallet_withdraw"}],
          [{"text": "⚙️ Configurações", "callback_data": "tset"}, {"text": "❓ Ajuda", "callback_data": "thelp"},
           {"text": "🔄 Atualizar", "callback_data": "thome"}]]
    kb.append([{"text": "💬 Grupo no Telegram", "url": GROUP_URL}])
    if is_owner(chat):
        kb.append([{"text": "📊 Painel do dono", "callback_data": "towner"}])
    return kb


# ------------------------------------------------------------------ DCA orders
DCA_PLANS = ((4, 1), (6, 4), (7, 24))             # (buys, every N hours)


def dcas(chat):
    return dict(settings(chat).get("dca") or {})


def add_dca(chat, mint, symbol, sol, n, every_h, now=None):
    d = dcas(chat)
    if len(d) >= ut.MAX_LIMITS:
        raise ValueError("máximo de " + str(ut.MAX_LIMITS) + " ordens DCA")
    oid = uuid.uuid4().hex[:8]
    d[oid] = {"mint": mint, "symbol": symbol, "sol": float(sol), "left": int(n), "every_h": float(every_h),
              "next_at": float(now or time.time())}
    set_setting(chat, "dca", d)
    return oid


def cancel_dca(chat, oid):
    d = dcas(chat)
    ok = d.pop(oid, None) is not None
    set_setting(chat, "dca", d)
    return ok


def dca_text(chat):
    rows = dcas(chat)
    if not rows:
        return "🔁 Nenhuma ordem DCA. No cartão de uma moeda, toque em 🔁 DCA (compra em partes, de tempos em tempos).", []
    L, kb = ["🔁 ORDENS DCA"], []
    for oid, o in rows.items():
        L.append("• " + str(o["symbol"]) + ": " + _sol(o["sol"]) + " a cada " + format(o["every_h"], "g") + " h · faltam " + str(o["left"]) +
                 " compras · próxima em " + format(max(0, o["next_at"] - time.time()) / 60, ".0f") + " min")
        kb.append([{"text": "❌ Cancelar " + str(o["symbol"]), "callback_data": "tdx:" + oid}])
    return "\n".join(L), kb


def run_dca(chat, send, now=None):
    now = now or time.time()
    for oid, o in dcas(chat).items():
        if o["next_at"] > now:
            continue
        d = dcas(chat)
        if oid not in d:
            continue
        d[oid]["left"] = int(d[oid]["left"]) - 1
        d[oid]["next_at"] = now + float(o["every_h"]) * 3600
        if d[oid]["left"] <= 0:
            d.pop(oid)
        set_setting(chat, "dca", d)
        send(chat, "🔁 DCA: comprando " + _sol(o["sol"]) + " de " + str(o["symbol"]) + " (faltam " + str(max(0, int(o["left"]) - 1)) + ")…")
        buy(chat, o["mint"], o["sol"], send, o["symbol"])


# ------------------------------------------------------------------ watchlist and scanner
def toggle_watch(chat, mint):
    w = list(settings(chat).get("watch") or [])
    if mint in w:
        w.remove(mint)
        on = False
    else:
        w = (w + [mint])[-20:]
        on = True
    set_setting(chat, "watch", w)
    return on


def watch_text(chat, get=None):
    import signals as sg
    w = list(settings(chat).get("watch") or [])
    if not w:
        return "⭐ Watchlist vazia. No cartão de uma moeda, toque em ☆ Watchlist.", []
    rows = {}
    try:
        for r in (get or sg._get)(sg.DEX + "/tokens/v1/solana/" + ",".join(w[:30])) or []:
            m = (r.get("baseToken") or {}).get("address")
            if m and (m not in rows or float((r.get("liquidity") or {}).get("usd") or 0) > rows[m]["liq"]):
                rows[m] = _row(r)
    except Exception:
        pass
    L, kb = ["⭐ WATCHLIST"], []
    for m in w:
        c = rows.get(m)
        L.append("• " + (c["symbol"] + ": US$ " + _price(c["price"]) + " · 1 h " + _pct(c["chg"].get("h1")) + " · 24 h " +
                         _pct(c["chg"].get("h24")) + " · MC US$ " + _usd(c["mcap"]) if c else m[:6] + "…: sem dados"))
        kb.append([{"text": "📊 " + (c["symbol"] if c else m[:6]), "callback_data": "tc:" + m},
                   {"text": "❌ Tirar", "callback_data": "tw:" + m}])
    return "\n".join(L), kb


def scanner_text(get=None):
    """Most boosted Solana coins on DexScreener right now (with their pool data)."""
    import signals as sg
    get = get or sg._get
    boosts = [b for b in (get(sg.DEX + "/token-boosts/top/v1") or []) if b.get("chainId") == "solana"][:12]
    mints = [b["tokenAddress"] for b in boosts if b.get("tokenAddress")]
    rows = {}
    if mints:
        for r in get(sg.DEX + "/tokens/v1/solana/" + ",".join(mints)) or []:
            m = (r.get("baseToken") or {}).get("address")
            if m and (m not in rows or float((r.get("liquidity") or {}).get("usd") or 0) > rows[m]["liq"]):
                rows[m] = _row(r)
    L, kb = ["🔎 SCANNER — moedas da Solana em alta no DexScreener agora"], []
    for m in mints:
        c = rows.get(m)
        if not c:
            continue
        L.append("• " + c["symbol"] + ": MC US$ " + _usd(c["mcap"]) + " · liq US$ " + _usd(c["liq"]) + " · 1 h " + _pct(c["chg"].get("h1")) +
                 " · 24 h " + _pct(c["chg"].get("h24")))
        kb.append([{"text": "📊 " + c["symbol"], "callback_data": "tc:" + m}])
    if len(L) == 1:
        L.append("Nada agora. Tente de novo em instantes.")
    L.append("⚠️ Em alta não quer dizer seguro: confira o anti-golpe no cartão.")
    return "\n".join(L), [kb[i] + kb[i + 1] if i + 1 < len(kb) else kb[i] for i in range(0, len(kb), 2)]


def upkeep_all(send, owner_chat):
    """Every minute: the owner's limit orders, everybody's DCA orders, the users' exits / limit orders.
    AIRDROP_ONLY: no new buys (limit / DCA off); open positions keep their exits."""
    if AIRDROP_ONLY:
        try:
            ut.upkeep(send, limits=False)
        except Exception as exc:
            logging.info("user trade upkeep: %s", type(exc).__name__)
        return
    if owner_chat:
        try:
            owner_upkeep(send, owner_chat)
            run_dca(owner_chat, send)
        except Exception as exc:
            logging.info("owner trade upkeep: %s", type(exc).__name__)
    for chat in ut.users():
        try:
            run_dca(chat, send)
        except Exception as exc:
            logging.info("dca %s: %s", chat, type(exc).__name__)
    try:
        ut.upkeep(send)
    except Exception as exc:
        logging.info("user trade upkeep: %s", type(exc).__name__)


def help_text():
    return ("🤖 COMO USAR\n"
            "1️⃣ 📥 Depositar: mande SOL para a sua carteira do bot.\n"
            "2️⃣ Cole aqui o contrato da moeda (ou o link do DexScreener, Pump.fun ou Birdeye).\n"
            "3️⃣ No cartão: 🟢 compre (valores rápidos ou X SOL), 🔴 venda 25 / 50 / 100%, 🎯 alvo, 🛑 stop loss, 🚀 stop móvel, "
            "📋 ordem limite (compra quando cair).\n"
            "4️⃣ 💼 Posições: lucro / prejuízo de cada moeda. 📋 Ordens limite: suas ordens. ⚙️ Configurações: valores rápidos, "
            "slippage, compra automática, alvo / stop padrão.\n"
            "5️⃣ 💸 Sacar: manda o SOL para a carteira que você quiser.\n"
            "Taxa para usuários: " + format(ut.FEE_RATE * 100, "g") + "% de cada compra e venda.")


AIRDROP_ALLOWED = {"thome", "/home", "tpl", "/posicoes", "towner", "tsec", "twal", "thelp", "/ajuda"}


def cb_blocked(action):
    """Trading buttons switched off in AIRDROP_ONLY (selling what is already open stays: ts / tsx)."""
    a = str(action or "")
    if a in AIRDROP_ALLOWED or a.startswith(("tad", "/airdrop")) or a.split(":")[0] in ("ts", "tsx"):
        return False
    if HOME == "robot" and (a in ("trpos", "/posicoes_robo", "tsn_on", "tsn_off", "tsn_reset", "trob", "trb_on", "trb_off", "trb_sol", "trb_bt", "trd_on", "trd_off", "trd_reset", "/robo", "/backtest", "tterms")):
        return False
    return a.startswith("t") and (a.split(":")[0] in ("tb", "tbx", "tbc", "tc", "tlo", "tld", "tla", "tlx", "tt", "tl", "tr", "tw", "tdo", "tdp",
                                                     "tdx", "tsp", "tsoon") or a in ("tbuy", "tsellm", "tlim", "tdl", "twl", "tscan", "tset", "tsb",
                                                                                     "tss", "tsa", "tst", "tsl", "tsr", "tsc", "tsv", "tsell", "tcopy",
                                                                                     "trob", "trb_on", "trb_off", "trb_sol", "trb_bt", "tterms",
                                                                                     "trade_help"))


def handle(action, text, send, chat, tail=None, run_async=None):
    """Callbacks of the trading screens and the typed amount. True when handled."""
    run_async = run_async or (lambda fn: threading.Thread(target=fn, daemon=True).start())
    tail = tail or []
    s = settings(chat)
    if action in ("vid", "vid_on", "vid_off", "vid_now", "vid_yt", "/videos"):
        if not is_owner(chat):
            return False
        import video_bot
        return video_bot.handle(action, send, chat, tail, run_async)
    if action in ("sup", "sup_on", "sup_off", "sup_code", "sup_min", "sup_now", "/tarefas"):
        import superteam_bot
        return superteam_bot.handle(action, send, chat, tail, run_async)
    if AIRDROP_ONLY and HOME == "robot" and (str(action).startswith(("tad", "/airdrop", "sup", "vid", "/tarefas", "/videos")) or
                                              action in ("towner", "tcopy")):
        send(chat, "🤖 O bot agora é só o robô das moedas grandes (demo e real). Essa função foi removida.", home_keyboard(chat))
        return True
    if airdrop_only(chat) and HOME == "btc" and str(action).startswith(("tad", "/airdrop")):
        send(chat, "O bot agora é só o robô Full Margin SOL (contas demo e real). Os airdrops foram removidos.", home_keyboard(chat))
        return True
    if AIRDROP_ONLY and HOME == "superteam" and (str(action).startswith(("tad", "/airdrop")) or
                                                  (text is not None and __import__("airdrop_bot").STATUS_RE.search(str(text)))):
        send(chat, "💰 O bot agora é só de tarefas pagas da Solana (Superteam Earn). Os airdrops foram removidos.", home_keyboard(chat))
        return True
    if user_blocked(chat, action):
        send(chat, "Aqui você pode comprar e vender moedas da Solana com a sua carteira (taxa " + format(ut.FEE_RATE * 100, "g") +
             "% por compra e por venda). Essa função não está disponível.", home_keyboard(chat))
        return True
    if airdrop_only(chat) and cb_blocked(action):
        send(chat, "O bot agora é só o robô Full Margin SOL (contas demo e real). Essa função foi removida."
             if HOME == "btc" else "🪂 O bot agora é só o caçador de airdrops. Essa função foi removida.", home_keyboard(chat))
        return True
    if action in ("tpl", "/posicoes"):
        run_async(lambda: send(chat, *(lambda t, kb: (t, kb + tail))(*positions_text(chat))))
        return True
    if action in ("tlim", "/ordens"):
        t, kb = limits_text(chat)
        send(chat, t, kb + tail)
        return True
    if action in ("tset", "/config"):
        t, kb = settings_text(chat)
        send(chat, t, kb + tail)
        return True
    if action in ("thelp", "/ajuda", "trade_help", "tbuy", "/comprar"):
        send(chat, help_text() if action != "tbuy" else "🟢 Cole aqui o contrato da moeda (ou o link do DexScreener / Pump.fun / Birdeye).", tail)
        return True
    if action == "tterms":
        ut.accept_terms(chat)
        send(chat, "✅ Pronto. Agora toque de novo no valor de compra no cartão da moeda.", tail)
        return True
    if action in ("tsb", "tss", "tsa", "tst", "tsl", "tsr"):
        if action == "tsb":
            presets = [list(p) for p in ut.BUY_PRESETS]
            cur = list(s["buy"])
            set_setting(chat, "buy", presets[(presets.index(cur) + 1) % len(presets)] if cur in presets else presets[0])
        elif action == "tss":
            set_setting(chat, "slippage_bps", _cycle(ut.SLIPPAGE_CHOICES, s["slippage_bps"]))
        elif action == "tsa":
            set_setting(chat, "auto_buy", _cycle((0.0,) + tuple(settings(chat)["buy"]), s.get("auto_buy")))
        else:
            field = {"tst": "tp", "tsl": "sl", "tsr": "trail"}[action]
            set_setting(chat, field, _cycle({"tp": TP_CHOICES, "sl": SL_CHOICES, "trail": TRAIL_CHOICES}[field], s.get(field)))
        t, kb = settings_text(chat)
        send(chat, t, kb + tail)
        return True
    if action in ("thome", "/home"):
        run_async(lambda: send(chat, home_text(chat), home_keyboard(chat)))
        return True
    if action == "tsellm":
        run_async(lambda: send(chat, *(lambda t, kb: ("🔴 VENDER: escolha a moeda\n" + t, kb + tail))(*positions_text(chat))))
        return True
    if action in ("tdl", "/dca"):
        t, kb = dca_text(chat)
        send(chat, t, kb + tail)
        return True
    if action in ("twl", "/watchlist"):
        run_async(lambda: send(chat, *(lambda t, kb: (t, kb + tail))(*watch_text(chat))))
        return True
    if action in ("tscan", "/scanner"):
        def go_scan():
            try:
                t, kb = scanner_text()
            except Exception as exc:
                t, kb = "🔎 Scanner indisponível agora (" + type(exc).__name__ + ").", []
            send(chat, t, kb + tail)
        run_async(go_scan)
        return True
    if action in ("tsc", "tsv"):
        key = "confirm" if action == "tsc" else "sell_protect"
        set_setting(chat, key, not s.get(key))
        t, kb = settings_text(chat)
        send(chat, t, kb + tail)
        return True
    if action == "tsell":
        on = s["tp"] or s["sl"] or s["trail"]
        for k, v in (("tp", 0.0), ("sl", 0.0), ("trail", 0.0)) if on else (("tp", 0.5), ("sl", 0.30), ("trail", 0.20)):
            set_setting(chat, k, v)
        t, kb = settings_text(chat)
        send(chat, ("🤖 Venda automática desligada." if on else "🤖 Venda automática ligada: alvo +50%, stop −30%, stop móvel 20% "
                    "(mude cada um nos botões padrão).") + "\n\n" + t, kb + tail)
        return True
    if action == "twal":
        w = _wallet(chat)
        send(chat, "👛 SUA CARTEIRA\n" + (w[0] if w else str(user_wallets.get_address(chat))) +
             ("\n" + format(w[1], ".4f") + " SOL (US$ " + format(w[2], ".2f") + ")" if w else "") +
             "\nCada conta do Telegram tem uma carteira só dela. Deposite em 📥 Depositar e retire em 💸 Sacar.", tail)
        return True
    if action == "tsec":
        send(chat, "🔐 SEGURANÇA DA CONTA\n• A chave privada fica criptografada no servidor e nunca aparece no chat.\n"
             "• Exportar a chave está desativado.\n• Só a sua conta do Telegram mexe na sua carteira.\n"
             "• Ninguém do bot pede sua chave ou frase secreta: se pedirem, é golpe.", tail)
        return True
    if action in ("trb_bt", "/backtest"):
        import big_bot
        why = big_bot.start_backtest(send, chat, run_async)
        send(chat, "⛔ " + why if why else "🧪 Backtest do robô moedas grandes rodando: " + str(big_bot.BT_DAYS) +
             " dias de velas reais de 1 h da Binance, a mesma regra do robô. Resultado em ~1 min. Só teste, nenhuma ordem real.", tail)
        return True
    import btc_fullmargin
    if action in ("fx", "fx_on", "fx_off", "fx_deploy", "fx_close", "/forex"):     # forex removed (owner, Nov 19)
        action = "bfm"
    if action in btc_fullmargin.ACTIONS:
        if not is_owner(chat):
            return False
        return btc_fullmargin.handle(action, send, chat, run_async)
    if airdrop_only(chat) and HOME == "btc" and action not in ("thome", "/home"):
        send(chat, "O bot agora é só o robô Full Margin SOL (contas demo e real). Essa função foi removida.", home_keyboard(chat))
        return True
    if action in ("trpos", "/posicoes_robo"):
        import big_bot
        run_async(lambda: send(chat, big_bot.positions_text(chat), home_keyboard(chat)))
        return True
    if action in ("tsn_on", "tsn_off", "tsn_reset"):
        send(chat, "🤖 O sniper foi removido: o bot agora é só o robô das moedas grandes.", home_keyboard(chat))
        return True
    if action in ("trd_on", "trd_off", "trd_reset"):
        import big_bot
        d = big_bot.demo(chat)
        if action == "trd_reset" or (action == "trd_on" and not d["started"]):
            d = big_bot.demo_reset(chat)
        if action in ("trd_on", "trd_off"):
            d["on"] = action == "trd_on"
            big_bot.set_demo(chat, d)
        send(chat, {"trd_on": "🧪 Conta DEMO ligada: US$ " + format(big_bot.DEMO_START, "g") + " de mentira, mesma regra do robô, preços reais. "
                              "Em " + str(big_bot.DEMO_DAYS) + " dias você vê o resultado do mês.",
                    "trd_off": "🧪 Conta DEMO desligada (o resultado fica guardado).",
                    "trd_reset": "🔁 Demo zerada: começa de novo com US$ " + format(big_bot.DEMO_START, "g") + " hoje (dia 0 de " +
                                 str(big_bot.DEMO_DAYS) + ")."}[action])
        action = "trob" if action != "trd_on" else "trd_go"
    if action in ("trob", "trb_on", "trb_off", "trb_sol", "/robo", "trd_go"):
        import big_bot
        r = big_bot.state(chat)
        if action == "trb_on" and is_owner(chat):
            try:
                import live_engine
                live_engine.start()                # the owner's real orders need the engine switched on
            except Exception as exc:
                send(chat, "⛔ Não consegui ligar as ordens reais: " + str(exc)[:150], tail)
                return True
        if action == "trb_on" and not is_owner(chat) and not ut.load(chat).get("terms_at"):
            send(chat, terms_text(), [[{"text": "✅ Aceito a taxa de " + format(ut.FEE_RATE * 100, "g") + "%", "callback_data": "tterms"}]])
            return True
        if action in ("trb_on", "trb_off"):
            r["on"] = action == "trb_on"
            big_bot.set_state(chat, r)
            if not is_owner(chat) and HOME != "robot":
                import owner_notify
                owner_notify.notify(("🤖 ROBÔ LIGADO por " if r["on"] else "🤖 Robô desligado por ") + owner_notify.who(chat) +
                                    " · " + format(r["sol"], "g") + " SOL por compra")
            send(chat, "🟢 Robô ligado: confere as moedas grandes a cada " + str(big_bot.CYCLE_S // 60) + " min e compra com a sua carteira."
                 if r["on"] else "🔴 Robô desligado: não compra mais. As moedas que ele comprou continuam com o stop loss e o stop móvel.")
        if action == "trb_sol":
            r["sol"] = _cycle(tuple(sorted(set(float(x) for x in s["buy"]))), r["sol"])
            big_bot.set_state(chat, r)

        def go_robot():
            try:
                sigs = big_bot.signals_now()
            except Exception:
                sigs = None
            send(chat, big_bot.status_text(chat, sigs), home_keyboard(chat) if HOME == "robot" else big_bot.keyboard(chat) + tail)
            if action == "trb_on":
                big_bot.run_for(chat, sigs or [], send)
            if action == "trd_go":
                big_bot.demo_run(chat, sigs or [], send)
        run_async(go_robot)
        return True
    if action == "tcopy" and is_owner(chat):
        send(chat, "👥 " + SOON["copy"] + ": em breve.", tail)
        return True
    if action.startswith("tsoon:"):
        send(chat, SOON.get(action.split(":", 1)[1], "Isso") + ": em breve. Ainda não está pronto.", tail)
        return True
    if action == "towner" and is_owner(chat):
        import owner_notify
        send(chat, "📊 PAINEL DO DONO\n" + owner_notify.summary() + "\n" + ut.owner_summary() + "\nTaxas vão para: " +
             str(__import__("user_copy").fee_wallet()) + "\n🔔 Você recebe aqui: pessoa nova, depósito, compra, venda, saque e robô ligado.", tail)
        return True
    head, _, rest = action.partition(":")
    if head == "tsp":
        if rest in ut.SPEEDS:
            set_setting(chat, "speed", rest)
        t, kb = settings_text(chat)
        send(chat, t, kb + tail)
        return True
    if head == "tw":
        on = toggle_watch(chat, rest)
        send(chat, "⭐ Na watchlist." if on else "Tirei da watchlist.", tail)
        return True
    if head == "tdo":
        send(chat, "🔁 ORDEM DCA: comprar em partes. Escolha o plano (valor de cada compra: " + _sol(s["buy"][0]) +
             ", mude em ⚙️ Compra rápida):",
             [[{"text": str(n) + "× a cada " + str(h) + " h", "callback_data": "tdp:" + str(i) + ":" + rest} for i, (n, h) in enumerate(DCA_PLANS)]] + tail)
        return True
    if head == "tdp":
        idx, _, mint = rest.partition(":")
        n, h = DCA_PLANS[int(idx)]

        def go_dca():
            if not is_owner(chat) and not ut.load(chat).get("terms_at"):
                send(chat, terms_text(), [[{"text": "✅ Aceito a taxa de " + format(ut.FEE_RATE * 100, "g") + "%", "callback_data": "tterms"}]])
                return
            c = info(mint) or {"symbol": mint[:6]}
            try:
                add_dca(chat, mint, c["symbol"], s["buy"][0], n, h)
            except ValueError as exc:
                send(chat, "⛔ " + str(exc), tail)
                return
            send(chat, "✅ DCA criado: " + str(n) + " compras de " + _sol(s["buy"][0]) + " de " + c["symbol"] + ", uma a cada " + str(h) +
                 " h. A primeira sai em até 1 min.", tail)
        run_async(go_dca)
        return True
    if head == "tdx":
        send(chat, ("❌ DCA cancelado." if cancel_dca(chat, rest) else "Esse DCA já não existe."), tail)
        return True
    if head == "tbc":
        sol, _, mint = rest.partition(":")
        send(chat, "🟡 Comprando " + sol + " SOL…")
        run_async(lambda: buy(chat, mint, float(sol), send))
        return True
    if head == "tsx":
        pct, _, mint = rest.partition(":")
        send(chat, "🟡 Vendendo " + pct + "%…")
        run_async(lambda: sell(chat, mint, int(pct), send))
        return True
    if head == "tb" and s.get("confirm"):
        sol, _, mint = rest.partition(":")
        send(chat, "Confirma a compra de " + sol + " SOL?", [[{"text": "✅ Comprar", "callback_data": "tbc:" + rest},
                                                              {"text": "❌ Cancelar", "callback_data": "tc:" + mint}]])
        return True
    if head == "ts" and s.get("sell_protect") and int(rest.partition(":")[0]) >= 75:
        pct, _, mint = rest.partition(":")
        send(chat, "🛡️ Proteção de venda: confirma vender " + pct + "%?", [[{"text": "✅ Vender " + pct + "%", "callback_data": "tsx:" + rest},
                                                                           {"text": "❌ Cancelar", "callback_data": "tc:" + mint}]])
        return True
    if head == "tb":
        sol, _, mint = rest.partition(":")
        send(chat, "🟡 Comprando " + sol + " SOL…")
        run_async(lambda: buy(chat, mint, float(sol), send))
        return True
    if head == "tbx":
        INPUT[chat] = {"mint": rest, "at": time.time()}
        send(chat, "✍️ Quantos SOL quer comprar? Mande só o número (ex.: 0.3). /cancelar para desistir.")
        return True
    if head == "ts":
        pct, _, mint = rest.partition(":")
        send(chat, "🟡 Vendendo " + pct + "%…")
        run_async(lambda: sell(chat, mint, int(pct), send))
        return True
    if head in ("tt", "tl", "tr"):
        field = {"tt": "tp", "tl": "sl", "tr": "trail"}[head]
        v, on_position = set_option(chat, rest, field)
        name = {"tp": "🎯 Alvo", "sl": "🛑 Stop loss", "trail": "🚀 Stop móvel"}[field]
        shown = (_opt(v, "+" if field == "tp" else "−") if field != "trail" or not v
                 else format(v * 100, "g") + "% abaixo do topo (liga em +" + format(TRAIL_ARM * 100, "g") + "%)")
        send(chat, name + ": " + shown + (" nessa posição." if on_position else " nas próximas compras."))
        run_async(lambda: show(send, chat, rest, tail, safety=False))
        return True
    if head == "tc":
        run_async(lambda: show(send, chat, rest, tail) or send(chat, "⛔ Não achei essa moeda no DexScreener.", tail))
        return True
    if head == "tlo":
        send(chat, "📋 ORDEM LIMITE: comprar quando o preço cair quanto?",
             [[{"text": "−" + format(x * 100, "g") + "%", "callback_data": "tld:" + format(x * 100, "g") + ":" + rest} for x in LIMIT_DROPS]] + tail)
        return True
    if head == "tld":
        drop, _, mint = rest.partition(":")
        send(chat, "📋 Comprar quanto quando cair " + drop + "%?",
             [[{"text": _sol(v), "callback_data": "tla:" + drop + ":" + str(i) + ":" + mint} for i, v in enumerate(s["buy"])]] + tail)
        return True
    if head == "tla":
        drop, idx, mint = rest.split(":", 2)

        def go():
            c = info(mint)
            if not c or not c["price"]:
                send(chat, "⛔ Não achei o preço dessa moeda agora.", tail)
                return
            if not is_owner(chat) and not ut.load(chat).get("terms_at"):
                send(chat, terms_text(), [[{"text": "✅ Aceito a taxa de " + format(ut.FEE_RATE * 100, "g") + "%", "callback_data": "tterms"}]])
                return
            try:
                add_limit(chat, mint, c["symbol"], c["price"], float(drop) / 100, float(s["buy"][int(idx)]))
            except ValueError as exc:
                send(chat, "⛔ " + str(exc), tail)
                return
            send(chat, "✅ Ordem limite criada: compra " + _sol(s["buy"][int(idx)]) + " de " + c["symbol"] + " quando chegar a US$ " +
                 _price(c["price"] * (1 - float(drop) / 100)) + " (−" + drop + "%). Vale 7 dias.", tail)
        run_async(go)
        return True
    if head == "tlx":
        send(chat, ("❌ Ordem cancelada." if cancel_limit(chat, rest) else "Essa ordem já não existe."), tail)
        return True
    import airdrop_bot
    if action in ("tad", "/airdrop", "tad_on", "tad_off", "tad_w", "tad_now"):
        a = airdrop_bot.state(chat)
        if action == "tad_w":
            airdrop_bot.WAIT[chat] = time.time()
            send(chat, "✏️ Mande o endereço PÚBLICO da carteira Solana que vai nas respostas dos airdrops (ex.: da sua Phantom). "
                 "Nunca mande chave nem frase secreta. /cancelar para desistir.")
            return True
        if action in ("tad_on", "tad_off"):
            if action == "tad_on" and not a["wallet"]:
                airdrop_bot.WAIT[chat] = time.time()
                send(chat, "Primeiro mande o endereço PÚBLICO da carteira Solana que vai nas respostas (ex.: da sua Phantom).")
                return True
            a["on"] = action == "tad_on"
            airdrop_bot.set_state(chat, a)
            if a["on"]:
                run_async(lambda: airdrop_bot.run_for(chat, send, airdrop_bot.find(), force=True) or
                          send(chat, "🪂 Nenhum airdrop novo da Solana agora. Te aviso quando aparecer."))
        if action == "tad_now":
            if not a["wallet"]:
                airdrop_bot.WAIT[chat] = time.time()
                send(chat, "Primeiro mande o endereço PÚBLICO da carteira Solana que vai nas respostas.")
                return True
            send(chat, "🔎 Procurando airdrops da Solana…")
            run_async(lambda: airdrop_bot.run_for(chat, send, airdrop_bot.find(), force=True) or
                      send(chat, "🪂 Nenhum airdrop novo da Solana agora.", tail))
            return True
        t, kb = airdrop_bot.screen(chat)
        send(chat, t, kb + tail)
        return True
    if chat in airdrop_bot.WAIT and text is not None:
        if str(text).strip() == "/cancelar" or time.time() - airdrop_bot.WAIT[chat] > 600:
            airdrop_bot.WAIT.pop(chat, None)
            if str(text).strip() == "/cancelar":
                send(chat, "Cancelado.", tail)
                return True
        else:
            w = airdrop_bot.valid_wallet(text)
            if not w:
                send(chat, "⛔ Endereço inválido. Mande o endereço público da carteira Solana (ou /cancelar).")
                return True
            airdrop_bot.WAIT.pop(chat, None)
            a = airdrop_bot.state(chat)
            a["wallet"] = w
            a["on"] = True                            # owner (Nov 18): hunts all day as soon as the wallet is set
            airdrop_bot.set_state(chat, a)
            run_async(lambda: airdrop_bot.run_for(chat, send, airdrop_bot.find(), force=True))
            t, kb = airdrop_bot.screen(chat)
            send(chat, "✅ Carteira dos airdrops: " + w + "\n\n" + t, kb + tail)
            return True
    if text and airdrop_bot.STATUS_RE.search(str(text)):
        out = airdrop_bot.from_post_link(chat, text)
        if out == "wallet":
            airdrop_bot.WAIT[chat] = time.time()
            send(chat, "Primeiro mande o endereço PÚBLICO da carteira Solana que vai nas respostas dos airdrops.")
            return True
        if out:
            send(chat, out[0], out[1] + tail)
            return True
    pending = INPUT.get(chat)
    if pending and time.time() - pending["at"] < 600 and text is not None and not str(text).startswith("/"):
        INPUT.pop(chat, None)
        try:
            sol = float(str(text).lower().replace("sol", "").replace(",", ".").strip())
        except ValueError:
            send(chat, "⛔ Valor inválido. Toque de novo em 'Comprar X SOL'.")
            return True
        if sol <= 0:
            send(chat, "⛔ Valor inválido.")
            return True
        send(chat, "🟡 Comprando " + format(sol, "g") + " SOL…")
        run_async(lambda: buy(chat, pending["mint"], sol, send))
        return True
    if text and str(text).strip() == "/cancelar" and INPUT.pop(chat, None):
        send(chat, "Cancelado.", tail)
        return True
    return False
