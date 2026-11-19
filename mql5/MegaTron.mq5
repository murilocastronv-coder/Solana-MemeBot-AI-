//+------------------------------------------------------------------+
//| MegaTron.mq5  -  robo "Mega Tron"  v2 (multi-pares + painel)     |
//| Expert Advisor para MetaTrader 5 - grade com preco medio (M1)    |
//|                                                                  |
//| Coloque em UM grafico qualquer: ele analisa TODOS os pares de    |
//| FOREX da corretora (ou so os que voce escrever em InpSymbols).   |
//| No maximo InpMaxPairs pares com ordens abertas ao mesmo tempo.   |
//|                                                                  |
//| Entrada (candle fechado do M1), em cada par:                     |
//|  - COMPRA no fundo: minima do candle = menor dos ultimos N,      |
//|    RSI abaixo de 30 e o candle fechou de alta.                   |
//|  - VENDA no topo: maxima do candle = maior dos ultimos N,        |
//|    RSI acima de 70 e o candle fechou de baixa.                   |
//| Grade (o preco foi contra a 1a ordem):                           |
//|  - depois de InpStep1 pips contra -> 2a ordem;                   |
//|  - depois de mais InpStep2 pips   -> 3a ordem.                   |
//|  Lote da 2a e da 3a = lote da 1a + InpAddLot (padrao 0.01):      |
//|    0.01 -> 0.02 / 0.02    0.02 -> 0.03 / 0.03                    |
//|    0.04 -> 0.05 / 0.05    0.10 -> 0.11 / 0.11                    |
//| Saida: fecha TODAS as ordens do par quando o preco esta          |
//| InpTakePips a favor do preco medio delas.                        |
//| Stop de emergencia: fecha tudo se as ordens abertas do robo      |
//| perderem InpMaxDDPercent do saldo (0 = desligado).               |
//|                                                                  |
//| AVISO: sem stop por ordem, uma tendencia forte contra as ordens  |
//| pode causar perda grande. Teste no Strategy Tester e na DEMO.    |
//+------------------------------------------------------------------+
#property copyright "Mega Tron"
#property link      ""
#property version   "2.00"
#property description "Mega Tron - analisa todos os pares de forex no M1: compra no fundo / vende no topo; 2a e 3a ordens a 700 e +800 pips contra (lote da 1a + 0.01); fecha o par com 20 pips sobre o preco medio; maximo 3 pares abertos; painel com lucro por dia."

#include <Trade\Trade.mqh>

input string  InpSymbols           = "";        // Pares (vazio = TODOS os pares de forex; ou ex: EURUSD,GBPUSD)
input int     InpMaxPairs          = 3;         // Maximo de pares com ordens abertas ao mesmo tempo
input ENUM_TIMEFRAMES InpTF        = PERIOD_M1; // Tempo grafico da analise
input int     InpLookback          = 20;        // Fundo/topo: candles para comparar
input int     InpRSIPeriod         = 14;        // Periodo do RSI
input double  InpRSILow            = 30.0;      // RSI abaixo disso = fundo
input double  InpRSIHigh           = 70.0;      // RSI acima disso = topo
input double  InpLot1              = 0.01;      // LOTE da 1a ordem (0.01 ou mais)
input double  InpAddLot            = 0.01;      // 2a e 3a = lote da 1a + isto (0.02->0.03, 0.04->0.05)
input double  InpStep1             = 700;       // Pips contra para abrir a 2a ordem
input double  InpStep2             = 800;       // Mais pips contra para abrir a 3a ordem
input double  InpTakePips          = 20;        // Fecha o par com X pips a favor do preco medio
input bool    InpUsePoints         = false;     // true = os valores acima sao PONTOS do MT5, nao pips
input double  InpMaxDDPercent      = 30.0;      // Stop de emergencia: perda maxima em % do saldo (0 = desliga)
input int     InpMaxSpread         = 50;        // Spread maximo para a 1a ordem (pontos)
input bool    InpDashboard         = true;      // Mostrar o painel no grafico
input ulong   InpMagic             = 20261119;  // Numero magico do robo

