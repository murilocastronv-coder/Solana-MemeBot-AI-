"""Telegram Solana token radar with optional real-money auto trading (live_engine)."""
import os,time,logging,threading,requests,re,secrets,html
from flask import Flask,Response,request
import paper_positions as paper
import paper_auto as auto
from live_preflight import live_status, risk_settings
import live_trade_state as trade_state
import live_exit_monitor
import telegram_owner
import live_order_bridge
import live_engine
import sim_engine
import pumpfun
import btc_perps
import trade_report
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
MEV_INPUT_PENDING={}
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
 return [[{"text":"⚡ MEV ON","callback_data":"mev_on"},{"text":"⏹️ MEV OFF","callback_data":"mev_off"}],
  [{"text":"📊 Status MEV","callback_data":"mev_status"},{"text":"⚙️ Configurar MEV","callback_data":"mev_config"}],
  [{"text":"🧪 Simulação","callback_data":"mev_mode_sim"},{"text":"💰 Modo REAL","callback_data":"mev_mode_real"}],
  [{"text":"🔎 Analisar agora","callback_data":"mev_scan"},{"text":"📜 Histórico MEV","callback_data":"mev_history"}],
  [{"text":"📥 Depositar SOL","callback_data":"wallet_deposit"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}],
  [{"text":"💸 Saque manual","callback_data":"wallet_withdraw"},{"text":"🔑 Exportar carteira","callback_data":"wallet_export"}]]
def mev_config_keyboard():
 return [[{"text":"💧 Valor SOL","callback_data":"mev_amount"},{"text":"📈 Lucro mínimo","callback_data":"mev_min"}],
  [{"text":"⚡ Gorjeta Jito","callback_data":"mev_tip"},{"text":"⬅️ Voltar","callback_data":"/menu"}]]
def mev_status_text():
 try:
  cfg=trade_state.mev_config();enabled=trade_state.flag("mev_enabled");armed=trade_state.flag("mev_real_armed");wallet=get_address()
  _,guard_reason,daily_count,daily_pnl=trade_state.mev_daily_guard()
  return ("📊 STATUS MEV — Solana/Jito\n"+"Busca automática: "+("LIGADA" if enabled else "DESLIGADA")+"\n"+
   "Execução: "+("REAL armada" if armed and os.getenv("MEV_LIVE_ENABLED")=="true" else "SIMULAÇÃO / cotação")+"\n"+
   "Permissão real no Render: "+("ATIVADA" if os.getenv("MEV_LIVE_ENABLED")=="true" else "DESATIVADA")+"\n"+
   "Carteira: "+(wallet or "não configurada")+"\n"+"Busca: 30s | até 10 moedas por ciclo\n"+"Entrada: "+format(cfg["input_lamports"]/1e9,".4f")+" SOL | lucro mínimo: "+format(cfg["min_profit_bps"]/100,".2f")+"%\n"+
   "Gorjeta Jito: "+format(cfg["jito_tip_lamports"]/1e9,".6f")+" SOL\n"+
   "Limite: 30 bundles/dia; pausa em -0,01 SOL. Hoje: "+str(daily_count)+" bundles / P&L "+format(daily_pnl/1e9,"+.8f")+" SOL\n"+
   "Proteção diária: "+guard_reason)
 except Exception as exc:return "Status MEV indisponível: "+type(exc).__name__
def mev_config_text():
 cfg=trade_state.mev_config()
 return ("⚙️ CONFIGURAÇÃO MEV\nEntrada SOL: "+format(cfg["input_lamports"]/1e9,".4f")+"\nLucro mínimo: "+format(cfg["min_profit_bps"]/100,".2f")+"%\n"+
  "Gorjeta Jito: "+format(cfg["jito_tip_lamports"]/1e9,".6f")+" SOL\nLimites: 0,001–0,1 SOL; 0,01–5%; tip 0,000001–0,00001 SOL.")
def leverage_keyboard():
 return [[{"text":str(x)+"x","callback_data":"lev_"+str(x)} for x in btc_perps.LEVERAGES],[{"text":"⬅️ Voltar","callback_data":"/menu"}]]
def btc_status():
 try:price=btc_perps.fetch_price()
 except Exception:price=None
 return btc_perps.status_text(price)
def trading_readiness():
 """Report observed switches and ledger state; never imply real trading works."""
 st=trade_state.status()
 checks={
  "Permissão Render":os.getenv("LIVE_TRADING_ENABLED")=="true",
  "Proprietário privado":bool(telegram_owner.get_owner()),
  "Jupiter API":bool(os.getenv("JUPITER_API_KEY")),
  "RPC Solana dedicado":bool(os.getenv("SOLANA_RPC_URL")) and os.getenv("SOLANA_RPC_URL")!="https://api.mainnet-beta.solana.com",
  "Carteira criptografada":bool(get_address()) and bool(os.getenv("BOT_WALLET_ENCRYPTION_PASSPHRASE")),
  "Sem ordens pendentes":st["pending"]==0,
  "Robô ligado pelo botão":st["enabled"] and not st["halted"],
 }
 running=live_engine.ready()
 return st,checks,running

