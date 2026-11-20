//+------------------------------------------------------------------+
//| FullMargin.mq5  -  robo "Full Margin" (scalper de sessao curta)  |
//|                                                                  |
//| Voce liga quando quiser operar. Ele guarda o capital da hora em  |
//| que foi ligado e opera scalp no par do grafico ate:              |
//|  - bater a META DO DIA (saldo 300) -> fecha e para ate 00:00;    |
//|  - ou bater o LIMITE DE PERDA     -> fecha tudo e para.          |
//| Para operar de novo (ex.: no dia seguinte) e so ligar de novo.   |
//|                                                                  |
//| Estrategia v4 (M1, 1 ordem por vez, DIA INTEIRO):               |
//|  - LE O GRAFICO M1 e da uma nota de -9 a +9: tendencia M5+M15,   |
//|    ultimos 10 candles, topos/fundos, EMA 20, padroes de candle   |
//|    (engolfo, martelo, estrela cadente) e candle de forca;        |
//|  - ao LIGAR ja abre para o lado da leitura;                      |
//|  - depois: nota >= +4 compra, <= -4 vende (InpMinScore);         |
//|  - stop atras dos ultimos 5 candles; ALVO = 1,5x o risco;        |
//|  - stop na entrada com +0,6 risco e stop movel com +1 risco;     |
//|  - 2 perdas seguidas = pausa de 15 min.                          |
//| v4.21 (dono): PRECO MEDIO - a cada 10 pips contra abre mais uma  |
//|  ordem (ate 5), 100% da margem dividida entre elas, sem stop por |
//|  ordem; FECHA TUDO com +5 pips (sobre o preco medio); abre ordens|
//|  o tempo todo (fechou, ja abre a proxima pela leitura).          |
//| Lote: FULL MARGIN - usa InpMarginUse % (padrao 100%) da margem   |
//| livre = lote maximo; a cada ganho o capital sobe e o proximo     |
//| lote fica maior (juros compostos) ate bater a meta.              |
//|                                                                  |
//| AVISO: margem alta = risco alto. Poucos pips contra podem levar  |
//| grande parte do capital. NAO existe garantia de acerto nem de    |
//| meta diaria. Teste no Strategy Tester e na conta DEMO.           |
//+------------------------------------------------------------------+
#property copyright "Full Margin"
#property version   "4.21"
#property description "Full Margin - le os candles do M1 e opera o dia inteiro; abre ordem assim que liga; full margin; para na META DO DIA (saldo 300) ou no limite de perda e volta depois da meia-noite. Alto risco."

#include <Trade\Trade.mqh>

