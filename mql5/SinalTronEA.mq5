//+------------------------------------------------------------------+
//| SinalTronEA.mq5  -  ROBO que opera os sinais do indicador        |
//|                     SinalTron (precisa dele em MQL5/Indicators)  |
//|                                                                  |
//| ENTRADA: seta de COMPRA/VENDA do SinalTron no candle que fechou  |
//|   (so em tendencia, nunca em range - igual ao indicador).        |
//| SAIDA: stop e alvo do ajuste que o indicador escolheu (em ATR),  |
//|   spread incluido. Ao chegar no ALVO ele NAO fecha: trava 80% do |
//|   alvo e deixa o lucro CORRER com STOP MOVEL (maximo de lucro).  |
//|   Com metade do alvo o stop vai para a entrada (sem perda).      |
//| LOTE: arrisca InpRiskPct % do saldo por ordem (pelo stop);       |
//|   o lote cresce junto com o saldo (juros compostos).             |
//| META: para quando o saldo chega em InpTargetUSD (ex.: 50 -> 200) |
//| LIMITE: para se perder InpMaxLossPct % do saldo inicial.         |
//|                                                                  |
//| AVISO: NAO existe garantia de chegar na meta. Teste no Strategy  |
//| Tester e na conta DEMO antes de usar dinheiro de verdade.        |
//+------------------------------------------------------------------+
#property copyright "SinalTron"
#property version   "1.01"
#property description "Robo do SinalTron: entra nas setas do indicador (so em tendencia), stop/alvo do ajuste do indicador, trava lucro e deixa correr com stop movel; arrisca X% por ordem; para na meta (ex.: 50 -> 200)."

#include <Trade\Trade.mqh>

input double InpTargetUSD   = 200.0;  // META: para de operar quando o saldo chegar nisto (0 = sem meta)
input double InpMaxLossPct  = 50.0;   // LIMITE: para se perder isto (%) do saldo inicial (0 = sem limite)
input double InpRiskPct     = 5.0;    // RISCO por ordem em % do saldo (maior = cresce mais rapido E perde mais rapido)
input double InpMaxLot      = 50.0;   // Lote maximo por ordem
input bool   InpLetRun      = true;   // MAXIMO LUCRO: no alvo trava parte do lucro e deixa correr com stop movel
input double InpLockPct     = 80.0;   // Ao chegar no alvo, trava X% do alvo como lucro
input double InpTrailPct    = 50.0;   // Stop movel a X% do tamanho do alvo atras do preco
input double InpBEPct       = 50.0;   // Com X% do alvo, o stop vai para a entrada (sem perda)
input int    InpMaxSpread   = 30;     // Spread maximo para entrar (pontos)
input int    InpMinScore    = 5;      // (indicador) forca minima, se o ajuste automatico estiver desligado
input bool   InpAutoTune    = true;   // (indicador) ajuste automatico de 70% de acerto
input double InpMinWin      = 70.0;   // (indicador) acerto minimo do ajuste automatico
input double InpADXMin      = 25.0;   // (indicador) ADX minimo para ser tendencia
input bool   InpPush        = true;   // Avisar no celular quando o robo abre e fecha
input int    InpTuneEvery   = 60;     // (indicador) refaz o ajuste a cada X candles (mais rapido no backtest)
input ulong  InpMagic       = 20261121;

CTrade   trade;
int      hSig = INVALID_HANDLE, hATR = INVALID_HANDLE;
datetime gLastBar = 0;
double   gStart = 0, gEntry = 0, gTPDist = 0, gSLDist = 0;
int      gDir = 0;
bool     gLocked = false;
bool     gStopped = false;
string   gWhy = "", gLast = "-";
int      gWins = 0, gLosses = 0;
int      gSeen = 0, gSkipSpread = 0, gReadFail = 0;
datetime gLastSigBar = 0;

void Panel();   // definida no fim do arquivo