CTrade   trade;
string   gSyms[];
int      gRSI[];
datetime gLastBar[];
int      gN = 0;
string   gLastSignal = "aguardando o 1o sinal";
int      gTimerCount = 0;

//--- estatisticas do painel (ultimos 7 dias, indice 6 = hoje)
double   gDayProfit[7];
int      gDayWin[7];
int      gDayLoss[7];
datetime gDayStart[7];

//+------------------------------------------------------------------+
//| Moedas de forex (fiat). Ouro, prata, indices e cripto ficam fora |
//+------------------------------------------------------------------+
bool IsFiat(const string c)
  {
   string list = ",USD,EUR,GBP,JPY,AUD,NZD,CAD,CHF,SEK,NOK,DKK,SGD,HKD,ZAR,MXN,TRY,PLN,CNH,HUF,CZK,";
   return(StringLen(c) == 3 && StringFind(list, "," + c + ",") >= 0);
  }

//--- par de forex = nome com 2 moedas fiat diferentes (EURUSD, GBPJPY, EURUSD.a, EURUSDm).
//--- Acoes, indices, ouro e cripto ficam de fora.
bool IsForex(const string s)
  {
   if(SymbolInfoInteger(s, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_DISABLED) return(false);
   string n = s;
   StringToUpper(n);
   if(StringLen(n) < 6) return(false);
   //--- o nome precisa comecar com 2 moedas diferentes: EURUSD, GBPJPY, EURUSD.a ...
   string a = StringSubstr(n, 0, 3), b = StringSubstr(n, 3, 3);
   if(a == b || !IsFiat(a) || !IsFiat(b)) return(false);
   //--- depois das 6 letras so pode vir sufixo curto da corretora (.a, m, .pro)
   if(StringLen(n) > 10) return(false);
   //--- se a corretora informa as moedas, elas tem que bater com o nome
   string base = SymbolInfoString(s, SYMBOL_CURRENCY_BASE), prof = SymbolInfoString(s, SYMBOL_CURRENCY_PROFIT);
   StringToUpper(base); StringToUpper(prof);
   if(StringLen(base) == 3 && StringLen(prof) == 3 && (base != a || prof != b)) return(false);
   return(true);
  }

#define MAX_SYMBOLS 100
int    gFound = 0, gSelFail = 0, gScanned = 0;
string gDiag = "";

//--- adiciona o par; o RSI e criado depois (se falhar agora, tenta de novo no ciclo)
void AddSymbol(const string s)
  {
   if(gN >= MAX_SYMBOLS) return;
   for(int i = 0; i < gN; i++) if(gSyms[i] == s) return;
   if(!SymbolInfoInteger(s, SYMBOL_SELECT) && !SymbolSelect(s, true)) { gSelFail++; Print("Mega Tron: nao consegui ativar ", s); return; }
   ArrayResize(gSyms, gN + 1);
   ArrayResize(gRSI, gN + 1);
   ArrayResize(gLastBar, gN + 1);
   gSyms[gN] = s; gRSI[gN] = iRSI(s, InpTF, InpRSIPeriod, PRICE_CLOSE); gLastBar[gN] = 0;
   gN++;
  }

void BuildSymbols()
  {
   for(int i = 0; i < gN; i++) if(gRSI[i] != INVALID_HANDLE) IndicatorRelease(gRSI[i]);
   gN = 0; gFound = 0; gSelFail = 0; gScanned = 0;
   string txt = InpSymbols;
   StringReplace(txt, " ", "");
   if(StringLen(txt) > 0)
     {
      string parts[];
      int k = StringSplit(txt, ',', parts);
      for(int i = 0; i < k; i++) if(StringLen(parts[i]) > 0) AddSymbol(parts[i]);
      gDiag = "lista manual: " + txt;
      Print("Mega Tron: usando a lista manual da opcao Pares: ", txt, " (deixe vazia para analisar TODOS)");
      if(gN > 0) return;
     }
   //--- 1o os pares da Observacao do Mercado, depois todos os da corretora
   int mw = SymbolsTotal(true);
   for(int i = 0; i < mw; i++)
     {
      string s = SymbolName(i, true);
      gScanned++;
      if(IsForex(s)) { gFound++; AddSymbol(s); }
     }
   int total = SymbolsTotal(false);
   for(int i = 0; i < total && gN < MAX_SYMBOLS; i++)
     {
      string s = SymbolName(i, false);
      gScanned++;
      if(IsForex(s)) { gFound++; AddSymbol(s); }
     }
   gDiag = "lidos " + IntegerToString(gScanned) + "  forex " + IntegerToString(gFound) + "  falhas " + IntegerToString(gSelFail);
   //--- se mesmo assim nao achar nada, opera pelo menos o par do grafico
   if(gN == 0) { Print("Mega Tron: nenhum par de forex reconhecido; usando so ", _Symbol); AddSymbol(_Symbol); gDiag += "  (so o grafico)"; }
   Print("Mega Tron: simbolos na corretora=", total, ", na Observacao do Mercado=", mw, ", pares de forex usados=", gN, " | ", gDiag);
  }

//+------------------------------------------------------------------+
//| Lotes                                                            |
//+------------------------------------------------------------------+
double AddLot()
  {
   if(InpAddLot > 0) return(InpAddLot);
   //--- 0 = automatico: soma 1 "degrau" do lote (0.01..0.09 -> 0.01; 0.1..0.9 -> 0.1; 1..9 -> 1)
   return(MathPow(10.0, MathFloor(MathLog10(InpLot1) + 1e-9)));
  }

double NormalizeLot(const string s, double lot)
  {
   double mn = SymbolInfoDouble(s, SYMBOL_VOLUME_MIN);
   double mx = SymbolInfoDouble(s, SYMBOL_VOLUME_MAX);
   double st = SymbolInfoDouble(s, SYMBOL_VOLUME_STEP);
   if(st <= 0) st = 0.01;
   lot = MathFloor(lot / st + 1e-9) * st;
   if(lot < mn) lot = mn;
   if(mx > 0 && lot > mx) lot = mx;
   return(NormalizeDouble(lot, 2));
  }

double Lot1(const string s) { return(NormalizeLot(s, InpLot1)); }
double LotNext(const string s) { return(NormalizeLot(s, InpLot1 + AddLot())); }

double PipSize(const string s)
  {
   double point = SymbolInfoDouble(s, SYMBOL_POINT);
   if(InpUsePoints) return(point);
   long digits = SymbolInfoInteger(s, SYMBOL_DIGITS);
   if(digits == 5 || digits == 3) return(point * 10.0);
   return(point);
  }

//+------------------------------------------------------------------+
//| Ordens do robo em um par                                         |
//+------------------------------------------------------------------+
struct Basket
  {
   int    count;
   int    type;
   double avg;
   double first;
   double last;
   double profit;
  };

void Scan(const string s, Basket &b)
  {
   b.count = 0; b.type = -1; b.avg = 0; b.first = 0; b.last = 0; b.profit = 0;
   double vol = 0;
   datetime firstT = 0, lastT = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != s || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double v = PositionGetDouble(POSITION_VOLUME);
      double p = PositionGetDouble(POSITION_PRICE_OPEN);
      datetime t = (datetime)PositionGetInteger(POSITION_TIME);
      b.type = (int)PositionGetInteger(POSITION_TYPE);
      b.avg += p * v;
      vol += v;
      b.profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      if(firstT == 0 || t < firstT) { firstT = t; b.first = p; }
      if(t >= lastT) { lastT = t; b.last = p; }
      b.count++;
     }
   if(vol > 0) b.avg /= vol;
  }

