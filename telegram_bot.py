"""Telegram Solana token radar. Read-only, no wallet credentials or trades."""
import os,time,logging,threading,requests
from flask import Flask
import paper_positions as paper
from datetime import datetime,timezone
app=Flask(__name__)
logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
PRIVATE_CHAT=None  # First private /start after process startup; resets on restart.
INTERVAL=max(60,int(os.getenv("SCAN_INTERVAL_SECONDS","300")))
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
 r=session.post(BASE+"/"+method,json=params,timeout=25);r.raise_for_status()
 return r.json()
def send(chat,text,keyboard=None):
 args={"chat_id":chat,"text":text,"disable_web_page_preview":True}
 if keyboard:args["reply_markup"]={"inline_keyboard":keyboard}
 return request_api("sendMessage",**args)
def keyboard():
 return [[{"text":"🔎 Analisar agora","callback_data":"scan"},{"text":"🪙 Últimas moedas","callback_data":"latest"}],[{"text":"📊 Simulações","callback_data":"positions"},{"text":"🔔 Alertas","callback_data":"alerts"}]]
def menu(chat):
 send(chat,"🤖 Solana MemeBot AI\nRadar de tokens, contratos para copiar e TP/SL indicativos. Nenhuma compra é executada.",keyboard())
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

def fetch():
 profiles=market_json("https://api.dexscreener.com/token-profiles/latest/v1")
 if not isinstance(profiles,list):raise ValueError("Invalid token profile list")
 addresses=list(dict.fromkeys(x.get("tokenAddress") for x in profiles if isinstance(x,dict) and x.get("chainId")=="solana" and x.get("tokenAddress")))[:90]
 pairs=[]
 for i in range(0,len(addresses),30):
  try:
   batch=market_json("https://api.dexscreener.com/tokens/v1/solana/"+",".join(addresses[i:i+30]))
   if isinstance(batch,list):pairs+=batch
  except RuntimeError:logging.warning("Market pair batch unavailable; continuing with remaining batches")
 if addresses and not pairs:raise RuntimeError("No market pair batches available")
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
 return CHAT or PRIVATE_CHAT

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
 if not CHAT and PRIVATE_CHAT is None and action in ("/start","/menu"):
  PRIVATE_CHAT=str(chat)
  logging.info("Private Telegram chat registered in memory; will reset on restart")
 if action=="/id":
  send(chat,"Seu chat ID privado: "+str(chat)+"\nNão é necessário configurar Chat ID para o modo temporário.");return
 if not authorized(chat):
  send(chat,"Este bot já está vinculado a outra conversa privada nesta execução. Se você é o proprietário, configure TELEGRAM_CHAT_ID no Render. Nenhuma carteira está conectada.");return
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
 elif action in ("positions","/posicoes"):
  items=paper.list_open(chat)
  if not items:send(chat,"Nenhuma simulação aberta. Use /simular CONTRATO PRECO_ENTRADA VALOR_USD")
  else:
   for mint,p in items:send(chat,"📊 SIMULAÇÃO\\nContrato: "+mint+"\\nEntrada: $"+fmt(p["entry"])+"\\nTP: $"+fmt(p["entry"]*(1+p["tp"]/100))+"\\nSL: $"+fmt(p["entry"]*(1-p["sl"]/100))+"\\n/remover "+mint)
 elif action=="/simular":
  parts=(msg or {}).get("text","").split()
  if len(parts)!=4:send(chat,"Uso: /simular CONTRATO PRECO_ENTRADA VALOR_USD\\nExemplo: /simular <contrato> 0.000012 2")
  else:
   try:
    mint=parts[1];entry=float(parts[2]);usd=float(parts[3])
    paper.add(chat,mint,entry,usd,TP,SL)
    send(chat,"✅ Simulação registrada (nenhuma compra real).\\nContrato: "+mint+"\\nTP: $"+fmt(entry*(1+TP/100))+"\\nSL: $"+fmt(entry*(1-SL/100))+"\\nAvisarei quando o preço observado atingir um alvo.")
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
def loop():
 offset=None;next_scan=0;next_positions=0
 while True:
  try:
   if time.monotonic()>=next_scan:
    next_scan=time.monotonic()+INTERVAL
    try:scan(True)
    except Exception:logging.exception("automatic opportunity scan failed")
   if active_chat() and time.monotonic()>=next_positions:
    next_positions=time.monotonic()+60
    try:paper.check(active_chat(),send,fmt)
    except Exception:logging.exception("paper position monitoring failed")
   if TOKEN:
    data=request_api("getUpdates",offset=offset,timeout=15,allowed_updates=["message","callback_query"])
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
@app.get("/")
def health():return {"service":"solana-memebot-telegram","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":False}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
