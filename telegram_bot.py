"""Telegram bot: memecoin copy trading on Solana (automatic wallet discovery, simulation or real money)."""
import os,time,logging,threading,requests,re,secrets,html
from flask import Flask,Response,request
from live_preflight import live_status, risk_settings
import live_trade_state as trade_state
import live_exit_monitor
import telegram_owner
import live_order_bridge
import live_engine
from live_swap_executor import configured as swap_configured
from jupiter_v2_executor import headers as jupiter_headers
from bot_wallet_service import get_address
import user_wallets
from datetime import datetime,timezone
app=Flask(__name__)
logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
PRIVATE_CHAT=None  # First private /start after process startup; resets on restart.
LIVE_FAIL_NOTICES={}
INTERVAL=max(30,int(os.getenv("SCAN_INTERVAL_SECONDS","30")))
TP=float(os.getenv("TP_PERCENT","15"))
SL=float(os.getenv("SL_PERCENT","8"))
MIN_LIQ=float(os.getenv("MIN_LIQUIDITY_USD","50000"))
MIN_VOLUME=float(os.getenv("MIN_VOLUME_24H_USD","20000"))
MIN_AGE_HOURS=float(os.getenv("MIN_TOKEN_AGE_HOURS","24"))
MAX_FDV_LIQ=float(os.getenv("MAX_FDV_LIQ_RATIO","100"))
MAX_ALERTS=int(os.getenv("MAX_ALERTS_PER_SCAN","5"))
BASE="https://api.telegram.org/bot"+TOKEN
session=requests.Session()
seen=set()
last=[]
ALERTS_ENABLED=True
def request_api(method,**params):
 if not TOKEN:return {}
 try:
  r=session.post(BASE+"/"+method,json=params,timeout=25)
 except requests.RequestException as exc:
  logging.warning("Telegram API transport failed method=%s type=%s",method,type(exc).__name__)
  raise RuntimeError("Telegram API transport failed") from None
 if not r.ok:
  raise RuntimeError("Telegram API returned HTTP "+str(r.status_code))
 try:return r.json()
 except ValueError:raise RuntimeError("Telegram API returned invalid JSON") from None
# Anti-repetition: automatic messages (not answers to a button/command) with the same content are
# sent at most once every NOTIFY_REPEAT_HOURS. Buys and sells always go through.
NOTIFY_REPEAT_S=float(os.getenv("NOTIFY_REPEAT_HOURS","2"))*3600
SENT_AT={}
SENT_LOCK=threading.Lock()
CTX=threading.local()   # CTX.interactive=True while answering the owner (button/command)
TRADE_RE=re.compile(r"COMPRANDO|COMPROU|SIM comprou|copiou compra|VENDEU|VENDENDO|vendeu|💰 LUCRO|🔻 PREJUÍZO|MOEDA ZERADA|Compra confirmada|Venda confirmada|STOP MÓVEL")
def is_trade_message(text):
 """A buy/sell notice: always sent, never held back (errors and warnings are not trades)."""
 t=(text or "").lstrip()
 return bool(TRADE_RE.search(t)) and not t.startswith(("⛔","⚠️","🚨","⏳"))
def fingerprint(text):
 """Same message with other numbers (prices, counts, minutes) counts as a repetition."""
 return re.sub(r"\s+"," ",re.sub(r"[0-9]+([.,][0-9]+)?","#",text or "")).strip()[:600]
def should_send(text,now=None,chat=None):
 if getattr(CTX,"interactive",False) or is_trade_message(text) or NOTIFY_REPEAT_S<=0:return True
 now=now or time.time();key=str(chat)+"|"+fingerprint(text)
 with SENT_LOCK:
  if now-SENT_AT.get(key,0)<NOTIFY_REPEAT_S:return False
  SENT_AT[key]=now
  if len(SENT_AT)>2000:
   for k in sorted(SENT_AT,key=SENT_AT.get)[:1000]:SENT_AT.pop(k,None)
 return True
def send(chat,text,keyboard=None):
 if not should_send(text,chat=chat):
  logging.info("repeated notice held back (%.0f h): %s",NOTIFY_REPEAT_S/3600,(text or "")[:80].replace("\n"," "))
  return {}
 args={"chat_id":chat,"text":text,"disable_web_page_preview":True}
 ents=address_entities(text)
 if ents:args["entities"]=ents     # full addresses in monospace: one tap copies them
 if keyboard:args["reply_markup"]={"inline_keyboard":keyboard}
 return request_api("sendMessage",**args)
