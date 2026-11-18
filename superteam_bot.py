"""Superteam Earn alerts (owner, Nov 18): paid Solana tasks (bounties and projects, paid in USDC / SOL) checked all day;
every new open task is sent to the accounts that have alerts on, with the reward, what it asks, the deadline and the
link. The person does the task themselves (automatic or mass submissions are spam and get banned).

Superteam Earn has no documented public API: the JSON endpoints its own site uses are tried in order (SUPERTEAM_URLS)
and the first that answers with a list of listings is used; the log says which one works or why none does."""
import json
import logging
import os
import re
import time

import requests

BASE = "https://earn.superteam.fun"
URLS = tuple(u.strip() for u in os.getenv("SUPERTEAM_URLS", ",".join((
    BASE + "/api/listings?context=all&tab=all&category=All&status=open&sortBy=Date&order=desc",
    BASE + "/api/listings/?take=50",
    BASE + "/api/homepage/listings?take=50",
    BASE + "/api/listings/live?take=50",
))).split(",") if u.strip())
EVERY_S = int(os.getenv("SUPERTEAM_EVERY_S", "600"))      # read every 10 min
MAX_PER_RUN = 8
DEV_SKILLS = {"development", "frontend", "backend", "blockchain", "mobile", "fullstack", "smart contracts", "rust", "solidity"}
MIN_CHOICES = (0, 50, 100, 250, 500, 1000)
_CACHE = {"at": 0.0, "items": [], "url": None}
_LOGGED = [0.0]


