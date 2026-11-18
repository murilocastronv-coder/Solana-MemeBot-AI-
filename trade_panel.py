"""Manual trading panel (owner, Nov 18): paste a coin's contract (or a DexScreener / Pump.fun / Birdeye link) in the
chat and get a card with the coin's data, the anti-scam check and buttons to buy (US$ 10 / 25 / 50 / 100 / any value),
sell (25 / 50 / 100 %) and set the position's take profit, stop loss and trailing stop.

The buys and sells go through the same engine as the robots (live_engine / live_order_bridge: Jupiter, the encrypted
wallet, settlement on the blockchain). The exits run in live_exit_monitor (mode "trade" -> exit_signal here)."""
import json
import logging
import os
import re
import threading
import time

MODE = "trade"
KEY = "trade_panel"
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
BUY_USD = tuple(float(x) for x in os.getenv("TRADE_BUY_USD", "10,25,50,100").split(",") if x.strip())
TP_CHOICES = (0.0, 0.25, 0.50, 1.0, 2.0)          # take profit: off, +25%, +50%, +100%, +200%
SL_CHOICES = (0.0, 0.15, 0.30, 0.50)              # stop loss: off, -15%, -30%, -50%
TRAIL_CHOICES = (0.0, 0.10, 0.20, 0.30)           # trailing stop: off, 10 / 20 / 30% below the top
TRAIL_ARM = float(os.getenv("TRADE_TRAIL_ARM", "0.10"))   # the trailing stop starts once the coin is +10%
B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
INPUT = {}                                        # chat -> {"mint", "at"}: waiting for a "US$ X" amount
_LOCK = threading.RLock()
_MEM = {}


def _load():
    with _LOCK:
        if not _MEM:
            d = {"tp": 0.0, "sl": 0.0, "trail": 0.0}
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
    """The coin named in `text`: DexScreener's most liquid pool. A pair address (DexScreener links) is resolved to
    the coin. None when nothing matches."""
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


# ------------------------------------------------------------------ exits of the panel's positions
def settings_of(position):
    d = _load()
    return {k: float(position.get("trade_" + k, d.get(k) or 0) or 0) for k in ("tp", "sl", "trail")}


def exit_signal(position, ratio, peak):
    """(action, reason) for a panel position: TP, SL, trailing stop (armed at +TRAIL_ARM), else hold."""
    s = settings_of(position)
    if s["sl"] and ratio <= 1 - s["sl"]:
        return "sell", "trade_sl"
    if s["tp"] and ratio >= 1 + s["tp"]:
        return "sell", "trade_tp"
    if s["trail"] and peak >= 1 + TRAIL_ARM:
        if ratio <= peak * (1 - s["trail"]):
            return "sell", "trade_trail"
        return "hold", "trailing"
    return "hold", "trade_hold"


# ------------------------------------------------------------------ the card
def _usd(x):
    x = float(x or 0)
    return (format(x / 1e9, ",.2f") + " bi" if x >= 1e9 else format(x / 1e6, ",.2f") + " mi" if x >= 1e6
            else format(x / 1e3, ",.1f") + " mil" if x >= 1e3 else format(x, ",.2f"))


def _price(x):
    x = float(x or 0)
    if x >= 1:
        return format(x, ",.4f")
    s = format(x, ".12f").rstrip("0")
    zeros = len(s.split(".")[1]) - len(s.split(".")[1].lstrip("0")) if "." in s else 0
    return format(x, "." + str(min(zeros + 4, 12)) + "f")


def _pct(v):
    try:
        return format(float(v), "+.1f") + "%"
    except (TypeError, ValueError):
        return "—"


def _opt(v, sign):
    return "off" if not v else sign + format(v * 100, "g") + "%"


def card_text(c, position=None, value=None, safety=None, sol=None, now=None):
    now = now or time.time()
    age = (now - c["created"]) / 86400 if c.get("created") else None
    L = ["🪙 " + (c["name"] + " (" + c["symbol"] + ")" if c.get("name") else c["symbol"]),
         "📄 " + c["mint"],
         "💲 Preço: US$ " + _price(c["price"]) + " · MC US$ " + _usd(c["mcap"]) + " · 💧 Liquidez US$ " + _usd(c["liq"]),
         "📈 5 min " + _pct(c["chg"].get("m5")) + " · 1 h " + _pct(c["chg"].get("h1")) + " · 6 h " + _pct(c["chg"].get("h6")) +
         " · 24 h " + _pct(c["chg"].get("h24")) + " · volume 24 h US$ " + _usd(c["vol24"]),
         "⏳ " + ("criada há " + (format(age, ".0f") + " dias" if age >= 1 else format(age * 24, ".0f") + " h") if age is not None else "idade ?") +
         (" · " + c["dex"] if c.get("dex") else ""),
         "🛡️ Anti-golpe: " + ("✅ OK" if safety == "" else "⚠️ " + str(safety) if safety else "não conferido")]
    if position:
        cost = float(position.get("cost_usd") or 0)
        s = settings_of(position)
        L.append("💼 Sua posição: custo US$ " + format(cost, ".2f") +
                 (" → vale US$ " + format(value, ".2f") + " (" + _pct((value / cost - 1) * 100 if cost else 0) + ")" if value is not None else "") +
                 ("" if position.get("mode") == MODE else " · robô: " + str(position.get("mode"))))
        if position.get("mode") == MODE:
            L.append("⚙️ Alvo " + _opt(s["tp"], "+") + " · stop " + _opt(s["sl"], "−") + " · stop móvel " +
                     ("off" if not s["trail"] else format(s["trail"] * 100, "g") + "% abaixo do topo (liga em +" + format(TRAIL_ARM * 100, "g") + "%)"))
    else:
        d = _load()
        L.append("⚙️ Na compra: alvo " + _opt(d["tp"], "+") + " · stop " + _opt(d["sl"], "−") + " · stop móvel " +
                 _opt(d["trail"], ""))
    if sol:
        L.append("👛 Carteira: " + format(sol[0], ".4f") + " SOL (US$ " + format(sol[1], ".2f") + ")")
    L.append("🔗 https://dexscreener.com/solana/" + c["mint"] + "\n🔗 https://rugcheck.xyz/tokens/" + c["mint"])
    return "\n".join(L)


