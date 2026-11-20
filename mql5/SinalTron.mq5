//+------------------------------------------------------------------+
//| SinalTron.mq5  -  INDICADOR de sinais de COMPRA e VENDA          |
//|                                                                  |
//| - Le o grafico (qualquer par / tempo) e da uma nota de -9 a +9:  |
//|   tendencia (EMA 50 e 200), ultimos 10 candles, topos e fundos,  |
//|   EMA 20, padroes de candle (engolfo, martelo, estrela cadente)  |
//|   e candle de forca.                                             |
//| - Nota >= +InpMinScore = seta de COMPRA; <= -InpMinScore = VENDA.|
//| - Cada sinal vem com ENTRADA, ALVO e STOP (pelo ATR).            |
//| - Manda o sinal para o CELULAR (app MetaTrader 5) e da alerta.   |
//| - MAXIMO RETORNO: a cada candle novo testa 100 ajustes (forca,   |
//|   alvo, stop) no historico e usa o que MAIS RENDEU POR DIA.      |
//| - PAINEL com o ACERTO REAL dos sinais no historico do grafico    |
//|   (quantos bateram o alvo antes do stop) - nao e promessa, e o   |
//|   que aconteceu no passado.                                      |
//|                                                                  |
//| Para receber no celular: no MT5 do PC, Ferramentas > Opcoes >    |
//| Notificacoes > marque "Ativar notificacoes push" e coloque o     |
//| MetaQuotes ID que aparece no app MT5 do celular (Configuracoes > |
//| Mensagens).                                                      |
//|                                                                  |
//| AVISO: nenhum indicador garante acerto nem lucro. Resultado      |
//| passado nao garante resultado futuro.                            |
//+------------------------------------------------------------------+
#property copyright "SinalTron"
#property version   "1.10"
#property description "SinalTron - sinais de compra e venda o dia inteiro, com alvo e stop, alerta no celular e acerto real no historico."
#property indicator_chart_window
#property indicator_buffers 2
#property indicator_plots   2
#property indicator_label1  "COMPRA"
#property indicator_type1   DRAW_ARROW
#property indicator_color1  clrLime
#property indicator_width1  3
#property indicator_label2  "VENDA"
#property indicator_type2   DRAW_ARROW
#property indicator_color2  clrRed
#property indicator_width2  3

input int    InpMinScore   = 5;      // Forca minima do sinal (maior = menos sinais, mais filtrados)
input double InpTPATR      = 1.0;    // ALVO = ATR x isto
input double InpSLATR      = 1.5;    // STOP = ATR x isto (stop maior que o alvo = acerta mais, ganha menos por sinal)
input int    InpMaxBars    = 30;     // Para o historico: o sinal tem ate X candles para bater alvo ou stop
input int    InpStatsBars  = 3000;   // Quantos candles do historico usar no calculo do acerto
input bool   InpPush       = true;   // Mandar o sinal para o CELULAR (app MetaTrader 5)
input bool   InpAlert      = true;   // Alerta com som no PC
input bool   InpTrendOnly  = true;   // So sinais a favor da tendencia (EMA 50 x EMA 200) - mais acerto
input bool   InpAutoTune   = true;   // MAXIMO RETORNO: testa forca/alvo/stop no historico e usa o que mais rendeu por dia
input double InpRiskPct    = 2.0;    // Risco por sinal (% do saldo) usado para calcular o retorno

double BufBuy[], BufSell[];
int    hE20 = INVALID_HANDLE, hE50 = INVALID_HANDLE, hE200 = INVALID_HANDLE, hATR = INVALID_HANDLE;
datetime gLastSignalBar = 0;
int      gMin = 5;                    // ajuste em uso (o melhor do historico quando InpAutoTune)
double   gTP = 1.0, gSL = 1.5;
datetime gTunedBar = 0;
double   gDayRet = 0, gTuneRate = 0;
int      gTuneN = 0;
double   gSc[];                        // nota de cada candle