# ---------------------------------------------------------------- reading the listings
def _rows(data):
    """The list of listings inside whatever JSON shape the endpoint returns."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("listings", "bounties", "data", "items", "results", "forYou", "all"):
            v = data.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and ("slug" in v[0] or "title" in v[0]):
                return v
    return []


def _ts(v):
    if not v:
        return None
    try:
        return time.mktime(time.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return None


def skills_of(r):
    out = []
    for s in r.get("skills") or []:
        if isinstance(s, dict):
            out.append(str(s.get("skills") or s.get("name") or ""))
            out += [str(x) for x in (s.get("subskills") or [])]
        else:
            out.append(str(s))
    return [s for s in out if s]


def parse(r, now=None):
    """One listing -> {slug, title, sponsor, reward, token, type, deadline, skills, dev, link} (None if closed / bad)."""
    now = now or time.time()
    slug = r.get("slug")
    title = str(r.get("title") or "").strip()
    if not slug or not title:
        return None
    status = str(r.get("status") or "OPEN").upper()
    deadline = _ts(r.get("deadline"))
    if status not in ("OPEN", "ACTIVE") or (deadline and deadline < now) or r.get("isWinnersAnnounced"):
        return None
    try:
        reward = float(r.get("rewardAmount") or r.get("usdValue") or r.get("maxRewardAsk") or 0)
    except (TypeError, ValueError):
        reward = 0.0
    sk = skills_of(r)
    sponsor = r.get("sponsor") or {}
    return {"slug": str(slug), "title": title[:120], "sponsor": str(sponsor.get("name") or "") if isinstance(sponsor, dict) else "",
            "reward": reward, "token": str(r.get("token") or "USDC"), "type": str(r.get("type") or "bounty").lower(),
            "deadline": deadline, "skills": sk[:6], "dev": any(s.lower() in DEV_SKILLS for s in sk),
            "link": BASE + "/listing/" + str(slug)}


def fetch(get=None, now=None):
    """Open listings, newest first; cached for EVERY_S. [] when every endpoint refuses (logged)."""
    now = now or time.time()
    if not get and now - _CACHE["at"] < EVERY_S and _CACHE["items"]:
        return _CACHE["items"]
    errors = []
    for url in ((_CACHE["url"],) if _CACHE["url"] else ()) + URLS:
        try:
            if get:
                data = get(url)
            else:
                r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 SolanaTaskAlert/1.0", "Accept": "application/json"})
                if r.status_code >= 400:
                    raise RuntimeError("HTTP " + str(r.status_code))
                data = r.json()
            items = [x for x in (parse(r, now) for r in _rows(data)) if x]
            if items or _rows(data):
                if not get:
                    if _CACHE["url"] != url:
                        logging.info("superteam: using %s (%s open tasks)", url.split("?")[0], len(items))
                    _CACHE.update(at=now, items=items, url=url)
                return items
            errors.append(url.split("?")[0] + ": no listings")
        except Exception as exc:
            errors.append(url.split("?")[0] + ": " + type(exc).__name__ + " " + str(exc)[:40])
    if not get:
        _CACHE["at"] = now
        if now - _LOGGED[0] > 1800:
            _LOGGED[0] = now
            logging.warning("superteam: no endpoint answered: %s", " | ".join(errors))
    return []


# ---------------------------------------------------------------- per account
def state(chat):
    import trade_panel as tp
    try:
        a = dict(tp.settings(chat).get("superteam") or {})
    except Exception:
        a = {}
    a.setdefault("on", True)
    a.setdefault("nocode", False)
    a.setdefault("min", 0)
    a.setdefault("seen", [])
    return a


def set_state(chat, a):
    import trade_panel as tp
    a["seen"] = list(a.get("seen") or [])[-500:]
    tp.set_setting(chat, "superteam", a)


def wanted(a, it):
    return (not a["nocode"] or not it["dev"]) and it["reward"] >= float(a["min"] or 0)


def card(it, now=None):
    now = now or time.time()
    left = ""
    if it["deadline"]:
        d = (it["deadline"] - now) / 86400
        left = "⏳ Prazo: " + (format(d, ".0f") + " dias" if d >= 1 else format(d * 24, ".0f") + " h")
    kind = {"bounty": "Recompensa (concorrência: ganham as melhores entregas)", "project": "Projeto (escolhem uma pessoa)",
            "hackathon": "Hackathon"}.get(it["type"], it["type"])
    lines = ["💰 TAREFA PAGA — SUPERTEAM EARN",
             "📌 " + it["title"],
             ("🏢 " + it["sponsor"] if it["sponsor"] else ""),
             "💵 Paga: " + (format(it["reward"], ",.0f") + " " + it["token"] if it["reward"] else "valor a combinar"),
             "📋 Tipo: " + kind,
             ("🧠 Habilidades: " + ", ".join(it["skills"]) if it["skills"] else ""),
             ("💻 Pede programação" if it["dev"] else "✍️ Não pede programação"),
             left,
             "Faça a tarefa você mesmo e entregue no site (entrega automática ou em massa é banida)."]
    kb = [[{"text": "📄 Abrir a tarefa", "url": it["link"]}]]
    return "\n".join(x for x in lines if x), kb


def run_for(chat, send, items, force=False):
    a = state(chat)
    if not a["on"] and not force:
        return 0
    sent = 0
    for it in items:
        if it["slug"] in a["seen"] or not wanted(a, it):
            continue
        t, kb = card(it)
        send(chat, t, kb)
        a["seen"].append(it["slug"])
        sent += 1
        if sent >= MAX_PER_RUN:
            break
    set_state(chat, a)
    if sent:
        logging.info("superteam: sent %s task(s) to %s", sent, chat)
    return sent


def cycle(send, chats, get=None):
    want = [c for c in chats if state(c)["on"]]
    if not want:
        return
    items = fetch(get)
    for c in want:
        try:
            run_for(c, send, items)
        except Exception as exc:
            logging.info("superteam %s: %s", c, type(exc).__name__)


def screen(chat):
    a = state(chat)
    L = ["💰 TAREFAS PAGAS DA SOLANA — SUPERTEAM EARN",
         "Projetos da Solana pagam em USDC / SOL por tarefas: texto, vídeo, design, tradução, testes, programação…",
         "🔔 Avisos: " + ("🟢 ligados (confiro a cada " + str(EVERY_S // 60) + " min, o dia inteiro)" if a["on"] else "🔴 desligados"),
         "🎛️ Filtro: " + ("só tarefas SEM programação" if a["nocode"] else "todas as tarefas") +
         (" · a partir de " + format(a["min"], ",.0f") + " dólares" if a["min"] else ""),
         "Quando sair uma tarefa nova, eu te mando com quanto paga, o prazo e o link. Você faz e entrega no site."]
    kb = [[{"text": "🔴 Desligar avisos" if a["on"] else "🟢 Ligar avisos", "callback_data": "sup_off" if a["on"] else "sup_on"},
           {"text": ("✅ " if a["nocode"] else "") + "Só sem programação", "callback_data": "sup_code"}],
          [{"text": "💵 Mínimo: " + (format(a["min"], ",.0f") + " dólares" if a["min"] else "qualquer valor"), "callback_data": "sup_min"},
           {"text": "🔎 Ver tarefas abertas", "callback_data": "sup_now"}]]
    return "\n".join(L), kb


def handle(action, send, chat, tail, run_async):
    a = state(chat)
    if action in ("sup_on", "sup_off"):
        a["on"] = action == "sup_on"
        set_state(chat, a)
    elif action == "sup_code":
        a["nocode"] = not a["nocode"]
        set_state(chat, a)
    elif action == "sup_min":
        nxt = [m for m in MIN_CHOICES if m > float(a["min"] or 0)]
        a["min"] = nxt[0] if nxt else 0
        set_state(chat, a)
    elif action == "sup_now":
        def go():
            items = [it for it in fetch() if wanted(state(chat), it)]
            if not items:
                send(chat, "Não consegui ler as tarefas agora ou não tem nenhuma aberta com esse filtro. Tento de novo sozinho.", tail)
                return
            for it in items[:MAX_PER_RUN]:
                t, kb = card(it)
                send(chat, t, kb)
            b = state(chat)
            b["seen"] = list(b["seen"]) + [it["slug"] for it in items[:MAX_PER_RUN] if it["slug"] not in b["seen"]]
            set_state(chat, b)
            send(chat, "Mostrei " + str(min(len(items), MAX_PER_RUN)) + " de " + str(len(items)) + " tarefas abertas. As novas chegam sozinhas.", tail)
        send(chat, "🔎 Buscando as tarefas abertas…")
        run_async(go)
        return True
    t, kb = screen(chat)
    send(chat, t, kb + tail)
    return True
