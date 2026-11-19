"""Robô FOREX (owner, Nov 18): trades the owner's MT5 account (DEMO first) through MetaApi (metaapi.cloud).

Credentials only from Render: METAAPI_TOKEN and METAAPI_ACCOUNT_ID (never in the chat).
Rule (trend following on 1-hour candles, the same for every pair):
  - trend: EMA 50 above EMA 200 = only buys; below = only sells;
  - entry: the last closed candle came back to the EMA 20 (pullback) and RSI(14) turns back in the trend direction
    (buy: RSI crossed up through 40; sell: RSI crossed down through 60);
  - stop = 1.5 x ATR(14), target = 3 x ATR; after +1.5 x ATR the stop follows the price 1.5 x ATR behind (trailing);
  - 0.01 lot per order, at most MAX_OPEN orders, one per pair; checked every CYCLE_S.
Nothing here promises profit: most retail forex accounts lose money; the demo shows whether this rule works."""
import json
import logging
import os
import threading
import time

import requests

PAIRS = tuple(p.strip().upper() for p in os.getenv("FOREX_PAIRS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF").split(",") if p.strip())
LOT = float(os.getenv("FOREX_LOT", "0.01"))
MAX_OPEN = int(os.getenv("FOREX_MAX_OPEN", "3"))
TF = os.getenv("FOREX_TF", "1h")
SL_ATR, TP_ATR, TRAIL_ATR = 1.5, 3.0, 1.5
CYCLE_S = int(os.getenv("FOREX_CYCLE_S", "300"))
COMMENT = "robo-forex"
PROV = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
KEY = "forex_bot"
_LOCK = threading.RLock()
_MEM = {}
_REGION = {}
LAST = {"text": None, "at": 0.0}           # last status screen (the home screen must answer at once)


def configured():
    return bool(os.getenv("METAAPI_TOKEN") and os.getenv("METAAPI_ACCOUNT_ID"))


# ---------------------------------------------------------------- state (pg_store)
def _load():
    with _LOCK:
        if not _MEM:
            d = {"on": False, "open": {}, "hist": [], "symbols": {}, "last_bar": {}, "log": []}
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
        logging.warning("forex save: %s", type(exc).__name__)


def note(text):
    with _LOCK:
        d = _load()
        d["log"] = (d.get("log") or [])[-29:] + [time.strftime("%d/%m %H:%M", time.gmtime()) + " " + text]
        _save()
    logging.info("forex: %s", text)


# ---------------------------------------------------------------- MetaApi REST
class Api:
    """Thin MetaApi REST client (requests). `http` can be replaced in tests."""

    def __init__(self, token=None, account=None, http=None):
        self.token = token or os.getenv("METAAPI_TOKEN", "")
        self.account = account or os.getenv("METAAPI_ACCOUNT_ID", "")
        self.http = http or requests

    def _h(self):
        return {"auth-token": self.token, "Content-Type": "application/json", "Accept": "application/json"}

    def _req(self, method, url, body=None, timeout=30):
        r = self.http.request(method, url, headers=self._h(), data=json.dumps(body) if body is not None else None, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError("MetaApi " + str(r.status_code) + ": " + str(getattr(r, "text", ""))[:200])
        return r.json() if getattr(r, "text", "") else {}

    def account_info(self):
        return self._req("GET", PROV + "/users/current/accounts/" + self.account)

    def deploy(self):
        return self._req("POST", PROV + "/users/current/accounts/" + self.account + "/deploy")

    def region(self):
        if self.account not in _REGION:
            _REGION[self.account] = (self.account_info().get("region") or "new-york")
        return _REGION[self.account]

    def client(self):
        return "https://mt-client-api-v1." + self.region() + ".agiliumtrade.ai/users/current/accounts/" + self.account

    def market(self):
        return "https://mt-market-data-client-api-v1." + self.region() + ".agiliumtrade.ai/users/current/accounts/" + self.account

    def info(self):
        return self._req("GET", self.client() + "/account-information")

    def positions(self):
        return self._req("GET", self.client() + "/positions") or []

    def symbols(self):
        return self._req("GET", self.client() + "/symbols") or []

    def price(self, symbol):
        return self._req("GET", self.client() + "/symbols/" + symbol + "/current-price")

    def candles(self, symbol, tf=TF, limit=300):
        return self._req("GET", self.market() + "/historical-market-data/symbols/" + symbol + "/timeframes/" + tf +
                         "/candles?limit=" + str(limit)) or []

    def trade(self, body):
        return self._req("POST", self.client() + "/trade", body)


# ---------------------------------------------------------------- indicators and rule
def ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    out, gain, loss = [None] * len(closes), 0.0, 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l_ = max(ch, 0), max(-ch, 0)
        if i <= n:
            gain += g / n
            loss += l_ / n
            if i == n:
                out[i] = 100 - 100 / (1 + gain / loss) if loss else 100.0
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l_) / n
            out[i] = 100 - 100 / (1 + gain / loss) if loss else 100.0
    return out


