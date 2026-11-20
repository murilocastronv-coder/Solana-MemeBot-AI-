"""Telegram bot: memecoin copy trading on Solana (automatic wallet discovery, simulation or real money)."""
import os,time,logging,threading,requests,re,secrets,html,json
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
_TG=threading.local()   # one HTTP session per thread: a shared Session is not thread-safe
def _tg_session(reset=False):
 s=getattr(_TG,"session",None)
 if reset and s is not None:
  try:s.close()
  except Exception:pass
  s=None
 if s is None:s=_TG.session=requests.Session()
 return s
def request_api(method,**params):
 if not TOKEN:return {}
 # long poll needs its own timeout + margin; every other call should answer fast
 timeout=(10,float(params.get("timeout",0))+15) if method=="getUpdates" else (10,15)
 try:
  r=_tg_session().post(BASE+"/"+method,json=params,timeout=timeout)
 except requests.RequestException as exc:
  _tg_session(reset=True)   # a stuck/half-closed connection must not be reused
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
TRADE_RE=re.compile(r"NOVA PESSOA NO BOT|DEPÓSITO:|COMPRA DE USUÁRIO|VENDA DE USUÁRIO|SAQUE DE USUÁRIO|ROBÔ LIGADO por|Robô desligado por|BACKTEST|COMPRANDO|COMPROU|SIM comprou|copiou compra|VENDEU|VENDENDO|vendeu|💰 LUCRO|🔻 PREJUÍZO|MOEDA ZERADA|Compra confirmada|Venda confirmada|STOP MÓVEL|SINAL DE ENTRADA|TP1 ATINGIDO|TP2 ATINGIDO|STOP LOSS —|VOLTOU À ENTRADA|SINAL ENCERRADO|DIP BOT:|SNIPER:")
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
def send_photo(chat,png,caption,keyboard=None):
 """A picture with a caption (memecoin proposal logo). Falls back to text when the upload fails."""
 if not TOKEN:return {}
 data={"chat_id":str(chat),"caption":(caption or "")[:1000]}
 if keyboard:data["reply_markup"]=json.dumps({"inline_keyboard":keyboard})
 try:
  r=_tg_session().post(BASE+"/sendPhoto",data=data,files={"photo":("logo.png",png,"image/png")},timeout=(10,30))
  if r.ok:return r.json()
 except requests.RequestException:pass
 return send(chat,caption,keyboard)
def send(chat,text,keyboard=None):
 try:
  import dashboard
  dashboard.record(text.replace("`","") if text else text)   # the web panel shows every notice
 except Exception:pass
 if not should_send(text,chat=chat):
  logging.info("repeated notice held back (%.0f h): %s",NOTIFY_REPEAT_S/3600,(text or "")[:80].replace("\n"," "))
  return {}
 text,code=code_spans(text)
 args={"chat_id":chat,"text":text,"disable_web_page_preview":True}
 ents=code+[e for e in address_entities(text) if not any(c["offset"]<=e["offset"]<c["offset"]+c["length"] for c in code)]
 if ents:args["entities"]=ents     # full addresses in monospace: one tap copies them
 if keyboard:args["reply_markup"]={"inline_keyboard":keyboard}
 return request_api("sendMessage",**args)
def edit_message(chat,message_id,text,keyboard=None):
 """Edits a message in place (live views). 'not modified' answers are ignored."""
 args={"chat_id":chat,"message_id":message_id,"text":text,"disable_web_page_preview":True}
 if keyboard:args["reply_markup"]={"inline_keyboard":keyboard}
 try:return request_api("editMessageText",**args)
 except RuntimeError:return {}