enum ENUM_SIDE { SIDE_BOTH = 0, SIDE_BUY = 1, SIDE_SELL = 2 };
input ENUM_SIDE InpSide      = SIDE_SELL; // Lado: 0 = compra e venda, 1 = SO COMPRA, 2 = SO VENDA
input bool   InpEnterNow     = true;   // Abrir a 1a ordem assim que ligar
input double InpTargetUSD    = 300.0;  // META DO DIA: saldo em dinheiro; ao chegar para ate a meia-noite (0 = usar o multiplicador)
input bool   InpDaily        = true;   // Todo dia depois da meia-noite (servidor) recomeca e tenta a meta de novo
input double InpTargetMult   = 3.0;    // META por multiplicador (so se a meta em dinheiro for 0): 3 = 50 -> 150
input double InpMaxLossPct   = 50.0;   // LIMITE DE PERDA da sessao em % (para tudo)
input double InpMarginUse    = 100.0;  // MARGEM TOTAL (%) dividida entre as ordens do preco medio (100 / 5 = 20% cada)
input double InpMaxLot       = 100.0;  // Lote maximo por ordem (limite de seguranca)
input double InpRR           = 1.5;    // ALVO = risco x isto (1.5 = ganha 1,5x o que arrisca)
input double InpBreakevenR   = 0.6;    // Com lucro de X riscos, stop vai para a entrada (sem perda)
input double InpTrailStartR  = 1.0;    // Com lucro de X riscos, comeca o stop movel
input double InpTrailDistR   = 0.5;    // Distancia do stop movel (em riscos)
input int    InpEMAFast      = 20;     // EMA do recuo no M1
input int    InpEMATrend     = 50;     // EMA de tendencia no M5 e no M15
input int    InpMinScore     = 4;      // Forca minima da leitura dos candles para entrar (1 a 8)
input int    InpPauseLosses  = 2;      // Perdas seguidas para pausar
input int    InpPauseMin     = 15;     // Minutos de pausa depois dessas perdas
input int    InpMaxSpread    = 50;     // Spread maximo (pontos)
input int    InpMaxTrades    = 0;      // Maximo de operacoes por sessao (0 = sem limite, opera sempre)
input int    InpStartHour    = 0;      // Opera a partir desta hora (servidor) - 0 a 24 = dia inteiro
input int    InpEndHour      = 24;     // Ate esta hora (servidor)
input int    InpGridMax      = 5;      // PRECO MEDIO: maximo de ordens abertas juntas
input double InpGridPips     = 10.0;   // PRECO MEDIO: abre mais uma ordem a cada X pips CONTRA
input double InpClosePips    = 5.0;    // FECHA TUDO com X pips de lucro (1 ordem: +5 pips; varias: +5 sobre o preco medio)
input bool   InpAlwaysIn     = true;   // Abre ordens O TEMPO TODO: fechou, ja abre a proxima
input ulong  InpMagic        = 20261120;

CTrade   trade;
int      hATR = INVALID_HANDLE, hEMA = INVALID_HANDLE, hEMA15 = INVALID_HANDLE, hEMA1 = INVALID_HANDLE;
double   gRisk = 0;          // distancia do stop na entrada (1 risco)
int      gConsecLoss = 0;
datetime gPauseUntil = 0;
double   gStart = 0;        // capital quando ligou
datetime gStartTime = 0;
datetime gLastBar = 0;
bool     gStopped = false;
string   gStopWhy = "";
int      gWins = 0, gLosses = 0, gTrades = 0;
double   gPeak = 0;
bool     gEntered = false;   // ja tentou a entrada imediata
string   gLastPattern = "-";
double   gLastLot = 0;

//+------------------------------------------------------------------+
double Equity() { return(AccountInfoDouble(ACCOUNT_EQUITY)); }

//--- meta da sessao: valor fixo (ex.: 210) ou capital inicial x multiplicador
double   gFirstStart = 0;    // capital do 1o dia (para a meta % quando ja comeca acima da meta)
int      gDay = 0;           // dia do servidor (AAAAMMDD) da sessao atual
bool     gDayDone = false;   // meta/limite do dia batido: espera a meia-noite
double   gDayTarget = 0;

int Today() { MqlDateTime t; TimeToStruct(TimeCurrent(), t); return(t.year * 10000 + t.mon * 100 + t.day); }

//--- meta do dia: saldo fixo (ex.: 300). Se o dia ja comeca acima dele, busca o mesmo crescimento % do 1o dia.
double Target()
  {
   if(InpTargetUSD <= 0) return(gStart * InpTargetMult);
   if(gStart < InpTargetUSD) return(InpTargetUSD);
   double mult = (gFirstStart > 0 && gFirstStart < InpTargetUSD) ? InpTargetUSD / gFirstStart : 2.0;
   return(gStart * mult);
  }

//--- comeca um dia novo: capital de agora, contadores zerados, abre a 1a ordem de novo
void NewDay()
  {
   gDay = Today();
   gStart = Equity();
   if(gFirstStart <= 0) gFirstStart = gStart;
   gPeak = gStart;
   gDayTarget = Target();
   gStopped = false;
   gStopWhy = "";
   gDayDone = false;
   gTrades = 0; gWins = 0; gLosses = 0; gConsecLoss = 0; gPauseUntil = 0;
   gEntered = false;
   Print("Full Margin: dia ", gDay, " - capital ", DoubleToString(gStart, 2), " meta do dia ", DoubleToString(gDayTarget, 2));
  }

