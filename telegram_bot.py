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
from bot_wallet_service import create_or_get, get_address, export_private_key
from datetime import datetime,timezone
app=Flask(__name__)
logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
PRIVATE_CHAT=None  # First private /start after process startup; resets on restart.
CONNECTED_WALLETS={}  # Public addresses only; temporary, never seed phrases.
EXPORT_TOKENS={}
EXPORT_LOCK=threading.Lock()
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
def send(chat,text,keyboard=None):
 args={"chat_id":chat,"text":text,"disable_web_page_preview":True}
 if keyboard:args["reply_markup"]={"inline_keyboard":keyboard}
 return request_api("sendMessage",**args)
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
  [{"text":"📦 Posições abertas","callback_data":"copy_positions"}],
  [{"text":"🧠 Clonar estratégia de uma carteira","callback_data":"clone_help"},{"text":"🔍 Auditar carteiras","callback_data":"audit"}],
  [{"text":"🤖 Robô estratégia SIM","callback_data":"strat_sim"},{"text":"🤖 Robô estratégia REAL","callback_data":"strat_real"}],
  [{"text":"⏹️ Parar robô estratégia","callback_data":"strat_off"},{"text":"📊 Status robô","callback_data":"strat_status"}],
  [{"text":"₿ Breakout SIM","callback_data":"brk_sim"},{"text":"₿ Breakout REAL","callback_data":"brk_real"}],
  [{"text":"⏹️ Parar BTC","callback_data":"brk_off"},{"text":"📊 Status BTC","callback_data":"brk_status"}],
  [{"text":"⚙️ Alavancagem BTC","callback_data":"brk_lev"},{"text":"📈 Backtest BTC 1 ano","callback_data":"brk_bt"}],
  [{"text":"🧠 Melhorar estratégia BTC","callback_data":"brk_opt"}],
  [{"text":"📥 Depositar SOL","callback_data":"wallet_deposit"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}],
  [{"text":"💸 Saque manual","callback_data":"wallet_withdraw"},{"text":"🔑 Exportar carteira","callback_data":"wallet_export"}]]
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
   robot_alive_line())
 except Exception as exc:return "Status indisponível: "+type(exc).__name__
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
def handle_safe(update):
 try:handle(update)
 except Exception:logging.exception("update failed")
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
  request_api("answerCallbackQuery",callback_query_id=cb["id"])
 else:
  chat=(msg or {}).get("chat",{}).get("id");action=(msg or {}).get("text","").split(" ")[0]
 if not chat:return
 chat_type=(cb.get("message",{}).get("chat",{}).get("type") if cb else (msg or {}).get("chat",{}).get("type"))
 if chat_type!="private":return
 if not CHAT and action in ("/start","/menu"):
  try:
   if not telegram_owner.claim(chat):
    return
  except Exception:
   logging.exception("persistent owner claim failed")
   return
 if action=="/id":
  send(chat,"Seu chat ID privado: "+str(chat)+"\nIdentificação privada. O proprietário vinculado é mantido no disco persistente.");return
 if not authorized(chat):
  if cb:return  # Do not flood chat on old inline buttons; do not grant access.
  send(chat,"🔒 Acesso não autorizado nesta sessão. Envie /id para obter seu ID e configure TELEGRAM_CHAT_ID no Render. A carteira existente permanece protegida.");return
 if action in ("strat_sim","strat_real","strat_off","strat_status","/robo"):
  import clone_strategy as cs
  try:
   pre=""
   if action=="strat_sim":cs.set_mode(True,False);pre="🧪 Robô da estratégia em SIMULAÇÃO (preço real, nenhuma ordem real).\n\n"
   elif action=="strat_real":live_engine.start();cs.set_mode(True,True);pre="💵 ROBÔ DA ESTRATÉGIA COM DINHEIRO REAL LIGADO.\n⚠️ Reproduz o perfil da carteira, não garante o resultado dela.\n\n"
   elif action=="strat_off":cs.set_mode(False);pre="⏹️ Robô da estratégia desligado. Posições abertas continuam com alvo/stop.\n\n"
   send(chat,pre+cs.status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Robô estratégia: "+str(exc)[:200],keyboard())
  return
 if action in ("audit","/auditar"):
  if not os.getenv("SOLANA_RPC_URL"):send(chat,"Preciso do SOLANA_RPC_URL configurado no Render.",keyboard());return
  send(chat,"🔍 Auditando todas as carteiras copiadas (tempo que seguram cada moeda). Leva alguns minutos…")
  run_async(audit_job,chat);return
 if action.startswith("brk_"):
  import btc_breakout as bb
  try:
   if action=="brk_sim":bb.set_mode(True,False);send(chat,"🧪 BTC breakout em SIMULAÇÃO (preço real, dinheiro de mentira).\n\n"+bb.status_text(),keyboard());return
   if action=="brk_off":bb.set_mode(False);send(chat,"⏹️ BTC breakout desligado: não abre novas posições. Posição aberta continua com alvo/stop.\n\n"+bb.status_text(),keyboard());return
   if action=="brk_status":send(chat,bb.status_text(),keyboard());return
   if action=="brk_lev":
    send(chat,"⚙️ Alavancagem do BTC (máx. 20x). Quanto maior, mais rápido ganha E mais rápido perde:",
     [[{"text":str(x)+"x","callback_data":"brk_lev_"+str(x)} for x in bb.LEVERAGES],[{"text":"⬅️ Voltar","callback_data":"/menu"}]]);return
   if action.startswith("brk_lev_"):
    lev=bb.set_leverage(int(action.split("_")[-1]))
    send(chat,"⚙️ Alavancagem BTC: "+str(lev)+"x. Em "+str(lev)+"x, ~"+format(90/lev,".1f")+"% contra a posição liquida a garantia (o stop fica antes).\n\n"+bb.status_text(),keyboard());return
   if action=="brk_bt":
    send(chat,"⏳ Baixando 1 ano de velas de 15 min do BTC e simulando 2x, 3x, 5x, 10x e 20x… leva 1–2 minutos.")
    def run_bt():
     try:
      text,_=bb.backtest_report()
      for part in split_message(text):send(chat,part,keyboard())
     except Exception as exc:send(chat,"⛔ Backtest falhou: "+type(exc).__name__+": "+str(exc)[:150],keyboard())
    run_async(run_bt);return
   if action=="brk_opt":
    send(chat,"🧠 Testando "+str(len(bb.VARIANTS))+" versões da estratégia em 1 ano de velas de 15 min (escolhe em 8 meses, confere em 4). Leva 2–5 minutos…")
    def run_opt():
     try:
      text,_,_=bb.optimize()
      for part in split_message(text):send(chat,part,keyboard())
     except Exception as exc:send(chat,"⛔ Melhoria falhou: "+type(exc).__name__+": "+str(exc)[:150],keyboard())
    run_async(run_opt);return
   if action in ("brk_real","brk_real_ok"):
    d=bb._load();bt=bb.backtest_for(d["leverage"])
    if not bt:send(chat,"⛔ Rode primeiro o 📈 Backtest BTC 1 ano: ele mostra se a estratégia teria dado lucro antes de usar dinheiro real.",keyboard());return
    if action=="brk_real" and bt["final"]<=bt["start"]:
     send(chat,"⚠️ ATENÇÃO: no backtest de 1 ano em "+str(d["leverage"])+"x esta estratégia PERDEU dinheiro ("+format(bt["return_pct"],"+.0f")+"%, queda máx. "+
      format(bt["max_dd_pct"],".0f")+"%, "+str(bt["liquidations"])+" liquidações).\nLigar com dinheiro real mesmo assim?",
      [[{"text":"💵 Ligar REAL mesmo assim","callback_data":"brk_real_ok"}],[{"text":"⬅️ Cancelar","callback_data":"/menu"}]]);return
    bb.set_mode(True,True)
    send(chat,"💵 BTC BREAKOUT COM DINHEIRO REAL LIGADO — "+str(d["leverage"])+"x na Jupiter Perps, "+format(bb.ORDER_FRACTION*100,".0f")+
     "% da conta por operação. Cada ordem é simulada na blockchain antes de enviar.\n⚠️ Alavancagem pode zerar a garantia rápido.\n\n"+bb.status_text(),keyboard());return
  except Exception as exc:send(chat,"⛔ BTC: "+str(exc)[:200],keyboard());return
 if action=="clone_help":
  send(chat,"🧠 CLONAR A ESTRATÉGIA DE UMA CARTEIRA\nEnvie:\n/analisar ENDEREÇO → mostra como ela entra e sai (valor, plataforma, idade da moeda, TP, SL, tempo)\n/clonar ENDEREÇO → analisa e passa a copiar as compras dela, saindo com o TP/SL/tempo dela\n/estrategia ENDEREÇO → monta um ROBÔ PRÓPRIO com a estratégia dela (sem copiar): procura sozinho moedas com o mesmo perfil e sai com o TP/SL/tempo dela. Depois ligue em 🤖 Robô estratégia SIM ou REAL.",keyboard());return
 if action in ("/analisar","/clonar","/estrategia"):
  parts=(msg or {}).get("text","").split()
  if len(parts)!=2 or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}",parts[1]):
   send(chat,"Uso: "+action+" ENDERECO_DA_CARTEIRA\n/analisar = só mostra a estratégia · /clonar = copia as entradas dele saindo com o TP/SL dele · /estrategia = robô próprio com a estratégia dele, sem copiar.",keyboard());return
  if not os.getenv("SOLANA_RPC_URL"):
   send(chat,"Preciso do SOLANA_RPC_URL configurado no Render.",keyboard());return
  send(chat,"🧠 Analisando "+parts[1][:6]+"…"+parts[1][-4:]+": lendo até 400 transações. Leva 1–3 minutos…")
  run_async(analyze_wallet_job,chat,parts[1],{"/clonar":"clone","/estrategia":"strategy"}.get(action))
  return
 if action in ("copy_positions","/posicoes_copy"):
  import copy_trader as ct
  try:
   ct.adopt_orphans()
   for part in split_message(ct.positions_text()):send(chat,part)
   send(chat,"Menu:",keyboard())
  except Exception as exc:send(chat,"Posições indisponíveis: "+type(exc).__name__,keyboard())
  return
 if action in ("copy_following","/copiadas"):
  import copy_trader as ct
  try:
   for part in split_message(ct.wallets_text()):send(chat,part)
   send(chat,"Menu:",keyboard())
  except Exception as exc:send(chat,"Lista indisponível: "+type(exc).__name__,keyboard())
  return
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
    live_engine.start();ct.set_mode(True,True)
    pre="💵 COPY TRADE COM DINHEIRO REAL LIGADO — US$ "+format(ct.ORDER_USD,".0f")+" por cópia, anti-golpe e teste de venda antes de cada compra, ordem confirmada na blockchain. "+("Sai só quando a carteira vender (sem alvo/stop fixo). " if ct.EXIT_MODE=="wallet" else "Vende quando a carteira vender, ou no stop −"+format(ct.STOP_LOSS*100,".0f")+"% / alvo +"+format(ct.TAKE_PROFIT*100,".0f")+"%. ")+("Para de comprar se perder US$ "+format(trade_state.DAILY_LOSS_LIMIT_USD,".0f")+" no dia." if trade_state.DAILY_LOSS_LIMIT_ENABLED else "Sem limite de perda diária.")+"\n⚠️ Copiar não garante lucro: você entra depois da carteira copiada.\n\n"
   elif action=="copy_off":ct.set_mode(False);pre="⏹️ Copy trade desligado: não abre novas cópias. As posições abertas continuam sendo vendidas quando a carteira copiada vender.\n\n"
   else:pre=""
   send(chat,pre+status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Copy trade: "+str(exc)[:200],keyboard())
  return
 legacy={"btc_on","/btc_on","btc_off","/btc_off","btc_status","/btc","lev","/alavancagem","strat_trend","strat_dip","strat_dip_ok","strat_scalp","strat_scalp_ok","tf_1h","tf_5m","mode_real","mode_sim","btc_test","/btc_teste","backtest","/backtest","/btc_reset",
  "live_start","/iniciar","/live_on","live_stop","/parar","/live_off","live_on","live_status","/status_real","unstick","/destravar",
  "sniper_on","/sniper_on","sniper_off","/sniper_off","pumpfun_on","pumpfun_off","pump_on","/pump_on","pump_off","/pump_off",
  "sim_on","/sim_on","sim_off","/sim_off","/sim_reset","history","/historico","autostatus","/auto","/autostatus","/auto_on","auto_start","/auto_off","auto_stop",
  "positions","/posicoes","/simular","/remover","alerts","/alerts","/pausar","/ativar","settings","/settings","scan","/scan","latest","/latest",
  "trade_on","trade_off","trade_status","snipe_on","snipe_off","candle_on","candle_off","launch_on","launch_off",
  "tsim_on","tsim_off","tsim_status","/tsim","/tsim_reset","tb_real_on","tb_real_off","sn_sim","sn_real","sn_off","sn_status","/sniper",
  "mm_on","mm_off","mm_status","/mm",
  "launch_on","launch_off","launch_status","vol_sim","vol_real","vol_off","vol_status","/volume",
  "btcx_sim","btcx_real","btcx_off","btcx_status","btcx_lev","btcx_test","btcx_bt",
  "mev_on","mev_off","mev_status","/mev","mev_config","mev_amount","mev_min","mev_tip","mev_mode_sim","mev_mode_real","mev_scan","mev_history"}
 if action in legacy or action.startswith("lev_"):  # old leverage buttons (BTC now uses brk_lev_)
  send(chat,"Esse controle foi removido. Use o menu abaixo.",keyboard());return
 if action in ("/start","/menu","help"):menu(chat)
 elif action in ("wallet_export","/exportar_chave"):
  if not get_address():
   send(chat,"Crie uma carteira primeiro.");return
  token=secrets.token_urlsafe(32)
  with EXPORT_LOCK:
   EXPORT_TOKENS.clear()
   EXPORT_TOKENS[token]={"expires":time.monotonic()+120,"attempts":0,"chat":str(chat)}
  url="https://solana-memebot-telegram.onrender.com/exportar?token="+token
  send(chat,"🔑 Exportação privada (link válido por 2 minutos, uma única utilização). Abra somente no seu celular e informe a senha da carteira. Não encaminhe o link. Depois de importar na Phantom, confira o endereço público.",[[{"text":"🔐 Abrir exportação segura","url":url}]])
 elif action in ("wallet_create","/criar_carteira"):
  try:
   address,created=create_or_get()
   CONNECTED_WALLETS[str(chat)]=address
   send(chat,("✅ CARTEIRA SOLANA GERADA" if created else "✅ CARTEIRA SOLANA EXISTENTE")+"\nEndereço público (Solana mainnet):\n"+address+"\n\n🔐 Chave privada criptografada no disco persistente. Faça backup seguro do arquivo e da senha antes de depositar. Deposite SOL e toque em 💵 Copiar (REAL) para o robô operar.")
  except Exception:
   logging.warning("Wallet creation unavailable (check persistent disk and secrets)")
   send(chat,"⚠️ Não foi possível criar a carteira. Configure BOT_WALLET_FILE no disco persistente e BOT_WALLET_ENCRYPTION_PASSPHRASE nos segredos do Render. Nenhum endereço foi criado.")
 elif action in ("wallet_connect","/carteira","/wallet","/depositar","/deposito","/sacar","wallet_deposit","wallet_withdraw","wallet_balance","/saldo"):
  address=get_address() if os.getenv("BOT_WALLET_FILE") else None
  address=address or CONNECTED_WALLETS.get(str(chat))
  if action in ("/sacar","wallet_withdraw"):
   send(chat,"💸 SAQUE — modo seguro\nAbra a carteira dedicada no aplicativo Phantom, toque em Enviar e escolha SOL. Confirme o endereço de destino e a taxa diretamente na Phantom.\nSaques automáticos ainda estão desativados; o bot não possui autorização para movimentar fundos.")
  elif address:
   if action in ("/depositar","/deposito","wallet_deposit"):
    send(chat,"📥 DEPÓSITO SOLANA\nEnvie SOL pela rede Solana para o endereço público da carteira dedicada:\n"+address+"\nConfira o endereço na Phantom antes de transferir. A detecção do saldo é automática ao consultar /wallet.")
    return
   try:
    rpc=os.getenv("SOLANA_RPC_URL","https://api.mainnet-beta.solana.com")
    response=session.post(rpc,json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]},timeout=12)
    response.raise_for_status()
    data=response.json()
    if "error" in data:raise ValueError("RPC error")
    balance=int(data["result"]["value"])/1_000_000_000
    send(chat,"💰 SALDO REAL — CARTEIRA SOLANA\n"+address+"\nSaldo: "+fmt(balance)+" SOL\nRede: Solana mainnet\nConsulta pública em tempo real. Sem movimentação de fundos.")
   except (requests.RequestException,ValueError,KeyError,TypeError):
    send(chat,"Endereço vinculado: "+address+"\nNão foi possível consultar o saldo agora.")
  else:
   send(chat,"👻 VINCULAR PHANTOM — SOMENTE LEITURA\nNa Phantom, toque em Receber > Solana > Copiar endereço público. Depois envie: /connect ENDERECO_PUBLICO\nNunca envie as 12 palavras, seed phrase ou chave privada neste chat. A vinculação não autoriza compras ou saques.")
 elif action=="/connect":
  parts=(msg or {}).get("text","").split()
  if len(parts)!=2 or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}",parts[1]):
   send(chat,"Uso: /connect ENDERECO_PUBLICO_SOLANA\nCopie apenas o endereço público na Phantom. Não envie 12 palavras nem chave privada.")
  else:
   CONNECTED_WALLETS[str(chat)]=parts[1]
   send(chat,"✅ Endereço público vinculado para consulta: "+parts[1]+"\nUse /wallet para consultar saldo ou /desconectar para remover. Sem assinatura ou operações reais.")
 elif action=="/desconectar":
  CONNECTED_WALLETS.pop(str(chat),None)
  send(chat,"Carteira pública desvinculada desta sessão.")
 else:menu(chat)
