"""Telegram Solana token radar. Read-only, no wallet credentials or trades."""
import os,time,logging,threading,requests,re,secrets,html
from flask import Flask,Response,request
import paper_positions as paper
import paper_auto as auto
from live_preflight import live_status, risk_settings
import live_trade_state as trade_state
import live_exit_monitor
import telegram_owner
import live_order_bridge
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
 return [[{"text":"🔎 Analisar agora","callback_data":"scan"},{"text":"🪙 Últimas moedas","callback_data":"latest"}],[{"text":"▶️ Iniciar trading real","callback_data":"live_start"},{"text":"⏹️ Parar trading","callback_data":"live_stop"}],[{"text":"📊 Status trading real","callback_data":"live_status"}],[{"text":"➕ Criar carteira","callback_data":"wallet_create"},{"text":"🔐 Conectar carteira","callback_data":"wallet_connect"}],[{"text":"📥 Depositar","callback_data":"wallet_deposit"},{"text":"🔑 Exportar chave","callback_data":"wallet_export"}],[{"text":"💸 Sacar","callback_data":"wallet_withdraw"},{"text":"💰 Ver saldo","callback_data":"wallet_balance"}]]
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
  "Compras automáticas integradas":False,
  "Vendas TP/SL automáticas integradas":False,
  "Recuperação e PnL validados":False,
 }
 running=False  # Never claim active until a working autonomous engine is implemented.
 return st,checks,running

def trading_status_text():
 st,checks,running=trading_readiness()
 lines=["📊 STATUS DO ROBÔ — DINHEIRO REAL",
        "🔴 ROBÔ DESLIGADO" if not running else "🟢 ROBÔ LIGADO",
        "Chave geral Render: "+("ATIVADA" if checks["Permissão Render"] else "DESATIVADA"),
        "Compras automáticas: NÃO",
        "Vendas automáticas TP/SL: NÃO",
        "Posições registradas: "+str(st["positions"])+" / 3",
        "Ordens pendentes: "+str(st["pending"]),
        "Perda diária registrada: US$ "+format(st["daily_loss_usd"],".2f")+" / US$ 3.00",
        "",
        "VERIFICAÇÕES:"]
 lines += [("✅ " if ok else "❌ ")+name for name,ok in checks.items()]
 lines += ["","⚠️ Chave geral ligada não significa trading em execução.",
           "Nenhuma compra é iniciada pelo botão enquanto o ciclo real não estiver concluído."]
 return "\n".join(lines)

def menu(chat):
 send(chat,"🤖 Solana MemeBot AI\n"+trading_status_text(),keyboard())
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
   send(chat,("✅ CARTEIRA SOLANA GERADA" if created else "✅ CARTEIRA SOLANA EXISTENTE")+"\nEndereço público (Solana mainnet):\n"+address+"\n\n🔐 Chave privada criptografada no disco persistente. Faça backup seguro do arquivo e da senha antes de depositar. Saques e trades reais não estão implementados.")
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
  # Never acknowledge live activation without an operational and reconciled
  # trading engine. The bridge alone is not an autonomous trading loop.
  try:
   live_order_bridge.preflight()
   st=trade_state.status()
   if st["pending"]:
    raise RuntimeError("Há transação pendente de reconciliação")
   send(chat,"⛔ Não foi possível ligar o robô.\n"+trading_status_text(),keyboard())
  except Exception as exc:
   send(chat,"⛔ Não foi possível iniciar trading real. Motivo: "+str(exc)[:250]+"\nNenhuma ordem foi enviada. Use /id para verificar o proprietário e configure as credenciais somente nos segredos do Render.")
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
def loop():
 offset=None;next_scan=0;next_positions=0;last_recovery_notice=0
 while True:
  try:
   if time.monotonic()>=next_scan:
    next_scan=time.monotonic()+INTERVAL
    try:scan(True)
    except Exception:logging.exception("automatic opportunity scan failed")
   if active_chat() and time.monotonic()-last_recovery_notice>3600:
    last_recovery_notice=time.monotonic()
    try:
     pending=trade_state.pending_orders()
     if pending:
      send(active_chat(),"⚠️ "+str(len(pending))+" operação(ões) pendente(s) após reinício ou falha. Trading bloqueado para evitar duplicidade. Verifique assinaturas e saldos on-chain antes de liberar novas ordens.")
    except Exception:logging.exception("recovery pending inspection failed")
   if active_chat() and time.monotonic()>=next_positions:
    next_positions=time.monotonic()+60

    try:live_exit_monitor.monitor(send,active_chat())
    except Exception:logging.exception("live exit quote monitoring failed")
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
def health():return {"service":"solana-memebot-telegram","configured":bool(TOKEN),"private_chat_registered":bool(active_chat()),"trading":False}
if __name__=="__main__":
 if TOKEN:threading.Thread(target=loop,daemon=True).start()
 else:logging.warning("Missing TELEGRAM_BOT_TOKEN; worker not started")
 app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