def card_keyboard(mint, position=None):
    kb = [[{"text": "🟢 US$ " + format(v, "g"), "callback_data": "tb:" + format(v, "g") + ":" + mint} for v in BUY_USD],
          [{"text": "🟢 Comprar US$ X", "callback_data": "tbx:" + mint}]]
    if position:
        kb.append([{"text": "🔴 Vender " + str(p) + "%", "callback_data": "ts:" + str(p) + ":" + mint} for p in (25, 50, 100)])
    target = position if position and position.get("mode") == MODE else None
    s = settings_of(target) if target else _load()
    kb.append([{"text": "🎯 Alvo " + _opt(s["tp"], "+"), "callback_data": "tt:" + mint},
               {"text": "🛑 Stop " + _opt(s["sl"], "−"), "callback_data": "tl:" + mint},
               {"text": "🚀 Móvel " + _opt(s["trail"], ""), "callback_data": "tr:" + mint}])
    kb.append([{"text": "🔄 Atualizar", "callback_data": "tc:" + mint}, {"text": "💼 Minhas trades", "callback_data": "tpl"}])
    return kb


def _position(mint):
    import live_trade_state as st
    return st._load()["positions"].get(mint)


def _value(mint, position):
    if not position:
        return None
    try:
        import live_exit_monitor as mon
        return mon.quote_value_usd(mint, int(position["token_amount_raw"]))
    except Exception:
        return None


def _sol():
    try:
        import live_engine
        import live_order_bridge as bridge
        w = live_engine.get_address()
        if not w:
            return None
        sol = bridge.get_sol_balance(w) / 1e9
        return sol, sol * float(bridge.sol_usd_price())
    except Exception:
        return None


def show(send, chat, text, keyboard_tail=None, safety=True):
    """Card of the coin named in `text`. Returns False when no coin was found."""
    c = info(text)
    if not c:
        return False
    position = _position(c["mint"])
    why = None
    if safety:
        try:
            import signals as sg
            why = sg.safety_reason(c["mint"]) or ""
        except Exception:
            why = None
    send(chat, card_text(c, position, _value(c["mint"], position), why, _sol()), card_keyboard(c["mint"], position) + (keyboard_tail or []))
    return True


# ------------------------------------------------------------------ buttons
def _cycle(choices, current):
    nxt = [c for c in choices if c > float(current or 0) + 1e-9]
    return nxt[0] if nxt else choices[0]


def set_option(mint, field):
    """Next value of tp / sl / trail: on the open panel position, else the default for the next buys."""
    import live_trade_state as st
    choices = {"tp": TP_CHOICES, "sl": SL_CHOICES, "trail": TRAIL_CHOICES}[field]
    position = _position(mint)
    if position and position.get("mode") == MODE:
        v = _cycle(choices, settings_of(position)[field])
        st.set_position_fields(mint, **{"trade_" + field: v})
        return v, True
    with _LOCK:
        d = _load()
        d[field] = _cycle(choices, d.get(field))
        _save()
        return d[field], False


def buy(mint, usd, send, chat):
    import live_engine
    c = info(mint) or {"symbol": mint[:6]}
    wallet = live_engine.get_address()
    if not wallet:
        send(chat, "⛔ Carteira do robô não configurada.")
        return None
    position = _position(mint)
    if position and position.get("mode") != MODE:
        send(chat, "⛔ Essa moeda é do " + str(position.get("mode")) + " (robô). Venda ou espere ele sair antes de comprar à mão.")
        return None
    settled = live_engine._execute_buy(None, mint, c["symbol"], send, chat, wallet, None, MODE, usd=float(usd),
                                       add_on=bool(position))
    if settled and settled.get("status") == "settled_buy" and not position:
        import live_trade_state as st
        d = _load()
        st.set_position_fields(mint, trade_tp=d["tp"], trade_sl=d["sl"], trade_trail=d["trail"], peak_ratio=1.0)
    return settled