def trading_status_text():
 st,checks,running=trading_readiness()
 try:sniper_on=trade_state.sniper_enabled();sniper_n=trade_state.positions_by_mode("sniper")
 except Exception:sniper_on=False;sniper_n=0
 try:pump_on=trade_state.flag("pumpfun_enabled");pump_n=trade_state.positions_by_mode("pumpfun")
 except Exception:pump_on=False;pump_n=0
 try:pnl=trade_state.pnl_summary()
 except Exception:pnl={"day_pnl_usd":0.0,"total_pnl_usd":0.0,"trades_closed":0,"trades_won":0}
 lines=["📊 STATUS DO ROBÔ — DINHEIRO REAL",
        "🟢 ROBÔ LIGADO" if running else "🔴 ROBÔ PARADO — "+(live_engine.why_stopped() or ""),
        "Chave geral Render: "+("ATIVADA" if checks["Permissão Render"] else "DESATIVADA"),
        "Compras automáticas (10% da conta): "+("SIM" if running else "NÃO"),
        "Vendas automáticas (SL -8% / stop móvel após +15%): "+("SIM" if running else "NÃO"),
        "🎯 Sniper (moedas novas, 5% da conta): "+("LIGADO" if sniper_on else "DESLIGADO")+" | posições sniper: "+str(sniper_n)+"/2",
        "🟣 Pump.fun (5% da conta): "+("LIGADO" if pump_on else "DESLIGADO")+" | posições pump.fun: "+str(pump_n)+"/2",
        "Posições registradas: "+str(st["positions"])+" / 3",
        "Ordens pendentes: "+str(st["pending"]),
        "Perda diária registrada: US$ "+format(st["daily_loss_usd"],".2f")+" / US$ "+format(trade_state.daily_loss_limit(),".2f"),
        "P/L hoje: "+format(pnl["day_pnl_usd"],"+.2f")+" USD | P/L total: "+format(pnl["total_pnl_usd"],"+.2f")+" USD",
        "Operações fechadas: "+str(pnl["trades_closed"])+" ("+str(pnl["trades_won"])+" no lucro)",
        "",
        "VERIFICAÇÕES:"]
 lines += [("✅ " if ok else "❌ ")+name for name,ok in checks.items()]
 lines += ["","⚠️ DINHEIRO REAL. Memecoins podem cair a zero; o SL pode não segurar em quedas bruscas."]
 return "\n".join(lines)

def menu(chat):
 send(chat,"⚡ Solana MEV Bot — Jupiter + Jito\n"+mev_status_text(),keyboard())
def market_json(url):
 last_error=None
 for attempt in range(3):
  try:
   response=session.get(url,timeout=20,headers={"Accept":"application/json","User-Agent":"SolanaMemeBotRadar/1.1"})
   response.raise_for_status()
   if "json" not in response.headers.get("Content-Type","").lower():
    raise ValueError("Market API returned non-JSON content")
   return response.json()
  except (requests.RequestException,ValueError) as exc:
   last_error=exc
   logging.warning("Market API attempt %s failed: %s",attempt+1,type(exc).__name__)
   if attempt<2:time.sleep(2**attempt)
 raise RuntimeError("Market API unavailable after retries") from last_error

TOKEN_FEEDS=("https://api.dexscreener.com/token-boosts/top/v1",
 "https://api.dexscreener.com/token-boosts/latest/v1",
 "https://api.dexscreener.com/token-profiles/latest/v1")
ESTABLISHED_PAIR_SEARCHES=("SOL","USDC")
def fetch():
 # Latest/boost feeds cover surfaced launches; Pump.fun adds recent/trending launchpad tokens.
 profiles=[]
 for url in TOKEN_FEEDS:
  try:
   data=market_json(url)
   if isinstance(data,list):profiles+=data
  except RuntimeError:logging.warning("Token feed unavailable: %s",url.rsplit("/",2)[-2])
 try:
  profiles.extend({"chainId":"solana","tokenAddress":t.get("id") or t.get("address")} for t in pumpfun.fetch() if isinstance(t,dict) and (t.get("id") or t.get("address")))
 except Exception as exc:logging.warning("Pump.fun discovery unavailable: %s",type(exc).__name__)
 if not profiles:raise RuntimeError("No token feeds available")
 addresses=list(dict.fromkeys(x.get("tokenAddress") for x in profiles if isinstance(x,dict) and x.get("chainId")=="solana" and x.get("tokenAddress")))[:300]
 pairs=[]
 for i in range(0,len(addresses),30):
  try:
   batch=market_json("https://api.dexscreener.com/tokens/v1/solana/"+",".join(addresses[i:i+30]))
   if isinstance(batch,list):pairs+=batch
  except RuntimeError:logging.warning("Market pair batch unavailable; continuing with remaining batches")
 if addresses and not pairs:raise RuntimeError("No market pair batches available")
 for query in ESTABLISHED_PAIR_SEARCHES:
  try:
   result=market_json("https://api.dexscreener.com/latest/dex/search?q="+query)
   if isinstance(result,dict) and isinstance(result.get("pairs"),list):pairs.extend(result["pairs"])
  except RuntimeError:logging.warning("Established pair search unavailable: %s",query)
 unique={}
 for p in pairs:
  if not isinstance(p,dict):continue
  mint=p.get("baseToken",{}).get("address")
  if p.get("chainId")!="solana" or not mint:continue
  if mint not in unique or float(p.get("liquidity",{}).get("usd") or 0)>float(unique[mint].get("liquidity",{}).get("usd") or 0):unique[mint]=p
 return sorted(unique.values(),key=lambda p:p.get("pairCreatedAt") or 0,reverse=True)
