//+------------------------------------------------------------------+
//| RoboTendencia.mq5                                                |
//| Expert Advisor para MetaTrader 5                                 |
//|                                                                  |
//| Estrategia: seguir a tendencia e entrar no recuo (pullback)      |
//|  - Tendencia: EMA 50 acima da EMA 200 = so compra;               |
//|               EMA 50 abaixo da EMA 200 = so vende.               |
//|  - Entrada: o ultimo candle fechado tocou a EMA 20 (recuo) e o   |
//|    RSI(14) voltou a favor da tendencia (compra: cruzou 40 para   |
//|    cima; venda: cruzou 60 para baixo).                           |
//|  - Saida: stop = 1,5 x ATR(14), alvo = 3 x ATR(14);              |
//|    depois de +1,5 ATR de lucro o stop segue o preco (stop movel).|
//|  - Risco: % do saldo por operacao (padrao 1%), ou lote fixo.     |
//|  - Filtros: horario, spread maximo, 1 posicao por simbolo.       |
//|                                                                  |
//| AVISO: nenhum robo garante lucro. A maioria das contas de forex  |
//| de varejo perde dinheiro. Teste no Strategy Tester e na conta    |
//| DEMO antes de usar dinheiro real.                                |
//+------------------------------------------------------------------+
#property copyright "RoboTendencia"
#property version   "1.00"
#property description "Tendencia (EMA 50/200) + recuo na EMA 20 + RSI, stop/alvo por ATR, stop movel, risco por % do saldo."

#include <Trade\Trade.mqh>

//--- parametros (podem ser mudados na janela do robo no MT5)
input ENUM_TIMEFRAMES InpTF          = PERIOD_H1; // Tempo grafico da analise
input int             InpFastEMA     = 20;        // EMA do recuo
input int             InpTrendEMA    = 50;        // EMA da tendencia
input int             InpSlowEMA     = 200;       // EMA lenta
input int             InpRSIPeriod   = 14;        // Periodo do RSI
input double          InpRSIBuy      = 40.0;      // RSI cruza para cima (compra)
input double          InpRSISell     = 60.0;      // RSI cruza para baixo (venda)
input int             InpATRPeriod   = 14;        // Periodo do ATR
input double          InpSL_ATR      = 1.5;       // Stop = ATR x
input double          InpTP_ATR      = 3.0;       // Alvo = ATR x
input double          InpTrail_ATR   = 1.5;       // Stop movel: distancia em ATR (0 = desliga)
input double          InpRiskPercent = 1.0;       // Risco por operacao (% do saldo)
input double          InpFixedLot    = 0.0;       // Lote fixo (>0 ignora o risco %)
input int             InpMaxSpread   = 30;        // Spread maximo (pontos)
input int             InpStartHour   = 7;         // Hora inicial (horario do servidor)
input int             InpEndHour     = 20;        // Hora final (horario do servidor)
input int             InpMaxDailyLossPct = 3;     // Para de operar no dia se perder X% (0 = desliga)
input ulong           InpMagic       = 20261118;  // Numero magico do robo

CTrade   trade;
int      hFast = INVALID_HANDLE, hTrend = INVALID_HANDLE, hSlow = INVALID_HANDLE;
int      hRSI = INVALID_HANDLE, hATR = INVALID_HANDLE;
datetime lastBar = 0;
double   dayStartBalance = 0.0;
int      dayOfYear = -1;

//+------------------------------------------------------------------+
int OnInit()
  {
   hFast  = iMA(_Symbol, InpTF, InpFastEMA, 0, MODE_EMA, PRICE_CLOSE);
   hTrend = iMA(_Symbol, InpTF, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
   hSlow  = iMA(_Symbol, InpTF, InpSlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   hRSI   = iRSI(_Symbol, InpTF, InpRSIPeriod, PRICE_CLOSE);
   hATR   = iATR(_Symbol, InpTF, InpATRPeriod);
   if(hFast == INVALID_HANDLE || hTrend == INVALID_HANDLE || hSlow == INVALID_HANDLE ||
      hRSI == INVALID_HANDLE || hATR == INVALID_HANDLE)
     {
      Print("RoboTendencia: nao consegui criar os indicadores");
      return(INIT_FAILED);
     }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);
   trade.SetTypeFillingBySymbol(_Symbol);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hFast);
   IndicatorRelease(hTrend);
   IndicatorRelease(hSlow);
   IndicatorRelease(hRSI);
   IndicatorRelease(hATR);
  }

//+------------------------------------------------------------------+
//| Copia 'count' valores do indicador a partir do candle 'shift'    |
//| (arr[0] = candle 'shift', arr[1] = o anterior...)                |
//+------------------------------------------------------------------+
bool GetBuf(const int handle, const int shift, const int count, double &arr[])
  {
   ArraySetAsSeries(arr, true);
   return(CopyBuffer(handle, 0, shift, count, arr) == count);
  }