//+------------------------------------------------------------------+
int OnInit()
  {
   //--- o indicador SinalTron com o celular e o alerta DESLIGADOS (quem avisa e o robo)
   hSig = iCustom(_Symbol, _Period, "SinalTron",
                  InpMinScore, 1.0, 1.5, 30, 3000, false, false, true, InpAutoTune, 2.0, InpMinWin, 3, true, InpADXMin, 0.5, InpTuneEvery);
   hATR = iATR(_Symbol, _Period, 14);
   if(hSig == INVALID_HANDLE || hATR == INVALID_HANDLE)
     {
      Alert("SinalTron EA: coloque o indicador SinalTron (compilado) na pasta MQL5/Indicators");
      return(INIT_FAILED);
     }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);
   trade.SetTypeFillingBySymbol(_Symbol);
   gStart = AccountInfoDouble(ACCOUNT_EQUITY);
   EventSetTimer(1);
   Print("SinalTron EA ligado: saldo ", DoubleToString(gStart, 2), " meta ", DoubleToString(InpTargetUSD, 2));
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   IndicatorRelease(hSig);
   IndicatorRelease(hATR);
   ObjectsDeleteAll(0, "STE_");
  }

//+------------------------------------------------------------------+
bool MyPosition(ulong &ticket)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionSelectByTicket(t) && PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic) { ticket = t; return(true); }
     }
   return(false);
  }

void Notify(const string msg)
  {
   Print(msg);
   if(InpPush) SendNotification(msg);
  }

void Stop(const string why)
  {
   ulong t;
   if(MyPosition(t)) trade.PositionClose(t);
   gStopped = true;
   gWhy = why;
   Notify("SinalTron EA: " + why + " - saldo " + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
  }

//--- lote que perde InpRiskPct % do saldo se bater o stop
double RiskLot(double slDist)
  {
   double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE), ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), mx = MathMin(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX), InpMaxLot);
   double st = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(st <= 0) st = 0.01;
   if(tv <= 0 || ts <= 0 || slDist <= 0) return(mn);
   double money = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPct / 100.0;
   double lot = MathFloor(money / (slDist / ts * tv) / st) * st;
   //--- nao passa da margem livre
   double m1 = 0;
   if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, 1.0, SymbolInfoDouble(_Symbol, SYMBOL_ASK), m1) && m1 > 0)
      lot = MathMin(lot, MathFloor(AccountInfoDouble(ACCOUNT_MARGIN_FREE) * 0.9 / m1 / st) * st);
   if(lot < mn) lot = mn;
   if(lot > mx) lot = mx;
   return(NormalizeDouble(lot, 2));
  }