def fmt(v):
 if v == 0:return "0"
 return format(v, ".12f").rstrip("0").rstrip(".") if abs(v)<1 else format(v, ".12f").rstrip("0").rstrip(".")
def coin_keyboard(p):
 token=p.get("baseToken",{})
 mint=str(token.get("address",""))
 try:
  price=float(p.get("priceUsd") or 0)
 except (ValueError,TypeError):price=0
 if not mint:return None
 buttons=[[{"text":"📋 Copiar contrato","copy_text":{"text":mint}}]]
 if price>0:
  buttons.append([{"text":"🎯 Copiar TP","copy_text":{"text":fmt(price*(1+TP/100))}},
                  {"text":"🛑 Copiar SL","copy_text":{"text":fmt(price*(1-SL/100))}}])
 return buttons
def send_coin(chat,p,prefix=""):
 return send(chat,prefix+message(p),coin_keyboard(p))

def message(p):
 token=p.get("baseToken",{});mint=token.get("address","")
 price=float(p.get("priceUsd") or 0);liq=float(p.get("liquidity",{}).get("usd") or 0)
 tp=fmt(price*(1+TP/100)) if price>0 else "indisponível"
 sl=fmt(price*(1-SL/100)) if price>0 else "indisponível"
 return ("🪙 "+str(token.get("name","Token"))[:80]+" ("+str(token.get("symbol","?"))[:20]+")\n"+
 "📄 CONTRATO (copie a linha abaixo):\n"+mint+"\n"+
 "💰 Preço de referência: $"+fmt(price)+"\n"+
 "🎯 TP +"+str(TP)+"%: "+tp+"\n"+
 "🛑 SL -"+str(SL)+"%: "+sl+"\n"+
 "💧 Liquidez: $"+fmt(liq)+"\n"+
 "⚠️ Não auditado. Risco de scam/rug pull; TP/SL não são ordens.")
def active_chat():
 if CHAT:return CHAT
 try:return telegram_owner.get_owner()
 except Exception:
  logging.exception("persistent Telegram owner unavailable; deny access")
  return None

def qualifies(p):
 try:
  liq=float(p.get("liquidity",{}).get("usd") or 0)
  vol=float(p.get("volume",{}).get("h24") or 0)
  fdv=float(p.get("fdv") or 0)
  created=float(p.get("pairCreatedAt") or 0)
  age=(time.time()*1000-created)/3600000 if created else -1
  return liq>=MIN_LIQ and vol>=MIN_VOLUME and age>=MIN_AGE_HOURS and liq>0 and 0<fdv/liq<=MAX_FDV_LIQ and float(p.get("priceUsd") or 0)>0
 except (TypeError,ValueError,ZeroDivisionError):return False