def startup_notice():
 """After a deploy/restart: say what state the robot resumed in."""
 chat=active_chat()
 if not chat:return
 send(chat,"🔄 Bot reiniciado (deploy).\n"+status_text(),keyboard())
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
def btc_loop():
 """BTC breakout: manage the position and look for breakouts every 10 s."""
 while True:
  time.sleep(10)
  try:
   import btc_breakout
   chat=active_chat()
   if chat:btc_breakout.cycle(send,chat)
  except Exception as exc:logging.warning("btc breakout cycle failed: %s",type(exc).__name__)
def strategy_loop():
 """Autonomous bot with a wallet's measured strategy: one pass every 20 s."""
 while True:
  time.sleep(20)
  try:
   import clone_strategy
   chat=active_chat()
   if not chat or not clone_strategy.enabled():continue
   for m in clone_strategy.cycle(send,chat):send(chat,m)
  except Exception as exc:logging.warning("strategy cycle failed: %s",type(exc).__name__)
def copy_loop():
 """Copy trading: read the followed wallets every COPY_POLL_SECONDS."""
 poll=max(3.0,float(os.getenv("COPY_POLL_SECONDS","5")))
 while True:
  time.sleep(poll)
  try:
   import copy_trader
   chat=active_chat()
   if not chat:continue
   for m in copy_trader.cycle(send,chat):send(chat,m)   # also runs with copy off: sells open copies with the wallet
  except Exception as exc:logging.warning("copy cycle failed: %s",type(exc).__name__)

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
   for candidate in candidates:
    if candidate["wallet"] in followed:continue
    if copy_trader.auto_follow(candidate):added.append(candidate["wallet"])
    elif copy_trader.replace_worst(candidate):added.append(candidate["wallet"])
   removed=copy_trader.prune_low_balance()+copy_trader.remove_fast_wallets()
   chat=active_chat()
   if chat and removed:
    send(chat,"🗑️ Parei de copiar "+str(len(removed))+" carteira(s): saldo < US$ "+format(copy_trader.MIN_WALLET_USD,".0f")+
     ", compras < US$ "+format(copy_trader.MIN_BUY_USD,".0f")+", idade < "+format(copy_trader.MIN_WALLET_DAYS,".0f")+
     " dias, sem operar quase todo dia ou fechando posições em menos de 5 min. A busca coloca outras no lugar.\n"+"\n".join("solscan.io/account/"+x for x in removed))
   if added:run_async(audit_job,None,list(added))   # new wallets are copied only after the 14-day check
   if chat and added:
    total=sum(1 for w in copy_trader._load()["wallets"].values() if not w.get("paused"))
    text=("🆕 NOVA CARTEIRA PARA COPIAR" if len(added)==1 else "🆕 "+str(len(added))+" NOVAS CARTEIRAS PARA COPIAR")+\
     " — agora copiando "+str(total)+".\nSó as próximas compras serão copiadas; nenhuma posição antiga.\n\n"+\
     "\n\n".join(copy_trader.wallet_card(x) for x in added)+"\n\n🔍 Auditando os últimos "+format(copy_trader.RESULT_DAYS,".0f")+" dias de cada uma: só copio se tiver bom resultado."
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
def analyze_wallet_job(chat,address,do_clone=None):
 """Read the wallet history, report its strategy and optionally clone it (runs off the Telegram thread)."""
 try:
  import wallet_profile,dex_wallets,copy_trader
  rpc=dex_wallets._rpc_factory(os.getenv("SOLANA_RPC_URL"))
  summary,closed,text=wallet_profile.analyze(address,rpc,copy_trader._sol_usd(),get_json=dex_wallets._get_json)
  for part in split_message(text):send(chat,part)
  if do_clone=="strategy":
   if not summary.get("positions"):
    send(chat,"⛔ Não montei o robô: sem operações fechadas para medir a estratégia.",keyboard());return
   import clone_strategy
   clone_strategy.configure(address,summary)
   send(chat,"🤖 ROBÔ MONTADO com a estratégia de "+address[:6]+"…"+address[-4:]+" (sem copiar a carteira):\n\n"+clone_strategy.status_text()+
    "\n\nLigue em 🤖 Robô estratégia SIM para testar ou REAL para operar.",keyboard());return
  if do_clone:
   if not summary.get("positions"):
    send(chat,"⛔ Não clonei: sem operações fechadas para medir a estratégia.",keyboard());return
   ex=copy_trader.clone(address,summary,closed)
   hold_status,med=copy_trader.holder_status(copy_trader._load()["wallets"].get(address,{}))
   if hold_status=="fast":
    copy_trader.remove_fast_wallets()
    send(chat,"🗑️ NÃO CLONEI: "+address[:6]+"…"+address[-4:]+" fecha as posições rápido demais (mediana "+
     format(med/60,".1f")+" min). Só copio carteiras que seguram ≥ "+format(copy_trader.MIN_HOLD_SECONDS/60,".0f")+
     " min na maioria das posições.",keyboard());return
   send(chat,"🧬 CLONADO: vou copiar as próximas compras de "+address[:6]+"…"+address[-4:]+" e sair como ele sai:\n"+
    "TP +"+format(ex["tp"]*100,".0f")+"% · SL -"+format(ex["sl"]*100,".0f")+"%"+
    (" · vende em no máximo "+format(ex["max_hold_s"]/60,".0f")+" min" if ex["max_hold_s"] else "")+
    "\n"+("⚠️ COPY_EXIT_MODE=wallet: só saio quando ele vender, o TP/SL acima não é usado. " if copy_trader.EXIT_MODE=="wallet" else "Também vendo quando ele vender. ")+"Valor por cópia: US$ "+format(copy_trader.ORDER_USD,".0f")+
    ". Precisa do copy ligado (SIMULAÇÃO ou REAL).",keyboard())
 except Exception as exc:
  logging.exception("wallet analysis failed")
  send(chat,"⛔ Análise falhou: "+type(exc).__name__+": "+str(exc)[:150],keyboard())