bool HasPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionSelectByTicket(t) && PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic) return(true);
     }
   return(false);
  }

void CloseAll()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionSelectByTicket(t) && (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
         trade.PositionClose(t);
     }
  }

void StopSession(const string why)
  {
   CloseAll();
   gStopped = true;
   gStopWhy = why;
   Print("Full Margin: sessao encerrada - ", why, " | capital ", DoubleToString(Equity(), 2));
   Alert("Full Margin: ", why, " - capital ", DoubleToString(Equity(), 2));
  }

//--- lote pela margem: InpMarginUse % da margem livre
double CalcLot(ENUM_ORDER_TYPE type, double price)
  {
   double m1 = 0;
   if(!OrderCalcMargin(type, _Symbol, 1.0, price, m1) || m1 <= 0) return(0);
   double free = Equity() * InpMarginUse / 100.0 / MathMax(1, InpGridMax);   // 100% dividido pelas 5 ordens
   double lot = free / m1;
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx = MathMin(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX), InpMaxLot);
   double st = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(st <= 0) st = 0.01;
   lot = MathFloor(lot / st + 1e-9) * st;
   if(lot < mn) return(0);              // capital nao da nem o lote minimo
   if(lot > mx) lot = mx;
   return(NormalizeDouble(lot, 2));
  }

bool InHours()
  {
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   return(t.hour >= InpStartHour && t.hour < InpEndHour);
  }

//+------------------------------------------------------------------+
//--- padrao de candle FORTE no ultimo candle fechado do M1: 1 = alta, -1 = baixa, 0 = nenhum
//--- (so engolfo e martelo/estrela com corpo/pavio de tamanho real; "3 candles" saiu: entrava tarde)
int CandleSignal(string &name, double a)
  {
   double o1 = iOpen(_Symbol, PERIOD_M1, 1), c1 = iClose(_Symbol, PERIOD_M1, 1);
   double h1 = iHigh(_Symbol, PERIOD_M1, 1), l1 = iLow(_Symbol, PERIOD_M1, 1);
   double o2 = iOpen(_Symbol, PERIOD_M1, 2), c2 = iClose(_Symbol, PERIOD_M1, 2);
   double range = h1 - l1;
   if(range <= 0 || range < 0.5 * a) return(0);                  // candle pequeno demais = ruido
   double body = MathAbs(c1 - o1);
   double upW = h1 - MathMax(o1, c1), dnW = MathMin(o1, c1) - l1;
   double body2 = MathAbs(c2 - o2);
   if(c2 < o2 && c1 > o1 && c1 > o2 && o1 <= c2 && body >= 1.2 * body2 && body >= 0.3 * a) { name = "engolfo de alta"; return(1); }
   if(c2 > o2 && c1 < o1 && c1 < o2 && o1 >= c2 && body >= 1.2 * body2 && body >= 0.3 * a) { name = "engolfo de baixa"; return(-1); }
   if(dnW >= 2 * body && dnW >= 0.6 * range && c1 >= o1) { name = "martelo"; return(1); }
   if(upW >= 2 * body && upW >= 0.6 * range && c1 <= o1) { name = "estrela cadente"; return(-1); }
   return(0);
  }