//+------------------------------------------------------------------+
//| Entrada: seta do indicador no candle que acabou de fechar        |
//+------------------------------------------------------------------+
void TryEntry()
  {
   //--- olha os 2 ultimos candles fechados (o indicador pode atualizar um tick depois); nao repete o mesmo sinal
   double b[2], s[2], tpm[1], slm[1], atr[1];
   datetime tm[2];
   if(CopyBuffer(hSig, 0, 1, 2, b) != 2 || CopyBuffer(hSig, 1, 1, 2, s) != 2 || CopyTime(_Symbol, _Period, 1, 2, tm) != 2)
     {
      gReadFail++;
      if(gReadFail % 50 == 1) Print("SinalTron EA: indicador ainda sem dados (", GetLastError(), ") - esta compilado em MQL5/Indicators?");
      return;
     }
   if(CopyBuffer(hSig, 2, 0, 1, tpm) != 1 || CopyBuffer(hSig, 3, 0, 1, slm) != 1) return;
   if(CopyBuffer(hATR, 0, 1, 1, atr) != 1 || atr[0] <= 0) return;
   int dir = 0;
   datetime sigBar = 0;
   for(int k = 1; k >= 0; k--)                     // indice 1 = candle mais recente (arrays na ordem do tempo)
     {
      if(tm[k] <= gLastSigBar) continue;
      if(b[k] != EMPTY_VALUE && b[k] != 0) { dir = 1; sigBar = tm[k]; break; }
      if(s[k] != EMPTY_VALUE && s[k] != 0) { dir = -1; sigBar = tm[k]; break; }
     }
   if(dir == 0) return;
   gLastSigBar = sigBar;
   gSeen++;
   Print("SinalTron EA: sinal de ", (dir == 1 ? "COMPRA" : "VENDA"), " no candle ", TimeToString(sigBar));
   if(SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) > InpMaxSpread)
     {
      gSkipSpread++;
      Print("SinalTron EA: sinal ignorado - spread ", SymbolInfoInteger(_Symbol, SYMBOL_SPREAD), " > maximo ", InpMaxSpread);
      return;
     }
   double tpA = (tpm[0] > 0 && tpm[0] != EMPTY_VALUE) ? tpm[0] : 1.0;
   double slA = (slm[0] > 0 && slm[0] != EMPTY_VALUE) ? slm[0] : 1.5;
   double sp = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) * _Point;
   double stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   double tpDist = MathMax(tpA * atr[0] + sp, stops + _Point);
   double slDist = MathMax(slA * atr[0], stops + sp + _Point);
   double lot = RiskLot(slDist);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK), bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   bool ok;
   //--- com "deixar correr" o alvo fica so no robo (o stop movel decide a saida); sem ele, alvo na corretora
   if(dir == 1)
      ok = trade.Buy(lot, _Symbol, ask, NormalizeDouble(ask - slDist, _Digits), InpLetRun ? 0 : NormalizeDouble(ask + tpDist, _Digits), "SinalTron compra");
   else
      ok = trade.Sell(lot, _Symbol, bid, NormalizeDouble(bid + slDist, _Digits), InpLetRun ? 0 : NormalizeDouble(bid - tpDist, _Digits), "SinalTron venda");
   if(!ok || trade.ResultRetcode() != TRADE_RETCODE_DONE)
     {
      Print("SinalTron EA: ordem recusada - ", trade.ResultRetcodeDescription());
      return;
     }
   gDir = dir; gEntry = trade.ResultPrice() > 0 ? trade.ResultPrice() : (dir == 1 ? ask : bid);
   gTPDist = tpDist; gSLDist = slDist; gLocked = false;
   gLast = (dir == 1 ? "COMPRA " : "VENDA ") + DoubleToString(lot, 2) + " lote " + TimeToString(TimeCurrent(), TIME_MINUTES);
   Notify("SinalTron EA " + (dir == 1 ? "COMPROU " : "VENDEU ") + _Symbol + " lote " + DoubleToString(lot, 2) +
          " | entrada " + DoubleToString(gEntry, _Digits) + " | stop " + DoubleToString(gEntry - dir * slDist, _Digits) +
          " | alvo " + DoubleToString(gEntry + dir * tpDist, _Digits) + (InpLetRun ? " (depois deixa correr)" : ""));
  }

//+------------------------------------------------------------------+
//| Saida: stop na entrada, trava no alvo e stop movel               |
//+------------------------------------------------------------------+
void Manage(ulong t)
  {
   if(!PositionSelectByTicket(t)) return;
   int dir = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
   double open = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
   if(gTPDist <= 0) { gTPDist = MathAbs(open - sl) / 1.5; gEntry = open; gDir = dir; }        // depois de reiniciar o MT5
   double price = (dir == 1) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double prof = (price - open) * dir;
   double sp = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) * _Point;
   double stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   double ns = sl;
   if(prof >= InpBEPct / 100.0 * gTPDist) ns = (dir == 1) ? MathMax(ns, open + sp) : MathMin(ns, open - sp);
   if(InpLetRun && prof >= gTPDist)
     {
      double lock = open + dir * InpLockPct / 100.0 * gTPDist;
      double trail = price - dir * InpTrailPct / 100.0 * gTPDist;
      ns = (dir == 1) ? MathMax(ns, MathMax(lock, trail)) : MathMin(ns, MathMin(lock, trail));
      if(!gLocked) { gLocked = true; Print("SinalTron EA: alvo atingido - lucro travado, deixando correr"); }
     }
   ns = (dir == 1) ? MathMin(ns, price - stops - _Point) : MathMax(ns, price + stops + _Point);
   ns = NormalizeDouble(ns, _Digits);
   if((dir == 1 && ns > sl + _Point) || (dir == -1 && (sl == 0 || ns < sl - _Point)))
      trade.PositionModify(t, ns, tp);
  }

//+------------------------------------------------------------------+
void Cycle()
  {
   if(gStopped) return;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(InpTargetUSD > 0 && eq >= InpTargetUSD) { Stop("META BATIDA"); return; }
   if(InpMaxLossPct > 0 && eq <= gStart * (1.0 - InpMaxLossPct / 100.0)) { Stop("LIMITE DE PERDA"); return; }
   ulong t;
   if(MyPosition(t)) { Manage(t); return; }
   TryEntry();                                       // le os 2 ultimos candles fechados; nao repete o mesmo sinal
  }

