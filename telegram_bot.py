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
MEV_INPUT_PENDING={}
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
   "Execução: "+("REAL armada" if armed and trade_state.mev_live_allowed() else "SIMULAÇÃO / cotação")+"\n"+
   "Permissão real no Render: "+("ATIVADA" if trade_state.mev_live_allowed() else "DESATIVADA")+"\n"+
   "Carteira: "+(wallet or "não configurada")+"\n"+__import__("jito_arbitrage").coverage_text()+"\n"+"Busca contínua: nova rodada a cada "+format(MEV_SCAN_SECONDS,".0f")+" s | "+str(__import__("jito_arbitrage")._int_env("MEV_TOKENS_PER_CYCLE",3,1,30))+" moedas por rodada (as de maior diferença de preço primeiro)\n"+"Entrada: "+format(cfg["input_lamports"]/1e9,".4f")+" SOL | lucro mínimo: "+format(cfg["min_profit_bps"]/100,".2f")+"%\n"+
   "Gorjeta Jito: "+format(cfg["jito_tip_lamports"]/1e9,".6f")+" SOL\n"+
   "Limite: 30 bundles/dia; pausa em -0,01 SOL. Hoje: "+str(daily_count)+" bundles / P&L "+format(daily_pnl/1e9,"+.8f")+" SOL\n"+
   "Proteção diária: "+guard_reason+"\n"+robot_alive_line())
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
def robot_alive_line():
 if not HEARTBEAT["at"]:return "⏳ Rotina automática ainda não começou"
 ago=int(time.time()-HEARTBEAT["at"])
 if ago>180:return "🚨 Rotina automática PARADA há "+str(ago//60)+" min (em: "+HEARTBEAT["step"]+"). Faça um Manual Deploy no Render."
 return "💓 Rotina automática ativa ("+str(ago)+" s atrás · "+HEARTBEAT["step"]+")"
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
  if not trade_state.mev_live_allowed():
   send(chat,"🔒 Modo real bloqueado pelas permissões do Render. O botão não habilita transações.",keyboard());return
  trade_state.set_flag("mev_real_armed",True);send(chat,"⚠️ Modo real armado; Jito ainda exige validação de rota, lucro, saldo e segurança.",keyboard());return
 elif action=="mev_scan":
  send(chat,"🔎 Analisando moedas… o resultado chega em instantes.");run_async(mev_scan_now,chat);return
 elif action=="mev_history":
  try:
   events=trade_state.mev_history(8)
   total_lamports,closed=trade_state.mev_pnl_summary()
   lines=["📜 HISTÓRICO MEV",
          "P/L realizado confirmado: "+format(total_lamports/1e9,"+.8f")+" SOL · bundles: "+str(closed)]
   for x in events:
    line=datetime.fromtimestamp(x.get("timestamp",0),timezone.utc).strftime("%d/%m %H:%M UTC")+" · "+str(x.get("status","?"))+" · "+str(x.get("mint","")[:8])
    if x.get("error"):line+=" · "+str(x["error"])[:60]
    elif isinstance(x.get("estimated_net_lamports"),int):line+=" · "+format(x["estimated_net_lamports"]/1e9,"+.6f")+" SOL"
    if isinstance(x.get("realized_net_lamports"),int):
     line+=" · P/L "+format(x["realized_net_lamports"]/1e9,"+.8f")+" SOL"
    lines.append(line)
   send(chat,"\n".join(lines if events else ["Ainda sem histórico MEV.","P/L realizado confirmado: "+format(total_lamports/1e9,"+.8f")+" SOL"]),keyboard())
  except Exception as exc:send(chat,"Histórico indisponível ("+type(exc).__name__+").",keyboard())
  return
 legacy={"btc_on","/btc_on","btc_off","/btc_off","btc_status","/btc","lev","/alavancagem","strat_trend","strat_dip","strat_dip_ok","strat_scalp","strat_scalp_ok","tf_1h","tf_5m","mode_real","mode_sim","btc_test","/btc_teste","backtest","/backtest","/btc_reset",
  "live_start","/iniciar","/live_on","live_stop","/parar","/live_off","live_on","live_status","/status_real","unstick","/destravar",
  "sniper_on","/sniper_on","sniper_off","/sniper_off","pumpfun_on","pumpfun_off","pump_on","/pump_on","pump_off","/pump_off",
  "sim_on","/sim_on","sim_off","/sim_off","/sim_reset","history","/historico","autostatus","/auto","/autostatus","/auto_on","auto_start","/auto_off","auto_stop",
  "positions","/posicoes","/simular","/remover","alerts","/alerts","/pausar","/ativar","settings","/settings","scan","/scan","latest","/latest",
  "trade_on","trade_off","trade_status","snipe_on","snipe_off","candle_on","candle_off","launch_on","launch_off"}
 if action in legacy or action.startswith("lev_"):
  try:btc_perps.set_enabled(False)
  except Exception:pass
  send(chat,"Esse controle foi removido: o bot agora opera só MEV. Use o menu abaixo.",keyboard());return
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
 try:applied=trade_state.apply_mev_only_once()
 except Exception:logging.exception("mev-only switch failed");applied=False
 chat=active_chat()
 if not chat:return
 try:btc_perps.set_enabled(False)
 except Exception:logging.warning("legacy BTC entry stop unavailable")
 if applied:
  send(chat,"✅ Modo SÓ MEV aplicado: Trading, Sniper, Pump.fun, Vela 1m e Lançamento foram DESLIGADOS. Posições abertas (se houver) continuam com stop até vender. O MEV segue ligado com entrada de 0,05 SOL e gorjeta máxima.")
 send(chat,"🔄 Bot MEV reiniciado (deploy).\n"+mev_status_text(),keyboard())
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
 next_scan=0;next_positions=0;next_btc=0;next_mev_reconcile=0;next_reclaim=time.monotonic()+60;next_trade=0;next_sync=0;next_snipe=0;last_recovery_notice=time.monotonic()
 while True:
  try:
   beat("reconciliação MEV")
   if active_chat() and time.monotonic()>=next_mev_reconcile:
    next_mev_reconcile=time.monotonic()+5
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
   beat("conferência de posições")
   if active_chat() and time.monotonic()>=next_sync:
    next_sync=time.monotonic()+60
    try:live_engine.sync_positions(send,active_chat())
    except Exception:logging.exception("position sync failed")
   beat("devolução de depósito")
   if active_chat() and time.monotonic()>=next_reclaim:
    next_reclaim=time.monotonic()+600
    try:
     import jito_arbitrage as ja, rent_reclaim
     if ja._live_enabled() and not ja._has_unresolved_bundle():
      closed,lamports,sig=rent_reclaim.reclaim(ja._rpc,ja._wallet())
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
def mev_result_line(x):
 net=x.get("estimated_net_lamports")
 net_text=(" · "+format(net/1e9,"+.6f")+" SOL") if isinstance(net,int) else ""
 mint=str(x.get("mint",""))[:8]
 status=x.get("status","?")
 if status=="submitted_pending":return "⚡ ORDEM ENVIADA · "+mint+net_text
 if status=="live_failed":return "❌ LUCRO ACHADO MAS NÃO ENVIOU · "+mint+net_text+" · "+str(x.get("error",""))
 if status=="dry_run_opportunity":return "✅ oportunidade · "+mint+net_text
 if status=="skipped":return "⏭️ pulada · "+mint+" · "+str(x.get("error",""))
 return status+" · "+mint+net_text
def mev_scan_now(chat):
 """Quote now; in armed real mode with MEV ON, a profitable cycle is executed right away."""
 try:
  from bot import assess_pair
  import jito_arbitrage as ja
  live=ja._live_enabled() and trade_state.flag("mev_enabled")
  header="🔎 MEV — "+("modo REAL: oportunidade com lucro é executada" if live else "apenas cotações; nenhuma transação")
  outcomes=ja.scan_cycle([assess_pair(p) for p in fetch(MEV_PAIR_SEARCHES)],force=True,quote_only=not live)
  if not outcomes and ja.LAST_RESULTS["results"]:
   # The automatic scan holds the lock; show its latest results instead of waiting.
   outcomes=ja.LAST_RESULTS["results"]
   header+="\n(última análise automática, "+datetime.fromtimestamp(ja.LAST_RESULTS["at"],timezone.utc).strftime("%H:%M:%S UTC")+")"
  if not outcomes:
   send(chat,"⏳ Primeira análise ainda rodando. Tente de novo em 1 minuto.",keyboard());return
  try:header+="\n"+ja.coverage_text()
  except Exception:pass
  send(chat,"\n".join([header]+[mev_result_line(x) for x in outcomes[:12]]),keyboard())
 except Exception as exc:send(chat,"Análise indisponível ("+type(exc).__name__+"); nenhuma transação enviada.",keyboard())
MEV_SCAN_SECONDS=max(1.0,float(os.getenv("MEV_SCAN_SECONDS","2")))
MEV_LAST={"at":0.0}
def mev_loop():
 """MEV scan in its own thread: next round starts MEV_SCAN_SECONDS after the previous one ends."""
 while True:
  try:
   if active_chat() and trade_state.mev_autotrade_allowed() and trade_state.flag("mev_enabled"):
    from bot import assess_pair
    from jito_arbitrage import scan_cycle
    assessments=[assess_pair(pair) for pair in fetch_cached(MEV_PAIR_SEARCHES)]
    outcomes=scan_cycle(assessments)
    MEV_LAST["at"]=time.time()
    for outcome in outcomes:
     if outcome.get("status")=="live_failed":
      reason=str(outcome.get("error",""))
      if time.monotonic()-LIVE_FAIL_NOTICES.get(reason,-1e9)>600:
       LIVE_FAIL_NOTICES[reason]=time.monotonic()
       send(active_chat(),"❌ Achei oportunidade com lucro mas a ordem NÃO foi enviada.\nMotivo: "+reason+"\nContrato: "+str(outcome.get("mint","")))
     if outcome.get("status")=="submitted_pending":
      send(active_chat(),"⚡ Bundle Jito enviado; aguardando confirmação. ID: "+outcome["bundle_id"]+"\nContrato: "+outcome["mint"]+"\nLucro mínimo estimado em SOL: "+format(outcome["estimated_net_lamports"]/1e9,".6f"))
  except Exception as exc:logging.error("MEV cycle stopped: %s",type(exc).__name__)
  time.sleep(MEV_SCAN_SECONDS)
def run_async(fn,*args):
 threading.Thread(target=fn,args=args,daemon=True).start()
def loop():
 offset=None
 try:startup_notice()
 except Exception:logging.exception("startup notice failed")
 threading.Thread(target=background_loop,daemon=True).start()
 threading.Thread(target=mev_loop,daemon=True).start()
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