//--- abre com lote maximo (full margin); tenta 5% menor se faltar margem
bool OpenOrder(int dir, double slDist, const string name)
  {
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK), bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   ENUM_ORDER_TYPE type = (dir == 1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double lot = CalcLot(type, dir == 1 ? ask : bid);
   if(lot <= 0) { StopSession("capital insuficiente para o lote minimo"); return(false); }
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double st = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(st <= 0) st = 0.01;
   double tpDist = slDist * InpRR;
   bool ok = false;
   for(int k = 0; k < 8 && lot >= mn; k++)
     {
      ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      if(dir == 1)
         ok = trade.Buy(lot, _Symbol, ask, 0, 0, "Full Margin compra");     // sem stop/alvo: fecha pelo preco medio
      else
         ok = trade.Sell(lot, _Symbol, bid, 0, 0, "Full Margin venda");
      if(ok && trade.ResultRetcode() == TRADE_RETCODE_DONE) break;
      if(trade.ResultRetcode() != TRADE_RETCODE_NO_MONEY) break;
      lot = NormalizeDouble(MathFloor(lot * 0.95 / st) * st, 2);
     }
   gLastLot = lot;
   if(ok && trade.ResultRetcode() == TRADE_RETCODE_DONE)
     {
      gTrades++;
      gRisk = slDist;
      gLastPattern = (dir == 1 ? "COMPRA - " : "VENDA - ") + name + " " + TimeToString(TimeCurrent(), TIME_MINUTES);
      return(true);
     }
   Print("Full Margin: ordem recusada - ", trade.ResultRetcodeDescription());
   return(false);
  }

//--- LEITURA DO GRAFICO M1: soma pontos de alta (+) e de baixa (-)
//---  tendencia M5+M15 (2) | ultimos 10 candles (1) | topos/fundos subindo ou caindo (1)
//---  preco x EMA 20 do M1 (1) | padrao forte no ultimo candle (2) | ultimo candle de forca (1)
int ReadChart(double a, double e5, double e15, double e1, string &why)
  {
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   int sc = 0;
   int t5 = (bid > e5) ? 1 : -1, t15 = (bid > e15) ? 1 : -1;
   if(t5 == t15) sc += 2 * t5; else sc += t5;
   int bulls = 0, bears = 0;
   for(int k = 1; k <= 10; k++)
     {
      double o = iOpen(_Symbol, PERIOD_M1, k), c = iClose(_Symbol, PERIOD_M1, k);
      if(c > o) bulls++; else if(c < o) bears++;
     }
   if(bulls - bears >= 3) sc++; else if(bears - bulls >= 3) sc--;
   int hh = 0, ll = 0;
   for(int k = 1; k <= 4; k++)
     {
      if(iHigh(_Symbol, PERIOD_M1, k) > iHigh(_Symbol, PERIOD_M1, k + 1) && iLow(_Symbol, PERIOD_M1, k) > iLow(_Symbol, PERIOD_M1, k + 1)) hh++;
      if(iHigh(_Symbol, PERIOD_M1, k) < iHigh(_Symbol, PERIOD_M1, k + 1) && iLow(_Symbol, PERIOD_M1, k) < iLow(_Symbol, PERIOD_M1, k + 1)) ll++;
     }
   if(hh >= 2 && hh > ll) sc++; else if(ll >= 2 && ll > hh) sc--;
   double c1 = iClose(_Symbol, PERIOD_M1, 1), o1 = iOpen(_Symbol, PERIOD_M1, 1);
   sc += (c1 > e1) ? 1 : -1;
   string name = "";
   int pat = CandleSignal(name, a);
   sc += 2 * pat;
   if(MathAbs(c1 - o1) >= 0.6 * a) sc += (c1 > o1) ? 1 : -1;
   why = "leitura " + (sc > 0 ? "+" : "") + IntegerToString(sc) + (name != "" ? " " + name : "");
   return(sc);
  }

