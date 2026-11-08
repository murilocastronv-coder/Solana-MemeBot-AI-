"""Telegram Solana token radar with optional real-money auto trading (live_engine)."""
import os,time,logging,threading,requests,re,secrets,html
from flask import Flask,Response,request
from live_preflight import live_status, risk_settings
import live_trade_state as trade_state
import live_exit_monitor
import telegram_owner
import live_order_bridge
import live_engine
import pumpfun
import btc_perps
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
 return [[{"text":"🆕 Lançamento ON","callback_data":"launch_on"},{"text":"⏹️ Lançamento OFF","callback_data":"launch_off"}],
  [{"text":"📊 Status Lançamento","callback_data":"launch_status"}],
  [{"text":"👥 Copy SIM","callback_data":"copy_sim"},{"text":"👥 Copy REAL","callback_data":"copy_real"}],
  [{"text":"⏹️ Copy OFF","callback_data":"copy_off"},{"text":"📊 Status Copy","callback_data":"copy_status"}],
  [{"text":"📈 Volume SIM","callback_data":"vol_sim"},{"text":"📈 Volume REAL","callback_data":"vol_real"}],
  [{"text":"⏹️ Volume OFF","callback_data":"vol_off"},{"text":"📊 Status Volume","callback_data":"vol_status"}],
  [{"text":"📥 Depositar SOL","callback_data":"wallet_deposit"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}],
  [{"text":"💸 Saque manual","callback_data":"wallet_withdraw"},{"text":"🔑 Exportar carteira","callback_data":"wallet_export"}]]
def leverage_keyboard():
 return [[{"text":str(x)+"x","callback_data":"lev_"+str(x)} for x in btc_perps.LEVERAGES],[{"text":"⬅️ Voltar","callback_data":"/menu"}]]
def btc_status():
 try:price=btc_perps.fetch_price()
 except Exception:price=None
 return btc_perps.status_text(price)