//--- quantos pares diferentes tem ordem aberta do robo
int OpenPairs()
  {
   string seen = ",";
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      string s = PositionGetString(POSITION_SYMBOL);
      if(StringFind(seen, "," + s + ",") >= 0) continue;
      seen += s + ",";
      n++;
     }
   return(n);
  }

double FloatingProfit()
  {
   double sum = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      sum += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return(sum);
  }

void ClosePositions(const string s, const string why)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      string ps = PositionGetString(POSITION_SYMBOL);
      if(s != "" && ps != s) continue;
      trade.SetTypeFillingBySymbol(ps);
      if(!trade.PositionClose(ticket))
         Print("Mega Tron: nao fechou ", ticket, " ", trade.ResultRetcodeDescription());
     }
   Print("Mega Tron: fechei ", (s == "" ? "TODAS as ordens" : "as ordens de " + s), " (", why, ")");
  }

//+------------------------------------------------------------------+
//| Grade + saida de um par que ja tem ordens                        |
//+------------------------------------------------------------------+
void ManageGrid(const string s, Basket &b)
  {
   double pip = PipSize(s);
   double bid = SymbolInfoDouble(s, SYMBOL_BID);
   double ask = SymbolInfoDouble(s, SYMBOL_ASK);
   if(bid <= 0 || ask <= 0) return;
   trade.SetTypeFillingBySymbol(s);

   if(b.type == POSITION_TYPE_BUY)
     {
      if(bid >= b.avg + InpTakePips * pip) { ClosePositions(s, "lucro sobre o preco medio"); return; }
      if(b.count == 1 && b.first - ask >= InpStep1 * pip)
         trade.Buy(LotNext(s), s, ask, 0, 0, "Mega Tron 2a compra");
      else if(b.count == 2 && b.last - ask >= InpStep2 * pip)
         trade.Buy(LotNext(s), s, ask, 0, 0, "Mega Tron 3a compra");
     }
   else if(b.type == POSITION_TYPE_SELL)
     {
      if(ask <= b.avg - InpTakePips * pip) { ClosePositions(s, "lucro sobre o preco medio"); return; }
      if(b.count == 1 && bid - b.first >= InpStep1 * pip)
         trade.Sell(LotNext(s), s, bid, 0, 0, "Mega Tron 2a venda");
      else if(b.count == 2 && bid - b.last >= InpStep2 * pip)
         trade.Sell(LotNext(s), s, bid, 0, 0, "Mega Tron 3a venda");
     }
  }