def atr(cs, n=14):
    trs = [cs[0]["high"] - cs[0]["low"]] + [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
                                          for p, c in zip(cs, cs[1:])]
    out, a = [], None
    for i, t in enumerate(trs):
        a = t if a is None else (a * (n - 1) + t) / n
        out.append(a)
    return out


def signal(cs):
    """('buy' | 'sell' | None, atr) from CLOSED candles [{time, open, high, low, close}] oldest first."""
    if len(cs) < 210:
        return None, 0.0
    closes = [c["close"] for c in cs]
    e20, e50, e200, r, a = ema(closes, 20), ema(closes, 50), ema(closes, 200), rsi(closes), atr(cs)
    c, i = cs[-1], len(cs) - 1
    if r[i] is None or r[i - 1] is None:
        return None, a[i]
    if e50[i] > e200[i] and c["low"] <= e20[i] <= c["high"] + a[i] * 0.1 and r[i - 1] < 40 <= r[i] and c["close"] > e50[i] - a[i]:
        return "buy", a[i]
    if e50[i] < e200[i] and c["low"] - a[i] * 0.1 <= e20[i] <= c["high"] and r[i - 1] > 60 >= r[i] and c["close"] < e50[i] + a[i]:
        return "sell", a[i]
    return None, a[i]


def levels(side, price, a):
    if side == "buy":
        return price - SL_ATR * a, price + TP_ATR * a
    return price + SL_ATR * a, price - TP_ATR * a


def trail_stop(side, open_price, price, stop, a):
    """New stop for a trailing position (or None): after +TRAIL_ATR*ATR the stop follows TRAIL_ATR*ATR behind."""
    if side == "buy" and price - open_price >= TRAIL_ATR * a:
        new = price - TRAIL_ATR * a
        return new if stop is None or new > stop else None
    if side == "sell" and open_price - price >= TRAIL_ATR * a:
        new = price + TRAIL_ATR * a
        return new if stop is None or new < stop else None
    return None