def scan(alert=False):
 global last
 rows=fetch();last=rows
 if alert and active_chat():
  try:live_engine.buy_cycle(rows,send,active_chat())
  except Exception:logging.exception("live buy cycle failed")
  try:sim_engine.buy_cycle(rows,send,active_chat())
  except Exception:logging.exception("sim buy cycle failed")
 if alert and active_chat() and ALERTS_ENABLED:
  sent=0
  for p in reversed(rows):
   mint=p.get("baseToken",{}).get("address")
   if not mint or mint in seen or not qualifies(p):continue
   if sent>=MAX_ALERTS:break
   send_coin(active_chat(),p,"🔔 Nova oportunidade para analisar (não é recomendação):\n")
   seen.add(mint)
   sent+=1
  if len(seen)>2000:seen.clear()
 return rows
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
 pending=MEV_INPUT_PENDING.get(str(chat))
 if msg and pending:
  try:
   factor=1_000_000_000 if pending in ("input_lamports","jito_tip_lamports") else 100
   trade_state.update_mev_setting(pending,int(round(float((msg.get("text") or "").strip().replace(",","."))*factor)))
   MEV_INPUT_PENDING.pop(str(chat),None);send(chat,"✅ Configuração salva.\n"+mev_config_text(),mev_config_keyboard())
  except (ValueError,TypeError):send(chat,"Valor inválido ou fora do limite; configuração não alterada.",mev_config_keyboard())
  return
 if action in ("mev_on","mev_off"):
  enabled=action=="mev_on";trade_state.set_flag("mev_enabled",enabled)
  if not enabled:trade_state.set_flag("mev_real_armed",False)
  send(chat,"⚡ MEV "+("ligado em cotação/simulação." if enabled else "desligado; modo real desarmado."),keyboard());return
 elif action in ("mev_status","/mev"):send(chat,mev_status_text(),keyboard());return
 elif action=="mev_config":send(chat,mev_config_text(),mev_config_keyboard());return
 elif action in ("mev_amount","mev_min","mev_tip"):
  name={"mev_amount":"input_lamports","mev_min":"min_profit_bps","mev_tip":"jito_tip_lamports"}[action]
  label={"input_lamports":"entrada em SOL (0,001 a 0,1)","min_profit_bps":"lucro mínimo % (0,01 a 5)","jito_tip_lamports":"gorjeta SOL (0,000001 a 0,00001)"}[name]
  MEV_INPUT_PENDING[str(chat)]=name;send(chat,"Envie "+label+".",mev_config_keyboard());return
 elif action=="mev_mode_sim":
  trade_state.set_flag("mev_real_armed",False);send(chat,"🧪 Simulação selecionada; não envia bundles.",keyboard());return
 elif action=="mev_mode_real":
  if os.getenv("MEV_AUTOTRADE_ENABLED")!="true" or os.getenv("MEV_LIVE_ENABLED")!="true":
   send(chat,"🔒 Modo real bloqueado pelas permissões do Render. O botão não habilita transações.",keyboard());return
  trade_state.set_flag("mev_real_armed",True);send(chat,"⚠️ Modo real armado; Jito ainda exige validação de rota, lucro, saldo e segurança.",keyboard());return
 elif action=="mev_scan":
  try:
   from bot import assess_pair
   from jito_arbitrage import scan_cycle
   outcomes=scan_cycle([assess_pair(p) for p in scan(False)],force=True,quote_only=True)
   lines=["🔎 MEV — apenas cotações; nenhuma assinatura/transação"]
   lines += [x.get("status","?")+" · "+str(x.get("mint","")[:8])+" · "+format(x.get("estimated_net_lamports",0)/1e9,"+.6f")+" SOL" for x in outcomes[:10]]
   send(chat,"\n".join(lines if outcomes else ["Nenhuma oportunidade elegível; nenhuma transação enviada."]),keyboard())
  except Exception as exc:send(chat,"Análise indisponível ("+type(exc).__name__+"); nenhuma transação enviada.",keyboard())
  return
 elif action=="mev_history":
  try:
   events=trade_state.mev_history(8)
   total_lamports,closed=trade_state.mev_pnl_summary()
   lines=["📜 HISTÓRICO MEV",
          "P/L realizado confirmado: "+format(total_lamports/1e9,"+.8f")+" SOL · bundles: "+str(closed)]
   for x in events:
    line=datetime.fromtimestamp(x.get("timestamp",0),timezone.utc).strftime("%d/%m %H:%M UTC")+" · "+str(x.get("status","?"))+" · "+str(x.get("mint","")[:8])
    if isinstance(x.get("realized_net_lamports"),int):
     line+=" · P/L "+format(x["realized_net_lamports"]/1e9,"+.8f")+" SOL"
    lines.append(line)
   send(chat,"\n".join(lines if events else ["Ainda sem histórico MEV.","P/L realizado confirmado: "+format(total_lamports/1e9,"+.8f")+" SOL"]),keyboard())
  except Exception as exc:send(chat,"Histórico indisponível ("+type(exc).__name__+").",keyboard())
  return
 legacy={"btc_on","/btc_on","btc_off","/btc_off","btc_status","/btc","lev","/alavancagem","strat_trend","strat_dip","strat_dip_ok","strat_scalp","strat_scalp_ok","tf_1h","tf_5m","mode_real","mode_sim","btc_test","/btc_teste","backtest","/backtest","live_start","/iniciar","/live_on","live_stop","/parar","/live_off","live_on","sniper_on","sniper_off","pumpfun_on","pumpfun_off"}
 if action in legacy or action.startswith("lev_"):
  try:btc_perps.set_enabled(False)
  except Exception:pass
  send(chat,"Controles antigos de BTC/Perps desativados. Use o menu MEV.",keyboard());return
 if action in ("/start","/menu","help"):menu(chat)
 elif action in ("scan","/scan"):
  try:
   rows=scan(False);selected=[p for p in rows if qualifies(p)];send(chat,"🔎 "+str(len(rows))+" tokens nesta amostra; "+str(len(selected))+" passaram nos filtros. Exibindo até 5.")
   for p in selected[:5]:send_coin(chat,p)
   if not selected:send(chat,"Nenhum token passou nos filtros; não vou sugerir tokens sem dados suficientes.")
  except Exception:logging.exception("scan failed");send(chat,"Falha temporária ao consultar DexScreener.")
 elif action in ("latest","/latest"):
  for p in last[:5]:send_coin(chat,p)
  if not last:send(chat,"Nenhuma leitura ainda. Use Analisar agora.")
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
   send(chat,("✅ CARTEIRA SOLANA GERADA" if created else "✅ CARTEIRA SOLANA EXISTENTE")+"\nEndereço público (Solana mainnet):\n"+address+"\n\n🔐 Chave privada criptografada no disco persistente. Faça backup seguro do arquivo e da senha antes de depositar. Deposite SOL e use ▶️ Iniciar trading real para o robô operar.")
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
 elif action in ("live_start","/iniciar","/live_on"):
  try:
   live_engine.start()
   send(chat,"🟢 ROBÔ LIGADO — DINHEIRO REAL\nA cada varredura compra até 2 moedas em alta e aprovada no anti-golpe, usando 10% da conta (máx. 3 posições). Vende sozinho: stop -8%; a partir de +15% deixa o lucro correr e vende 10% abaixo do topo. Para ao perder 15% da conta no dia.\nUse ⏹️ Parar trading para desligar.",keyboard())
  except Exception as exc:
   send(chat,"⛔ Não foi possível ligar o robô. Motivo: "+str(exc)[:250]+"\nNenhuma ordem foi enviada.\n\n"+trading_status_text(),keyboard())
 elif action in ("btc_on","/btc_on"):
  btc_perps.set_enabled(True);lev=btc_perps.summary()["leverage"]
  st=btc_perps.summary()
  send(chat,"₿ ROBÔ BTC LIGADO — "+str(lev)+"x · "+("💵 DINHEIRO REAL" if st["mode"]=="real" else "🧪 SIMULAÇÃO com preço real")+" · gráfico "+st["timeframe"]+"\nRompimento de 20 velas a favor da média de 200 + stop por ATR e stop móvel. LONG e SHORT, 20% da conta por ordem. Decide a cada vela fechada e controla o stop a cada ~20 s.",keyboard())
 elif action in ("backtest","/backtest"):
  send(chat,"⏳ Backtest LONG DCA M5 · 07/11/2025 → 07/11/2026 com velas de 1 min (~540 mil). Na 1ª vez baixa os dados (2–5 min); depois fica salvo e é rápido.")
  def run_bt():
   try:
    import dca_backtest
    marks=set()
    def progress(f):
     step=int(f*4)
     if 0<step<4 and step not in marks:
      marks.add(step);send(chat,"⏳ Baixando dados: "+str(step*25)+"%")
    for part in dca_backtest.run_report(btc_perps._get,progress):
     for chunk in split_message(part):send(chat,chunk)
    send(chat,"Fim do backtest.",keyboard())
   except Exception as exc:
    logging.exception("backtest failed");send(chat,"❌ Backtest falhou: "+type(exc).__name__+": "+str(exc)[:200])
  threading.Thread(target=run_bt,daemon=True).start()
 elif action in ("strat_dip","strat_dip_ok","strat_scalp","strat_scalp_ok"):
  st=btc_perps.summary()
  note=""
  if st["mode"]=="real" and not st["position"]:
   btc_perps.set_option("mode","sim");note="\n🧪 Coloquei em MODO SIMULAÇÃO (operações reais não são ativadas automaticamente). Para dinheiro real, toque em 💵 Modo REAL depois de validar no backtest."
  elif st["mode"]=="real":
   note="\n⚠️ Há uma posição real aberta; o modo continua REAL até ela fechar."
  btc_perps.set_option("strategy","dip")
  send(chat,"📉 LONG DCA M5 ATIVA — só LONG, nunca short"+note+"\n• Entrada (vela de 5 min fechada): RSI 14 < 30 + queda ≥ 2% em 4 h + fechou acima da máxima da vela anterior + H1 sem tendência de baixa forte.\n• Margem $25 × 10x por compra (posição $250), até 3 compras ($750), uma de cada vez: 2ª a −2,5% da 1ª, 3ª a −3,5% da 2ª.\n• Fecha 100% quando o lucro LÍQUIDO (taxas, slippage e juros) chega a +$2,50.\n• Stop de emergência: −5% do preço médio ou perda máx. de $50 no ciclo (sempre antes da liquidação).",keyboard())
 elif action=="strat_trend":
  btc_perps.set_option("strategy","trend");send(chat,"📈 Estratégia de tendência ativa (rompimento + stop móvel).",keyboard())
 elif action in ("tf_1h","tf_5m"):
  try:
   tf=btc_perps.set_option("timeframe",action[3:])
   send(chat,"✅ Gráfico "+tf+(": tendência, poucas operações por semana, deixa o lucro correr." if tf=="1h" else ": operações rápidas o dia todo (rompimentos de 100 min a favor da tendência de ~16 h), stop 1,5×ATR e stop móvel 2,5×ATR. Pula sinais em que a taxa da Jupiter comeria mais de 1/3 do risco.\n⚠️ Com 10x, cada ida e volta custa ~1,2% da garantia em taxas."),keyboard())
  except ValueError as exc:send(chat,"⚠️ "+str(exc),keyboard())
 elif action in ("mode_real","mode_sim"):
  try:
   mode=btc_perps.set_option("mode","real" if action=="mode_real" else "sim")
   send(chat,("💵 MODO REAL ATIVO: o robô vai abrir LONG/SHORT de verdade na Jupiter Perps com a carteira do bot (garantia em USDC; troca SOL→USDC sozinho quando precisa). Mantenha ~0,05 SOL para taxas." if mode=="real" else "🧪 Modo simulação ativo: dinheiro de mentira, preço real."),keyboard())
  except ValueError as exc:send(chat,"⚠️ "+str(exc),keyboard())
 elif action in ("btc_test","/btc_teste"):
  try:btc_perps.test_real_order(send,chat)
  except Exception as exc:logging.exception("btc test failed");send(chat,"❌ Teste falhou: "+str(exc)[:300])
 elif action in ("btc_off","/btc_off"):
  btc_perps.set_enabled(False);send(chat,"₿ Robô BTC desligado. Uma posição simulada aberta fica parada até religar.",keyboard())
 elif action in ("btc_status","/btc"):
  send(chat,btc_status(),keyboard())
 elif action in ("lev","/alavancagem"):
  send(chat,"⚙️ Escolha a alavancagem (atual: "+str(btc_perps.summary()["leverage"])+"x).\nQuanto maior, maior o ganho e a perda: com 10x, ~9% contra liquida; com 20x, ~4,5%. O stop fica sempre antes da liquidação. Recomendado: 2x–3x.",leverage_keyboard())
 elif action.startswith("lev_"):
  try:
   lev=btc_perps.set_leverage(action[4:])
   pos=btc_perps.summary()["position"]
   send(chat,"✅ Alavancagem: "+str(lev)+"x. Vale para as próximas ordens"+(" (a posição aberta continua com "+str(pos["leverage"])+"x)." if pos else "."),keyboard())
  except ValueError:send(chat,"Alavancagem inválida.",leverage_keyboard())
 elif action=="/btc_reset":
  btc_perps.reset();send(chat,"₿ Simulação BTC reiniciada com $"+format(btc_perps.START_USD,".2f")+".")
 elif action in ("pump_on","/pump_on"):
  trade_state.set_flag("pumpfun_enabled",True)
  extra="" if live_engine.ready() else "\n⚠️ O robô principal está desligado: toque em ▶️ Iniciar trading real para operar com dinheiro real (a 🧪 simulação testa o pump.fun mesmo assim)."
  send(chat,"🟣 PUMP.FUN LIGADO — RISCO MUITO ALTO\nA cada ~20 s olha as moedas novas e em alta do pump.fun (lista da Jupiter). Compra só se tiver 5–90 min, liquidez ≥ US$ 15 mil, ≥ 150 donos, mint/freeze revogados, 10 maiores donos < 25%, dev < 5%, compras fortes e espalhadas sem já ter disparado, e passar no TESTE DE VENDA (≥ 92%).\nCompra 5% da conta (máx. 2 posições pump.fun).\nSaída: stop -12%; a partir de +25% deixa correr e vende 12% abaixo do topo; vende após 30 min se não decolar."+extra,keyboard())
 elif action in ("pump_off","/pump_off"):
  trade_state.set_flag("pumpfun_enabled",False)
  send(chat,"🟣 Pump.fun DESLIGADO. Posições abertas continuam com stop e venda automática.",keyboard())
 elif action in ("history","/historico"):
  try:send(chat,trade_report.btc_history_text(btc_perps.summary()),keyboard())
  except Exception as exc:logging.exception("history failed");send(chat,"⚠️ Histórico indisponível: "+str(exc)[:150])
 elif action in ("sim_on","/sim_on"):
  sim_engine.set_enabled(True);st=sim_engine.summary()
  send(chat,"🧪 SIMULAÇÃO LIGADA\nRoda as MESMAS estratégias (normal + sniper) com preços reais da Jupiter e dinheiro de mentira (saldo simulado: US$ "+format(st["equity"],".2f")+"). Inclui 1% de custo por lado. Não mexe na carteira.\nVeja os resultados em 📜 Histórico. /sim_reset recomeça do zero.",keyboard())
 elif action in ("sim_off","/sim_off"):
  sim_engine.set_enabled(False);send(chat,"🧪 Simulação desligada. O histórico simulado fica salvo.",keyboard())
 elif action=="/sim_reset":
  sim_engine.reset();send(chat,"🧪 Simulação reiniciada com US$ "+format(sim_engine.START_USD,".2f")+".")
 elif action in ("unstick","/destravar"):
  try:live_engine.unstick(send,chat)
  except Exception as exc:
   logging.exception("unstick failed");send(chat,"❌ Falha ao destravar: "+type(exc).__name__+": "+str(exc)[:200])
 elif action in ("sniper_on","/sniper_on"):
  try:
   trade_state.set_sniper(True)
   trade_state.set_flag("pumpfun_enabled",True)  # sniper also trades pump.fun tokens
   extra="" if live_engine.ready() else "\n⚠️ O robô principal está desligado: toque em ▶️ Iniciar trading real para o sniper operar."
   send(chat,"🎯 SNIPER LIGADO (DINHEIRO REAL) — inclui moedas do 🟣 PUMP.FUN — RISCO MUITO ALTO\nA cada ~20 s procura moedas de 5 a 60 min, liquidez ≥ US$ 15 mil, compras fortes e espalhadas (sem já ter disparado). Só compra se passar no anti-golpe, se os 10 maiores donos tiverem < 25% e no TESTE DE VENDA (comprar e vender de volta tem que devolver ≥ 92%).\nCompra 5% da conta (máx. 2 posições sniper).\nSaída: stop -12%; a partir de +25% deixa o lucro correr e vende 12% abaixo do topo; se não decolar em 30 min, vende.\n⚠️ A maioria das moedas novas vai a zero. Quedas rápidas podem passar muito do stop."+extra,keyboard())
  except Exception:
   logging.exception("sniper switch failed");send(chat,"⚠️ Não consegui salvar a configuração do sniper.")
 elif action in ("sniper_off","/sniper_off"):
  try:
   trade_state.set_sniper(False)
   trade_state.set_flag("pumpfun_enabled",False)
   send(chat,"🎯 Sniper DESLIGADO (inclusive pump.fun). Posições abertas continuam com stop e venda automática.",keyboard())
  except Exception:
   logging.exception("sniper switch failed");send(chat,"⚠️ Não consegui salvar a configuração do sniper.")
 elif action in ("live_stop","/parar","/live_off"):
  auto.set_enabled(chat,False)
  try:
   trade_state.stop()
   send(chat,"⏹️ Novas operações bloqueadas no estado persistente. Posições eventualmente abertas precisam continuar monitoradas até liquidação.")
  except Exception:
   logging.exception("persistent trading stop failed")
   send(chat,"⚠️ Falha ao salvar parada no disco. Trading real permanece bloqueado no executor; verifique o Render.")
 elif action in ("live_status","/status_real"):
  send(chat,trading_status_text(),keyboard())
 elif action in ("autostatus","/auto","/autostatus"):
  st=auto.status(chat)
  send(chat,"🤖 TRADING AUTOMÁTICO — SIMULAÇÃO\nStatus: "+("ATIVADO" if st["enabled"] else "DESATIVADO")+"\nCompra: US$ 10 | TP: +15% | SL: -8%\nSaldo fictício livre: $"+fmt(st["balance"])+"\nPosições automáticas: "+str(st["open"])+" / 3\nResultado realizado: $"+fmt(st["realized"])+"\nPerdas realizadas hoje: $"+fmt(st["daily_loss"])+" / $3\n/auto_on para iniciar | /auto_off para parar novas entradas e monitoramento.\n⚠️ Sem carteira real. Dados podem atrasar e não há verificação on-chain de scam. Estado temporário: reinícios podem zerar a simulação.")
 elif action in ("/auto_on","auto_start"):
  auto.set_enabled(chat,True);send(chat,"🧪 Trading SIMULADO ATIVADO. Compras fictícias de $10, até 3 posições, TP +15%, SL -8%. Não há ordens reais.")
 elif action in ("/auto_off","auto_stop"):
  auto.set_enabled(chat,False);send(chat,"⏸️ Trading simulado pausado. Nenhuma ordem real existe. Posições fictícias não serão monitoradas até /auto_on.")
 elif action in ("positions","/posicoes"):
  items=paper.list_open(chat)
  if not items:send(chat,"Nenhuma simulação aberta. Use /simular CONTRATO PRECO_ENTRADA VALOR_USD")
  else:
   for mint,p in items:send(chat,"📊 SIMULAÇÃO\nContrato: "+mint+"\nEntrada: $"+fmt(p["entry"])+"\nTP: $"+fmt(p["entry"]*(1+p["tp"]/100))+"\nSL: $"+fmt(p["entry"]*(1-p["sl"]/100))+"\n/remover "+mint)
 elif action=="/simular":
  parts=(msg or {}).get("text","").split()
  if len(parts)!=4:send(chat,"Uso: /simular CONTRATO PRECO_ENTRADA VALOR_USD\nExemplo: /simular <contrato> 0.000012 2")
  else:
   try:
    mint=parts[1];entry=float(parts[2]);usd=float(parts[3])
    paper.add(chat,mint,entry,usd,TP,SL)
    send(chat,"✅ Simulação registrada (nenhuma compra real).\nContrato: "+mint+"\nTP: $"+fmt(entry*(1+TP/100))+"\nSL: $"+fmt(entry*(1-SL/100))+"\nAvisarei quando o preço observado atingir um alvo.")
   except (ValueError,OverflowError) as exc:send(chat,"Erro: "+str(exc))
 elif action=="/remover":
  parts=(msg or {}).get("text","").split()
  if len(parts)!=2:send(chat,"Uso: /remover CONTRATO")
  else:send(chat,"Simulação removida." if paper.remove(chat,parts[1]) else "Contrato não encontrado.")
 elif action in ("alerts","/alerts"):
  send(chat,"🔔 Alertas automáticos: "+("ATIVADOS" if ALERTS_ENABLED else "PAUSADOS")+"\nVarredura a cada "+str(INTERVAL)+" segundos enquanto o serviço estiver ativo.\nUse /pausar ou /ativar.")
 elif action=="/pausar":
  ALERTS_ENABLED=False;send(chat,"🔕 Alertas pausados. Use /ativar para retomar.")
 elif action=="/ativar":
  ALERTS_ENABLED=True;send(chat,"🔔 Alertas automáticos ativados.")
 elif action in ("settings","/settings"):
  send(chat,"⚙️ TP: +"+str(TP)+"% | SL: -"+str(SL)+"%\nLiquidez mínima: $"+str(MIN_LIQ)+"\nVolume 24h mínimo: $"+str(MIN_VOLUME)+"\nIdade mínima: "+str(MIN_AGE_HOURS)+" horas\nFDV/liquidez máximo: "+str(MAX_FDV_LIQ)+"\nAlertas por varredura: "+str(MAX_ALERTS)+"\nSem auditoria on-chain. Ajuste no Render.")
 else:menu(chat)