ADDRESS_RE=re.compile(r"(?<![/\w.])[1-9A-HJ-NP-Za-km-z]{32,44}(?![\w])")
def address_entities(text):
 """Telegram 'code' entities for every full Solana address that is not part of a link (UTF-16 offsets)."""
 out=[]
 for m in ADDRESS_RE.finditer(text or ""):
  start=len(text[:m.start()].encode("utf-16-le"))//2
  out.append({"type":"code","offset":start,"length":len(m.group(0).encode("utf-16-le"))//2})
 return out[:100]
def send_parts(chat,text,keyboard=None):
 """Send a long text in parts; the menu goes on the last part (one message less to wait for)."""
 parts=split_message(text) or [""]
 for part in parts[:-1]:send(chat,part)
 send(chat,parts[-1],keyboard)
def split_message(text,limit=3800):
 """Telegram allows ~4096 characters per message."""
 parts,cur=[],""
 for line in text.split("\n"):
  if len(cur)+len(line)+1>limit and cur:parts.append(cur);cur=""
  cur+=("\n" if cur else "")+line
 if cur:parts.append(cur)
 return parts
def keyboard():
 return [[{"text":"👥 Copiar (SIMULAÇÃO)","callback_data":"copy_sim"},{"text":"💵 Copiar (REAL)","callback_data":"copy_real"}],
  [{"text":"⏹️ Parar cópia","callback_data":"copy_off"},{"text":"📊 Status","callback_data":"copy_status"}],
  [{"text":"🔎 Carteiras encontradas","callback_data":"copy_wallets"},{"text":"👛 Carteiras copiadas","callback_data":"copy_following"}],
  [{"text":"📦 Posições abertas","callback_data":"copy_positions"},{"text":"🔍 Auditar carteiras","callback_data":"audit"}],
  [{"text":"🤝 Consenso SIM","callback_data":"cns_sim"},{"text":"🤝 Consenso REAL","callback_data":"cns_real"}],
  [{"text":"⏹️ Parar consenso","callback_data":"cns_off"},{"text":"📊 Status consenso","callback_data":"cns_status"}],
  [{"text":"🔧 Destravar robô","callback_data":"unstick"}],
  [{"text":"🎯 Sniper SIM","callback_data":"snp_sim"},{"text":"🎯 Sniper REAL","callback_data":"snp_real"}],
  [{"text":"⏹️ Parar sniper","callback_data":"snp_off"},{"text":"📊 Status sniper","callback_data":"snp_status"}],
  [{"text":"📥 Depositar SOL","callback_data":"wallet_deposit"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}],
  [{"text":"💸 Saque","callback_data":"wallet_withdraw"}]]
def user_keyboard():
 """Menu of a Telegram account that is not the owner: its own copy trade and its own wallet."""
 return [[{"text":"👥 Copiar (SIMULAÇÃO)","callback_data":"u_sim"},{"text":"💵 Copiar (REAL)","callback_data":"u_real"}],
  [{"text":"⏹️ Parar cópia","callback_data":"u_off"},{"text":"📊 Meu status","callback_data":"u_status"}],
  [{"text":"📦 Minhas posições","callback_data":"u_pos"},{"text":"⚙️ Valor por cópia","callback_data":"u_size"}],
  [{"text":"👛 Minha carteira","callback_data":"wallet_create"}],
  [{"text":"📥 Depositar SOL","callback_data":"wallet_deposit"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}],
  [{"text":"💸 Saque","callback_data":"wallet_withdraw"}]]
def keyboard_for(chat):
 return keyboard() if authorized(chat) else user_keyboard()
def robot_alive_line():
 if not HEARTBEAT["at"]:return "⏳ Rotina automática ainda não começou"
 ago=int(time.time()-HEARTBEAT["at"])
 if ago>180:return "🚨 Rotina automática PARADA há "+str(ago//60)+" min (em: "+HEARTBEAT["step"]+"). Faça um Manual Deploy no Render."
 return "💓 Rotina automática ativa ("+str(ago)+" s atrás · "+HEARTBEAT["step"]+")"
def status_text():
 try:
  import copy_trader as ct
  st=trade_state.status();pnl=trade_state.pnl_summary()
  return (ct.status_text()+"\n\n"+
   "Robô (dinheiro real): "+("🟢 PRONTO" if live_engine.ready() else "🔴 PARADO — "+str(live_engine.why_stopped() or ""))+"\n"+
   "Posições reais abertas: "+str(st["positions"])+"/"+str(ct.MAX_OPEN_TOTAL)+" (máx.) · ordens pendentes: "+str(st["pending"])+"\n"+
   (wallet_holdings_line()+"\n" if get_address() else "")+
   "Perda do dia: US$ "+format(st["daily_loss_usd"],".2f")+(" / "+format(trade_state.daily_loss_limit(),".2f") if trade_state.DAILY_LOSS_LIMIT_ENABLED else " (sem limite diário)")+"\n"+
   "P/L real hoje: "+format(pnl["day_pnl_usd"],"+.2f")+" USD · total: "+format(pnl["total_pnl_usd"],"+.2f")+" USD · operações: "+str(pnl["trades_closed"])+" ("+str(pnl["trades_won"])+" no lucro)\n"+
   users_line()+copy_beat_line()+"\n"+robot_alive_line())
 except Exception as exc:return "Status indisponível: "+type(exc).__name__
def users_line():
 try:
  import user_copy
  return user_copy.owner_summary()+"\n"
 except Exception:return ""
def last_cycle_line(c):
 if not c:return "Última rodada: aguardando a primeira (a cada 2 min)."
 ago=int(time.time()-c.get("at",0))
 line=("Última rodada (há "+str(ago//60)+" min): "+str(c.get("tx_read",0))+" negociações lidas, "+
  str(c.get("buyers",0))+" compradores ("+str(c.get("new_buyers",0))+" novos), "+
  str(c.get("histories",0))+" históricos analisados ("+str(c.get("histories_with_trades",0))+" com operações fechadas)")
 if c.get("screened"):
  r=c.get("rejected") or {}
  line+=("\nTriagem: "+str(c["screened"])+" carteiras · reprovadas por idade "+str(r.get("idade",0))+
   ", atividade "+str(r.get("atividade",0))+", saldo "+str(r.get("saldo",0)))
 if c.get("failed") or c.get("rpc_errors"):
  line+="\n⚠️ RPC: "+str(c.get("failed",0))+" leituras perdidas, "+str(c.get("rpc_errors",0))+" recusas"+(" ("+c.get("last_error","")+")" if c.get("last_error") else "")
 return line
def answer_callback(callback_id):
 try:request_api("answerCallbackQuery",callback_query_id=callback_id)
 except Exception:pass   # an old/expired button: nothing to stop
def handle_safe(update):
 CTX.interactive=True   # answers to the owner are never held back
 try:handle(update)
 except Exception:logging.exception("update failed")
 finally:CTX.interactive=False
def holdings_loop():
 """Keep the wallet's real coin count fresh for the status, off the Telegram thread."""
 while True:
  try:
   import wallet_holdings
   address=get_address()
   if address:wallet_holdings.refresh(address)
  except Exception as exc:logging.warning("holdings refresh failed: %s",type(exc).__name__)
  time.sleep(30)
def wallet_holdings_line():
 try:
  import wallet_holdings
  return wallet_holdings.status_line(get_address())
 except Exception as exc:return "Moedas na carteira agora: indisponível ("+type(exc).__name__+")"
def discovery_text():
 """What the automatic wallet search found so far."""
 import wallet_copy,copy_trader
 if not os.getenv("SOLANA_RPC_URL"):
  return "🔎 Busca automática PARADA: configure SOLANA_RPC_URL (RPC dedicado, ex.: Helius) no Render."
 s=wallet_copy.summary()
 lines=["🔎 BUSCA AUTOMÁTICA DE CARTEIRAS",
  "Fonte: moedas em alta no DexScreener → negociações delas na blockchain",
  "Moedas observadas agora: "+(", ".join(t["symbol"] for t in s["dex_tokens"][:8]) or "aguardando DexScreener"),
  "Negociações analisadas: "+str(s["dex_transactions_scanned"]+s["transactions_scanned"]),
  "Carteiras com histórico analisado: "+str(s["wallets_analyzed"]),
  last_cycle_line(s.get("last_cycle") or {}),
  "Carteiras com operações fechadas: "+str(s["wallets_seen"])+" · aprovadas: "+str(s["qualified_wallets"]),
  "Aprovada = lucro realizado, acerto ≥ "+format(wallet_copy.MIN_WIN_RATE*100,".0f")+"%, ≥ "+str(wallet_copy.MIN_CLOSED_TRADES)+" operações fechadas e retenção média ≥ "+format(wallet_copy.MIN_HOLD_SECONDS/60,".0f")+" min por moeda.",
  "Para copiar também: saldo ≥ US$ "+format(copy_trader.MIN_WALLET_USD,".0f")+", compra média ≥ US$ "+format(copy_trader.MIN_BUY_USD,".0f")+", criada há ≥ 7 dias e ativa em ≥ 6 dos últimos 7 dias (veja 👛 Carteiras copiadas)."]
 for w in wallet_copy.best_wallets(limit=10) if s["qualified_wallets"] else []:
  lines.append("• solscan.io/account/"+w["wallet"]+"\n  lucro "+format(w["realized_lamports"]/1e9,"+.3f")+" SOL · acerto "+format(w["win_rate"]*100,".0f")+"% · "+str(w["closed_trades"])+" ops · segura "+format(w["average_hold_seconds"]/60,".0f")+" min")
 if not s["qualified_wallets"]:lines.append("Ainda nenhuma aprovada. A cada 2 min o bot analisa o histórico de novas carteiras que compraram moedas em alta.")
 return "\n".join(lines)
def menu(chat):
 send(chat,status_text(),keyboard())
def fmt(v):
 if v == 0:return "0"
 return format(v, ".12f").rstrip("0").rstrip(".") if abs(v)<1 else format(v, ".12f").rstrip("0").rstrip(".")
def active_chat():
 if CHAT:return CHAT
 try:return telegram_owner.get_owner()
 except Exception:
  logging.exception("persistent Telegram owner unavailable; deny access")
  return None

def authorized(chat):
 return bool(active_chat()) and str(chat)==str(active_chat())
def handle(update):
 global PRIVATE_CHAT, ALERTS_ENABLED
 msg=update.get("message");cb=update.get("callback_query")
 if cb:
  chat=cb.get("message",{}).get("chat",{}).get("id");action=cb.get("data","")
  # stop the button's loading spinner in parallel: the answer does not wait for this round trip
  threading.Thread(target=answer_callback,args=(cb["id"],),daemon=True).start()
 else:
  chat=(msg or {}).get("chat",{}).get("id");action=(msg or {}).get("text","").split(" ")[0]
 if not chat:return
 chat_type=(cb.get("message",{}).get("chat",{}).get("type") if cb else (msg or {}).get("chat",{}).get("type"))
 if chat_type!="private":return
 if not CHAT and action in ("/start","/menu"):
  try:telegram_owner.claim(chat)   # the first account becomes the owner; the others get their own wallet
  except Exception:
   logging.exception("persistent owner claim failed")
   return
 if action=="/id":
  send(chat,"Seu chat ID privado: "+str(chat)+"\nIdentificação privada. O proprietário vinculado é mantido no disco persistente.");return
 if not authorized(chat) and action.startswith("u_"):
  user_copy_action(chat,action);return
 if action in WALLET_ACTIONS or not authorized(chat):
  # every Telegram account has its own unique wallet and its own copy trade; only the owner runs the main one
  wallet_action(chat,action if action in WALLET_ACTIONS else "/start");return
 if action in ("snp_sim","snp_real","snp_real_ok","snp_off","snp_status","/sniper_status"):
  import sniper as sn
  try:
   if action=="snp_sim":sn.set_mode(True,False);pre="🧪 SNIPER em SIMULAÇÃO (preços reais, US$ 100 de mentira).\n\n"
   elif action=="snp_real":
    send(chat,"🎯 SNIPER COM DINHEIRO REAL\nCompra US$ "+format(sn.ORDER_USD,".0f")+" por moeda (máx. "+str(sn.MAX_POSITIONS)+
     " ao mesmo tempo) com a carteira principal, o dia inteiro.\n⚠️ Memecoin nova é o tipo de moeda mais arriscado: as proteções reduzem golpes, mas não evitam perdas. Recomendo testar antes em 🎯 Sniper SIM.",
     [[{"text":"✅ Ligar sniper REAL","callback_data":"snp_real_ok"}],[{"text":"⬅️ Cancelar","callback_data":"snp_status"}]]);return
   elif action=="snp_real_ok":
    sn.set_mode(True,True)
    pre="💵 SNIPER REAL LIGADO.\n\n" if start_real_or_wait(chat,"sniper") else "⏳ SNIPER REAL LIGADO: começa assim que a ordem pendente for conferida (~2 min).\n\n"
   elif action=="snp_off":sn.set_mode(False);pre="⏹️ Sniper desligado: não abre novas posições. As abertas continuam com stop e stop móvel.\n\n"
   else:pre=""
   send(chat,pre+sn.status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Sniper: "+str(exc)[:200],keyboard())
  return
 if action in ("cns_sim","cns_real","cns_real_ok","cns_off","cns_status","/consenso"):
  import consensus as cn
  try:
   if action=="cns_sim":cn.set_mode(True,False);pre="🧪 CONSENSO em SIMULAÇÃO (preços reais, US$ 100 de mentira).\n\n"
   elif action=="cns_real":
    send(chat,"🤝 CONSENSO COM DINHEIRO REAL\nCompra US$ "+format(cn.ORDER_USD,".0f")+" quando "+str(cn.MIN_WALLETS)+
     "+ carteiras acompanhadas compram a mesma moeda em 1 h (máx. "+str(cn.MAX_POSITIONS)+" posições). Stop −40%; a partir de +25% stop móvel.\n⚠️ Recomendo testar antes em 🤝 Consenso SIM.",
     [[{"text":"✅ Ligar consenso REAL","callback_data":"cns_real_ok"}],[{"text":"⬅️ Cancelar","callback_data":"cns_status"}]]);return
   elif action=="cns_real_ok":
    cn.set_mode(True,True)
    pre="💵 CONSENSO REAL LIGADO.\n\n" if start_real_or_wait(chat,"consenso") else "⏳ CONSENSO REAL LIGADO: começa assim que a ordem pendente for conferida (~2 min).\n\n"
   elif action=="cns_off":cn.set_mode(False);pre="⏹️ Consenso desligado: não abre novas posições. As abertas continuam com stop e stop móvel.\n\n"
   else:pre=""
   send(chat,pre+cn.status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Consenso: "+str(exc)[:200],keyboard())
  return
 if action in ("unstick","/destravar"):
  send(chat,"🔧 Conferindo as ordens pendentes na blockchain…")
  def run_unstick():
   try:live_engine.unstick(send,chat)
   except Exception as exc:send(chat,"⛔ Destravar: "+type(exc).__name__+": "+str(exc)[:150],keyboard())
  run_async(run_unstick);return
 if action in ("audit","/auditar"):
  if not os.getenv("SOLANA_RPC_URL"):send(chat,"Preciso do SOLANA_RPC_URL configurado no Render.",keyboard());return
  send(chat,"🔍 Auditando todas as carteiras copiadas (tempo que seguram cada moeda). Leva alguns minutos…")
  run_async(audit_job,chat);return
 if action in ("copy_positions","/posicoes_copy"):
  import copy_trader as ct
  try:
   send_parts(chat,ct.positions_text(),ct.wallet_buttons(ct.positions_wallets())+keyboard())   # answer at once
  except Exception as exc:send(chat,"Posições indisponíveis: "+type(exc).__name__,keyboard());return
  def check_chain():
   """Blockchain check for coins the bot lost track of: off the tap, reports only if it found something."""
   try:
    ct.adopt_orphans()
    found=ct.recover_real_orphans(force=True)
    for m in found:send(chat,m)
    if found:send_parts(chat,ct.positions_text(),ct.wallet_buttons(ct.positions_wallets())+keyboard())
   except Exception as exc:logging.warning("orphan recovery failed: %s",type(exc).__name__)
  run_async(check_chain)
  return
 if action in ("copy_following","/copiadas"):
  import copy_trader as ct
  try:
   send_parts(chat,ct.wallets_text(),ct.manage_buttons()+keyboard())
  except Exception as exc:send(chat,"Lista indisponível: "+type(exc).__name__,keyboard())
  return
 if action.startswith(("cw_off:","cw_on:")) or action in ("/pausar","/retomar","/ativar"):
  import copy_trader as ct
  if ":" in action:addr=action.split(":",1)[1];pause=action.startswith("cw_off")
  else:
   parts=(msg or {}).get("text","").split()
   if len(parts)!=2:send(chat,"Uso: /pausar ENDERECO ou /retomar ENDERECO (ou use os botões em 👛 Carteiras copiadas)",keyboard());return
   addr=parts[1];pause=action=="/pausar"
  if ct.set_paused(addr,pause) is None:send(chat,"Essa carteira não está na lista de copiadas.",keyboard());return
  if cb:   # the old list still shows the old status: remove it, the updated list is sent below
   try:request_api("deleteMessage",chat_id=chat,message_id=cb.get("message",{}).get("message_id"))
   except Exception:pass
  w=ct._load()["wallets"].get(addr,{})
  state=("⏸️ PAUSADA" if w.get("paused") else "✅ COPIANDO")
  send(chat,("⏸️ Pausei "+addr[:6]+"…"+addr[-4:]+": não copio mais as compras dela. Posições abertas dela continuam e vendem quando ela vender." if pause
   else "▶️ Retomei "+addr[:6]+"…"+addr[-4:]+": volto a copiar as compras dela.")+"\nStatus agora: "+state)
  send_parts(chat,ct.wallets_text(),ct.manage_buttons()+keyboard());return
 if action=="/proporcional":
  import copy_trader as ct
  parts=(msg or {}).get("text","").split()
  if len(parts)!=2:send(chat,"Uso: /proporcional ENDERECO — liga/desliga copiar com a MESMA % do saldo que a carteira usa em cada compra.",keyboard());return
  on=not ct.proportional(parts[1])
  if ct.set_proportional(parts[1],on) is None:send(chat,"Essa carteira não está na lista de copiadas.",keyboard());return
  send(chat,("📐 "+parts[1][:6]+"…"+parts[1][-4:]+": agora copio cada compra com a MESMA % do saldo que ela usa (10% → 10%, 30% → 30%), no máximo "+
   "entre "+format(ct.PROPORTIONAL_MIN_FRACTION*100,".0f")+"% e "+format(ct.PROPORTIONAL_MAX_FRACTION*100,".0f")+"% da carteira do bot por compra." if on else
   "📐 "+parts[1][:6]+"…: voltou ao valor fixo de US$ "+format(ct.ORDER_USD,".0f")+" por cópia."),keyboard());return
 if action in ("copy_wallets","/carteiras"):
  try:send(chat,discovery_text(),keyboard())
  except Exception as exc:send(chat,"Busca indisponível: "+type(exc).__name__,keyboard())
  return
 if action in ("copy_sim","copy_real","copy_off","copy_status","/copy","/seguir","/deixar","/status"):
  import copy_trader as ct
  try:
   parts=(msg or {}).get("text","").split()
   if action=="/seguir":
    if len(parts)!=2:send(chat,"Uso: /seguir ENDERECO_DA_CARTEIRA. Não precisa: o bot acha carteiras sozinho.",keyboard());return
    ct.follow(parts[1]);pre="✅ Seguindo "+parts[1]+". Compras antigas não são copiadas, só as próximas. Vou medir quanto tempo ela segura as moedas antes de copiar.\n\n"
    run_async(audit_job,chat,[parts[1]])
   elif action=="/deixar":
    if len(parts)!=2:send(chat,"Uso: /deixar ENDERECO_DA_CARTEIRA",keyboard());return
    pre=("🗑️ Parei de seguir "+parts[1]+".\n\n" if ct.unfollow(parts[1]) else
     "⏳ "+parts[1][:6]+"… tem uma posição copiada aberta: não copio mais compras dela, mas vendo junto quando ela vender e depois tiro da lista.\n\n")
   elif action=="copy_sim":ct.set_mode(True,False);pre="🧪 Copy trade em SIMULAÇÃO (preço real, nenhuma ordem real).\n\n"
   elif action=="copy_real":
    if not start_real_or_wait(chat,"copy trade"):
     ct.set_mode(True,True);send(chat,"⏳ Copy trade REAL ligado. Tem 1 ordem sendo conferida na blockchain: o robô volta a comprar sozinho assim que ela for liberada (~2 min).",keyboard());return
    ct.set_mode(True,True)
    pre="💵 COPY TRADE COM DINHEIRO REAL LIGADO — US$ "+format(ct.ORDER_USD,".0f")+" por cópia, anti-golpe e teste de venda antes de cada compra, ordem confirmada na blockchain. "+("Sai só quando a carteira vender (sem alvo/stop fixo). " if ct.EXIT_MODE=="wallet" else "Vende quando a carteira vender, ou no stop −"+format(ct.STOP_LOSS*100,".0f")+"% / alvo +"+format(ct.TAKE_PROFIT*100,".0f")+"%. ")+("Para de comprar se perder US$ "+format(trade_state.DAILY_LOSS_LIMIT_USD,".0f")+" no dia." if trade_state.DAILY_LOSS_LIMIT_ENABLED else "Sem limite de perda diária.")+"\n⚠️ Copiar não garante lucro: você entra depois da carteira copiada.\n\n"
   elif action=="copy_off":ct.set_mode(False);pre="⏹️ Copy trade desligado: não abre novas cópias. As posições abertas continuam sendo vendidas quando a carteira copiada vender.\n\n"
   else:pre=""
   send(chat,pre+status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Copy trade: "+str(exc)[:200],keyboard())
  return
 legacy={"btc_on","/btc_on","btc_off","/btc_off","btc_status","/btc","lev","/alavancagem","strat_trend","strat_dip","strat_dip_ok","strat_scalp","strat_scalp_ok","tf_1h","tf_5m","mode_real","mode_sim","btc_test","/btc_teste","backtest","/backtest","/btc_reset",
  "live_start","/iniciar","/live_on","live_stop","/parar","/live_off","live_on","live_status","/status_real",
  "sniper_on","/sniper_on","sniper_off","/sniper_off","pumpfun_on","pumpfun_off","pump_on","/pump_on","pump_off","/pump_off",
  "sim_on","/sim_on","sim_off","/sim_off","/sim_reset","history","/historico","autostatus","/auto","/autostatus","/auto_on","auto_start","/auto_off","auto_stop",
  "positions","/posicoes","/simular","/remover","alerts","/alerts","settings","/settings","scan","/scan","latest","/latest",
  "trade_on","trade_off","trade_status","snipe_on","snipe_off","candle_on","candle_off","launch_on","launch_off",
  "tsim_on","tsim_off","tsim_status","/tsim","/tsim_reset","tb_real_on","tb_real_off","sn_sim","sn_real","sn_off","sn_status","/sniper",
  "mm_on","mm_off","mm_status","/mm",
  "launch_on","launch_off","launch_status","vol_sim","vol_real","vol_off","vol_status","/volume",
  "btcx_sim","btcx_real","btcx_off","btcx_status","btcx_lev","btcx_test","btcx_bt",
  "clone_help","/analisar","/clonar","/estrategia","/robo",
  "mev_on","mev_off","mev_status","/mev","mev_config","mev_amount","mev_min","mev_tip","mev_mode_sim","mev_mode_real","mev_scan","mev_history"}
 if action in legacy or action.startswith(("lev_","brk_","strat_")):  # removed controls (BTC, strategy robot, clone)
  send(chat,"Esse controle foi removido. Use o menu abaixo.",keyboard());return
 if action in ("/start","/menu","help"):menu(chat)
 else:menu(chat)
def user_copy_action(chat,action):
 """Copy trade of an account that is not the owner (its own wallet, 4% fee per buy and sell)."""
 import user_copy as uc
 kb=user_keyboard();fee=format(uc.FEE_RATE*100,".0f")+"%"
 try:
  if action=="u_sim":
   uc.set_mode(chat,"sim")
   send(chat,"🧪 Seu copy trade em SIMULAÇÃO (preços reais, US$ 100 de mentira, taxa de "+fee+" simulada).\n\n"+uc.status_text(chat),kb);return
  if action=="u_real":
   if not uc.load(chat).get("terms_at"):
    send(chat,"💵 COPY TRADE REAL — LEIA ANTES DE LIGAR\n"
     "• O bot compra e vende com a SUA carteira do bot, copiando as carteiras que ele encontrou e auditou.\n"
     "• TAXA: "+fee+" do valor de CADA COMPRA e de CADA VENDA, cobrada automaticamente na MESMA transação da operação e enviada em SOL para a carteira do dono do bot.\n"
     "• Valor por cópia: US$ "+format(uc.load(chat)["order_usd"],".0f")+" (mude em ⚙️ Valor por cópia). Máx. "+str(uc.MAX_POSITIONS)+" posições.\n"
     "• Memecoins são de alto risco: você pode perder todo o valor. Copiar não garante lucro.\n"
     "• Deixe ~0,01 SOL a mais para taxas da rede.",
     [[{"text":"✅ Aceito a taxa de "+fee+" e ligo o REAL","callback_data":"u_real_ok"}],[{"text":"⬅️ Cancelar","callback_data":"u_status"}]]);return
   action="u_real_ok"
  if action=="u_real_ok":
   uc.accept_terms(chat);uc.set_mode(chat,"real")
   address,_=user_wallets.create_or_get(chat)
   send(chat,"💵 SEU COPY TRADE REAL ESTÁ LIGADO (taxa "+fee+" por compra e por venda).\nCarteira: "+address+"\n\n"+uc.status_text(chat),kb);return
  if action=="u_off":
   uc.set_mode(chat,"off")
   send(chat,"⏹️ Seu copy trade foi desligado: não abre novas cópias. As posições abertas são vendidas quando a carteira copiada vender.\n\n"+uc.status_text(chat),kb);return
  if action=="u_pos":
   import copy_trader as ct
   leaders=[p.get("leader") for p in uc.load(chat)["positions"].values()]
   send_parts(chat,uc.positions_text(chat),ct.wallet_buttons(leaders)+kb);return
  if action=="u_size":
   send(chat,"⚙️ Quanto usar em cada cópia? (agora: US$ "+format(uc.load(chat)["order_usd"],".0f")+")",
    [[{"text":"US$ "+str(v),"callback_data":"u_size_"+str(v)} for v in uc.ORDER_CHOICES],[{"text":"⬅️ Voltar","callback_data":"u_status"}]]);return
  if action.startswith("u_size_"):
   uc.set_order_usd(chat,int(action.split("_")[-1]))
   send(chat,"✅ Valor por cópia: US$ "+action.split("_")[-1]+".\n\n"+uc.status_text(chat),kb);return
  send(chat,uc.status_text(chat),kb)
 except Exception as exc:send(chat,"⛔ Copy trade: "+str(exc)[:200],kb)
WALLET_ACTIONS={"wallet_export","/exportar_chave","wallet_create","/criar_carteira","/carteira","/wallet","wallet_connect",
 "/depositar","/deposito","wallet_deposit","/sacar","wallet_withdraw","wallet_balance","/saldo"}
def wallet_action(chat,action):
 """The account's own wallet (created automatically, unique per Telegram account)."""
 owner=authorized(chat);kb=keyboard_for(chat)
 try:
  address,created=user_wallets.create_or_get(chat)
 except Exception as exc:
  logging.warning("wallet unavailable: %s",type(exc).__name__)
  send(chat,"⚠️ Não foi possível abrir sua carteira agora. Tente de novo em instantes.",kb);return
 if created:
  send(chat,"🆕 CARTEIRA CRIADA SÓ PARA VOCÊ\nCada conta do Telegram recebe a sua própria carteira Solana, única e nova.\n\nEndereço (Solana mainnet):\n"+address+
   "\n\n🔐 A chave privada fica criptografada no servidor e não é exportada."+
   ("" if owner else "\n\n👥 Você também tem copy trade: teste em 👥 Copiar (SIMULAÇÃO) ou deposite SOL e ligue 💵 Copiar (REAL) (taxa de 4% por compra e por venda)."),kb)
  if action=="/start" and not owner:return
 if action=="/start" and not owner:
  send(chat,"👛 SUA CARTEIRA DO BOT\n"+address+"\n\nEla é só sua: cada conta do Telegram tem a própria carteira.",kb);return
 if action in ("wallet_export","/exportar_chave"):
  send(chat,"🔒 A exportação de chave privada foi DESATIVADA no bot, para todas as contas.",kb);return
 if action in ("/sacar","wallet_withdraw"):
  send(chat,"💸 SAQUE\nA exportação de chave privada está desativada e o bot ainda não faz saques automáticos.\nPara sacar, fale com o dono do bot.\nSua carteira: "+address,kb);return
 if action in ("/depositar","/deposito","wallet_deposit"):
  send(chat,"📥 DEPÓSITO SOLANA\nEnvie SOL pela rede Solana para a SUA carteira do bot:\n"+address+"\nConfira o endereço antes de transferir. Veja o saldo em 💰 Ver saldo.",kb);return
 if action in ("wallet_balance","/saldo","/wallet","/carteira","wallet_connect"):
  try:
   rpc=os.getenv("SOLANA_RPC_URL","https://api.mainnet-beta.solana.com")
   response=session.post(rpc,json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]},timeout=6)
   response.raise_for_status()
   data=response.json()
   if "error" in data:raise ValueError("RPC error")
   balance=int(data["result"]["value"])/1_000_000_000
   send(chat,"💰 SALDO — SUA CARTEIRA DO BOT\n"+address+"\nSaldo: "+fmt(balance)+" SOL\nRede: Solana mainnet",kb)
  except (requests.RequestException,ValueError,KeyError,TypeError):
   send(chat,"👛 Sua carteira: "+address+"\nNão foi possível consultar o saldo agora.",kb)
  return
 send(chat,"👛 SUA CARTEIRA DO BOT\n"+address,kb)
def start_real_or_wait(chat,what):
 """Start the live engine. If an order is still being checked on-chain, wait for it in the background
 and start then (instead of failing with 'Unreconciled transaction'). True = started now."""
 try:
  live_engine.start();return True
 except Exception as exc:
  if not trade_state.pending_orders():raise
 def wait_and_start():
  for _ in range(60):          # up to 10 min
   time.sleep(10)
   if not trade_state.pending_orders():
    try:
     live_engine.start();send(chat,"🟢 Ordem pendente conferida: robô real liberado e "+what+" operando.",keyboard())
    except Exception as exc:send(chat,"⛔ Não consegui ligar o "+what+": "+str(exc)[:150],keyboard())
    return
  send(chat,"⚠️ A ordem pendente ainda não foi conferida depois de 10 min. Toque em 🔧 Destravar robô.",keyboard())
 run_async(wait_and_start);return False
def leftover_btc_warning():
 """The BTC breakout robot was removed: warn if its saved state still shows an open real position."""
 try:
  import json
  path=trade_state._path().with_name("btc_breakout.json")
  if path.exists() and (json.loads(path.read_text(encoding="utf-8")).get("real_position")):
   return ("⚠️ O robô BTC foi removido, mas ficou uma posição REAL de BTC aberta na Jupiter Perps. "
    "Feche ela pela Jupiter (jup.ag/perps) com a carteira do bot.")
 except Exception:pass
 return None
def startup_notice():
 """After a deploy/restart: say what state the robot resumed in."""
 chat=active_chat()
 if not chat:return
 send(chat,"🔄 Bot reiniciado (deploy).\n"+status_text(),keyboard())
 warn=leftover_btc_warning()
 if warn:send(chat,warn)
 if not trade_state.status()["positions"] and not trade_state.status()["pending"]:return
 st=trade_state.status()
 reason=live_engine.why_stopped()
 send(chat,"🔄 Bot reiniciado (deploy). Estado salvo recuperado:\n"+
  ("🟢 Robô LIGADO e operando" if reason is None else "🔴 Robô parado: "+reason)+
  "\n📦 Posições abertas: "+str(st["positions"])+"/"+str(trade_state.MAX_POSITIONS)+" (venda automática continua)"+
  ("\n⏳ "+str(st["pending"])+" ordem(ns) pendente(s): confiro sozinho nos saldos e libero em ~2 min." if st["pending"] else ""))
HEARTBEAT={"at":0.0,"step":"iniciando"}
def beat(step):
 HEARTBEAT.update(at=time.time(),step=step)
def background_loop():
 """Position upkeep (sync, recovery, exits), kept off the Telegram thread so buttons answer at once."""
 next_positions=0;next_reclaim=time.monotonic()+60;next_sync=0;last_recovery_notice=time.monotonic()
 while True:
  try:
   beat("conferência de posições")
   if active_chat() and time.monotonic()>=next_sync:
    next_sync=time.monotonic()+60
    try:live_engine.sync_positions(send,active_chat())
    except Exception:logging.exception("position sync failed")
   beat("devolução de depósito")
   if active_chat() and time.monotonic()>=next_reclaim:
    next_reclaim=time.monotonic()+600
    try:
     import rent_reclaim
     if not trade_state.pending_orders():
      closed,lamports,sig=rent_reclaim.reclaim(rent_reclaim.rpc_full,rent_reclaim.bot_signer())
      if closed:
       send(active_chat(),"♻️ Fechei "+str(closed)+" conta(s) de token vazia(s) e devolvi "+format(lamports/1e9,".6f")+" SOL de depósito para a carteira.")
    except Exception as exc:logging.warning("rent reclaim failed: %s",str(exc)[:120])
   if active_chat() and time.monotonic()-last_recovery_notice>3600:
    last_recovery_notice=time.monotonic()
    try:
     pending=trade_state.pending_orders()
     if pending:
      send(active_chat(),"⏳ "+str(len(pending))+" operação(ões) pendente(s). Estou conferindo sozinho os saldos na blockchain; o robô volta a operar assim que confirmar.")
    except Exception:logging.exception("recovery pending inspection failed")
   beat("vendas automáticas")
   if active_chat() and time.monotonic()>=next_positions:
    next_positions=time.monotonic()+20

    try:live_engine.recover(send,active_chat())
    except Exception:logging.exception("pending order recovery failed")
    try:
     if os.getenv("LIVE_TRADING_ENABLED","true").strip().lower()!="false":live_engine.exit_cycle(send,active_chat())
     else:live_exit_monitor.monitor(send,active_chat())
    except Exception:logging.exception("live exit cycle failed")
  except Exception:logging.exception("background worker error")
  beat("aguardando")
  time.sleep(1)
def sniper_loop():
 """Sniper: scans DexScreener every SNIPER_SCAN_SECONDS, all day."""
 import sniper
 while True:
  time.sleep(sniper.SCAN_EVERY_S)
  try:
   chat=active_chat()
   if chat:sniper.cycle(send,chat)
  except Exception as exc:logging.warning("sniper cycle failed: %s",type(exc).__name__)
USERS_UPKEEP={"next":0.0}
COPY_BEAT={"at":0.0,"took":0.0,"error":""}
def copy_beat_line():
 if not COPY_BEAT["at"]:return "⏳ Copy trade: primeira rodada ainda não terminou"
 ago=int(time.time()-COPY_BEAT["at"])
 return ("🚨 " if ago>120 else "🔁 ")+"Copy trade: última rodada há "+str(ago)+" s (levou "+format(COPY_BEAT["took"],".0f")+" s)"+(" · erro "+COPY_BEAT["error"] if COPY_BEAT.get("error") else "")
def copy_loop():
 """Copy trading: read the followed wallets every COPY_POLL_SECONDS."""
 poll=max(3.0,float(os.getenv("COPY_POLL_SECONDS","5")))
 while True:
  time.sleep(poll)
  try:
   import copy_trader
   chat=active_chat()
   if not chat:continue
   started=time.time()
   for m in copy_trader.cycle(send,chat):send(chat,m)   # also runs with copy off: sells open copies with the wallet
   took=time.time()-started
   COPY_BEAT.update(at=time.time(),took=took)
   if took>30:logging.warning("copy cycle slow: %.0f s",took)
  except Exception as exc:
   COPY_BEAT.update(at=time.time(),error=type(exc).__name__)
   logging.warning("copy cycle failed: %s: %s",type(exc).__name__,str(exc)[:200])
  try:
   import consensus
   chat=active_chat()
   if chat:consensus.cycle(send,chat)        # consensus robot: buys what several followed wallets buy
  except Exception as exc:logging.warning("consensus cycle failed: %s",type(exc).__name__)
  if time.monotonic()>=USERS_UPKEEP["next"]:
   USERS_UPKEEP["next"]=time.monotonic()+60
   try:
    import user_copy
    user_copy.upkeep(send)       # users: owed fees, unconfirmed buys, failed sells
   except Exception as exc:logging.warning("users upkeep failed: %s",type(exc).__name__)

def dex_rpc():
 import dex_wallets
 return dex_wallets._rpc_factory(os.getenv("SOLANA_RPC_URL"))
def wallet_discovery_loop():
 """Analyze confirmed blocks and enroll qualifying wallets for future signals."""
 source=os.getenv("COPY_DISCOVERY_SOURCE","dexscreener").strip().lower()
 interval=max(20,int(os.getenv("COPY_DISCOVERY_INTERVAL_SECONDS","120" if source=="dexscreener" else "30")))
 if os.getenv("COPY_AUTO_DISCOVERY_ENABLED","true").strip().lower()=="false":return
 warned=False
 while True:
  try:
   import wallet_copy
   import copy_trader
   if not os.getenv("SOLANA_RPC_URL"):
    if not warned and active_chat():
     warned=True
     send(active_chat(),"⚠️ A busca automática de carteiras precisa de um RPC Solana dedicado. Configure SOLANA_RPC_URL no Render (ex.: Helius, plano grátis).")
    time.sleep(interval);continue
   if source in ("dexscreener","ambos","both"):
    import dex_wallets
    dex_wallets.scan()
   if source in ("blocks","blocos","ambos","both"):
    wallet_copy.scan_confirmed(max_blocks=100)
   added=[]
   candidates=wallet_copy.best_wallets(limit=50)
   followed={c["wallet"] for c in candidates if copy_trader.refresh_stats(c)}
   # audit BEFORE following (a few per round): only approved wallets enter the list
   rpc=dex_rpc()
   checked=0;budget=copy_trader.preaudit_budget()   # more per round while the list has free slots (max 20)
   for candidate in candidates:
    if checked>=budget:break
    a=candidate["wallet"]
    if a in followed or copy_trader.is_rejected(a) or not copy_trader.prefilter(candidate):continue
    checked+=1
    try:outcome=copy_trader.preaudit_and_follow(candidate,rpc,copy_trader._sol_usd())
    except Exception as exc:
     logging.warning("pre-audit failed: %s",type(exc).__name__);continue
    if outcome in ("added","replaced"):added.append(a)
   removed=copy_trader.prune_low_balance()+copy_trader.remove_fast_wallets()
   chat=active_chat()
   if chat and removed:
    send(chat,"🗑️ Parei de copiar "+str(len(removed))+" carteira(s) depois do teste de "+format(copy_trader.TEST_DAYS,".0f")+
     " dias (sem lucro nas cópias). A busca coloca novas no lugar.\n"+
     "\n".join("• "+x[:6]+"…"+x[-4:]+": "+copy_trader.REMOVAL_REASON.get(x,"saldo baixo")+"\n  solscan.io/account/"+x for x in removed))
   if chat and added:
    total=sum(1 for w in copy_trader._load()["wallets"].values() if not w.get("paused"))
    text=("🆕 NOVA CARTEIRA PARA COPIAR" if len(added)==1 else "🆕 "+str(len(added))+" NOVAS CARTEIRAS PARA COPIAR")+\
     " — agora copiando "+str(total)+" (limite "+str(copy_trader.MAX_AUTO_WALLETS)+").\nSó as próximas compras serão copiadas; nenhuma posição antiga.\n\n"+\
     "\n\n".join(copy_trader.wallet_card(x) for x in added)+"\n\n✅ Já auditada: passou nos últimos "+format(copy_trader.RESULT_DAYS,".0f")+" dias (lucro, ≥ "+str(copy_trader.RESULT_MIN_TRADES)+" operações, acerto)."
    for part in split_message(text):send(chat,part)
  except Exception as exc:
   logging.warning("wallet discovery failed: %s: %s",type(exc).__name__,str(exc)[:200])
  time.sleep(interval)
def audit_job(chat=None,addresses=None):
 """Measure how long each followed wallet holds its coins; block the fast ones."""
 try:
  import copy_trader,dex_wallets
  rpc=dex_wallets._rpc_factory(os.getenv("SOLANA_RPC_URL"))
  if addresses:
   lines=[]
   for a in addresses:
    res=copy_trader.audit_wallet(a,rpc,copy_trader._sol_usd())
    if res:lines.append(copy_trader.audit_line(a,res))
   copy_trader.remove_fast_wallets()
   text="\n".join(lines)
  else:
   text=copy_trader.audit_all(rpc,copy_trader._sol_usd())
  chat=chat or active_chat()
  if chat and text:
   for part in split_message(text):send(chat,part)
 except Exception as exc:
  logging.warning("audit failed: %s: %s",type(exc).__name__,str(exc)[:200])
  if chat:send(chat,"⛔ Auditoria falhou: "+type(exc).__name__)
def audit_loop():
 """Audit every followed wallet at start and every 6 h."""
 time.sleep(60)
 while True:
  if os.getenv("SOLANA_RPC_URL") and active_chat():audit_job()
  time.sleep(int(os.getenv("COPY_AUDIT_EVERY_HOURS","6"))*3600)
def run_async(fn,*args):
 interactive=getattr(CTX,"interactive",False)   # a job the owner asked for answers in full
 def run():
  CTX.interactive=interactive
  fn(*args)
 threading.Thread(target=run,daemon=True).start()
def loop():
 offset=None
 try:startup_notice()
 except Exception:logging.exception("startup notice failed")
 threading.Thread(target=background_loop,daemon=True).start()
 threading.Thread(target=copy_loop,daemon=True).start()
 try:
  import copy_trader
  copy_trader.start_buy_worker()        # real copy buys run one by one off the wallet-reading loop
 except Exception:logging.exception("copy buy worker failed to start")
 threading.Thread(target=sniper_loop,daemon=True).start()
 threading.Thread(target=audit_loop,daemon=True).start()
 threading.Thread(target=holdings_loop,daemon=True).start()
 threading.Thread(target=wallet_discovery_loop,daemon=True).start()
 while True:
  try:
   if TOKEN:
    data=request_api("getUpdates",offset=offset,timeout=8,allowed_updates=["message","callback_query"])
    for u in data.get("result",[]):
     offset=u["update_id"]+1
     run_async(handle_safe,u)   # each tap answers on its own: a slow one never blocks the others
   else:time.sleep(10)
  except requests.HTTPError as exc:
   if exc.response is not None and exc.response.status_code==409:
    logging.warning("Telegram polling conflict: another instance may be polling this token")
   else:logging.exception("telegram HTTP error")
   time.sleep(15)
  except Exception:logging.exception("telegram worker error");time.sleep(10)

@app.route("/exportar",methods=["GET","POST"])
def export_page():
 """Private key export was removed from the bot (for every account)."""
 return Response("<!doctype html><meta charset='utf-8'><p>Exportação de chave privada desativada.</p>",status=410,
  mimetype="text/html",headers={"Cache-Control":"no-store","X-Frame-Options":"DENY"})
@app.get("/")
def health():return {"service":"solana-memecoin-copytrade","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":live_engine.ready()}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
