"""Web panel of the bot: follow everything in the browser instead of Telegram, and turn the copy
trade / Sniper on and off. The bot runs on the server: closing the page changes nothing.

Protected by a password: set DASHBOARD_PASSWORD (12+ characters) in Render > Environment.
Open https://<service>.onrender.com/painel ; the browser asks user (anything) and password.
It never shows private keys; the only actions are the on/off switches (form posts with a CSRF token).
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


def csrf_token():
    return hmac.new(password().encode(), b"painel-csrf-v1", "sha256").hexdigest()


def csrf_ok(token):
    return bool(password()) and hmac.compare_digest(str(token or ""), csrf_token())


ACTIONS = {"copy_real": "Copy trade REAL ligado", "copy_off": "Copy trade desligado",
           "rk_on": "Sniper ligado", "rk_off": "Sniper desligado",
           "rks_on": "Sinais do Sniper ligados", "rks_off": "Sinais do Sniper desligados",
           "bx_on": "BTC 20x ligado", "bx_off": "BTC 20x desligado"}


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
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.btn{border:0;border-radius:10px;padding:10px 14px;font-size:15px;font-weight:600;color:#fff;background:#3a3f4b;cursor:pointer;margin:2px 4px 2px 0}
.btn.go{background:#2e8b57}.btn.stop{background:#b23b3b}
.trail{border-color:#5b4bd1}.flash{animation:fl 1s ease-in-out 4}
@keyframes fl{50%{background:#2b2560;border-color:#a594ff}}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:12px 0 4px;padding-bottom:4px}
.tabs button{flex:0 0 auto;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:99px;padding:8px 12px;font-size:14px;cursor:pointer}
.tabs button.on{background:var(--acc);border-color:var(--acc)}.tab{display:none}.tab.on{display:block}
.big{font-size:17px;font-weight:600}
.pos{border-top:1px solid var(--line);padding:10px 0}.pos:first-of-type{border-top:0}.ph{margin-bottom:6px}
.pg{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:4px}.pg .l{color:var(--mut);font-size:12px}
.note{border-bottom:1px solid var(--line);padding:8px 0}.note:last-child{border:0}.note .t{color:var(--mut);font-size:12px}
"""


def _button(action, label, style=""):
    return ("<form method='post' action='/painel/acao' style='display:inline'><input type='hidden' name='csrf' value='" +
            csrf_token() + "'><input type='hidden' name='acao' value='" + action + "'><button class='btn " + style + "'>" +
            label + "</button></form> ")


def controls(s):
    try:
        import rocket_bot
        rk_on = rocket_bot.enabled()
        rks_on = rocket_bot.signals_on()
    except Exception:
        rk_on = rks_on = None
    try:
        import btc_bot
        bx_on = btc_bot.enabled()
    except Exception:
        bx_on = None
    copy_on = isinstance(s, dict) and s.get("copy_real")
    return ("<div class='card'><h2>🎛️ Controles</h2><div class='row'><div><b>₿ BTC 20x (Jupiter)</b> — " +
            ("<span class='pill ok'>ligado</span>" if bx_on else "<span class='pill bad'>desligado</span>") +
            "<div style='margin-top:8px'>" + _button("bx_on", "₿ Ligar", "go") + _button("bx_off", "⏹️ Parar", "stop") +
            "</div></div></div><div class='mut' style='margin-top:10px'>O bot roda no servidor 24 h: pode fechar esta aba "
            "ou o celular que ele continua operando até você tocar em Parar. Parar não vende as posições abertas: "
            "elas continuam com stop, stop móvel e alvos até fechar.</div></div>")


TRAIL_MARK = "STOP MÓVEL ATIVADO"

NOTIFY_JS = """
<script>
(function(){
  var box=document.getElementById('trail'); if(!box) return;
  var latest=Number(box.dataset.latest||0), seen=0;
  try{seen=Number(localStorage.getItem('trailSeen')||0);}catch(e){}
  var btn=document.getElementById('notif');
  function perm(){return ('Notification' in window)?Notification.permission:'unsupported';}
  function label(){ if(!btn) return; var p=perm();
    btn.textContent = p==='granted'?'🔔 Notificações ligadas':(p==='denied'?'🔕 Notificações bloqueadas no navegador':
      (p==='unsupported'?'🔕 Este navegador não tem notificações':'🔔 Ativar notificações'));
    btn.disabled = p!=='default'; }
  label();
  if(btn) btn.onclick=function(){ if(perm()==='default') Notification.requestPermission().then(label); };
  if('serviceWorker' in navigator){ navigator.serviceWorker.register('/painel-sw.js').catch(function(){}); }
  if(latest>seen && seen>0){
    box.classList.add('flash');
    try{var c=new (window.AudioContext||window.webkitAudioContext)(),o=c.createOscillator();o.frequency.value=880;
        o.connect(c.destination);o.start();setTimeout(function(){o.stop();},350);}catch(e){}
    if(perm()==='granted'){
      var title='🚀 Stop móvel ativado', opts={body:box.dataset.text||'',tag:'trail-'+latest};
      // phones (Android) only show page notifications through a service worker
      if('serviceWorker' in navigator){
        navigator.serviceWorker.register('/painel-sw.js').then(function(r){return navigator.serviceWorker.ready;})
          .then(function(r){r.showNotification(title,opts);}).catch(function(){try{new Notification(title,opts);}catch(e){}});
      } else { try{new Notification(title,opts);}catch(e){} }
    }
  }
  try{ if(latest) localStorage.setItem('trailSeen', String(latest)); }catch(e){}
})();
</script>
"""


