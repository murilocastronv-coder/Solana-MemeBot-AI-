"""Web panel of the bot (read-only): follow everything in the browser instead of Telegram.

Protected by a password: set DASHBOARD_PASSWORD (12+ characters) in Render > Environment.
Open https://<service>.onrender.com/painel ; the browser asks user (anything) and password.
Read-only: it never buys, sells or shows private keys.
"""
import collections
import hmac
import html
import os
import threading
import time

NOTICES = collections.deque(maxlen=150)     # (time, text) of every message the bot tried to send
_LOCK = threading.Lock()


def record(text):
    with _LOCK:
        NOTICES.append((time.time(), str(text or "")))


def password():
    return os.getenv("DASHBOARD_PASSWORD", "").strip()


def authorized(auth):
    pw = password()
    if len(pw) < 12 or auth is None:
        return False
    return hmac.compare_digest(str(auth.password or "").encode(), pw.encode())


def _e(x):
    return html.escape(str(x))


def _ago(t):
    s = int(max(0, time.time() - float(t or 0)))
    if s < 60:
        return str(s) + " s"
    if s < 3600:
        return str(s // 60) + " min"
    if s < 86400:
        return str(s // 3600) + " h " + str(s % 3600 // 60) + " min"
    return str(s // 86400) + " d"


def _safe(fn, default=""):
    try:
        return fn()
    except Exception as exc:
        return default or ("indisponível agora (" + type(exc).__name__ + ")")


MODE = {"copy": "👥 Copy", "rocket": "🚀 Sniper", "signal": "📡 Sinal", "dip": "📉 Dip", "clone": "🤖 Clone",
        "consensus": "🤝 Consenso", "sniper": "🎯 Sniper antigo"}


def summary():
    import copy_trader as ct
    import live_engine
    import live_trade_state as st
    s = st.status()
    pnl = st.pnl_summary()
    addr = _safe(live_engine.get_address, "")
    free = _safe(lambda: ct.wallet_usd(addr), None) if addr else None
    invested = sum(float(p.get("cost_usd") or 0) for p in st._load()["positions"].values())
    value = (free or 0) + invested
    ready = _safe(live_engine.ready, False)
    why = "" if ready else _safe(live_engine.why_stopped, "")
    return {"ready": ready, "why": why, "positions": s["positions"], "pending": s["pending"],
            "free": free, "invested": invested, "value": value,
            "exposure": invested / value * 100 if value else 0.0, "max_exposure": ct.MAX_EXPOSURE * 100,
            "day": pnl["day_pnl_usd"], "total": pnl["total_pnl_usd"], "closed": pnl["trades_closed"],
            "won": pnl["trades_won"], "max_positions": st.MAX_POSITIONS, "address": addr,
            "copy_on": _safe(ct.enabled, False), "copy_real": _safe(ct.real_enabled, False)}


def positions_rows():
    import live_exit_monitor as lem
    import live_trade_state as st
    rows = []
    for mint, p in st._load()["positions"].items():
        cost = float(p.get("cost_usd") or 0)
        last = lem.LAST_VALUE.get(mint)
        now_pct = ((last[1] / cost - 1) * 100) if last and cost else None
        peak = (float(p.get("peak_ratio") or 0) - 1) * 100 if p.get("peak_ratio") else None
        rows.append({"symbol": p.get("symbol") or mint[:6], "mint": mint, "mode": MODE.get(p.get("mode"), p.get("mode") or "?"),
                     "cost": cost, "now": now_pct, "peak": peak, "armed": bool(p.get("trail_armed")),
                     "opened": p.get("opened_at"), "quoted": last[0] if last else None})
    return rows


def _pct(x):
    if x is None:
        return "—"
    cls = "up" if x >= 0 else "down"
    return "<span class='" + cls + "'>" + format(x, "+.1f") + "%</span>"


def _usd(x, sign=False):
    if x is None:
        return "—"
    txt = ("US$ " + format(x, "+.2f")) if sign else ("US$ " + format(x, ",.2f"))
    if sign:
        return "<span class='" + ("up" if x >= 0 else "down") + "'>" + txt + "</span>"
    return txt


CSS = """
:root{--bg:#0f1115;--card:#181b22;--fg:#e8eaed;--mut:#9aa0a6;--up:#34c759;--down:#ff453a;--line:#2a2e37;--acc:#7c5cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 2px}h2{font-size:16px;margin:0 0 10px}
.mut{color:var(--mut);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0;overflow:hidden}
.kpi .v{font-size:20px;font-weight:600;margin-top:2px}.kpi .l{color:var(--mut);font-size:12px}
.up{color:var(--up)}.down{color:var(--down)}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mut);font-weight:500}
.tw{overflow-x:auto}pre{white-space:pre-wrap;word-break:break-word;margin:0;font:13px/1.5 ui-monospace,Menlo,monospace;color:#d6d9de}
a{color:#9db4ff}.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;background:#2a2e37}
.ok{background:rgba(52,199,89,.15);color:var(--up)}.bad{background:rgba(255,69,58,.15);color:var(--down)}
.note{border-bottom:1px solid var(--line);padding:8px 0}.note:last-child{border:0}.note .t{color:var(--mut);font-size:12px}
"""


def page():
    import copy_trader as ct
    try:
        import rocket_bot
    except Exception:
        rocket_bot = None
    s = _safe(summary, None)
    out = ["<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
           "<meta http-equiv='refresh' content='30'><title>Painel do bot</title><style>", CSS, "</style></head><body><main>",
           "<h1>📊 Painel do bot</h1><div class='mut'>Atualiza sozinho a cada 30 s · ", _e(time.strftime("%d/%m %H:%M:%S UTC", time.gmtime())),
           "</div>"]
    if isinstance(s, dict):
        state = ("<span class='pill ok'>robô pronto</span>" if s["ready"] else
                 "<span class='pill bad'>parado: " + _e(s["why"] or "?") + "</span>")
        copy = ("<span class='pill ok'>copy REAL ligado</span>" if s["copy_real"] else
                "<span class='pill'>copy SIMULAÇÃO</span>" if s["copy_on"] else "<span class='pill bad'>copy DESLIGADO</span>")
        out += ["<div style='margin-top:8px'>", state, " ", copy, "</div><div class='grid'>"]
        for label, val in (("Conta (SOL livre + moedas)", _usd(s["value"])), ("SOL livre", _usd(s["free"])),
                           ("Em moedas", _usd(s["invested"]) + " <span class='mut'>(" + format(s["exposure"], ".0f") +
                            "% de máx. " + format(s["max_exposure"], ".0f") + "%)</span>"),
                           ("Posições abertas", str(s["positions"]) + "/" + str(s["max_positions"]) +
                            (" · " + str(s["pending"]) + " pendente" if s["pending"] else "")),
                           ("Resultado hoje", _usd(s["day"], True)), ("Resultado total", _usd(s["total"], True)),
                           ("Operações fechadas", str(s["closed"]) + " · " + str(s["won"]) + " no lucro")):
            out += ["<div class='card kpi' style='margin:0'><div class='l'>", label, "</div><div class='v'>", val, "</div></div>"]
        out.append("</div>")
        if s["address"]:
            out += ["<div class='mut'>Carteira: <a href='https://solscan.io/account/", _e(s["address"]), "' target='_blank'>",
                    _e(s["address"]), "</a></div>"]
    else:
        out += ["<div class='card'>", _e(s), "</div>"]

    rows = _safe(positions_rows, [])
    out.append("<div class='card'><h2>📦 Posições abertas</h2>")
    if isinstance(rows, list) and rows:
        out.append("<div class='tw'><table><tr><th>Moeda</th><th>Robô</th><th>Custo</th><th>Agora</th><th>Topo</th>"
                   "<th>Stop móvel</th><th>Aberta há</th><th></th></tr>")
        for r in rows:
            out += ["<tr><td><b>", _e(r["symbol"]), "</b></td><td>", _e(r["mode"]), "</td><td>", _usd(r["cost"]), "</td><td>",
                    _pct(r["now"]), (" <span class='mut'>(" + _ago(r["quoted"]) + ")</span>" if r["quoted"] else ""),
                    "</td><td>", _pct(r["peak"]), "</td><td>", ("✅ ativo" if r["armed"] else "—"), "</td><td>",
                    _ago(r["opened"]) if r["opened"] else "—", "</td><td><a href='https://dexscreener.com/solana/",
                    _e(r["mint"]), "' target='_blank'>gráfico</a></td></tr>"]
        out.append("</table></div>")
    else:
        out.append("<div class='mut'>Nenhuma posição aberta.</div>" if isinstance(rows, list) else _e(rows))
    out.append("</div>")

    hist = _safe(lambda: __import__("live_trade_state").history()[-40:][::-1], [])
    out.append("<div class='card'><h2>🧾 Últimas operações fechadas</h2>")
    if isinstance(hist, list) and hist:
        out.append("<div class='tw'><table><tr><th>Quando</th><th>Moeda</th><th>Robô</th><th>Custo</th><th>Resultado</th>"
                   "<th>Saída</th><th>Durou</th></tr>")
        for h in hist:
            pnl = h.get("pnl_usd")
            out += ["<tr><td>há ", _ago(h.get("closed_at")), "</td><td>", _e(h.get("symbol")), "</td><td>",
                    _e(MODE.get(h.get("mode"), h.get("mode"))), "</td><td>", _usd(float(h.get("cost_usd") or 0)), "</td><td>",
                    _usd(pnl, True) if pnl is not None else "—", "</td><td>", _e(h.get("reason")), "</td><td>",
                    (format(h["minutes"], ".0f") + " min") if h.get("minutes") is not None else "—", "</td></tr>"]
        out.append("</table></div>")
    else:
        out.append("<div class='mut'>Nenhuma operação fechada ainda.</div>")
    out.append("</div>")

    if rocket_bot:
        out += ["<div class='card'><h2>🚀 Sniper</h2><pre>", _e(_safe(rocket_bot.status_text)), "</pre></div>"]
    out += ["<div class='card'><h2>👥 Copy trade</h2><pre>", _e(_safe(ct.status_text)), "</pre></div>",
            "<div class='card'><h2>📦 Cópias abertas</h2><pre>", _e(_safe(ct.positions_text)), "</pre></div>"]

    with _LOCK:
        notes = list(NOTICES)[::-1]
    out.append("<div class='card'><h2>🔔 Avisos do bot (os mesmos do Telegram)</h2>")
    if notes:
        for t, text in notes[:100]:
            out += ["<div class='note'><div class='t'>há ", _ago(t), "</div><pre>", _e(text), "</pre></div>"]
    else:
        out.append("<div class='mut'>Nenhum aviso desde que o bot reiniciou.</div>")
    out.append("</div><div class='mut'>Painel só de leitura: não compra, não vende e não mostra chaves.</div></main></body></html>")
    return "".join(out)