//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0, BufBuy, INDICATOR_DATA);
   SetIndexBuffer(1, BufSell, INDICATOR_DATA);
   PlotIndexSetInteger(0, PLOT_ARROW, 233);
   PlotIndexSetInteger(1, PLOT_ARROW, 234);
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   hE20 = iMA(_Symbol, _Period, 20, 0, MODE_EMA, PRICE_CLOSE);
   hE50 = iMA(_Symbol, _Period, 50, 0, MODE_EMA, PRICE_CLOSE);
   hE200 = iMA(_Symbol, _Period, 200, 0, MODE_EMA, PRICE_CLOSE);
   hATR = iATR(_Symbol, _Period, 14);
   if(hE20 == INVALID_HANDLE || hE50 == INVALID_HANDLE || hE200 == INVALID_HANDLE || hATR == INVALID_HANDLE)
      return(INIT_FAILED);
   IndicatorSetString(INDICATOR_SHORTNAME, "SinalTron");
   gMin = InpMinScore; gTP = InpTPATR; gSL = InpSLATR;
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   IndicatorRelease(hE20); IndicatorRelease(hE50); IndicatorRelease(hE200); IndicatorRelease(hATR);
   ObjectsDeleteAll(0, "ST_");
  }

//+------------------------------------------------------------------+
//| Nota do candle i (indice normal: 0 = mais antigo)                |
//+------------------------------------------------------------------+
int Score(int i, const double &o[], const double &h[], const double &l[], const double &c[],
          const double &e20[], const double &e50[], const double &e200[], const double &atr[], string &name)
  {
   name = "";
   double a = atr[i];
   if(a <= 0) return(0);
   int sc = 0;
   //--- tendencia
   if(c[i] > e50[i] && e50[i] > e200[i]) sc += 2;
   else if(c[i] < e50[i] && e50[i] < e200[i]) sc -= 2;
   else sc += (c[i] > e50[i]) ? 1 : -1;
   //--- ultimos 10 candles
   int bulls = 0, bears = 0;
   for(int k = 0; k < 10; k++)
     {
      if(c[i - k] > o[i - k]) bulls++; else if(c[i - k] < o[i - k]) bears++;
     }
   if(bulls - bears >= 3) sc++; else if(bears - bulls >= 3) sc--;
   //--- topos e fundos
   int hh = 0, ll = 0;
   for(int k = 0; k < 4; k++)
     {
      if(h[i - k] > h[i - k - 1] && l[i - k] > l[i - k - 1]) hh++;
      if(h[i - k] < h[i - k - 1] && l[i - k] < l[i - k - 1]) ll++;
     }
   if(hh >= 2 && hh > ll) sc++; else if(ll >= 2 && ll > hh) sc--;
   //--- EMA 20
   sc += (c[i] > e20[i]) ? 1 : -1;
   //--- padroes de candle
   double rng = h[i] - l[i], body = MathAbs(c[i] - o[i]), body2 = MathAbs(c[i - 1] - o[i - 1]);
   double upW = h[i] - MathMax(o[i], c[i]), dnW = MathMin(o[i], c[i]) - l[i];
   if(rng >= 0.5 * a)
     {
      if(c[i - 1] < o[i - 1] && c[i] > o[i] && c[i] > o[i - 1] && o[i] <= c[i - 1] && body >= 1.2 * body2) { sc += 2; name = "engolfo de alta"; }
      else if(c[i - 1] > o[i - 1] && c[i] < o[i] && c[i] < o[i - 1] && o[i] >= c[i - 1] && body >= 1.2 * body2) { sc -= 2; name = "engolfo de baixa"; }
      else if(dnW >= 2 * body && dnW >= 0.6 * rng && c[i] >= o[i]) { sc += 2; name = "martelo"; }
      else if(upW >= 2 * body && upW >= 0.6 * rng && c[i] <= o[i]) { sc -= 2; name = "estrela cadente"; }
     }
   //--- candle de forca
   if(body >= 0.6 * a) sc += (c[i] > o[i]) ? 1 : -1;
   return(sc);
  }