void OnTick()  { Cycle(); }
void OnTimer() { Cycle(); Panel(); }

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &req, const MqlTradeResult &res)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0 || !HistoryDealSelect(trans.deal)) return;
   if((ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagic) return;
   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) return;
   double p = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) + HistoryDealGetDouble(trans.deal, DEAL_SWAP) +
              HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   if(p >= 0) gWins++; else gLosses++;
   gTPDist = 0;
   Notify("SinalTron EA fechou: " + (p >= 0 ? "LUCRO +" : "PREJUIZO ") + DoubleToString(p, 2) + " | saldo " +
          DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
  }

//+------------------------------------------------------------------+
//| Painel                                                           |
//+------------------------------------------------------------------+
void L(const string n, int y, const string text, color c, int size = 10, const string font = "Consolas")
  {
   if(ObjectFind(0, n) < 0)
     {
      ObjectCreate(0, n, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, n, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, n, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, n, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, n, OBJPROP_XDISTANCE, 24);
     }
   ObjectSetInteger(0, n, OBJPROP_YDISTANCE, y);
   ObjectSetString(0, n, OBJPROP_FONT, font);
   ObjectSetInteger(0, n, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, n, OBJPROP_COLOR, c);
   ObjectSetString(0, n, OBJPROP_TEXT, text == "" ? " " : text);
  }

void Panel()
  {
   if(ObjectFind(0, "STE_BG") < 0)
     {
      ObjectCreate(0, "STE_BG", OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, "STE_BG", OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, "STE_BG", OBJPROP_XDISTANCE, 12);
      ObjectSetInteger(0, "STE_BG", OBJPROP_YDISTANCE, 20);
      ObjectSetInteger(0, "STE_BG", OBJPROP_XSIZE, 360);
      ObjectSetInteger(0, "STE_BG", OBJPROP_YSIZE, 205);
      ObjectSetInteger(0, "STE_BG", OBJPROP_BGCOLOR, C'14,16,26');
      ObjectSetInteger(0, "STE_BG", OBJPROP_COLOR, C'255,196,0');
      ObjectSetInteger(0, "STE_BG", OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, "STE_BG", OBJPROP_SELECTABLE, false);
     }
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   double prog = (InpTargetUSD > gStart) ? MathMax(0, MathMin(1, (eq - gStart) / (InpTargetUSD - gStart))) : 0;
   bool algo = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   L("STE_0", 28, "SINALTRON ROBO", C'255,196,0', 14, "Arial Black");
   L("STE_1", 56, gStopped ? "PARADO: " + gWhy : (algo ? "OPERANDO os sinais do SinalTron" : "ligue o Algo Trading"),
     gStopped ? (gWhy == "META BATIDA" ? clrLime : C'255,80,90') : (algo ? clrLime : C'255,80,90'), 10, "Arial Bold");
   L("STE_2", 82, "Saldo: " + DoubleToString(eq, 2) + "  (comecou " + DoubleToString(gStart, 2) + ")", C'210,214,230');
   L("STE_3", 102, "Meta: " + DoubleToString(InpTargetUSD, 2) + "  -  " + DoubleToString(prog * 100, 0) + "% do caminho", C'255,196,0');
   int tot = gWins + gLosses;
   L("STE_4", 122, "Ganhos " + IntegerToString(gWins) + " / Perdas " + IntegerToString(gLosses) +
     (tot > 0 ? "  -  acerto " + DoubleToString(100.0 * gWins / tot, 0) + "%" : ""), C'210,214,230');
   L("STE_5", 142, "Risco por ordem: " + DoubleToString(InpRiskPct, 1) + "% do saldo" + (InpLetRun ? "  -  deixa o lucro correr" : ""), C'120,126,150', 9);
   L("STE_6", 162, "Ultima: " + gLast, C'120,126,150', 9);
   L("STE_8", 200, "Sinais vistos " + IntegerToString(gSeen) + " - ignorados por spread " + IntegerToString(gSkipSpread),
     C'120,126,150', 8);
   L("STE_7", 182, gLocked ? "Lucro TRAVADO - stop movel seguindo o preco" : " ", clrLime, 9);
   ChartRedraw(0);
  }
//+------------------------------------------------------------------+
