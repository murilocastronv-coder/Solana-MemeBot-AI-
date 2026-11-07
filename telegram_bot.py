"""Telegram Solana token radar. Read-only, no wallet credentials or trades."""
import os,time,logging,threading,requests
from flask import Flask
from datetime import datetime,timezone
app=Flask(__name__)
logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
PRIVATE_CHAT=None  # First private /start after process startup; resets on restart.
INTERVAL=max(60,int(os.getenv("SCAN_INTERVAL_SECONDS","300")))
TP=float(os.getenv("TP_PERCENT","15"))
SL=float(os.getenv("SL_PERCENT","8"))
MIN_LIQ=float(os.getenv("MIN_LIQUIDITY_USD","25000"))
BASE="https://api.telegram.org/bot"+TOKEN
session=requests.Session()
seen=set()
last=[]
def request_api(method,**params):
 if not TOKEN:return {}
 r=session.post(BASE+"/"+method,json=params,timeout=25);r.raise_for_status()
 return r.json()
def send(chat,text,keyboard=None):
 args={"chat_id":chat,"text":text,"disable_web_page_preview":True}
 if keyboard:args["reply_markup"]={"inline_keyboard":keyboard}
 return request_api("sendMessage",**args)
def keyboard():
 return [[{"text":"🔎 Analisar agora","callback_data":"scan"},{"text":"🪙 Últimas moedas","callback_data":"latest"}],[{"text":"⚙️ TP / SL","callback_data":"settings"},{"text":"📋 Ajuda","callback_data":"help"}]]
def menu(chat):
 send(chat,"🤖 Solana MemeBot AI\nRadar de tokens, contratos para copiar e TP/SL indicativos. Nenhuma compra é executada.",keyboard())
def fetch():
 profiles=session.get("https://api.dexscreener.com/token-profiles/latest/v1",timeout=20).json()
 addresses=list(dict.fromkeys(x.get("tokenAddress") for x in profiles if x.get("chainId")=="solana" and x.get("tokenAddress")))[:90]
 pairs=[]
 for i in range(0,len(addresses),30):
  r=session.get("https://api.dexscreener.com/tokens/v1/solana/"+",".join(addresses[i:i+30]),timeout=20)
  r.raise_for_status();pairs+=r.json()
 unique={}
 for p in pairs:
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

def scan(alert=False):
 global last
 rows=fetch();last=rows
 if alert and active_chat():
  for p in reversed(rows):
   mint=p.get("baseToken",{}).get("address")
   if mint in seen:continue
   seen.add(mint)
   if float(p.get("liquidity",{}).get("usd") or 0)>=MIN_LIQ:
    send_coin(active_chat(),p,"🔔 Token detectado (não é recomendação):\n")
  if len(seen)>2000:seen.clear()
 return rows
def authorized(chat):
 return bool(active_chat()) and str(chat)==str(active_chat())
def handle(update):
 global PRIVATE_CHAT
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
   rows=scan(False);send(chat,"🔎 "+str(len(rows))+" tokens nesta amostra. Exibindo até 5 recém-detectados.")
   for p in rows[:5]:send_coin(chat,p)
  except Exception:logging.exception("scan failed");send(chat,"Falha temporária ao consultar DexScreener.")
 elif action in ("latest","/latest"):
  for p in last[:5]:send_coin(chat,p)
  if not last:send(chat,"Nenhuma leitura ainda. Use Analisar agora.")
 elif action in ("settings","/settings"):
  send(chat,"⚙️ TP: +"+str(TP)+"% | SL: -"+str(SL)+"%\nAlertas: liquidez mínima $"+str(MIN_LIQ)+"\nValores indicativos, não ordens. Ajuste TP_PERCENT e SL_PERCENT no Render.")
 else:menu(chat)
def loop():
 offset=None;next_scan=0
 while True:
  try:
   if time.monotonic()>=next_scan:
    scan(True);next_scan=time.monotonic()+INTERVAL
   if TOKEN:
    data=request_api("getUpdates",offset=offset,timeout=15,allowed_updates=["message","callback_query"])
    for u in data.get("result",[]):
     offset=u["update_id"]+1
     try:handle(u)
     except Exception:logging.exception("update failed")
   else:time.sleep(10)
  except Exception:logging.exception("telegram worker error");time.sleep(10)
@app.get("/")
def health():return {"service":"solana-memebot-telegram","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":False}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