def run_async(fn,*args):
 threading.Thread(target=fn,args=args,daemon=True).start()
def loop():
 offset=None
 try:startup_notice()
 except Exception:logging.exception("startup notice failed")
 threading.Thread(target=background_loop,daemon=True).start()
 threading.Thread(target=copy_loop,daemon=True).start()
 threading.Thread(target=strategy_loop,daemon=True).start()
 threading.Thread(target=audit_loop,daemon=True).start()
 threading.Thread(target=holdings_loop,daemon=True).start()
 threading.Thread(target=btc_loop,daemon=True).start()
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

EXPORT_HEADERS={"Cache-Control":"no-store, no-cache, must-revalidate","Pragma":"no-cache","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"}
def export_html(body):
 return Response('<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Exportar carteira</title><style>body{background:#101626;color:#fff;font:16px system-ui;margin:20px auto;padding:16px;max-width:500px}input,button,textarea{display:block;box-sizing:border-box;width:100%;margin:14px 0;padding:14px;border-radius:8px}input,textarea{background:#26314a;color:#fff;border:1px solid #65728e}button{background:#6c86ff;color:#fff;border:0}textarea{height:130px;overflow-wrap:anywhere}small{color:#e6c77b}</style><h2>🔐 Exportar carteira Solana</h2>'+body+'</html>',mimetype="text/html",headers=EXPORT_HEADERS)