ADDRESS_RE=re.compile(r"(?<![/\w.])[1-9A-HJ-NP-Za-km-z]{32,44}(?![\w])")
CODE_RE=re.compile(r"`([^`\n]{1,120})`")
def code_spans(text):
 """`value` -> value shown in monospace (Telegram 'code': one tap copies it). Returns (text, entities)."""
 out,ents,pos=[],[],0
 for m in CODE_RE.finditer(text or ""):
  out.append(text[pos:m.start()])
  start=len("".join(out).encode("utf-16-le"))//2
  out.append(m.group(1))
  ents.append({"type":"code","offset":start,"length":len(m.group(1).encode("utf-16-le"))//2})
  pos=m.end()
 out.append((text or "")[pos:])
 return "".join(out),ents
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
 """Owner's buttons: the trading bot home (owner, Nov 18: only the trading bot)."""
 import trade_panel
 return trade_panel.home_keyboard(active_chat())
def user_keyboard():
 """Menu of a Telegram account that is not the owner: its own copy trade and its own wallet."""
 return [[{"text":"👥 Copiar (SIMULAÇÃO)","callback_data":"u_sim"},{"text":"💵 Copiar (REAL)","callback_data":"u_real"}],
  [{"text":"⏹️ Parar cópia","callback_data":"u_off"},{"text":"📊 Meu status","callback_data":"u_status"}],
  [{"text":"📦 Minhas posições","callback_data":"u_pos"},{"text":"⚙️ Valor por cópia","callback_data":"u_size"}],
  [{"text":"👛 Minha carteira","callback_data":"wallet_create"}],
  [{"text":"📥 Depositar SOL","callback_data":"wallet_deposit"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}],
  [{"text":"💸 Saque","callback_data":"wallet_withdraw"},{"text":"⬅️ Início","callback_data":"thome"}]]
def keyboard_for(chat):
 import trade_panel
 return trade_panel.home_keyboard(chat)
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
 import trade_panel
 send(chat,trade_panel.home_text(chat),trade_panel.home_keyboard(chat))   # owner (Nov 18): only the trading bot
def fmt(v):
 if v == 0:return "0"
 return format(v, ".12f").rstrip("0").rstrip(".") if abs(v)<1 else format(v, ".12f").rstrip("0").rstrip(".")
def active_chat():
 if CHAT:return CHAT
 try:return telegram_owner.get_owner()
 except Exception:
  logging.exception("persistent Telegram owner unavailable; deny access")
  return None

TRADE_ACTIONS={"bfm","/btc","bfm_real_ok","bfm_on_demo","bfm_on_real","bfm_off_demo","bfm_off_real","bfm_close_demo","bfm_close_real","bfm_lev2","bfm_lev3","bfm_lev5","bfm_lev10","bfm_lev15","bfm_lev20","bfm_lev30","bfm_lev40","bfm_lev50","bfm_res","/resultados_btc","bfm_bt","/backtest_btc","bfm_bt90","/backtest_btc_3m","bfm_bt_sol","bfm_bt90_sol","bfm_bt_eth","bfm_bt90_eth","/backtest_sol","/backtest_eth","bfm_bts_sol","bfm_bts90_sol","bfm_bts_eth","bfm_bts90_eth","bfm_btd_sol","bfm_btd90_sol","bfm_opt_sol","bfm_opt_btc","bfm_opt_eth","bfm_use_demo","bfm_use_real","bfm_live_demo","bfm_live_real","bfm_live_stop","/aovivo","fx","fx_on","fx_off","fx_deploy","fx_close","/forex","trpos","/posicoes_robo","tsn_on","tsn_off","tsn_reset","trd_on","trd_off","trd_reset","vid","vid_on","vid_off","vid_now","vid_yt","/videos","sup","sup_on","sup_off","sup_code","sup_min","sup_now","/tarefas","tad","/airdrop","tad_on","tad_off","tad_w","tad_now","trb_bt","/backtest","trob","trb_on","trb_off","trb_sol","/robo","tpl","tlim","tset","thelp","tbuy","tterms","tsb","tss","tsa","tst","tsl","tsr","thome","tsellm","tdl","twl","tscan",
 "tsc","tsv","tsell","twal","tsec","towner","trade_help","/posicoes","/ordens","/config","/ajuda","/comprar","/home","/dca","/watchlist","/scanner"}
TRADE_PREFIXES=("tb","tbx","ts","tt","tl","tr","tc","tlo","tld","tla","tlx","tsoon","tsp","tw","tdo","tdp","tdx","tbc","tsx")
def TRADE_ROUTE(cb,action):
 if action in TRADE_ACTIONS:return True
 return bool(cb) and ":" in action and action.split(":")[0] in TRADE_PREFIXES
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
  if "btc full margin" in (msg or {}).get("text","").lower() or action.lower() in ("/btc","btc"):action="bfm"   # typed / forwarded button text
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
 import trade_panel
 text=(msg or {}).get("text","")
 if not authorized(chat):
  try:__import__("owner_notify").visit(chat,(cb or msg or {}).get("from"))
  except Exception:logging.exception("visit stats failed")
 if action in ("tres","/resultados"):
  def show_results():
   import demo_report
   try:
    t,png=demo_report.results(chat)
    send_photo(chat,png,"📈 Saldo das carteiras de teste ao longo do tempo")
    send(chat,t,keyboard_for(chat))
   except Exception as exc:send(chat,"⛔ Resultados: "+type(exc).__name__+": "+str(exc)[:150],keyboard_for(chat))
  run_async(show_results);return
 import airdrop_bot
 if TRADE_ROUTE(cb,action) or (not cb and (chat in trade_panel.INPUT or chat in airdrop_bot.WAIT or airdrop_bot.STATUS_RE.search(text or ""))):
  if trade_panel.handle(action,None if cb else text,send,chat,[[{"text":"⬅️ Início","callback_data":"thome"}]],run_async):return
 if action=="tcopy":
  if authorized(chat):trade_panel.handle("tsoon:copy",None,send,chat,[[{"text":"⬅️ Início","callback_data":"thome"}]],run_async)
  else:user_copy_action(chat,"u_status")
  return
 if (action in ("/sacar","/retirar","wallet_withdraw") or action.startswith("wd_") or
   (not cb and chat in WITHDRAW and time.time()-WITHDRAW[chat]["at"]<600 and
    (not text.startswith("/") or text.strip()=="/cancelar"))):
  owner_withdraw(chat,action,text.strip());return
 if trade_panel.AIRDROP_ONLY and trade_panel.HOME!="superteam" and not cb and trade_panel.candidates(text) and not text.startswith("/"):
  airdrop_bot.WAIT[chat]=time.time()          # only airdrops now: a pasted address is the airdrop wallet
  trade_panel.handle(action,text,send,chat,[[{"text":"⬅️ Início","callback_data":"thome"}]],run_async);return
 if not cb and trade_panel.candidates(text) and not text.startswith("/") and not (authorized(chat) and (chat in EVAL_WALLET or chat in ADD_WALLET)):
  send(chat,"🔎 Procurando a moeda…")
  run_async(lambda:trade_panel.show(send,chat,text,[[{"text":"⬅️ Início","callback_data":"thome"}]]) or
   send(chat,"⛔ Não achei essa moeda no DexScreener (só moedas da Solana com pool).",keyboard_for(chat)))
  return
 if not authorized(chat) and action in ("/start","/menu","help"):
  try:address,created=user_wallets.create_or_get(chat)
  except Exception as exc:
   logging.warning("wallet unavailable: %s",type(exc).__name__)
   send(chat,"⚠️ Não foi possível abrir sua carteira agora. Tente de novo em instantes.");return
  if created:
   send(chat,"🆕 CARTEIRA CRIADA SÓ PARA VOCÊ\nCada conta do Telegram recebe a sua própria carteira Solana.\n\n"+address+
    "\n\n🔐 A chave privada fica criptografada no servidor. Deposite SOL nesse endereço para começar.",
    [[{"text":"📥 Depositar","callback_data":"wallet_deposit"}]])
  send(chat,trade_panel.home_text(chat),trade_panel.home_keyboard(chat));return
 if not authorized(chat) and action.startswith("u_"):
  user_copy_action(chat,action);return
 if action in WALLET_ACTIONS or not authorized(chat):
  # every Telegram account has its own unique wallet and its own copy trade; only the owner runs the main one
  wallet_action(chat,action if action in WALLET_ACTIONS else "/start");return
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
 if action in ("cw_eval","/analisar") or (not cb and chat in EVAL_WALLET and time.time()-EVAL_WALLET[chat]<600
   and SOL_ADDRESS.fullmatch(action or "")):
  eval_wallet_action(chat,action,(msg or {}).get("text",""));return
 if action.startswith("cw_addx:"):
  copy_wallet_action(chat,"/copiar","/copiar "+action.split(":",1)[1]);return
 if action in ("cw_add","cw_list","/copiar","/carteiras") or action.startswith("cw_rm:") or (
   not cb and chat in ADD_WALLET and time.time()-ADD_WALLET[chat]<600 and SOL_ADDRESS.fullmatch(action or "")):
  copy_wallet_action(chat,action,(msg or {}).get("text",""));return
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
    ct.mark_owner_removed(parts[1])
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
 if action in ("/start","/menu","help") or not (cb or action.startswith("/") or re.fullmatch(r"[a-z0-9]+_[a-z0-9_]+",action)):menu(chat)
 else:send(chat,"Esse controle foi removido: o bot agora é só copy trade. Use o menu abaixo.",keyboard())
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
WALLET_ACTIONS={"wallet_export","wallet_export_ok","wallet_export_no","/exportar_chave","wallet_create","/criar_carteira","/carteira","/wallet","wallet_connect",
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
   "\n\n🔐 A chave privada fica criptografada no servidor."+
   ("" if owner else "\n\n👥 Você também tem copy trade: teste em 👥 Copiar (SIMULAÇÃO) ou deposite SOL e ligue 💵 Copiar (REAL) (taxa de 4% por compra e por venda)."),kb)
  if action=="/start" and not owner:return
 if action=="/start" and not owner:
  send(chat,"👛 SUA CARTEIRA DO BOT\n"+address+"\n\nEla é só sua: cada conta do Telegram tem a própria carteira.",kb);return
 if action in ("wallet_export","wallet_export_ok","wallet_export_no","/exportar_chave"):
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
   usd=""
   if owner:
    try:usd=" (≈ US$ "+format(balance*float(__import__("copy_trader")._sol_usd()),",.2f")+")"
    except Exception:usd=""
   send(chat,"💰 SALDO — SUA CARTEIRA DO BOT\n"+address+"\nSaldo: "+fmt(balance)+" SOL"+usd+"\nRede: Solana mainnet"+
    ("\n(Garantia de posições abertas do robô BTC fica na Jupiter, fora desse saldo.)" if owner else ""),kb)
  except (requests.RequestException,ValueError,KeyError,TypeError):
   send(chat,"👛 Sua carteira: "+address+"\nNão foi possível consultar o saldo agora.",kb)
  return
 send(chat,"👛 SUA CARTEIRA DO BOT\n"+address,kb)
WITHDRAW={}
def owner_withdraw(chat,action,text):
 """Withdrawal of SOL from the account's OWN bot wallet (owner: main wallet; others: their wallet):
 address -> amount -> confirm button -> send."""
 import wallet_withdraw as ww
 kb=keyboard_for(chat);owner=authorized(chat)
 own=None
 if action in ("/sacar","/retirar","wallet_withdraw","wd_start"):
  WITHDRAW[chat]={"step":"addr","at":time.time()}
  send(chat,"💸 SACAR SOL da sua carteira do bot\nMande o ENDEREÇO Solana de destino (sua carteira, ex.: Phantom).\n"
   "Para cancelar mande /cancelar.");return
 st=WITHDRAW.get(chat)
 if action=="wd_no" or text in ("/cancelar","cancelar") or not st:
  WITHDRAW.pop(chat,None);send(chat,"❌ Saque cancelado.",kb);return
 try:
  if action=="wd_ok" and st["step"]=="confirm":
   WITHDRAW.pop(chat,None)
   send(chat,"⏳ Enviando "+format(st["lamports"]/1e9,".4f")+" SOL…")
   def run():
    try:
     if owner:sig=ww.send_sol(st["addr"],st["lamports"])
     else:sig=__import__("user_trade").withdraw(chat,st["addr"],st["lamports"])
     send(chat,"✅ SAQUE ENVIADO: "+format(st["lamports"]/1e9,".4f")+" SOL\n➡️ "+st["addr"]+"\n🔗 https://solscan.io/tx/"+str(sig),kb)
    except Exception as exc:send(chat,"⛔ Saque falhou: "+str(exc)[:200],kb)
   run_async(run);return
  if st["step"]=="addr" and not action.startswith("wd_"):
   own=ww.get_address() if owner else user_wallets.get_address(chat)
   addr=ww.valid_address(text)
   if not addr or addr==own:send(chat,"⛔ Endereço inválido (ou é a própria carteira do bot). Mande de novo ou /cancelar.");return
   bal=ww.balance_lamports() if owner else ww.balance_lamports(own)
   st.update(step="amount",addr=addr,at=time.time())
   send(chat,"💰 Saldo da sua carteira do bot: "+format(bal/1e9,".4f")+" SOL\nQuanto sacar? Mande o valor em SOL (ex.: 0.5) ou *tudo*.");return
  if st["step"]=="amount" and not action.startswith("wd_"):
   lam=ww.amount_lamports(text,ww.balance_lamports() if owner else ww.balance_lamports(user_wallets.get_address(chat)))
   st.update(step="confirm",lamports=lam,at=time.time())
   send(chat,"Confirma o saque?\n💸 "+format(lam/1e9,".4f")+" SOL\n➡️ "+st["addr"]+"\nConfira o endereço: transferência na blockchain não tem volta.",
    [[{"text":"✅ Confirmar saque","callback_data":"wd_ok"},{"text":"❌ Cancelar","callback_data":"wd_no"}]]);return
  send(chat,"Use os botões ou /cancelar.")
 except ww.WithdrawError as exc:send(chat,"⛔ "+str(exc)+". Mande de novo ou /cancelar.")
 except Exception as exc:send(chat,"⛔ Saque: "+type(exc).__name__+": "+str(exc)[:150],kb)
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
 import trade_panel
 try:
  moved=trade_panel.adopt_robot_positions()
  if moved:send(chat,"🖐️ Os robôs foram removidos. Essas moedas agora são suas trades manuais (sem alvo / stop até você ligar no cartão): "+", ".join(moved))
 except Exception:logging.exception("adopt robot positions failed")
 send(chat,"🔄 Bot reiniciado (deploy).\n"+trade_panel.home_text(chat),keyboard())
 if not trade_state.status()["positions"] and not trade_state.status()["pending"]:return
 warn=leftover_btc_warning()
 if warn:send(chat,warn)
 if not trade_state.status()["positions"] and not trade_state.status()["pending"]:return
 st=trade_state.status()
 reason=live_engine.why_stopped()
 send(chat,"🔄 Bot reiniciado (deploy). Estado salvo recuperado:\n"+
  ("🟢 Robô LIGADO e operando" if reason is None else "🔴 Robô parado: "+reason)+
  "\n📦 Posições abertas: "+str(st["positions"])+" (venda automática continua)"+
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
   try:live_engine.resend_undelivered(send)
   except Exception:pass
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
       logging.info("rent reclaim: %s accounts, %.6f SOL",closed,lamports/1e9)   # owner: only GRADUADOS messages
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
USERS_UPKEEP={"next":0.0}
COPY_BEAT={"at":0.0,"took":0.0,"error":""}
def copy_beat_line():
 if not COPY_BEAT["at"]:return "⏳ Copy trade: primeira rodada ainda não terminou"
 ago=int(time.time()-COPY_BEAT["at"])
 return ("🚨 " if ago>120 else "🔁 ")+"Copy trade: última rodada há "+str(ago)+" s (levou "+format(COPY_BEAT["took"],".0f")+" s)"+(" · erro "+COPY_BEAT["error"] if COPY_BEAT.get("error") else "")
ADD_WALLET={}
EVAL_WALLET={}
def eval_wallet_action(chat,action,text):
 """Owner sends any wallet: the bot checks it with the search rules and says if it is worth copying."""
 if action=="cw_eval":
  EVAL_WALLET[chat]=time.time();ADD_WALLET.pop(chat,None)
  send(chat,"🔍 ANALISAR CARTEIRA\nMande o ENDEREÇO da carteira Solana (só o endereço) ou /analisar ENDERECO.\n"
   "Eu leio o histórico dela na blockchain e confiro com as mesmas regras da busca automática.");return
 parts=text.split()
 address=parts[1] if action=="/analisar" and len(parts)>1 else (parts[0] if parts else "")
 if not SOL_ADDRESS.fullmatch(address):
  send(chat,"Uso: /analisar ENDERECO_DA_CARTEIRA",keyboard());return
 EVAL_WALLET.pop(chat,None)
 if not os.getenv("SOLANA_RPC_URL"):
  send(chat,"⛔ Para analisar carteiras o bot precisa do RPC Solana (SOLANA_RPC_URL no Render).",keyboard());return
 send(chat,"⏳ Analisando `"+address+"`… (lê o histórico na blockchain, leva 1–3 min)")
 def job():
  import copy_trader as ct
  try:
   ok,text=ct.evaluate_wallet(address,dex_rpc(),ct._sol_usd())
   kb=([[{"text":"➕ Copiar essa carteira","callback_data":"cw_addx:"+address}]] if ok else
       [[{"text":"➕ Copiar mesmo assim","callback_data":"cw_addx:"+address}]])+keyboard()
   send(chat,text,kb)
  except Exception as exc:send(chat,"⛔ Não consegui analisar: "+type(exc).__name__+": "+str(exc)[:150],keyboard())
 run_async(job)
SOL_ADDRESS=re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
def copy_wallet_action(chat,action,text):
 """Owner chooses the wallets to copy: add by address, list, remove."""
 import copy_trader as ct
 try:
  if action=="cw_add":
   ADD_WALLET[chat]=time.time()
   send(chat,"➕ ADICIONAR CARTEIRA\nMande aqui o ENDEREÇO da carteira Solana que você quer copiar (só o endereço).\n"
    "Também funciona: /copiar ENDERECO\n\nO bot copia cada compra e venda dela, no mesmo tamanho proporcional, com anti-golpe e o limite de 70% da conta.",keyboard());return
  if action=="cw_list" or action=="/carteiras":
   rows=ct.owner_wallets()
   kb=[[({"text":"▶️ Retomar "+a[:4]+"…","callback_data":"cw_on:"+a} if w.get("paused") else {"text":"⏸️ Pausar "+a[:4]+"…","callback_data":"cw_off:"+a}),
     {"text":"🗑️ Remover","callback_data":"cw_rm:"+a}] for a,w in rows[:20]]+keyboard()
   send(chat,ct.picks_text(),kb);return
  if action.startswith("cw_rm:"):
   a=action.split(":",1)[1]
   now=ct.unfollow(a);ct.mark_owner_removed(a)
   send(chat,("🗑️ Parei de copiar `"+a+"`." if now else "⏳ `"+a+"` tem uma cópia aberta: não copio mais compras dela, vendo junto quando ela vender e depois ela sai da lista."),keyboard());return
  parts=text.split()
  address=parts[1] if action=="/copiar" and len(parts)>1 else (parts[0] if parts else "")
  if not SOL_ADDRESS.fullmatch(address):
   send(chat,"Uso: /copiar ENDERECO_DA_CARTEIRA",keyboard());return
  ADD_WALLET.pop(chat,None)
  ct.add_owner_wallet(address)
  send(chat,"✅ Copiando a carteira:\n`"+address+"`\nAs próximas compras e vendas dela serão copiadas"+
   (" com dinheiro real." if ct.real_enabled() else ". ⚠️ O copy trade está DESLIGADO: toque em 💵 Copiar (REAL) para começar.")+
   "\nCompras antigas não são copiadas.",keyboard())
 except Exception as exc:send(chat,"⛔ Carteira: "+str(exc)[:200],keyboard())
def trade_loop():
 """Trading bot upkeep every minute: limit orders, DCA orders, the users' TP / SL / trailing stops."""
 import trade_panel
 import big_bot,owner_notify
 owner_notify.setup(send,active_chat)
 time.sleep(30)
 next_robot=0;next_deposit=0
 while True:
  try:trade_panel.upkeep_all(send,active_chat())
  except Exception as exc:logging.warning("trade upkeep: %s: %s",type(exc).__name__,str(exc)[:150])
  if time.time()>=next_deposit and owner_notify.ENABLED:
   next_deposit=time.time()+180
   try:owner_notify.check_deposits()
   except Exception as exc:logging.warning("deposit check: %s",type(exc).__name__)
  if trade_panel.HOME=="btc":       # the BTC robot runs in its own loop (btc_fullmargin_loop)
   time.sleep(60)
   continue
  if trade_panel.HOME=="robot":
   chats=([active_chat()] if active_chat() else [])+__import__("user_trade").users()
   try:big_bot.demo_tick(send,chats)
   except Exception as exc:logging.warning("big demo: %s",type(exc).__name__)
  if time.time()>=next_robot and (not trade_panel.AIRDROP_ONLY or trade_panel.HOME=="robot"):
   next_robot=time.time()+big_bot.CYCLE_S
   try:big_bot.cycle(send,active_chat())
   except Exception as exc:logging.warning("big robot: %s: %s",type(exc).__name__,str(exc)[:150])
  time.sleep(60)
def video_loop():
 """Owner (Nov 18): trending-topic videos to YouTube / Instagram at the scheduled hours, automatic (⏸️ Desligar stops it)."""
 import video_bot
 time.sleep(45)
 while True:
  try:video_bot.tick(send,active_chat())
  except Exception as exc:logging.warning("video tick: %s",type(exc).__name__)
  time.sleep(60)
def airdrop_loop():
 """Owner (Nov 18, later): Superteam Earn paid tasks every minute (each source read every 10 min); the airdrop
 hunter only when BOT_HOME=airdrop."""
 import airdrop_bot,superteam_bot,trade_panel,user_trade
 time.sleep(20)
 while True:
  chats=([active_chat()] if active_chat() else [])+user_trade.users()
  if trade_panel.HOME not in ("superteam","airdrop"):
   time.sleep(60)                # owner (Nov 18, latest): airdrops and Superteam removed
   continue
  if trade_panel.HOME=="superteam":
   try:superteam_bot.cycle(send,chats)
   except Exception as exc:logging.warning("superteam cycle: %s",type(exc).__name__)
   time.sleep(60)
   continue
  try:airdrop_bot.cycle(send,chats)
  except Exception as exc:logging.warning("airdrop cycle: %s",type(exc).__name__)
  time.sleep(max(10,airdrop_bot.EVERY_S))
def copy_loop():
 """Copy trading: read the followed wallets every COPY_POLL_SECONDS."""
 poll=max(3.0,float(os.getenv("COPY_POLL_SECONDS","5")))
 try:
  import copy_trader
  if copy_trader.relaunch_real_once():
   try:live_engine.start()
   except Exception:logging.exception("copy relaunch: real engine not started")
   logging.info("copy trade back on (real money)")
 except Exception:logging.exception("copy relaunch failed")
 while True:
  time.sleep(poll)
  try:
   import copy_trader
   chat=active_chat()
   if not chat:continue
   started=time.time()
   quiet=True                                       # owner (Nov 17): only the robô BAIXA's notifications
   for m in copy_trader.cycle((lambda *a,**k:None) if quiet else send,chat):
    if not quiet:send(chat,m)
   took=time.time()-started
   COPY_BEAT.update(at=time.time(),took=took)
   if took>30:logging.warning("copy cycle slow: %.0f s",took)
  except Exception as exc:
   COPY_BEAT.update(at=time.time(),error=type(exc).__name__)
   logging.warning("copy cycle failed: %s: %s",type(exc).__name__,str(exc)[:200])
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
   if not copy_trader.enabled() and not copy_trader.users_active():
    time.sleep(interval);continue              # nobody is copying: no wallet search
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
   # all qualified wallets, best first: the top ones were often already rejected (skipped below), so the
   # audit budget goes to the next fresh ones instead of finding nothing every round
   candidates=wallet_copy.best_wallets(limit=int(os.getenv("COPY_DISCOVERY_CANDIDATES","300")))
   followed={c["wallet"] for c in candidates if copy_trader.refresh_stats(c)}
   # audit BEFORE following (a few per round): only approved wallets enter the list
   rpc=dex_rpc()
   checked=0;budget=copy_trader.preaudit_budget()   # more per round while the list has free slots (max 20)
   skip={"seguida":0,"recusada":0,"filtro":0}
   for candidate in candidates:
    if checked>=budget:break
    a=candidate["wallet"]
    if a in followed:skip["seguida"]+=1;continue
    if copy_trader.is_rejected(a):skip["recusada"]+=1;continue
    if not copy_trader.prefilter(candidate):skip["filtro"]+=1;continue
    checked+=1
    try:outcome=copy_trader.preaudit_and_follow(candidate,rpc,copy_trader._sol_usd())
    except Exception as exc:
     logging.warning("pre-audit failed: %s",type(exc).__name__);continue
    if outcome in ("added","replaced"):added.append(a)
   logging.info("wallet discovery: %s candidatas, %s analisadas, %s novas | puladas: %s",len(candidates),checked,len(added),
    ", ".join(k+"="+str(v) for k,v in skip.items()))
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
  pass                                             # owner (Nov 17): copy trade removed, no wallet audit messages
  time.sleep(int(os.getenv("COPY_AUDIT_EVERY_HOURS","6"))*3600)
def run_async(fn,*args):
 interactive=getattr(CTX,"interactive",False)   # a job the owner asked for answers in full
 def run():
  CTX.interactive=interactive
  fn(*args)
 threading.Thread(target=run,daemon=True).start()
BTC_LIVE_ERR={}
def btc_live_loop():
 """RSI2 robot removed (owner, Nov 15): switched off once; only closes a position it may still hold."""
 import btc_live
 try:
  if btc_live._load()["enabled"]:btc_live.set_enabled(False)
 except Exception:logging.exception("btc live off failed")
 while True:
  if not btc_live._load()["positions"]:return
  time.sleep(btc_live.CHECK_EVERY_S)
  try:
   chat=active_chat()
   if chat:btc_live.cycle(send,chat)
  except Exception as exc:
   msg=type(exc).__name__+": "+str(exc)[:120]
   btc_live.LAST["why"]="erro: "+msg
   if msg!=BTC_LIVE_ERR.get("last") or time.time()-BTC_LIVE_ERR.get("at",0)>600:
    BTC_LIVE_ERR.update(last=msg,at=time.time());logging.warning("btc live cycle failed: %s",msg)
BFM_ERR={}
def btc_fullmargin_loop():
 """BTC Full Margin (owner, Nov 19): demo + real accounts, US$ 100/day, stop at US$ 210; checks every 20 s."""
 import btc_fullmargin as bfm
 bfm.PHOTO=send_photo
 bfm.EDIT=edit_message
 while True:
  time.sleep(bfm.CHECK_EVERY_S)
  try:
   chat=active_chat()
   if not chat:continue
   bfm.cycle_all(send,chat)
  except Exception as exc:
   msg=type(exc).__name__+": "+str(exc)[:120]
   bfm.LAST["why"]="erro: "+msg
   if msg!=BFM_ERR.get("last") or time.time()-BFM_ERR.get("at",0)>600:
    BFM_ERR.update(last=msg,at=time.time());logging.warning("btc full margin cycle failed: %s",msg)
def btc_leftover_loop():
 """BTC robot removed: only manage a position it may still hold (automatic sale), then stop."""
 import btc_dip_live
 while True:
  try:
   if not btc_dip_live._load()["buys"]:return
   chat=active_chat()
   if chat:btc_dip_live.cycle(send,chat)
  except Exception as exc:logging.warning("btc leftover cycle failed: %s",type(exc).__name__)
  time.sleep(btc_dip_live.CHECK_EVERY_S)
def loop():
 offset=None
 def notice():
  try:startup_notice()
  except Exception:logging.exception("startup notice failed")
 # off the polling thread: a slow Telegram must not delay the answer to /start after a deploy
 threading.Thread(target=notice,daemon=True).start()
 threading.Thread(target=background_loop,daemon=True).start()
 threading.Thread(target=copy_loop,daemon=True).start()
 try:
  import copy_trader
  copy_trader.start_buy_worker()        # real copy buys run one by one off the wallet-reading loop
 except Exception:logging.exception("copy buy worker failed to start")
 threading.Thread(target=audit_loop,daemon=True).start()
 threading.Thread(target=wallet_discovery_loop,daemon=True).start()
 threading.Thread(target=trade_loop,daemon=True).start()
 threading.Thread(target=airdrop_loop,daemon=True).start()
 threading.Thread(target=video_loop,daemon=True).start()       # owner (Nov 18): automatic videos, on by default     # owner (Nov 18): only the airdrop hunter       # owner (Nov 18): trading bot (limit / DCA / user exits)
 # removed BTC robots: only close a position they may still hold, then stop
 threading.Thread(target=btc_live_loop,daemon=True).start()
 threading.Thread(target=btc_leftover_loop,daemon=True).start()
 threading.Thread(target=btc_fullmargin_loop,daemon=True).start()
 threading.Thread(target=holdings_loop,daemon=True).start()
 while True:
  try:
   if TOKEN:
    data=request_api("getUpdates",offset=offset,timeout=8,allowed_updates=["message","callback_query"])
    for u in data.get("result",[]):
     offset=u["update_id"]+1
     run_async(handle_safe,u)   # each tap answers on its own: a slow one never blocks the others
   else:time.sleep(10)
  except RuntimeError as exc:
   if "HTTP 409" in str(exc):
    logging.warning("Telegram polling conflict: another instance may be polling this token");time.sleep(5)
   else:logging.warning("telegram polling failed: %s",exc);time.sleep(2)
  except Exception:logging.exception("telegram worker error");time.sleep(3)

@app.route("/exportar",methods=["GET","POST"])
def export_page():
 """Private key export was removed from the bot (for every account)."""
 return Response("<!doctype html><meta charset='utf-8'><p>Exportação de chave privada desativada.</p>",status=410,
  mimetype="text/html",headers={"Cache-Control":"no-store","X-Frame-Options":"DENY"})
@app.get("/painel")
def panel():
 """Web panel (read-only, password DASHBOARD_PASSWORD): follow the bot without Telegram."""
 import dashboard
 if len(dashboard.password())<12:
  return Response("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'><p style='font-family:sans-serif'>"
   "Painel desligado: crie a variável <b>DASHBOARD_PASSWORD</b> (12+ caracteres) no Render &gt; Environment.</p>",status=503,mimetype="text/html")
 if not dashboard.authorized(request.authorization):
  return Response("Senha do painel",status=401,headers={"WWW-Authenticate":'Basic realm="Painel do bot"',"Cache-Control":"no-store"})
 msg=dashboard.ACTIONS.get(request.args.get("ok",""),"")
 return Response(dashboard.page(msg,request.args.get("ordem","lucro")),mimetype="text/html",headers={"Cache-Control":"no-store","X-Frame-Options":"DENY",
  "X-Content-Type-Options":"nosniff","Referrer-Policy":"same-origin"})
def web_action(action,form=None):
 """On/off switches of the web panel (same effect as the Telegram buttons)."""
 import copy_trader as ct
 chat=active_chat()
 def start_engine(what):
  try:live_engine.start()
  except Exception:
   if not trade_state.pending_orders():raise
   if chat:start_real_or_wait(chat,what)
 if action=="copy_real":start_engine("copy trade");ct.set_mode(True,True)
 elif action=="copy_off":ct.set_mode(False)
 else:raise ValueError("ação desconhecida")
 note="🌐 Pelo site: "+__import__("dashboard").ACTIONS[action]
 try:
  if chat:send(chat,note)
  else:__import__("dashboard").record(note)
 except Exception:__import__("dashboard").record(note)
@app.get("/painel-sw.js")
def panel_sw():
 """Empty service worker: lets the panel show notifications on phones (no data, no caching)."""
 return Response("self.addEventListener('notificationclick',function(e){e.notification.close();e.waitUntil(clients.openWindow('/painel'));});",
  mimetype="application/javascript",headers={"Cache-Control":"no-store"})
@app.post("/painel/acao")
def panel_action():
 import dashboard
 from urllib.parse import urlparse
 if len(dashboard.password())<12 or not dashboard.authorized(request.authorization):
  return Response("Senha do painel",status=401,headers={"WWW-Authenticate":'Basic realm="Painel do bot"'})
 origin=request.headers.get("Origin") or request.headers.get("Referer") or ""
 if origin and urlparse(origin).netloc!=request.host:return Response("Origem inválida",status=403)
 if not dashboard.csrf_ok(request.form.get("csrf")):return Response("Formulário expirado: recarregue o painel",status=403)
 action=request.form.get("acao","")
 if action not in dashboard.ACTIONS:return Response("Ação inválida",status=400)
 try:web_action(action,request.form)
 except Exception as exc:
  return Response("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'><p style='font-family:sans-serif'>⛔ Não consegui: "+
   __import__("html").escape(str(exc)[:200])+"</p><p><a href='/painel'>Voltar ao painel</a></p>",status=500,mimetype="text/html")
 return Response(status=303,headers={"Location":"/painel?ok="+action})
@app.get("/videos/<name>")
def video_file(name):
 """The rendered videos, for Instagram to download (random names, deleted after 2 days)."""
 if not re.fullmatch(r"[0-9a-f]{16}\.mp4",name or ""):return Response("not found",status=404)
 import video_bot
 from flask import send_from_directory
 return send_from_directory(video_bot.folder(),name,mimetype="video/mp4")
@app.get("/")
def health():return {"service":"solana-memecoin-copytrade","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":live_engine.ready()}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