# ---------------------------------------------------------------- cycle
def _bars(raw):
    out = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        try:
            out.append({"time": str(c.get("time")), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
                        "close": float(c["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda c: c["time"])


def resolve_symbols(api):
    """Broker names of the pairs (some brokers add suffixes like EURUSD.m)."""
    d = _load()
    if d.get("symbols") and all(p in d["symbols"] for p in PAIRS):
        return d["symbols"]
    names = [str(s) for s in api.symbols()]
    out = {}
    for p in PAIRS:
        exact = [n for n in names if n.upper() == p]
        pref = [n for n in names if n.upper().startswith(p)]
        if exact or pref:
            out[p] = (exact or sorted(pref, key=len))[0]
    with _LOCK:
        d["symbols"] = out
        _save()
    return out


def _side(pos):
    return "buy" if "BUY" in str(pos.get("type", "")).upper() else "sell"


def cycle(send=None, chat=None, api=None, now=None):
    """One check: notices of closed robot positions, trailing stops, then new entries on newly closed candles."""
    d = _load()
    if not configured() and api is None:
        return "não configurado"
    api = api or Api()
    poss = [p for p in api.positions() if str(p.get("comment") or "").startswith(COMMENT) or str(p.get("id")) in d["open"]]
    live_ids = {str(p.get("id")) for p in poss}
    for pid, o in list(d["open"].items()):                      # closed since the last check (stop / target / by hand)
        if pid not in live_ids:
            with _LOCK:
                d["open"].pop(pid, None)
                d["hist"] = (d.get("hist") or [])[-299:] + [dict(o, closed=time.time())]
                _save()
            if send and chat:
                send(chat, "💱 FOREX: posição FECHADA — " + o["symbol"] + " " + ("COMPRA" if o["side"] == "buy" else "VENDA") +
                     " aberta a " + format(o["price"], ".5g") + " (stop " + format(o["sl"], ".5g") + " / alvo " + format(o["tp"], ".5g") +
                     "). Veja o resultado no MT5 → Histórico.")
    for p in poss:                                               # trailing stop
        pid = str(p.get("id"))
        o = d["open"].get(pid) or {}
        a = float(o.get("atr") or 0)
        cur = float(p.get("currentPrice") or 0)
        if not a or not cur:
            continue
        new = trail_stop(_side(p), float(p.get("openPrice") or o.get("price") or 0), cur, p.get("stopLoss"), a)
        if new:
            try:
                api.trade({"actionType": "POSITION_MODIFY", "positionId": pid, "stopLoss": round(new, 6),
                           "takeProfit": p.get("takeProfit")})
                note(o.get("symbol", "?") + ": stop móvel subiu para " + format(new, ".5g"))
            except Exception as exc:
                note("stop móvel " + o.get("symbol", "?") + " falhou: " + str(exc)[:80])
    if not d.get("on"):
        return "desligado"
    held = {str(p.get("symbol", "")).upper()[:6] for p in poss}
    if len(poss) >= MAX_OPEN:
        return "máximo de ordens abertas"
    syms = resolve_symbols(api)
    opened = []
    for pair, sym in syms.items():
        if pair in held or len(poss) + len(opened) >= MAX_OPEN:
            continue
        cs = _bars(api.candles(sym))
        if len(cs) < 2:
            continue
        closed = cs[:-1]                                          # the last candle is still forming
        last = closed[-1]["time"] if closed else None
        if d["last_bar"].get(pair) == last:
            continue                                              # this candle was already checked
        with _LOCK:
            d["last_bar"][pair] = last
            _save()
        side, a = signal(closed)
        if not side:
            continue
        q = api.price(sym)
        px = float(q.get("ask") if side == "buy" else q.get("bid"))
        sl, tp = levels(side, px, a)
        res = api.trade({"actionType": "ORDER_TYPE_BUY" if side == "buy" else "ORDER_TYPE_SELL", "symbol": sym, "volume": LOT,
                         "stopLoss": round(sl, 6), "takeProfit": round(tp, 6), "comment": COMMENT})
        pid = str(res.get("positionId") or res.get("orderId") or "")
        with _LOCK:
            d["open"][pid] = {"symbol": sym, "side": side, "price": px, "sl": sl, "tp": tp, "atr": a, "lot": LOT, "t": time.time()}
            _save()
        opened.append(pair)
        note(("COMPRA " if side == "buy" else "VENDA ") + sym + " a " + format(px, ".5g"))
        if send and chat:
            send(chat, "💱 FOREX ABRIU " + ("🟢 COMPRA" if side == "buy" else "🔴 VENDA") + " " + sym + " (conta demo)\n"
                 "💲 Preço: " + format(px, ".5g") + " · " + format(LOT, "g") + " lote\n"
                 "🛑 Stop: " + format(sl, ".5g") + " · 🎯 Alvo: " + format(tp, ".5g") + " · 🚀 stop móvel depois de +" + format(TRAIL_ATR, "g") + " ATR\n"
                 "📈 Motivo: tendência de " + ("alta" if side == "buy" else "baixa") + " (média 50 x 200) e o preço voltou à média de 20")
    return "abriu " + ", ".join(opened) if opened else "sem sinal"


# ---------------------------------------------------------------- Telegram screen
def status_text(api=None):
    d = _load()
    L = ["💱 ROBÔ FOREX — MT5 via MetaApi · " + ("🟢 LIGADO" if d.get("on") else "🔴 DESLIGADO")]
    if not configured() and api is None:
        L.append("⛔ Falta METAAPI_TOKEN e METAAPI_ACCOUNT_ID no Render.")
        return "\n".join(L)
    api = api or Api()
    try:
        acc = api.account_info()
        L.append("Conta: " + str(acc.get("name") or "") + " · " + str(acc.get("type") or "") + " · servidor " + str(acc.get("server") or "") +
                 " · estado " + str(acc.get("state")) + " / " + str(acc.get("connectionStatus")))
        if acc.get("state") != "DEPLOYED":
            L.append("⚠️ A conta não está ligada no MetaApi (DEPLOYED). Toque em 🔌 Ligar conta no MetaApi.")
    except Exception as exc:
        L.append("⛔ Não consegui falar com o MetaApi: " + str(exc)[:150])
        return "\n".join(L)
    try:
        i = api.info()
        L.append("💰 Saldo " + format(float(i.get("balance") or 0), ",.2f") + " · patrimônio " + format(float(i.get("equity") or 0), ",.2f") +
                 " " + str(i.get("currency") or "") + " · margem livre " + format(float(i.get("freeMargin") or 0), ",.2f"))
        poss = api.positions()
        L.append("📦 Posições abertas: " + str(len(poss)))
        for p in poss[:10]:
            L.append("• " + str(p.get("symbol")) + " " + ("COMPRA" if _side(p) == "buy" else "VENDA") + " " + format(float(p.get("volume") or 0), "g") +
                     " lote a " + format(float(p.get("openPrice") or 0), ".5g") + " · agora " + format(float(p.get("currentPrice") or 0), ".5g") +
                     " · lucro " + format(float(p.get("profit") or 0), "+.2f"))
    except Exception as exc:
        L.append("⚠️ Conta ainda conectando no MT5 (" + str(exc)[:100] + "). Tente de novo em 1–2 min.")
    L.append("Regra: tendência no gráfico de 1 h (média 50 x 200), entra quando o preço volta à média de 20 · stop 1,5 ATR · alvo 3 ATR · "
             "stop móvel · " + format(LOT, "g") + " lote · máx. " + str(MAX_OPEN) + " ordens · pares " + ", ".join(PAIRS))
    if d.get("log"):
        L.append("Últimos:\n" + "\n".join(d["log"][-5:]))
    L.append("⚠️ A maioria das contas de forex de varejo perde dinheiro. Teste na DEMO antes de usar dinheiro real.")
    return "\n".join(L)


def cached_status():
    if LAST["text"]:
        return LAST["text"] + "\n(atualizado há " + format((time.time() - LAST["at"]) / 60, ".0f") + " min · toque em 🔄 Atualizar)"
    if not configured():
        return status_text()
    return "💱 ROBÔ FOREX — " + ("🟢 LIGADO" if _load().get("on") else "🔴 DESLIGADO") + "\nToque em 🔄 Atualizar para ver a conta."


def refresh():
    LAST.update(text=status_text(), at=time.time())
    return LAST["text"]


def keyboard():
    d = _load()
    return [[{"text": "⏸️ Desligar robô" if d.get("on") else "▶️ Ligar robô", "callback_data": "fx_off" if d.get("on") else "fx_on"},
             {"text": "🔄 Atualizar", "callback_data": "fx"}],
            [{"text": "🔌 Ligar conta no MetaApi", "callback_data": "fx_deploy"}, {"text": "❌ Fechar todas", "callback_data": "fx_close"}],
            [{"text": "₿ Voltar ao BTC Full Margin", "callback_data": "bfm"}]]


def handle(action, send, chat, run_async):
    d = _load()
    if action in ("fx_on", "fx_off"):
        with _LOCK:
            d["on"] = action == "fx_on"
            _save()
        send(chat, "▶️ Robô forex LIGADO: confere os pares a cada " + str(CYCLE_S // 60) + " min." if d["on"] else
             "⏸️ Robô forex desligado: não abre ordens novas (as abertas seguem com stop e alvo no MT5).")
        if d["on"]:
            run_async(lambda: send(chat, "💱 Primeira checagem: " + str(cycle(send, chat))))
    elif action == "fx_deploy":
        def go():
            try:
                Api().deploy()
                send(chat, "🔌 Pedi para o MetaApi ligar a conta. Leva 1–3 min para conectar no MT5.")
            except Exception as exc:
                send(chat, "⛔ " + str(exc)[:200])
        run_async(go)
        return True
    elif action == "fx_close":
        def close_all():
            api, n = Api(), 0
            try:
                for p in api.positions():
                    if str(p.get("comment") or "").startswith(COMMENT) or str(p.get("id")) in d["open"]:
                        api.trade({"actionType": "POSITION_CLOSE_ID", "positionId": str(p.get("id"))})
                        n += 1
                send(chat, "❌ Fechei " + str(n) + " posição(ões) do robô.")
            except Exception as exc:
                send(chat, "⛔ " + str(exc)[:200])
        run_async(close_all)
        return True
    run_async(lambda: send(chat, refresh(), keyboard()))
    return True