@app.route("/exportar",methods=["GET","POST"])
def export_page():
 token=request.args.get("token","")
 with EXPORT_LOCK:
  state=EXPORT_TOKENS.get(token)
  if not state or time.monotonic()>state["expires"] or state["attempts"]>=3:
   EXPORT_TOKENS.pop(token,None)
   return export_html("<p>Link expirado ou inválido. Gere outro no Telegram.</p>"),403
  if request.method=="POST":
   state["attempts"]+=1
   password=request.form.get("password","")
   if len(password)>512:
    return export_html("<p>Senha inválida.</p>"),400
   try:
    address,private_key=export_private_key(password)
   except Exception:
    if state["attempts"]>=3:EXPORT_TOKENS.pop(token,None)
    return export_html("<p>Senha incorreta ou carteira indisponível. Volte e tente novamente.</p>"),403
   EXPORT_TOKENS.pop(token,None)
   page = """<p><small>Não compartilhe sua chave privada. Importe na Phantom e feche esta página.</small></p>
<p>Endereço público:</p><textarea readonly>""" + html.escape(address) + """</textarea>
<p>Chave privada (importar na Phantom):</p><textarea id="private-key" readonly>""" + html.escape(private_key) + """</textarea>
<button type="button" onclick="copyKey()">📋 Copiar chave privada</button><p id="copy-status" role="status"></p>
<script>
async function copyKey() {
 const el = document.getElementById('private-key');
 try {
  await navigator.clipboard.writeText(el.value);
  document.getElementById('copy-status').textContent = 'Chave copiada. Importe na Phantom e limpe a área de transferência depois.';
 } catch (e) {
  el.focus(); el.select();
  document.getElementById('copy-status').textContent = 'Selecione Copiar no menu do celular.';
 }
}
</script>"""
   return export_html(page)
 return export_html('<p>Link válido por 2 minutos. A senha é enviada apenas por HTTPS para descriptografar a carteira. Não abra em dispositivo compartilhado.</p><form method="post"><label>Senha da carteira</label><input type="password" name="password" required autocomplete="off" maxlength="512"><button type="submit">Mostrar minha chave privada</button></form><small>A chave nunca será enviada ao Telegram. Confirme o endereço após importar na Phantom.</small>')
