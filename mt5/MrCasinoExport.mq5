//+------------------------------------------------------------------+
//|                                              MrCasinoExport.mq5 |
//|   Скрипт: выгрузка свечей и status.json робота для сверки зон    |
//|   движка анализа с роботом (python -m tools.parity).             |
//|   Запускать на графике золота, пока робот работает: скрипт       |
//|   пишет всё в Common\Files\MrCasino\parity\ за один момент.      |
//+------------------------------------------------------------------+
#property copyright "Consigliere"
#property version   "1.00"
#property description "Выгрузка свечей M5/M15/H1/H4/D1 и status.json робота для сверки зон (tools/parity.py)"
#property script_show_inputs

input string InpSymbol    = "";       // Символ (пусто = символ графика)
input long   InpMagic     = 777001;   // Magic робота (для пути к его status.json)
input int    InpServerUtc = -99;      // UTC-сдвиг сервера в часах; -99 = авто, как у робота
input int    InpM5Bars    = 900;      // Столько свечей читает робот (ReplayZone)
input int    InpM15Bars   = 700;      // MaxOverlap
input int    InpH1Bars    = 200;
input int    InpH4Bars    = 100;
input int    InpD1Bars    = 60;

int GmtOffset()
  {
   if(InpServerUtc != -99) return InpServerUtc * 3600;
   double off = (double)(TimeCurrent() - TimeGMT());
   return (int)(MathRound(off / 900.0) * 900.0);
  }

// CSV: time (epoch UTC), open, high, low, close; от старых свечей к новым, последняя строка — формирующаяся
bool ExportTF(string sym, ENUM_TIMEFRAMES tf, string name, int n, int off, int digits)
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int got = CopyRates(sym, tf, 0, n, r);
   if(got <= 0)
     {
      Print("Нет свечей ", sym, " ", name, ", err ", GetLastError());
      return false;
     }
   int h = FileOpen("MrCasino\\parity\\" + name + ".csv", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
     {
      Print("Не открыть файл ", name, ".csv, err ", GetLastError());
      return false;
     }
   FileWriteString(h, "time,open,high,low,close\n");
   for(int i = 0; i < got; i++)
      FileWriteString(h, IntegerToString((long)(r[i].time - off)) + "," + DoubleToString(r[i].open, digits) + "," +
                      DoubleToString(r[i].high, digits) + "," + DoubleToString(r[i].low, digits) + "," +
                      DoubleToString(r[i].close, digits) + "\n");
   FileClose(h);
   Print(name, ": ", got, " свечей");
   return true;
  }

void OnStart()
  {
   string sym = InpSymbol == "" ? _Symbol : InpSymbol;
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   int off = GmtOffset();
   FolderCreate("MrCasino", FILE_COMMON);
   FolderCreate("MrCasino\\parity", FILE_COMMON);
   bool ok = ExportTF(sym, PERIOD_M5, "M5", InpM5Bars, off, digits);
   ok = ExportTF(sym, PERIOD_M15, "M15", InpM15Bars, off, digits) && ok;
   ok = ExportTF(sym, PERIOD_H1, "H1", InpH1Bars, off, digits) && ok;
   ok = ExportTF(sym, PERIOD_H4, "H4", InpH4Bars, off, digits) && ok;
   ok = ExportTF(sym, PERIOD_D1, "D1", InpD1Bars, off, digits) && ok;

   string meta = "{\"symbol\":\"" + sym + "\",\"digits\":" + IntegerToString(digits) + ",\"bid\":" +
                 DoubleToString(SymbolInfoDouble(sym, SYMBOL_BID), digits) + ",\"ask\":" +
                 DoubleToString(SymbolInfoDouble(sym, SYMBOL_ASK), digits) + ",\"now_utc\":" +
                 IntegerToString((long)TimeGMT()) + ",\"server_offset_sec\":" + IntegerToString(off) + "}";
   int hm = FileOpen("MrCasino\\parity\\meta.json", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(hm != INVALID_HANDLE)
     {
      FileWriteString(hm, meta);
      FileClose(hm);
     }

   // status.json робота в тот же момент: так сравниваются зоны на одном и том же срезе
   string robot = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "_" + IntegerToString(InpMagic);
   string src = "MrCasino\\" + robot + "\\status.json";
   if(FileIsExist(src, FILE_COMMON))
     {
      if(!FileCopy(src, FILE_COMMON, "MrCasino\\parity\\status.json", FILE_REWRITE | FILE_COMMON))
         Print("Не скопировать status.json, err ", GetLastError());
     }
   else
      Print("status.json робота не найден: ", src, " (робот запущен? InpMagic верный?)");
   Print(ok ? "Выгрузка готова: Common\\Files\\MrCasino\\parity. Дальше: python -m tools.parity"
            : "Выгрузка неполная, см. сообщения выше");
  }