def sell(mint, pct, send, chat):
    import live_engine
    position = _position(mint)
    if not position:
        send(chat, "⛔ O bot não tem posição dessa moeda (se você comprou fora do bot, venda na sua carteira).")
        return "gone"
    if int(pct) >= 100:
        return live_engine.sell_now(mint, "trade_manual", send, chat)
    return live_engine.sell_part(mint, int(pct) / 100, "trade_manual", send, chat, note="você mandou vender " + str(int(pct)) + "%")


def positions_text():
    import live_trade_state as st
    rows = [(m, p) for m, p in st._load()["positions"].items() if p.get("mode") == MODE]
    if not rows:
        return "💼 Nenhuma trade manual aberta. Cole o contrato de uma moeda aqui para ver o cartão e comprar.", []
    L, kb, total_cost, total_val = ["💼 MINHAS TRADES"], [], 0.0, 0.0
    for m, p in rows:
        cost = float(p.get("cost_usd") or 0)
        val = _value(m, p)
        total_cost += cost
        total_val += val if val is not None else cost
        s = settings_of(p)
        L.append("• " + str(p.get("symbol") or m[:6]) + ": US$ " + format(cost, ".2f") +
                 (" → US$ " + format(val, ".2f") + " (" + _pct((val / cost - 1) * 100 if cost else 0) + ")" if val is not None else " → ?") +
                 " · alvo " + _opt(s["tp"], "+") + " · stop " + _opt(s["sl"], "−") + " · móvel " + _opt(s["trail"], ""))
        kb.append([{"text": "📊 " + str(p.get("symbol") or m[:6]), "callback_data": "tc:" + m},
                   {"text": "🔴 Vender 100%", "callback_data": "ts:100:" + m}])
    L.append("Total: US$ " + format(total_cost, ".2f") + " → US$ " + format(total_val, ".2f") + " (" +
             format(total_val - total_cost, "+.2f") + ")")
    return "\n".join(L), kb


def handle(action, text, send, chat, keyboard_tail=None, run_async=None):
    """Telegram callbacks tb / tbx / ts / tt / tl / tr / tc / tpl and the typed amount. True when handled."""
    run_async = run_async or (lambda fn: threading.Thread(target=fn, daemon=True).start())
    tail = keyboard_tail or []
    if action == "tpl":
        t, kb = positions_text()
        send(chat, t, kb + tail)
        return True
    head, _, rest = action.partition(":")
    if head == "tb":
        usd, _, mint = rest.partition(":")
        send(chat, "🟡 Comprando US$ " + usd + "…")
        run_async(lambda: buy(mint, float(usd), send, chat))
        return True
    if head == "tbx":
        INPUT[chat] = {"mint": rest, "at": time.time()}
        send(chat, "✍️ Quanto quer comprar? Mande só o número em US$ (ex.: 15). /cancelar para desistir.")
        return True
    if head == "ts":
        pct, _, mint = rest.partition(":")
        send(chat, "🟡 Vendendo " + pct + "%…")

        def go():
            try:
                sell(mint, int(pct), send, chat)
            except Exception as exc:
                send(chat, "⛔ Venda: " + type(exc).__name__ + ": " + str(exc)[:150])
        run_async(go)
        return True
    if head in ("tt", "tl", "tr"):
        field = {"tt": "tp", "tl": "sl", "tr": "trail"}[head]
        v, on_position = set_option(rest, field)
        name = {"tp": "🎯 Alvo", "sl": "🛑 Stop loss", "trail": "🚀 Stop móvel"}[field]
        send(chat, name + ": " + (_opt(v, "+" if field == "tp" else "−" if field == "sl" else "") if field != "trail" or not v
                                  else format(v * 100, "g") + "% abaixo do topo (liga em +" + format(TRAIL_ARM * 100, "g") + "%)") +
             (" nessa posição." if on_position else " nas próximas compras pelo painel."))
        run_async(lambda: show(send, chat, rest, tail, safety=False))
        return True
    if head == "tc":
        run_async(lambda: show(send, chat, rest, tail) or send(chat, "⛔ Não achei essa moeda no DexScreener.", tail))
        return True
    pending = INPUT.get(chat)
    if pending and time.time() - pending["at"] < 600 and text is not None and not str(text).startswith("/"):
        INPUT.pop(chat, None)
        try:
            usd = float(str(text).replace("US$", "").replace("$", "").replace(",", ".").strip())
        except ValueError:
            send(chat, "⛔ Valor inválido. Toque de novo em 'Comprar US$ X'.")
            return True
        if usd < 1:
            send(chat, "⛔ Mínimo US$ 1.")
            return True
        send(chat, "🟡 Comprando US$ " + format(usd, "g") + "…")
        run_async(lambda: buy(pending["mint"], usd, send, chat))
        return True
    if text and str(text).strip() == "/cancelar" and INPUT.pop(chat, None):
        send(chat, "Cancelado.", tail)
        return True
    return False