def trailing_card(notes):
    """Highlighted list of trailing-stop activations, with browser notification + beep when a new one comes."""
    trail = [(t, text) for t, text in notes if TRAIL_MARK in text]
    latest = int(trail[0][0] * 1000) if trail else 0
    first = trail[0][1].split("\n")[0] if trail else ""
    out = ["<div class='card trail' id='trail' data-latest='", str(latest), "' data-text='", _e(first), "'>",
           "<h2>🚀 Stop móvel ativado</h2>",
           "<button id='notif' class='btn' type='button'>🔔 Ativar notificações</button>",
           "<div class='mut' style='margin:6px 0 8px'>Com a página aberta (pode estar em outra aba), toca um bip e chega uma "
           "notificação no celular/computador quando o stop móvel de uma moeda for ativado.</div>"]
    if trail:
        for t, text in trail[:10]:
            out += ["<div class='note'><div class='t'>há ", _ago(t), "</div><pre>", _e(text), "</pre></div>"]
    else:
        out.append("<div class='mut'>Nenhum stop móvel ativado desde que o bot reiniciou.</div>")
    out.append("</div>")
    return "".join(out)


def trail_rule(p):
    """(arm %, drop %) of the position's trailing stop, or None when it has none (sells with the wallet / fixed target)."""
    import live_exit_monitor as lem
    mode = p.get("mode", "normal")
    if mode == "rocket":
        if p.get("dca_done"):
            return None
        import rocket_bot
        return rocket_bot.TRAIL_ARM * 100, rocket_bot.TRAIL_DROP * 100
    if mode == "copy":
        if p.get("copy_orphan"):
            return (float(lem.ORPHAN_EXIT["arm"]) - 1) * 100, float(lem.ORPHAN_EXIT["drop"]) * 100
        return None
    rules = {"consensus": lem.CONSENSUS_EXIT, "sniper": lem.SNIPER_EXIT, "pumpfun": lem.PUMPFUN_EXIT}
    rules.update(lem.REMOVED_BOT_EXIT)
    r = rules.get(mode)
    if r:
        return (float(r["arm"]) - 1) * 100, float(r["drop"]) * 100
    return 15.0, 10.0


def trailing_rows():
    import live_exit_monitor as lem
    import live_trade_state as st
    rows = []
    for mint, p in st._load()["positions"].items():
        cost = float(p.get("cost_usd") or 0)
        last = lem.LAST_VALUE.get(mint)
        now = (last[1] / cost - 1) * 100 if last and cost else None
        peak = float(p.get("peak_ratio") or 0)
        rule = trail_rule(p)
        sell_at = (peak * (1 - rule[1] / 100) - 1) * 100 if rule and peak and p.get("trail_armed") else None
        rows.append({"symbol": p.get("symbol") or mint[:6], "mint": mint, "mode": MODE.get(p.get("mode"), p.get("mode") or "?"),
                     "cost": cost, "now": now, "peak": (peak - 1) * 100 if peak else None, "armed": bool(p.get("trail_armed")),
                     "rule": rule, "sell_at": sell_at, "quoted": last[0] if last else None})
    rows.sort(key=lambda r: (not r["armed"], -(r["now"] or -999)))
    return rows