def robot_alive_line():
 if not HEARTBEAT["at"]:return "⏳ Rotina automática ainda não começou"
 ago=int(time.time()-HEARTBEAT["at"])
 if ago>180:return "🚨 Rotina automática PARADA há "+str(ago//60)+" min (em: "+HEARTBEAT["step"]+"). Faça um Manual Deploy no Render."
 return "💓 Rotina automática ativa ("+str(ago)+" s atrás · "+HEARTBEAT["step"]+")"
def launch_status_text():
 try:
  import launch_sniper as ls
  st=trade_state.status();pnl=trade_state.pnl_summary()
  return ("🆕 SNIPER DE LANÇAMENTO (pump.fun) — DINHEIRO REAL\n"+ls.status_line()+"\n"+
   "Robô: "+("🟢 LIGADO" if live_engine.ready() else "🔴 PARADO — "+str(live_engine.why_stopped() or ""))+"\n"+
   "Posições abertas: "+str(st["positions"])+" · ordens pendentes: "+str(st["pending"])+"\n"+
   "Perda do dia: US$ "+format(st["daily_loss_usd"],".2f")+" / "+format(trade_state.daily_loss_limit(),".2f")+"\n"+
   "P/L hoje: "+format(pnl["day_pnl_usd"],"+.2f")+" USD · total: "+format(pnl["total_pnl_usd"],"+.2f")+" USD · operações: "+str(pnl["trades_closed"])+" ("+str(pnl["trades_won"])+" no lucro)\n"+
   robot_alive_line())
 except Exception as exc:return "Status indisponível: "+type(exc).__name__
def menu(chat):
 send(chat,launch_status_text(),keyboard())
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
# Extra established Solana tokens searched only for the MEV scan.
MEV_PAIR_SEARCHES=tuple(x.strip() for x in os.getenv("MEV_PAIR_SEARCHES","USDT,JUP,BONK,WIF,POPCAT,RAY,JTO,PYTH,TRUMP,PENGU,FARTCOIN,MEW,PNUT,GIGA,MOODENG,AI16Z,GOAT,ORCA,RENDER,W").split(",") if x.strip())
JUPITER_TOP_FEEDS=("https://api.jup.ag/tokens/v2/toptraded/1h?limit=100","https://api.jup.ag/tokens/v2/toptrending/1h?limit=100")
_TOP_CACHE={"at":0.0,"mints":[]}
SKIP_MINTS={"So11111111111111111111111111111111111111112","EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v","Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
def jupiter_top_traded():
 """Top traded/trending Solana tokens of the last hour (cached 2 min)."""
 if time.monotonic()-_TOP_CACHE["at"]<120:return _TOP_CACHE["mints"]
 mints=[]
 try:
  import jupiter_rate_limiter
  from jupiter_v2_executor import headers as jup_headers
  for url in JUPITER_TOP_FEEDS:
   try:
    r=jupiter_rate_limiter.get(url,headers=jup_headers(),timeout=12)
    if r.ok and isinstance(r.json(),list):
     mints+=[str(t.get("id") or "") for t in r.json() if isinstance(t,dict)]
   except Exception as exc:logging.warning("Jupiter top feed unavailable: %s",type(exc).__name__)
 except Exception as exc:logging.warning("Jupiter top feeds unavailable: %s",type(exc).__name__)
 mints=[m for m in dict.fromkeys(mints) if m and m not in SKIP_MINTS]
 _TOP_CACHE.update(at=time.monotonic(),mints=mints)
 return mints
_FETCH_CACHE={"at":0.0,"key":None,"rows":[]}
_FETCH_LOCK=threading.Lock()
def fetch_cached(extra_searches=(),max_age=25):
 """Share one market scan between the MEV and trading cycles."""
 with _FETCH_LOCK:
  key=tuple(extra_searches)
  if _FETCH_CACHE["key"]==key and time.monotonic()-_FETCH_CACHE["at"]<max_age:return _FETCH_CACHE["rows"]
  rows=fetch(extra_searches)
  _FETCH_CACHE.update(at=time.monotonic(),key=key,rows=rows)
  return rows
def fetch(extra_searches=()):
 # Latest/boost feeds cover surfaced launches; Pump.fun adds recent/trending launchpad tokens.
 profiles=[]
 for url in TOKEN_FEEDS:
  try:
   data=market_json(url)
   if isinstance(data,list):profiles+=data
  except RuntimeError:logging.warning("Token feed unavailable: %s",url.rsplit("/",2)[-2])
 # Most traded tokens on Jupiter right now go first (high volume, fast movers).
 profiles=[{"chainId":"solana","tokenAddress":m} for m in jupiter_top_traded()]+profiles
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
 for query in ESTABLISHED_PAIR_SEARCHES+tuple(extra_searches):
  try:
   result=market_json("https://api.dexscreener.com/latest/dex/search?q="+query)
   if isinstance(result,dict) and isinstance(result.get("pairs"),list):pairs.extend(result["pairs"])
  except RuntimeError:logging.warning("Established pair search unavailable: %s",query)
 unique={}
 pools_by_mint={}
 for p in pairs:
  if not isinstance(p,dict):continue
  mint=p.get("baseToken",{}).get("address")
  if p.get("chainId")!="solana" or not mint:continue
  liquidity=float(p.get("liquidity",{}).get("usd") or 0)
  dex_id=str(p.get("dexId") or "")
  try:price_usd=float(p.get("priceUsd") or 0)
  except (TypeError,ValueError):price_usd=0
  if dex_id and price_usd>0:
   pools_by_mint.setdefault(mint,[]).append({"dex_id":dex_id,
    "price_usd":price_usd,"liquidity_usd":liquidity})
  if mint not in unique or liquidity>float(unique[mint].get("liquidity",{}).get("usd") or 0):unique[mint]=p
 for mint,p in unique.items():
  seen_pools=set();selected=[]
  for pool in sorted(pools_by_mint.get(mint,[]),key=lambda x:x["liquidity_usd"],reverse=True):
   key=pool["dex_id"].lower()
   if key in seen_pools:continue
   seen_pools.add(key);selected.append(pool)
   if len(selected)>=6:break
  p["_mev_pools"]=selected
 return sorted(unique.values(),key=lambda p:p.get("pairCreatedAt") or 0,reverse=True)
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
 if action in ("vol_sim","vol_real","vol_off","vol_status","/volume"):
  import sniper_bot as sb
  try:
   if action=="vol_sim":sb.set_mode(True,False);pre="🧪 Sniper de volume (5–25 min) em SIMULAÇÃO (preço real, nenhuma ordem real).\n\n"
   elif action=="vol_real":
    live_engine.start();sb.set_mode(True,True)
    pre="📈 SNIPER DE VOLUME COM DINHEIRO REAL LIGADO — moedas do pump.fun com 5 a 25 min, mais compradores que vendedores, volume ≥ US$ "+format(sb.MIN_VOLUME_5M/1000,".0f")+" mil em 5 min, anti-golpe e teste de venda. US$ "+format(sb.ORDER_USD,".0f")+" por compra, máx. "+str(sb.MAX_POSITIONS)+". Stop −12%, stop móvel após +25%, sai em 30 min. Para de comprar se perder US$ "+format(trade_state.DAILY_LOSS_LIMIT_USD,".0f")+" no dia.\n⚠️ Moeda nova pode virar golpe.\n\n"
   elif action=="vol_off":sb.set_mode(False);pre="⏹️ Sniper de volume desligado. Posições abertas continuam com stop até vender.\n\n"
   else:pre=""
   send(chat,pre+sb.status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Sniper de volume: "+str(exc)[:200]+"\nNenhuma ordem foi enviada.",keyboard())
  return
 if action in ("copy_sim","copy_real","copy_off","copy_status","/copy","/seguir","/deixar"):
  import copy_trader as ct
  try:
   parts=(msg or {}).get("text","").split()
   if action=="/seguir":
    if len(parts)!=2:send(chat,"Uso: /seguir ENDERECO_DA_CARTEIRA (pegue no ranking do GMGN, Birdeye ou Cielo).",keyboard());return
    ct.follow(parts[1]);pre="✅ Seguindo "+parts[1]+". Compras antigas não são copiadas, só as próximas.\n\n"
   elif action=="/deixar":
    if len(parts)!=2:send(chat,"Uso: /deixar ENDERECO_DA_CARTEIRA",keyboard());return
    ct.unfollow(parts[1]);pre="🗑️ Parei de seguir "+parts[1]+".\n\n"
   elif action=="copy_sim":ct.set_mode(True,False);pre="🧪 Copy trade em SIMULAÇÃO (preço real, nenhuma ordem real).\n\n"
   elif action=="copy_real":
    live_engine.start();ct.set_mode(True,True)
    pre="💵 COPY TRADE COM DINHEIRO REAL LIGADO — US$ "+format(ct.ORDER_USD,".0f")+" por cópia, anti-golpe e teste de venda antes de cada compra, ordem confirmada na blockchain. Vende quando a carteira vender, ou no stop −"+format(ct.STOP_LOSS*100,".0f")+"% / alvo +"+format(ct.TAKE_PROFIT*100,".0f")+"%. Para de comprar se perder US$ "+format(trade_state.DAILY_LOSS_LIMIT_USD,".0f")+" no dia.\n⚠️ Copiar não garante lucro: você entra depois da carteira copiada.\n\n"
   elif action=="copy_off":ct.set_mode(False);pre="⏹️ Copy trade desligado. Posições abertas continuam com stop/alvo até vender.\n\n"
   else:pre=""
   send(chat,pre+ct.status_text(),keyboard())
  except Exception as exc:send(chat,"⛔ Copy trade: "+str(exc)[:200],keyboard())
  return
 if action=="launch_on":
  try:
   import launch_sniper as ls
   if not ls.ws_url():
    send(chat,"⛔ Preciso do websocket do RPC: configure SOLANA_RPC_URL (https://…) ou SOLANA_WS_URL (wss://…) no Render.",keyboard());return
   live_engine.start();trade_state.set_flag("launch_enabled",True)
   send(chat,"🆕 SNIPER DE LANÇAMENTO LIGADO — DINHEIRO REAL · RISCO EXTREMO\nOuve o pump.fun ao vivo (criação + cada compra/venda).\n1) Na criação: precisa ter site/X/Telegram e o criador não pode ter lançado várias moedas na última hora.\n2) Confirmação: acompanha a moeda por "+format(ls.CONFIRM_SECONDS/60,".0f")+" min e só compra se tiver ≥ "+str(ls.MIN_BUYERS)+" carteiras diferentes comprando, ≥ "+format(ls.MIN_SOL_IN,".0f")+" SOL entrando e ≥ "+format(ls.MIN_NET_SOL,".1f")+" SOL ficando (compras − vendas), vendas ≤ "+format(ls.MAX_SELL_RATIO*100,".0f")+"% das compras, nenhuma carteira com mais de "+format(ls.MAX_TOP_BUYER*100,".0f")+"% das compras (bundle) e o criador sem vender.\nCompra "+format(ls.LAUNCH_FRACTION*100,".0f")+"% da conta, 1 por vez.\nVende TUDO assim que der +"+format(ls.LAUNCH_TAKE_PROFIT*100,".0f")+"% de lucro já descontadas as taxas; na hora se o CRIADOR VENDER; sai cedo se cair 10% ou não subir +4% em 25 s; stop −15%; máx. 2 min.\nPara de comprar se perder US$ "+format(trade_state.DAILY_LOSS_LIMIT_USD,".0f")+" no dia.\n⚠️ Mesmo confirmada, moeda nova pode virar golpe.",keyboard())
  except Exception as exc:send(chat,"⛔ Não consegui ligar o sniper de lançamento: "+str(exc)[:250],keyboard())
  return
 if action=="launch_off":
  trade_state.set_flag("launch_enabled",False);trade_state.stop()
  send(chat,"⏹️ Sniper de lançamento desligado. Posição aberta continua com as regras de saída.",keyboard());return
 if action in ("launch_status","/status"):
  send(chat,launch_status_text(),keyboard());return
 legacy={"btc_on","/btc_on","btc_off","/btc_off","btc_status","/btc","lev","/alavancagem","strat_trend","strat_dip","strat_dip_ok","strat_scalp","strat_scalp_ok","tf_1h","tf_5m","mode_real","mode_sim","btc_test","/btc_teste","backtest","/backtest","/btc_reset",
  "live_start","/iniciar","/live_on","live_stop","/parar","/live_off","live_on","live_status","/status_real","unstick","/destravar",
  "sniper_on","/sniper_on","sniper_off","/sniper_off","pumpfun_on","pumpfun_off","pump_on","/pump_on","pump_off","/pump_off",
  "sim_on","/sim_on","sim_off","/sim_off","/sim_reset","history","/historico","autostatus","/auto","/autostatus","/auto_on","auto_start","/auto_off","auto_stop",
  "positions","/posicoes","/simular","/remover","alerts","/alerts","/pausar","/ativar","settings","/settings","scan","/scan","latest","/latest",
  "trade_on","trade_off","trade_status","snipe_on","snipe_off","candle_on","candle_off","launch_on","launch_off",
  "tsim_on","tsim_off","tsim_status","/tsim","/tsim_reset","tb_real_on","tb_real_off","sn_sim","sn_real","sn_off","sn_status","/sniper",
  "mm_on","mm_off","mm_status","/mm",
  "mev_on","mev_off","mev_status","/mev","mev_config","mev_amount","mev_min","mev_tip","mev_mode_sim","mev_mode_real","mev_scan","mev_history"}
 if action in legacy or action.startswith("lev_"):
  try:btc_perps.set_enabled(False)
  except Exception:pass
  send(chat,"Esse controle foi removido: o bot agora é o sniper de lançamento. Use o menu abaixo.",keyboard());return
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
 else:menu(chat)
def startup_notice():
 """After a deploy/restart: say what state the robot resumed in."""
 chat=active_chat()
 if not chat:return
 try:btc_perps.set_enabled(False)
 except Exception:logging.warning("legacy BTC entry stop unavailable")
 send(chat,"🔄 Bot reiniciado (deploy).\n"+launch_status_text(),keyboard())
 if not trade_state.status()["positions"] and not trade_state.status()["pending"]:return
 st=trade_state.status()
 reason=live_engine.why_stopped()
 send(chat,"🔄 Bot reiniciado (deploy). Estado salvo recuperado:\n"+
  ("🟢 Robô LIGADO e operando" if reason is None else "🔴 Robô parado: "+reason)+
  "\n📦 Posições abertas: "+str(st["positions"])+"/3 (stop e venda automática continuam)"+
  ("\n⏳ "+str(st["pending"])+" ordem(ns) pendente(s): confiro sozinho nos saldos e libero em ~2 min." if st["pending"] else ""))
HEARTBEAT={"at":0.0,"step":"iniciando"}
def beat(step):
 HEARTBEAT.update(at=time.time(),step=step)
def background_loop():
 """MEV scan/execution and position upkeep, kept off the Telegram thread so buttons answer at once."""
 next_positions=0;next_btc=0;next_reclaim=time.monotonic()+60;next_sync=0;last_recovery_notice=time.monotonic()
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
def volume_loop():
 """Volume sniper (5-25 min tokens): one pass every 20 s."""
 while True:
  time.sleep(20)
  try:
   import sniper_bot
   chat=active_chat()
   if not chat:continue
   for c in sniper_bot.exit_cycle():
    send(chat,"🧪 VOLUME SIM "+("💰 LUCRO" if c["pnl_usd"]>0 else "🔻 PREJUÍZO")+" — "+str(c["symbol"])+": "+format(c["pnl_usd"],"+.2f")+" USD ("+c["reason"]+")")
   if sniper_bot.enabled():
    for o in sniper_bot.buy_cycle(None,send,chat):
     if not o.get("real"):send(chat,"🧪 VOLUME SIM 🟢 comprou "+o["symbol"]+" · US$ "+format(o["cost_usd"],".2f")+"\n"+o["mint"])
  except Exception as exc:logging.warning("volume sniper cycle failed: %s",type(exc).__name__)
def copy_loop():
 """Copy trading: read the followed wallets every COPY_POLL_SECONDS."""
 poll=max(3.0,float(os.getenv("COPY_POLL_SECONDS","5")))
 while True:
  time.sleep(poll)
  try:
   import copy_trader
   chat=active_chat()
   if not chat or not copy_trader.enabled():continue
   for m in copy_trader.cycle(send,chat):send(chat,m)
  except Exception as exc:logging.warning("copy cycle failed: %s",type(exc).__name__)

def wallet_discovery_loop():
 """Analyze confirmed blocks and enroll qualifying wallets for future signals."""
 interval=max(20,int(os.getenv("COPY_DISCOVERY_INTERVAL_SECONDS","30")))
 if os.getenv("COPY_AUTO_DISCOVERY_ENABLED","false").strip().lower()=="false":return
 while True:
  try:
   import wallet_copy
   import copy_trader
   if not os.getenv("SOLANA_RPC_URL"):
    time.sleep(interval);continue
   wallet_copy.scan_confirmed(max_blocks=100)
   added=[]
   for candidate in wallet_copy.best_wallets(limit=50):
    if copy_trader.auto_follow(candidate):added.append(candidate["wallet"])
   chat=active_chat()
   if chat and added:
    send(chat,"🔎 Descobri " + str(len(added)) + " carteira(s) Solana com histórico lucrativo e retenção mediana ≥ 5 min. Vou observar apenas as próximas compras; nenhuma posição antiga foi copiada.\n" + "\n".join(x[:6]+"…"+x[-4:] for x in added))
  except Exception as exc:
   logging.warning("wallet discovery failed: %s",type(exc).__name__)
  time.sleep(interval)
def run_async(fn,*args):
 threading.Thread(target=fn,args=args,daemon=True).start()
def loop():
 offset=None
 try:startup_notice()
 except Exception:logging.exception("startup notice failed")
 threading.Thread(target=background_loop,daemon=True).start()
 threading.Thread(target=copy_loop,daemon=True).start()
 threading.Thread(target=volume_loop,daemon=True).start()
 threading.Thread(target=wallet_discovery_loop,daemon=True).start()
 try:
  import launch_sniper
  launch_sniper.start(send,active_chat)
 except Exception:logging.exception("launch sniper start failed")
 while True:
  try:
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