@app.get("/carteira")
def wallet_page():
 return Response("""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Carteira de teste</title><style>body{background:#101626;color:#f5f7ff;font:16px system-ui;margin:0;padding:25px}main{max-width:460px;margin:auto}input,button{box-sizing:border-box;width:100%;padding:14px;margin:10px 0;border-radius:9px;border:1px solid #576179;background:#202a40;color:white}button{background:#4669ef;border:0;font-weight:bold}small{color:#b7c5da}#result{padding:12px}</style></head><body><main><h2>🔐 Carteira — teste local</h2><p>Este campo aceita texto de uma chave apenas para testar a interface. <b>Não conecta sua Phantom nem autoriza operações.</b></p><label for="secret">Chave privada de carteira de teste</label><input id="secret" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Não use a chave da carteira principal"><button id="check">Verificar campo localmente</button><button id="clear">Apagar campo</button><p id="result" role="status"></p><small>O conteúdo nunca é enviado ao servidor, nem guardado no navegador. Não insira frases de recuperação ou chaves com fundos. Trading real desativado.</small></main><script>const input=document.getElementById('secret'),result=document.getElementById('result');document.getElementById('check').onclick=()=>{result.textContent=input.value.trim()?'Campo preenchido localmente. Nenhuma chave foi importada, validada ou enviada.':'Campo vazio.'};document.getElementById('clear').onclick=()=>{input.value='';result.textContent='Campo apagado.'};</script></body></html>""",mimetype="text/html",headers={"Cache-Control":"no-store","Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff"})
@app.get("/")
def health():return {"service":"solana-memecoin-copytrade","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":live_engine.ready()}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