//--- now = true: entrada imediata ao ligar. Retorna false se os dados ainda nao carregaram.
bool TryEntry(bool now)
  {
   double atr[1], e5[1], e15[1], e1[1];
   if(CopyBuffer(hATR, 0, 1, 1, atr) != 1) return(false);
   if(CopyBuffer(hEMA, 0, 0, 1, e5) != 1) return(false);
   if(CopyBuffer(hEMA15, 0, 0, 1, e15) != 1) return(false);
   if(CopyBuffer(hEMA1, 0, 1, 1, e1) != 1) return(false);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double a = atr[0];
   if(a <= 0 || bid <= 0) return(false);
   double spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) * _Point;
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point + spread;

   string why = "";
   int sc = ReadChart(a, e5[0], e15[0], e1[0], why);
   int dir = 0;
   if(now) dir = (sc >= 0) ? 1 : -1;                              // ao ligar: entra para o lado da leitura
   else if(sc >= InpMinScore) dir = 1;
   else if(sc <= -InpMinScore) dir = -1;
   //--- lado escolhido: so venda / so compra
   if(InpSide == SIDE_SELL) dir = now ? -1 : (dir == -1 ? -1 : 0);
   if(InpSide == SIDE_BUY)  dir = now ?  1 : (dir ==  1 ?  1 : 0);
   if(dir == 0) return(true);

   //--- stop atras dos ultimos 5 candles (min 0.8 ATR, max 2.5 ATR)
   double ref = (dir == 1) ? bid - (iLow(_Symbol, PERIOD_M1, iLowest(_Symbol, PERIOD_M1, MODE_LOW, 5, 1)) - 0.2 * a)
                           : (iHigh(_Symbol, PERIOD_M1, iHighest(_Symbol, PERIOD_M1, MODE_HIGH, 5, 1)) + 0.2 * a) - bid;
   double sl = MathMin(MathMax(ref, 0.8 * a), 2.5 * a);
   sl = MathMax(sl, minDist + _Point);
   if(!now && spread > 0.3 * sl) return(true);                   // spread caro demais para esse stop
   OpenOrder(dir, sl, (now ? "ao ligar, " : "") + why);
   return(true);
  }

//--- protege o lucro: stop na entrada e stop movel
void ManagePosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || !PositionSelectByTicket(t)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double open = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
      double R = (gRisk > 0) ? gRisk : MathAbs(open - sl);
      if(R <= 0) continue;
      double spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) * _Point;
      double stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
      if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
        {
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID), prof = bid - open, ns = sl;
         if(prof >= InpBreakevenR * R) ns = MathMax(ns, open + spread);
         if(prof >= InpTrailStartR * R) ns = MathMax(ns, bid - InpTrailDistR * R);
         ns = NormalizeDouble(MathMin(ns, bid - stops - _Point), _Digits);
         if(ns > sl + _Point) trade.PositionModify(t, ns, tp);
        }
      else
        {
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK), prof = open - ask, ns = (sl > 0) ? sl : DBL_MAX;
         if(prof >= InpBreakevenR * R) ns = MathMin(ns, open - spread);
         if(prof >= InpTrailStartR * R) ns = MathMin(ns, ask + InpTrailDistR * R);
         ns = NormalizeDouble(MathMax(ns, ask + stops + _Point), _Digits);
         if(ns < sl - _Point || (sl == 0 && ns < DBL_MAX)) trade.PositionModify(t, ns, tp);
        }
     }
  }

double PipSize() { return((_Digits == 5 || _Digits == 3) ? _Point * 10.0 : _Point); }
double gBasketPips = 0;
int    gBasketN = 0;

//--- ordens abertas: quantas, lado, preco medio (pelo lote) e preco da ultima
int Basket(int &type, double &avg, double &last)
  {
   int n = 0;
   double vol = 0;
   datetime lastT = 0;
   type = -1; avg = 0; last = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || !PositionSelectByTicket(t)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double v = PositionGetDouble(POSITION_VOLUME), p = PositionGetDouble(POSITION_PRICE_OPEN);
      avg += p * v; vol += v; n++;
      type = (int)PositionGetInteger(POSITION_TYPE);
      datetime pt = (datetime)PositionGetInteger(POSITION_TIME);
      if(pt >= lastT) { lastT = pt; last = p; }
     }
   if(vol > 0) avg /= vol;
   return(n);
  }