def startup_notice():
 """After a deploy/restart: say what state the robot resumed in."""
 chat=active_chat()
 if not chat:return
 try:btc_perps.set_enabled(False)
 except Exception:logging.warning("legacy BTC entry stop unavailable")
 send(chat,"🔄 Bot MEV reiniciado (deploy).\n"+mev_status_text(),keyboard())
 if not trade_state.status()["positions"] and not trade_state.status()["pending"]:return
 st=trade_state.status()
 reason=live_engine.why_stopped()
 send(chat,"🔄 Bot reiniciado (deploy). Estado salvo recuperado:\n"+
  ("🟢 Robô LIGADO e operando" if reason is None else "🔴 Robô parado: "+reason)+
  "\n📦 Posições abertas: "+str(st["positions"])+"/3 (stop e venda automática continuam)"+
  ("\n⏳ "+str(st["pending"])+" ordem(ns) pendente(s): confiro sozinho nos saldos e libero em ~2 min." if st["pending"] else ""))
def loop():
 offset=None;next_scan=0;next_positions=0;next_sniper=0;next_btc=0;next_mev_reconcile=0;last_recovery_notice=time.monotonic()
 try:startup_notice()
 except Exception:logging.exception("startup notice failed")
 while True:
  try:
   if active_chat() and time.monotonic()>=next_mev_reconcile:
    next_mev_reconcile=time.monotonic()+20
    try:
     from jito_arbitrage import reconcile_pending_bundle
     reconcile_pending_bundle()
     for settled in trade_state.mev_notifications_pending(3):
      try:
       if settled["status"]=="landed":
        pnl=settled.get("realized_net_lamports")
        pnl_text=("P/L realizado: "+format(pnl/1e9,"+.8f")+" SOL" if isinstance(pnl,int) else "P/L ainda indisponível no RPC")
        message="✅ Arbitragem Jito confirmada na Solana.\nContrato: "+str(settled.get("mint",""))+"\n"+pnl_text
        if settled.get("signature"):message+="\nAssinatura: "+str(settled["signature"])
       else:
        message="⚠️ Bundle Jito não aterrissou; ciclo atômico não executado.\nID: "+str(settled.get("bundle_id",""))
       send(active_chat(),message,keyboard())
       trade_state.mark_mev_notification_sent(settled.get("bundle_id"))
      except Exception as exc:
       logging.warning("MEV Telegram notification deferred type=%s",type(exc).__name__)
    except Exception as exc:logging.warning("MEV bundle reconciliation failed type=%s",type(exc).__name__)
   if active_chat() and os.getenv("MEV_AUTOTRADE_ENABLED")=="true" and trade_state.flag("mev_enabled") and time.monotonic()>=next_scan:
    next_scan=time.monotonic()+max(30,INTERVAL)
    try:
     from bot import assess_pair
     from jito_arbitrage import scan_cycle
     assessments=[assess_pair(pair) for pair in scan(False)]
     outcomes=scan_cycle(assessments)
     for outcome in outcomes:
      if outcome.get("status")=="submitted_pending":
       send(active_chat(),"⚡ Bundle Jito enviado; aguardando confirmação. ID: "+outcome["bundle_id"]+"\nContrato: "+outcome["mint"]+"\nLucro mínimo estimado em SOL: "+format(outcome["estimated_net_lamports"]/1e9,".6f"))
    except Exception as exc:
     logging.error("MEV cycle stopped: %s",type(exc).__name__)
   if active_chat() and time.monotonic()>=next_btc:
    next_btc=time.monotonic()+10
    try:btc_perps.cycle(send,active_chat())
    except Exception:logging.exception("btc cycle failed")
   if active_chat() and time.monotonic()-last_recovery_notice>3600:
    last_recovery_notice=time.monotonic()
    try:
     pending=trade_state.pending_orders()
     if pending:
      send(active_chat(),"⏳ "+str(len(pending))+" operação(ões) pendente(s). Estou conferindo sozinho os saldos na blockchain; o robô volta a operar assim que confirmar.")
    except Exception:logging.exception("recovery pending inspection failed")
   if active_chat() and time.monotonic()>=next_positions:
    next_positions=time.monotonic()+20

    try:live_engine.recover(send,active_chat())
    except Exception:logging.exception("pending order recovery failed")
    try:
     if os.getenv("LIVE_TRADING_ENABLED")=="true":live_engine.exit_cycle(send,active_chat())
     else:live_exit_monitor.monitor(send,active_chat())
    except Exception:logging.exception("live exit cycle failed")
   if TOKEN:
    data=request_api("getUpdates",offset=offset,timeout=8,allowed_updates=["message","callback_query"])
    for u in data.get("result",[]):
     offset=u["update_id"]+1
     try:handle(u)
     except Exception:logging.exception("update failed")
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
def health():return {"service":"solana-memebot-telegram","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":live_engine.ready()}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