//+------------------------------------------------------------------+
double NormalizeLot(double lot)
  {
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double st = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(st <= 0) st = 0.01;
   lot = MathFloor(lot / st) * st;
   if(lot < mn) lot = mn;
   if(lot > mx) lot = mx;
   return(NormalizeDouble(lot, 2));
  }

//+------------------------------------------------------------------+
//| Lote para arriscar InpRiskPercent do saldo com o stop slDist     |
//+------------------------------------------------------------------+
double LotForRisk(const double slDist)
  {
   if(InpFixedLot > 0) return(NormalizeLot(InpFixedLot));
   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0 || tickSize <= 0 || slDist <= 0)
      return(NormalizeLot(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN)));
   double riskMoney  = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
   double lossPerLot = slDist / tickSize * tickValue;
   if(lossPerLot <= 0) return(NormalizeLot(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN)));
   return(NormalizeLot(riskMoney / lossPerLot));
  }

//+------------------------------------------------------------------+
bool HasMyPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
//| Stop movel: depois de +InpTrail_ATR x ATR, o stop segue o preco  |
//+------------------------------------------------------------------+
void TrailPositions()
  {
   if(InpTrail_ATR <= 0) return;
   double atr[];
   if(!GetBuf(hATR, 1, 1, atr)) return;
   double dist = InpTrail_ATR * atr[0];
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      long   type = PositionGetInteger(POSITION_TYPE);
      if(type == POSITION_TYPE_BUY)
        {
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         if(bid - open >= dist)
           {
            double newSL = NormalizeDouble(bid - dist, _Digits);
            if(newSL > sl + point) trade.PositionModify(ticket, newSL, tp);
           }
        }
      else if(type == POSITION_TYPE_SELL)
        {
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         if(open - ask >= dist)
           {
            double newSL = NormalizeDouble(ask + dist, _Digits);
            if(sl == 0 || newSL < sl - point) trade.PositionModify(ticket, newSL, tp);
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| Limite de perda do dia (% do saldo do inicio do dia)            |
//+------------------------------------------------------------------+
bool DailyLossHit()
  {
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   if(t.day_of_year != dayOfYear)
     {
      dayOfYear = t.day_of_year;
      dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
     }
   if(InpMaxDailyLossPct <= 0 || dayStartBalance <= 0) return(false);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   return((dayStartBalance - equity) / dayStartBalance * 100.0 >= InpMaxDailyLossPct);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   TrailPositions();

   datetime bar = iTime(_Symbol, InpTF, 0);
   if(bar == lastBar) return;               // decide uma vez por candle
   lastBar = bar;

   if(DailyLossHit()) return;
   if(HasMyPosition()) return;

   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   if(t.hour < InpStartHour || t.hour >= InpEndHour) return;
   if(SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) > InpMaxSpread) return;

   double fast[], trend[], slow[], rsi[], atr[];
   if(!GetBuf(hFast, 1, 1, fast) || !GetBuf(hTrend, 1, 1, trend) || !GetBuf(hSlow, 1, 1, slow) ||
      !GetBuf(hRSI, 1, 2, rsi) || !GetBuf(hATR, 1, 1, atr))
      return;

   double high1  = iHigh(_Symbol, InpTF, 1);
   double low1   = iLow(_Symbol, InpTF, 1);
   double close1 = iClose(_Symbol, InpTF, 1);
   double a      = atr[0];
   if(a <= 0) return;

   bool upTrend   = trend[0] > slow[0];
   bool downTrend = trend[0] < slow[0];
   bool touched   = (low1 <= fast[0] && high1 >= fast[0]);
   bool rsiUp     = (rsi[1] < InpRSIBuy && rsi[0] >= InpRSIBuy);     // rsi[1] = candle 2, rsi[0] = candle 1
   bool rsiDown   = (rsi[1] > InpRSISell && rsi[0] <= InpRSISell);

   if(upTrend && touched && rsiUp && close1 > fast[0])
     {
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl  = NormalizeDouble(ask - InpSL_ATR * a, _Digits);
      double tp  = NormalizeDouble(ask + InpTP_ATR * a, _Digits);
      double lot = LotForRisk(ask - sl);
      if(!trade.Buy(lot, _Symbol, ask, sl, tp, "RoboTendencia compra"))
         Print("RoboTendencia: compra recusada ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
     }
   else if(downTrend && touched && rsiDown && close1 < fast[0])
     {
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double sl  = NormalizeDouble(bid + InpSL_ATR * a, _Digits);
      double tp  = NormalizeDouble(bid - InpTP_ATR * a, _Digits);
      double lot = LotForRisk(sl - bid);
      if(!trade.Sell(lot, _Symbol, bid, sl, tp, "RoboTendencia venda"))
         Print("RoboTendencia: venda recusada ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
     }
  }
//+------------------------------------------------------------------+