//--- PRECO MEDIO: +InpClosePips sobre o medio -> fecha tudo; InpGridPips contra a ultima -> mais uma (ate InpGridMax)
void ManageBasket()
  {
   int type; double avg, last;
   int n = Basket(type, avg, last);
   gBasketN = n;
   if(n == 0) { gBasketPips = 0; return; }
   double pip = PipSize();
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID), ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   gBasketPips = (type == POSITION_TYPE_BUY) ? (bid - avg) / pip : (avg - ask) / pip;
   if(gBasketPips >= InpClosePips)
     {
      CloseAll();
      Print("Full Margin: fechei ", n, " ordem(ns) com ", DoubleToString(gBasketPips, 1), " pips de lucro");
      return;
     }
   if(n >= InpGridMax || SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) > InpMaxSpread) return;
   bool against = (type == POSITION_TYPE_BUY) ? (ask <= last - InpGridPips * pip) : (bid >= last + InpGridPips * pip);
   if(against) OpenOrder(type == POSITION_TYPE_BUY ? 1 : -1, InpGridPips * pip, "preco medio " + IntegerToString(n + 1) + "/" + IntegerToString(InpGridMax));
  }

void Cycle()
  {
   //--- depois da meia-noite: dia novo (so sem ordem aberta)
   if(InpDaily && Today() != gDay && !HasPosition())
     {
      if(gDay != 0 && gDayDone) Alert("Full Margin: novo dia - tentando a meta do dia de novo");
      NewDay();
     }
   if(gStopped) return;
   double eq = Equity();
   if(eq > gPeak) gPeak = eq;
   if(eq >= gDayTarget) { StopSession("META DO DIA BATIDA"); gDayDone = true; return; }
   if(eq <= gStart * (1.0 - InpMaxLossPct / 100.0)) { StopSession("LIMITE DE PERDA DO DIA"); gDayDone = true; return; }
   if(HasPosition()) { ManageBasket(); return; }      // preco medio (sem stop por ordem)
   gBasketPips = 0; gBasketN = 0;
   if(InpMaxTrades > 0 && gTrades >= InpMaxTrades) { StopSession("maximo de operacoes da sessao"); return; }

   bool spreadOk = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) <= InpMaxSpread;
   datetime bar = iTime(_Symbol, PERIOD_M1, 0);
   //--- entrada imediata ao ligar (so espera o spread ficar abaixo do maximo)
   //--- O TEMPO TODO: sem ordem aberta, ja abre a proxima para o lado da leitura (mesma estrategia de antes)
   if(InpAlwaysIn && gEntered)
     {
      if(spreadOk && InHours() && TimeCurrent() >= gPauseUntil) TryEntry(true);
      return;
     }
   if(InpEnterNow && !gEntered)
     {
      if(spreadOk && TryEntry(true)) { gEntered = true; gLastBar = bar; }
      return;
     }
   if(bar == 0 || bar == gLastBar) return;      // 1 analise por candle
   gLastBar = bar;
   if(TimeCurrent() < gPauseUntil) return;      // pausa depois de perdas seguidas
   if(!InHours() || !spreadOk) return;
   TryEntry(false);
  }

//+------------------------------------------------------------------+
//| Painel                                                           |
//+------------------------------------------------------------------+
color CLR_BG = C'14,16,26', CLR_GOLD = C'255,196,0', CLR_TXT = C'210,214,230',
      CLR_DIM = C'120,126,150', CLR_GREEN = C'0,220,120', CLR_RED = C'255,80,90';

void Obj(const string n, ENUM_OBJECT t)
  {
   if(ObjectFind(0, n) >= 0) return;
   ObjectCreate(0, n, t, 0, 0, 0);
   ObjectSetInteger(0, n, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, n, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, n, OBJPROP_HIDDEN, true);
  }