def trailing_tab():
    rows = _safe(trailing_rows, [])
    out = ["<div class='card'><h2>🚀 Stop móvel das posições abertas</h2>"]
    if not isinstance(rows, list):
        return "".join(out + ["<div class='mut'>", _e(rows), "</div></div>"])
    armed = [r for r in rows if r["armed"]]
    out.append("<div class='mut' style='margin-bottom:8px'>" + str(len(armed)) + " ativo(s) de " + str(len(rows)) +
               " posição(ões) aberta(s). Atualiza a cada 30 s.</div>")
    if not rows:
        out.append("<div class='mut'>Nenhuma posição aberta.</div>")
    else:
        for r in rows:
            rule = r["rule"]
            if r["armed"]:
                status = "<span class='pill ok'>✅ ATIVO</span>"
            elif rule:
                status = "<span class='pill'>liga em +" + format(rule[0], ".0f") + "%</span>"
            else:
                status = "<span class='pill'>sem stop móvel</span>"
            rule_txt = ("liga em +" + format(rule[0], ".0f") + "%, vende " + format(rule[1], ".0f") + "% abaixo do topo"
                        if rule else ("vende junto com a carteira copiada" if "Copy" in r["mode"] else "alvo fixo"))
            out += ["<div class='pos'><div class='ph'><b>", _e(r["symbol"]), "</b> <span class='mut'>", _e(r["mode"]),
                    " · ", _usd(r["cost"]), "</span> ", status, "</div><div class='pg'>",
                    "<div><div class='l'>Agora</div>", _pct(r["now"]), "</div>",
                    "<div><div class='l'>Topo</div>", _pct(r["peak"]), "</div>",
                    "<div><div class='l'>Vende se cair para</div><span class='big'>",
                    _pct(r["sell_at"]) if r["sell_at"] is not None else "—", "</span></div></div>",
                    "<div class='mut'>", _e(rule_txt), " · <a href='https://dexscreener.com/solana/", _e(r["mint"]),
                    "' target='_blank'>gráfico</a></div></div>"]
    out.append("</div>")
    return "".join(out)


TABS_JS = """
<script>
(function(){
  var btns=document.querySelectorAll('.tabs button'), tabs=document.querySelectorAll('.tab');
  function show(id){ tabs.forEach(function(t){t.classList.toggle('on',t.id===id);});
    btns.forEach(function(b){b.classList.toggle('on',b.dataset.t===id);});
    try{localStorage.setItem('tab',id);}catch(e){} }
  btns.forEach(function(b){ b.onclick=function(){show(b.dataset.t);}; });
  var first='t-resumo'; try{ first=localStorage.getItem('tab')||first; }catch(e){}
  if(!document.getElementById(first)) first='t-resumo';
  show(first);
})();
</script>
"""

TAB_LIST = (("t-resumo", "🏠 Resumo"), ("t-stop", "🚀 Stop móvel"), ("t-pos", "📦 Posições"), ("t-ops", "🧾 Operações"),
            ("t-bots", "🤖 Robôs"), ("t-avisos", "🔔 Avisos"))


def page(msg=""):
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
    if msg:
        out += ["<div class='card' style='border-color:var(--acc)'>✅ ", _e(msg), "</div>"]
    out.append("<nav class='tabs'>" + "".join("<button type='button' data-t='" + t + "'>" + label + "</button>" for t, label in TAB_LIST) + "</nav>")

    # ---- Resumo
    out.append("<section class='tab' id='t-resumo'>")
    out.append(controls(s))
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
    out.append("</section>")

    # ---- Stop móvel
    with _LOCK:
        notes = list(NOTICES)[::-1]
    out += ["<section class='tab' id='t-stop'>", trailing_tab(), trailing_card(notes), "</section>"]

    # ---- Posições
    rows = _safe(positions_rows, [])
    out.append("<section class='tab' id='t-pos'><div class='card'><h2>📦 Posições abertas</h2>")
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
    out.append("</div><div class='card'><h2>👥 Cópias abertas</h2><pre>" + _e(_safe(ct.positions_text)) + "</pre></div></section>")

    # ---- Operações
    hist = _safe(lambda: __import__("live_trade_state").history()[-40:][::-1], [])
    out.append("<section class='tab' id='t-ops'><div class='card'><h2>🧾 Últimas operações fechadas</h2>")
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
    out.append("</div></section>")

    # ---- Robôs
    out.append("<section class='tab' id='t-bots'>")
    if rocket_bot:
        out += ["<div class='card'><h2>🚀 Sniper</h2><pre>", _e(_safe(rocket_bot.status_text)), "</pre></div>"]
    try:
        import btc_bot
        out += ["<div class='card'><h2>₿ BTC 20x</h2><pre>", _e(_safe(btc_bot.status_text)), "</pre></div>"]
        if btc_bot.BACKTEST["text"]:
            out += ["<div class='card'><h2>🧪 Backtest 60 dias</h2><pre>", _e(btc_bot.BACKTEST["text"]), "</pre></div>"]
    except Exception:
        pass
    out += ["<div class='card'><h2>👥 Copy trade</h2><pre>", _e(_safe(ct.status_text)), "</pre></div></section>"]

    # ---- Avisos
    out.append("<section class='tab' id='t-avisos'><div class='card'><h2>🔔 Avisos do bot (os mesmos do Telegram)</h2>")
    if notes:
        for t, text in notes[:100]:
            out += ["<div class='note'><div class='t'>há ", _ago(t), "</div><pre>", _e(text), "</pre></div>"]
    else:
        out.append("<div class='mut'>Nenhum aviso desde que o bot reiniciou.</div>")
    out.append("</div></section><div class='mut'>O painel não vende nem mostra chaves; só liga/desliga os robôs.</div></main>" +
               TABS_JS + NOTIFY_JS + "</body></html>")
    return "".join(out)