//--- sinal no candle i: 1 compra, -1 venda, 0 nada (so quando a nota CRUZA o minimo: nao repete)
int SignalAt(int i, const double &o[], const double &h[], const double &l[], const double &c[],
             const double &e20[], const double &e50[], const double &e200[], const double &atr[], string &name, int &score)
  {
   string n2;
   score = Score(i, o, h, l, c, e20, e50, e200, atr, name);
   int prev = Score(i - 1, o, h, l, c, e20, e50, e200, atr, n2);
   bool upTrend = e50[i] > e200[i], dnTrend = e50[i] < e200[i];
   if(score >= gMin && prev < gMin && (!InpTrendOnly || upTrend)) return(1);
   if(score <= -gMin && prev > -gMin && (!InpTrendOnly || dnTrend)) return(-1);
   return(0);
  }

//+------------------------------------------------------------------+
//| Painel                                                           |
//+------------------------------------------------------------------+
void Label(const string n, int y, const string text, color c, int size = 9, const string font = "Consolas")
  {
   if(ObjectFind(0, n) < 0)
     {
      ObjectCreate(0, n, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, n, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
      ObjectSetInteger(0, n, OBJPROP_ANCHOR, ANCHOR_RIGHT_UPPER);
      ObjectSetInteger(0, n, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, n, OBJPROP_HIDDEN, true);
      ObjectSetInteger(0, n, OBJPROP_XDISTANCE, 14);
     }
   ObjectSetInteger(0, n, OBJPROP_YDISTANCE, y);
   ObjectSetString(0, n, OBJPROP_FONT, font);
   ObjectSetInteger(0, n, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, n, OBJPROP_COLOR, c);
   ObjectSetString(0, n, OBJPROP_TEXT, text);
  }

void Panel(int wins, int losses, double growth, const string last)
  {
   int tot = wins + losses;
   double rate = tot > 0 ? 100.0 * wins / tot : 0;
   if(ObjectFind(0, "ST_BG") < 0)
     {
      ObjectCreate(0, "ST_BG", OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, "ST_BG", OBJPROP_CORNER, CORNER_RIGHT_UPPER);
      ObjectSetInteger(0, "ST_BG", OBJPROP_XDISTANCE, 390);
      ObjectSetInteger(0, "ST_BG", OBJPROP_YDISTANCE, 20);
      ObjectSetInteger(0, "ST_BG", OBJPROP_XSIZE, 380);
      ObjectSetInteger(0, "ST_BG", OBJPROP_YSIZE, 140);
      ObjectSetInteger(0, "ST_BG", OBJPROP_BGCOLOR, C'14,16,26');
      ObjectSetInteger(0, "ST_BG", OBJPROP_COLOR, C'255,196,0');
      ObjectSetInteger(0, "ST_BG", OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, "ST_BG", OBJPROP_SELECTABLE, false);
     }
   Label("ST_T", 28, "SINALTRON  " + _Symbol + " " + EnumToString((ENUM_TIMEFRAMES)_Period), C'255,196,0', 12, "Arial Black");
   Label("ST_1", 56, "Acerto no historico: " + (tot > 0 ? DoubleToString(rate, 0) + "%" : "-") + " (" + IntegerToString(wins) + " G / " +
         IntegerToString(losses) + " P)", rate >= 60 ? clrLime : (rate >= 50 ? C'255,196,0' : C'255,80,90'), 10);
   Label("ST_2", 78, (InpAutoTune ? "MELHOR AJUSTE: " : "") + "alvo " + DoubleToString(gTP, 1) + " ATR / stop " + DoubleToString(gSL, 1) +
         " ATR / forca " + IntegerToString(gMin), C'210,214,230');
   Label("ST_3", 98, "Arriscando " + DoubleToString(InpRiskPct, 1) + "% por sinal: " + (growth >= 0 ? "+" : "") + DoubleToString(growth, 0) +
         "%  (~" + DoubleToString(gDayRet, 2) + "% por dia)", growth >= 0 ? clrLime : C'255,80,90');
   Label("ST_4", 118, "Ultimo: " + last, C'120,126,150', 8);
   Label("ST_5", 136, InpPush ? "Celular: LIGADO (app MT5)" : "Celular: desligado", InpPush ? clrLime : C'120,126,150', 8);
  }

//+------------------------------------------------------------------+
//| Resultado de um ajuste no historico: crescimento composto        |
//+------------------------------------------------------------------+
double Simulate(int mn, double tpA, double slA, int from, int to, const double &h[], const double &l[], const double &c[],
                const double &e50[], const double &e200[], const double &atr[], int &w, int &ls)
  {
   double g = 1.0;
   w = 0; ls = 0;
   for(int i = from; i < to; i++)
     {
      int dir = 0;
      if(gSc[i] >= mn && gSc[i - 1] < mn && (!InpTrendOnly || e50[i] > e200[i])) dir = 1;
      else if(gSc[i] <= -mn && gSc[i - 1] > -mn && (!InpTrendOnly || e50[i] < e200[i])) dir = -1;
      if(dir == 0) continue;
      double tp = c[i] + dir * tpA * atr[i], sl = c[i] - dir * slA * atr[i];
      for(int k = i + 1; k < MathMin(to + 1, i + 1 + InpMaxBars); k++)
        {
         if((dir == 1) ? (l[k] <= sl) : (h[k] >= sl)) { ls++; g *= 1.0 - InpRiskPct / 100.0; break; }
         if((dir == 1) ? (h[k] >= tp) : (l[k] <= tp)) { w++; g *= 1.0 + InpRiskPct / 100.0 * tpA / slA; break; }
        }
     }
   return(g);
  }

//--- testa varias combinacoes e fica com a de MAIOR RETORNO POR DIA (precisa de pelo menos 20 sinais)
void Tune(int from, int to, const datetime &t[], const double &h[], const double &l[], const double &c[],
          const double &e50[], const double &e200[], const double &atr[])
  {
   double days = MathMax(1.0, (double)(t[to] - t[from]) / 86400.0);
   double bestG = -1;
   int mins[] = {3, 4, 5, 6, 7};
   double tps[] = {0.5, 1.0, 1.5, 2.0, 3.0};
   double sls[] = {0.5, 1.0, 1.5, 2.0};
   for(int a = 0; a < ArraySize(mins); a++)
      for(int b = 0; b < ArraySize(tps); b++)
         for(int d = 0; d < ArraySize(sls); d++)
           {
            int w, ls;
            double g = Simulate(mins[a], tps[b], sls[d], from, to, h, l, c, e50, e200, atr, w, ls);
            if(w + ls < 20) continue;
            if(g > bestG) { bestG = g; gMin = mins[a]; gTP = tps[b]; gSL = sls[d]; gTuneN = w + ls; gTuneRate = 100.0 * w / (w + ls); }
           }
   gDayRet = bestG > 0 ? (MathPow(bestG, 1.0 / days) - 1.0) * 100.0 : 0;
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated, const datetime &time[],
                const double &open[], const double &high[], const double &low[], const double &close[],
                const long &tick_volume[], const long &volume[], const int &spread[])
  {
   if(rates_total < 260) return(0);
   double e20[], e50[], e200[], atr[];
   if(CopyBuffer(hE20, 0, 0, rates_total, e20) != rates_total) return(0);
   if(CopyBuffer(hE50, 0, 0, rates_total, e50) != rates_total) return(0);
   if(CopyBuffer(hE200, 0, 0, rates_total, e200) != rates_total) return(0);
   if(CopyBuffer(hATR, 0, 0, rates_total, atr) != rates_total) return(0);

   //--- nota de todos os candles (uma vez por candle novo) e o melhor ajuste
   ArrayResize(gSc, rates_total);
   int from0 = MathMax(220, rates_total - 1 - InpStatsBars);
   string nm;
   for(int i = (prev_calculated > 0 ? MathMax(from0 - 1, prev_calculated - 2) : from0 - 1); i < rates_total; i++)
      gSc[i] = Score(i, open, high, low, close, e20, e50, e200, atr, nm);
   bool retune = false;
   if(InpAutoTune && time[rates_total - 1] != gTunedBar)
     {
      gTunedBar = time[rates_total - 1];
      int oM = gMin; double oT = gTP, oS = gSL;
      Tune(from0, rates_total - 2, time, high, low, close, e50, e200, atr);
      retune = (oM != gMin || oT != gTP || oS != gSL);
     }
   int start = (prev_calculated > 0 && !retune) ? prev_calculated - 1 : 220;
   start = MathMax(start, MathMax(220, rates_total - InpStatsBars - InpMaxBars));
   if(prev_calculated == 0 || retune)
      for(int i = 0; i < rates_total; i++) { BufBuy[i] = EMPTY_VALUE; BufSell[i] = EMPTY_VALUE; }

   //--- setas so nos candles FECHADOS (o ultimo, rates_total-1, ainda esta formando)
   string name;
   int score;
   for(int i = start; i < rates_total - 1; i++)
     {
      BufBuy[i] = EMPTY_VALUE; BufSell[i] = EMPTY_VALUE;
      int s = SignalAt(i, open, high, low, close, e20, e50, e200, atr, name, score);
      if(s == 1) BufBuy[i] = low[i] - 0.3 * atr[i];
      if(s == -1) BufSell[i] = high[i] + 0.3 * atr[i];
     }
   BufBuy[rates_total - 1] = EMPTY_VALUE; BufSell[rates_total - 1] = EMPTY_VALUE;

   //--- ACERTO REAL no historico: cada sinal bateu o alvo antes do stop?
   int wins = 0, losses = 0;
   double growth = 1.0;
   string last = "-";
   int from = MathMax(220, rates_total - 1 - InpStatsBars);
   for(int i = from; i < rates_total - 1; i++)
     {
      int dir = (BufBuy[i] != EMPTY_VALUE) ? 1 : ((BufSell[i] != EMPTY_VALUE) ? -1 : 0);
      if(dir == 0) continue;
      double entry = close[i], tp = entry + dir * gTP * atr[i], sl = entry - dir * gSL * atr[i];
      last = (dir == 1 ? "COMPRA " : "VENDA ") + TimeToString(time[i], TIME_DATE | TIME_MINUTES);
      for(int k = i + 1; k < MathMin(rates_total, i + 1 + InpMaxBars); k++)
        {
         bool hitSL = (dir == 1) ? (low[k] <= sl) : (high[k] >= sl);
         bool hitTP = (dir == 1) ? (high[k] >= tp) : (low[k] <= tp);
         if(hitSL) { losses++; growth *= 1.0 - InpRiskPct / 100.0; break; }                 // mesmo candle: conta como perda
         if(hitTP) { wins++; growth *= 1.0 + InpRiskPct / 100.0 * gTP / gSL; break; }
        }
     }
   Panel(wins, losses, (growth - 1.0) * 100.0, last);

   //--- sinal NOVO no candle que acabou de fechar: alerta + celular (uma vez por candle)
   int i = rates_total - 2;
   if(time[i] != gLastSignalBar && prev_calculated > 0)
     {
      int s = SignalAt(i, open, high, low, close, e20, e50, e200, atr, name, score);
      if(s != 0)
        {
         gLastSignalBar = time[i];
         double entry = close[i], tp = entry + s * gTP * atr[i], sl = entry - s * gSL * atr[i];
         string msg = "SINALTRON " + (s == 1 ? "COMPRA " : "VENDA ") + _Symbol + " " + EnumToString((ENUM_TIMEFRAMES)_Period) +
                      " | entrada " + DoubleToString(entry, _Digits) + " | alvo " + DoubleToString(tp, _Digits) +
                      " | stop " + DoubleToString(sl, _Digits) + " | nota " + IntegerToString(score) + (name != "" ? " " + name : "");
         if(InpAlert) Alert(msg);
         if(InpPush && !SendNotification(msg)) Print("SinalTron: celular nao recebeu (ative as notificacoes push no MT5)");
        }
     }
   return(rates_total);
  }
//+------------------------------------------------------------------+