//+------------------------------------------------------------------+
//| Sinal no candle fechado: 1 = fundo (compra), -1 = topo (venda)   |
//+------------------------------------------------------------------+
int Signal(const int i)
  {
   string s = gSyms[i];
   if(Bars(s, InpTF) < InpLookback + InpRSIPeriod + 5) return(0);
   if(gRSI[i] == INVALID_HANDLE) gRSI[i] = iRSI(s, InpTF, InpRSIPeriod, PRICE_CLOSE);
   if(gRSI[i] == INVALID_HANDLE) return(0);
   double rsi[];
   ArraySetAsSeries(rsi, true);
   if(CopyBuffer(gRSI[i], 0, 1, 1, rsi) != 1) return(0);
   int lowest  = iLowest(s, InpTF, MODE_LOW, InpLookback, 1);
   int highest = iHighest(s, InpTF, MODE_HIGH, InpLookback, 1);
   double open1  = iOpen(s, InpTF, 1);
   double close1 = iClose(s, InpTF, 1);
   if(lowest == 1 && rsi[0] < InpRSILow && close1 > open1) return(1);
   if(highest == 1 && rsi[0] > InpRSIHigh && close1 < open1) return(-1);
   return(0);
  }

//+------------------------------------------------------------------+
//| Um ciclo: cuida das ordens abertas e procura entradas novas      |
//+------------------------------------------------------------------+
void Cycle()
  {
   //--- stop de emergencia (todas as ordens do robo, todos os pares)
   if(InpMaxDDPercent > 0)
     {
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      double fl = FloatingProfit();
      if(bal > 0 && -fl / bal * 100.0 >= InpMaxDDPercent)
        {
         ClosePositions("", "stop de emergencia");
         return;
        }
     }

   int pairs = OpenPairs();
   for(int i = 0; i < gN; i++)
     {
      string s = gSyms[i];
      Basket b;
      Scan(s, b);
      if(b.count > 0) { ManageGrid(s, b); continue; }

      datetime bar = iTime(s, InpTF, 0);
      if(bar == 0 || bar == gLastBar[i]) continue;   // 1 analise por candle em cada par
      gLastBar[i] = bar;
      if(pairs >= InpMaxPairs) continue;             // ja tem o maximo de pares abertos
      if(SymbolInfoInteger(s, SYMBOL_SPREAD) > InpMaxSpread) continue;

      int sig = Signal(i);
      if(sig == 0) continue;
      trade.SetTypeFillingBySymbol(s);
      bool ok;
      if(sig == 1)
         ok = trade.Buy(Lot1(s), s, SymbolInfoDouble(s, SYMBOL_ASK), 0, 0, "Mega Tron 1a compra");
      else
         ok = trade.Sell(Lot1(s), s, SymbolInfoDouble(s, SYMBOL_BID), 0, 0, "Mega Tron 1a venda");
      if(ok && (trade.ResultRetcode() == TRADE_RETCODE_DONE || trade.ResultRetcode() == TRADE_RETCODE_PLACED))
        {
         pairs++;
         gLastSignal = s + (sig == 1 ? " COMPRA (fundo) " : " VENDA (topo) ") + TimeToString(TimeCurrent(), TIME_MINUTES);
        }
      else
         Print("Mega Tron: ordem recusada em ", s, ": ", trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
//| Estatisticas do historico: lucro por dia, pares ganhos/perdidos  |
//| (um "par ganho" = um conjunto de ordens do par fechado no lucro) |
//+------------------------------------------------------------------+
void FlushBasket(int day, double p)
  {
   if(day < 0 || day > 6) return;
   if(p >= 0) gDayWin[day]++; else gDayLoss[day]++;
  }

void Stats()
  {
   datetime now = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(now, dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   datetime today = StructToTime(dt);
   datetime from = today - 6 * 86400;
   for(int d = 0; d < 7; d++) { gDayProfit[d] = 0; gDayWin[d] = 0; gDayLoss[d] = 0; gDayStart[d] = from + d * 86400; }
   if(!HistorySelect(from, now + 3600)) return;

   string bSym = "";
   datetime bT = 0;
   double bP = 0;
   int bDay = -1;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0 || (ulong)HistoryDealGetInteger(t, DEAL_MAGIC) != InpMagic) continue;
      datetime tm = (datetime)HistoryDealGetInteger(t, DEAL_TIME);
      int d = (int)((tm - from) / 86400);
      if(d < 0 || d > 6) continue;
      double p = HistoryDealGetDouble(t, DEAL_PROFIT) + HistoryDealGetDouble(t, DEAL_SWAP) +
                 HistoryDealGetDouble(t, DEAL_COMMISSION) + HistoryDealGetDouble(t, DEAL_FEE);
      gDayProfit[d] += p;
      long entry = HistoryDealGetInteger(t, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      string s = HistoryDealGetString(t, DEAL_SYMBOL);
      if(s == bSym && tm - bT <= 15) { bP += p; bT = tm; continue; }   // mesmo fechamento do par
      if(bSym != "") FlushBasket(bDay, bP);
      bSym = s; bT = tm; bP = p; bDay = d;
     }
   if(bSym != "") FlushBasket(bDay, bP);
  }

//+------------------------------------------------------------------+
//| Painel                                                           |
//+------------------------------------------------------------------+
#define DASH_ROWS 29
#define DASH_X    12
#define DASH_Y    22
#define DASH_W    390
#define ROW_H     19

color CLR_BG    = C'16,18,28';
color CLR_GOLD  = C'255,196,0';
color CLR_TXT   = C'210,214,230';
color CLR_DIM   = C'120,126,150';
color CLR_GREEN = C'0,220,120';
color CLR_RED   = C'255,80,90';

void Rect(const string n, int x, int y, int w, int h, color bg, color border)
  {
   if(ObjectFind(0, n) < 0)
     {
      ObjectCreate(0, n, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, n, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, n, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, n, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, n, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, n, OBJPROP_BACK, false);
     }
   ObjectSetInteger(0, n, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, n, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, n, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, n, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, n, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, n, OBJPROP_COLOR, border);
  }

void Txt(int row, const string text, color c, int size = 9, const string font = "Consolas", int dx = 0)
  {
   string n = "MT_L" + IntegerToString(row) + "_" + IntegerToString(dx);
   if(ObjectFind(0, n) < 0)
     {
      ObjectCreate(0, n, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, n, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, n, OBJPROP_ANCHOR, ANCHOR_LEFT_UPPER);
      ObjectSetInteger(0, n, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, n, OBJPROP_HIDDEN, true);
     }
   ObjectSetInteger(0, n, OBJPROP_XDISTANCE, DASH_X + 12 + dx);
   ObjectSetInteger(0, n, OBJPROP_YDISTANCE, DASH_Y + 10 + row * ROW_H + (row > 0 ? 10 : 0));
   ObjectSetString(0, n, OBJPROP_FONT, font);
   ObjectSetInteger(0, n, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, n, OBJPROP_COLOR, c);
   ObjectSetString(0, n, OBJPROP_TEXT, text);
  }

string Money(double v) { return((v >= 0 ? "+" : "") + DoubleToString(v, 2) + " " + AccountInfoString(ACCOUNT_CURRENCY)); }
color  PnlColor(double v) { return(v > 0 ? CLR_GREEN : (v < 0 ? CLR_RED : CLR_TXT)); }

void Dashboard()
  {
   if(!InpDashboard) return;
   Rect("MT_BG", DASH_X, DASH_Y, DASH_W, DASH_ROWS * ROW_H + 28, CLR_BG, CLR_GOLD);
   Rect("MT_HEAD", DASH_X + 1, DASH_Y + 1, DASH_W - 2, ROW_H * 2 + 16, C'34,30,12', C'34,30,12');

   int r = 0;
   Txt(r, "MEGA TRON", CLR_GOLD, 14, "Arial Black");
   Txt(r, "v2  forex M1", CLR_DIM, 8, "Consolas", 250);
   r += 1;

   bool algo = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   bool conn = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool blink = (gTimerCount % 2 == 0);
   if(!conn)       Txt(r, "● SEM CONEXAO COM A CORRETORA", CLR_RED, 10, "Arial Bold");
   else if(!algo)  Txt(r, "● PARADO: ligue o Algo Trading", CLR_RED, 10, "Arial Bold");
   else if(gN == 0) Txt(r, "● CARREGANDO OS PARES...", CLR_GOLD, 10, "Arial Bold");
   else            Txt(r, (blink ? "● " : "○ ") + "FUNCIONANDO  -  analisando " + IntegerToString(gN) + " pares", CLR_GREEN, 10, "Arial Bold");
   r += 2;

   int pairs = OpenPairs();
   double fl = FloatingProfit();
   string names = "";
   for(int i = 0; i < gN && i < 6; i++) names += (i > 0 ? " " : "") + gSyms[i];
   if(gN > 6) names += " +" + IntegerToString(gN - 6);
   Txt(r++, "Pares: " + names, CLR_DIM, 8);
   Txt(r++, "Busca: " + gDiag, CLR_DIM, 8);
   if(StringLen(InpSymbols) > 0) Txt(r++, "(lista manual na opcao Pares - deixe vazia p/ todos)", CLR_GOLD, 8);
   Txt(r++, "Pares abertos : " + IntegerToString(pairs) + " / " + IntegerToString(InpMaxPairs), pairs >= InpMaxPairs ? CLR_GOLD : CLR_TXT);
   Txt(r++, "Lotes         : " + DoubleToString(InpLot1, 2) + "  ->  " + DoubleToString(InpLot1 + AddLot(), 2) +
            "  ->  " + DoubleToString(InpLot1 + AddLot(), 2), CLR_TXT);
   Txt(r++, "Saldo         : " + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
            "   Capital: " + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2), CLR_TXT);
   Txt(r++, "Lucro aberto  : " + Money(fl), PnlColor(fl));
   r++;

   Txt(r++, "HOJE", CLR_GOLD, 10, "Arial Bold");
   Txt(r++, "Lucro do dia  : " + Money(gDayProfit[6]), PnlColor(gDayProfit[6]), 11, "Consolas");
   Txt(r++, "Pares ganhos  : " + IntegerToString(gDayWin[6]) + "    Pares perdidos: " + IntegerToString(gDayLoss[6]), CLR_TXT);
   r++;

   Txt(r++, "LUCRO POR DIA (7 dias)", CLR_GOLD, 10, "Arial Bold");
   double week = 0; int ww = 0, wl = 0;
   for(int d = 6; d >= 0; d--)
     {
      week += gDayProfit[d]; ww += gDayWin[d]; wl += gDayLoss[d];
      string day = StringSubstr(TimeToString(gDayStart[d], TIME_DATE), 5);   // "11.19"
      StringReplace(day, ".", "/");
      string line = StringFormat("%s %s  %14s   %d G / %d P", day, (d == 6 ? "hoje" : "    "), Money(gDayProfit[d]), gDayWin[d], gDayLoss[d]);
      Txt(r++, line, d == 6 ? PnlColor(gDayProfit[d]) : (gDayProfit[d] == 0 ? CLR_DIM : PnlColor(gDayProfit[d])));
     }
   Txt(r++, "Total 7 dias  : " + Money(week) + "   " + IntegerToString(ww) + " G / " + IntegerToString(wl) + " P", PnlColor(week));
   r++;

   Txt(r++, "ABERTOS AGORA", CLR_GOLD, 10, "Arial Bold");
   int shown = 0;
   for(int i = 0; i < gN && shown < 4; i++)
     {
      Basket b;
      Scan(gSyms[i], b);
      if(b.count == 0) continue;
      string line = StringFormat("%-10s %-6s %d ordem(ns)  %s", gSyms[i], (b.type == POSITION_TYPE_BUY ? "COMPRA" : "VENDA"), b.count, Money(b.profit));
      Txt(r++, line, PnlColor(b.profit));
      shown++;
     }
   if(shown == 0) Txt(r++, "nenhum - esperando fundo ou topo", CLR_DIM);
   Txt(r++, "Ultima entrada: " + gLastSignal, CLR_DIM, 8);
   for(; r < DASH_ROWS; r++) Txt(r, " ", CLR_TXT);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpLot1 < 0.01)
     {
      Alert("Mega Tron: o lote da 1a ordem precisa ser 0.01 ou mais");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpMaxPairs < 1)
     {
      Alert("Mega Tron: o maximo de pares precisa ser 1 ou mais");
      return(INIT_PARAMETERS_INCORRECT);
     }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(30);
   BuildSymbols();
   if(gN == 0)
      Print("Mega Tron: ainda nao carregou os pares; vou tentar de novo a cada 10 segundos");
   Print("Mega Tron ligado: ", gN, " pares, maximo ", InpMaxPairs, " abertos, lotes ",
         DoubleToString(InpLot1, 2), " / ", DoubleToString(InpLot1 + AddLot(), 2), " / ", DoubleToString(InpLot1 + AddLot(), 2));
   Stats();
   Dashboard();
   EventSetTimer(1);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   for(int i = 0; i < gN; i++) if(gRSI[i] != INVALID_HANDLE) IndicatorRelease(gRSI[i]);
   ObjectsDeleteAll(0, "MT_");
   ChartRedraw(0);
  }

void OnTimer()
  {
   gTimerCount++;
   if((gN == 0 || (gFound == 0 && StringLen(InpSymbols) == 0)) && gTimerCount % 30 == 0) BuildSymbols();
   Cycle();
   if(gTimerCount % 5 == 0) Stats();
   Dashboard();
  }

void OnTick()
  {
   Cycle();
  }

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &req, const MqlTradeResult &res)
  {
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD) { Stats(); Dashboard(); }
  }
//+------------------------------------------------------------------+