void Box(const string n, int x, int y, int w, int h, color bg, color border)
  {
   Obj(n, OBJ_RECTANGLE_LABEL);
   ObjectSetInteger(0, n, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, n, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, n, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, n, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, n, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, n, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, n, OBJPROP_COLOR, border);
  }

void Line(int row, const string text, color c, int size = 10, const string font = "Consolas")
  {
   string n = "FM_L" + IntegerToString(row);
   Obj(n, OBJ_LABEL);
   ObjectSetInteger(0, n, OBJPROP_XDISTANCE, 26);
   ObjectSetInteger(0, n, OBJPROP_YDISTANCE, 32 + row * 21 + (row > 0 ? 10 : 0));
   ObjectSetString(0, n, OBJPROP_FONT, font);
   ObjectSetInteger(0, n, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, n, OBJPROP_COLOR, c);
   ObjectSetString(0, n, OBJPROP_TEXT, text == "" ? " " : text);
  }

void Dashboard()
  {
   double eq = Equity(), target = gDayTarget, lossFloor = gStart * (1.0 - InpMaxLossPct / 100.0);
   double pnl = eq - gStart;
   double prog = (target > gStart) ? MathMax(0, MathMin(1, (eq - gStart) / (target - gStart))) : 0;
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   Box("FM_BG", 12, 22, 380, 376, CLR_BG, CLR_GOLD);
   Box("FM_HEAD", 13, 23, 378, 54, C'34,30,12', C'34,30,12');

   Line(0, "FULL MARGIN", CLR_GOLD, 15, "Arial Black");
   bool algo = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   if(gStopped)
      Line(1, "■ " + gStopWhy + (gDayDone && InpDaily ? " - volta 00:00" : ""), StringFind(gStopWhy, "META") >= 0 ? CLR_GREEN : CLR_RED, 10, "Arial Bold");
   else if(!algo)
      Line(1, "● ligue o Algo Trading", CLR_RED, 10, "Arial Bold");
   else if(TimeCurrent() < gPauseUntil)
      Line(1, "● PAUSA ate " + TimeToString(gPauseUntil, TIME_MINUTES) + " (perdas seguidas)", CLR_GOLD, 10, "Arial Bold");
   else
      Line(1, "● OPERANDO  " + _Symbol + (InpSide == SIDE_SELL ? "  SO VENDA" : (InpSide == SIDE_BUY ? "  SO COMPRA" : "  candles M1")), CLR_GREEN, 10, "Arial Bold");

   Line(3, "Comecou hoje: " + DoubleToString(gStart, 2) + " " + cur, CLR_TXT);
   Line(4, "Capital     : " + DoubleToString(eq, 2) + " " + cur, CLR_TXT, 11);
   Line(5, "Resultado   : " + (pnl >= 0 ? "+" : "") + DoubleToString(pnl, 2) + "  (" +
           (gStart > 0 ? DoubleToString(pnl / gStart * 100.0, 1) : "0") + "%)", pnl >= 0 ? CLR_GREEN : CLR_RED, 11);
   Line(6, "Meta do dia : " + DoubleToString(target, 2) + "   Limite: " + DoubleToString(lossFloor, 2), CLR_DIM);

   //--- barra de progresso ate a meta
   int bx = 26, by = 32 + 7 * 21 + 14, bw = 340;
   Box("FM_BAR0", bx, by, bw, 16, C'40,44,60', C'60,64,84');
   Box("FM_BAR1", bx, by, MathMax(2, (int)(bw * prog)), 16, CLR_GOLD, CLR_GOLD);
   Line(8, "", CLR_TXT);
   Line(9, "Progresso da meta: " + DoubleToString(prog * 100.0, 0) + "%", CLR_GOLD);

   int tot = gWins + gLosses;
   Line(10, "Operacoes : " + IntegerToString(gTrades) + "   Ganhos: " + IntegerToString(gWins) + "   Perdas: " + IntegerToString(gLosses), CLR_TXT);
   Line(11, "Acerto    : " + (tot > 0 ? DoubleToString(100.0 * gWins / tot, 0) + "%" : "-"), CLR_TXT);
   Line(14, gBasketN > 0 ? "Ordens: " + IntegerToString(gBasketN) + "/" + IntegerToString(InpGridMax) + "   preco medio: " +
                           DoubleToString(gBasketPips, 1) + " pips (fecha com +" + DoubleToString(InpClosePips, 0) + ")"
                         : "Sem ordens - abre a proxima ja", gBasketPips >= 0 ? CLR_GREEN : CLR_RED, 9);
   Line(12, "Ultima: " + gLastPattern + "  lote " + DoubleToString(gLastLot, 2), CLR_DIM, 8);
   Line(13, "Ligado as " + TimeToString(gStartTime, TIME_MINUTES) + "  -  margem " + DoubleToString(InpMarginUse, 0) + "% em " + IntegerToString(InpGridMax) + " ordens", CLR_DIM, 8);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   if((InpTargetUSD <= 0 && InpTargetMult <= 1.0) || InpMaxLossPct <= 0 || InpMaxLossPct >= 100 || InpMarginUse <= 0 || InpMarginUse > 100)
     {
      Alert("Full Margin: confira meta (>1), limite de perda (1-99%) e margem usada (1-100%)");
      return(INIT_PARAMETERS_INCORRECT);
     }
   hATR = iATR(_Symbol, PERIOD_M1, 14);
   hEMA = iMA(_Symbol, PERIOD_M5, InpEMATrend, 0, MODE_EMA, PRICE_CLOSE);
   hEMA15 = iMA(_Symbol, PERIOD_M15, InpEMATrend, 0, MODE_EMA, PRICE_CLOSE);
   hEMA1 = iMA(_Symbol, PERIOD_M1, InpEMAFast, 0, MODE_EMA, PRICE_CLOSE);
   if(hATR == INVALID_HANDLE || hEMA == INVALID_HANDLE || hEMA15 == INVALID_HANDLE || hEMA1 == INVALID_HANDLE)
     {
      Print("Full Margin: nao consegui criar os indicadores");
      return(INIT_FAILED);
     }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);
   trade.SetTypeFillingBySymbol(_Symbol);
   NewDay();
   gStartTime = TimeCurrent();
   Print("Full Margin ligado: capital ", DoubleToString(gStart, 2), " meta do dia ", DoubleToString(gDayTarget, 2),
         " limite ", DoubleToString(gStart * (1.0 - InpMaxLossPct / 100.0), 2));
   EventSetTimer(1);
   Dashboard();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   IndicatorRelease(hATR); IndicatorRelease(hEMA); IndicatorRelease(hEMA15); IndicatorRelease(hEMA1);
   ObjectsDeleteAll(0, "FM_");
   ChartRedraw(0);
  }

void OnTick()  { Cycle(); }
void OnTimer() { Cycle(); Dashboard(); }

//--- conta ganhos/perdas quando uma ordem do robo fecha
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &req, const MqlTradeResult &res)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if((ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagic) return;
   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) return;
   double p = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) + HistoryDealGetDouble(trans.deal, DEAL_SWAP) +
              HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   if(p >= 0) { gWins++; gConsecLoss = 0; }
   else
     {
      gLosses++;
      gConsecLoss++;
      if(InpPauseLosses > 0 && gConsecLoss >= InpPauseLosses)
        {
         gPauseUntil = TimeCurrent() + InpPauseMin * 60;
         gConsecLoss = 0;
         Print("Full Margin: ", InpPauseLosses, " perdas seguidas - pausa de ", InpPauseMin, " min");
        }
     }
  }
//+------------------------------------------------------------------+
