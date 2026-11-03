//+------------------------------------------------------------------+
//|                                                    MrCasino.mq5 |
//|   Советник MT5 по стратегии Mr. Casino (Flashcards), золото      |
//|   v3: только золото, стоп True Stop, свечи и дисциплина из книги |
//|   "Oddiy savdo kitobi". Разделы ТЗ v2 помечены как [§N].         |
//|   v3.2: 5 сделок в день, тейк RR 1:2, риск 5% в день (1% на      |
//|   сделку + дневной лимит убытка), слабые подтверждения весь день,|
//|   новые триггеры F (FU из 3 свечей), S (пробой хая/лоя, книга),  |
//|   HCS x2/x3, книга: 3 солдата / 3 вороны, повешенный, волчок.    |
//|                                                                  |
//|   v3.3: файловый мост с ассистентом (status.json, events, cmd),   |
//|   таймер, команды из Telegram (после «да» ассистента), тревоги,  |
//|   скрины, настройки g_set. Торговая логика не менялась.          |
//|                                                                  |
//|   Единицы: 1 пункт = $1 цены золота (пороги в ценовых единицах). |
//|   Внутри паттерны и зоны считаются для BUY ("бычья логика");     |
//|   для SELL цены зеркалятся (аксессоры Ov/Hv/Lv/Cv с d = -1).     |
//+------------------------------------------------------------------+
#property copyright "Consigliere"
#property version   "3.30"
#property description "Mr. Casino v3.3 (золото, мост с ассистентом): зоны OB/FU/ликвидность -> манипуляция A-D, F, S + свечи из книги (K) -> стоп True Stop, тейк RR 1:2, 5 сделок в день, риск 5% в день"

#include <Trade\Trade.mqh>

enum ENUM_RISK_MODE { RISK_FIXED = 0, RISK_PERCENT = 1, RISK_HALF = 2, RISK_FULL = 3 };
enum ENUM_MIN_FU    { FU_CLASS_A = 3, FU_CLASS_B = 2, FU_CLASS_C = 1 };
enum ENUM_TP_MODE   { TP_RR = 0, TP_LAL = 1 };

//--- прототипы (мост v3.3, определены ниже)
void Emit(string type, int level, string severity, string fields, string text, int shotTf);
void WriteStatus();
string BridgeLine();

//--- входные параметры [§11]
input group "Символ и метка"
input string          InpSymbol         = "";           // Символ (пусто = график; только золото)
input long            InpMagic          = 777001;       // Magic Number

input group "Риск и лот [§2]"
input ENUM_RISK_MODE  InpRiskMode       = RISK_PERCENT; // Режим риска
input double          InpFixedLot       = 0.05;         // Лот для FIXED
input double          InpRiskPercent    = 0.0;          // % баланса на сделку (0 = риск в день / сделок в день = 1%)
input double          InpDailyRiskPct   = 5.0;          // Риск в день: макс. убыток за день, % баланса (0 = выкл)
input bool            InpMinLotIfFits   = true;         // Лот по риску меньше минимального — мин. лот, если влезает в дневной риск
input double          InpMarginReserve  = 30.0;         // Запас свободной маржи, %

input group "Направления и баллы [§4, §6.2]"
input bool            InpTradeBuy       = true;         // Разрешить BUY
input bool            InpTradeSell      = true;         // Разрешить SELL
input bool            InpAllowCounter   = true;         // Сделки COUNTER (против D1 и H4)
input int             InpMinScoreWith   = 2;            // Мин. балл зоны: WITH
input int             InpMinScoreMixed  = 3;            // Мин. балл зоны: MIXED / NEUTRAL
input int             InpMinScoreCounter= 5;            // Мин. балл зоны: COUNTER
input int             InpRepeatM15Bars  = 2;            // Пауза после стопа в ту же сторону, свечей M15

input group "Зоны [§6]"
input double          InpZoneRadiusPt   = 60.0;         // Радиус поиска зон от цены, пунктов
input int             InpMaxZonesSide   = 3;            // Зон на сторону
input bool            InpUseM15OB       = true;         // OB на M15
input double          InpOBWickPct      = 45.0;         // Макс. сумма теней свечи OB, %
input int             InpOBMoveBars     = 6;            // Свечей на уход цены от OB
input bool            InpOBNeedFU       = false;        // FU после OB обязателен
input double          InpFUZoneMinWickPt= 3.0;          // Мин. тень FU для зоны из тени
input double          InpSweepZonePt    = 6.0;          // Зона за уровнем ликвидности
input double          InpEqTolPt        = 1.0;          // Допуск равных хаёв/лоёв
input double          InpZoneMaxPt      = 10.0;         // Макс. ширина зоны
input double          InpZoneMinPt      = 3.0;          // Мин. ширина зоны
input double          InpConfirmAwayPt  = 10.0;         // Отход от зоны для CONFIRMED (ретест)
input double          InpInvalidBeyondPt= 2.0;          // Закрытие M15 за дальним краем для INVALID
input int             InpZoneLifeHours  = 48;           // Срок жизни зоны, часов
input int             InpZoneLifeM15    = 24;           // Срок жизни зоны M15, часов

input group "Паттерны [§5]"
input double          InpDojiBodyPct    = 25.0;         // Тело Doji, % диапазона
input double          InpWickPct        = 40.0;         // Тень у FU / сигнала D, %
input double          InpLqMinPt        = 0.3;          // Прокол экстремума, пунктов
input ENUM_MIN_FU     InpMinFUClass     = FU_CLASS_B;   // Мин. класс FU для триггера A
input double          InpHCSTolPt       = 1.0;          // Допуск HCS, пунктов
input double          InpPierceMaxPt    = 6.0;          // Допуск прокола за край зоны, пунктов
input int             InpTrigLookback   = 3;            // Свечей M5 на поиск прокола (A)
input double          InpSweepMaxPt     = 12.0;         // Заход (стоп-хант) за край зоны: до N пунктов
input int             InpSweepBars      = 6;            // D: заход за уровень — до N свечей M5

input group "Триггеры [§7]"
input bool            InpUseTrigA       = true;         // A: Manipulation Cycle
input bool            InpUseTrigB       = true;         // B: HCS (x2/x3 — сильный)
input bool            InpUseTrigC       = true;         // C: Negation
input bool            InpUseTrigD       = true;         // D: снятие ликвидности
input bool            InpUseTrigF       = true;         // F: FU из трёх свечей
input bool            InpUseTrigS       = true;         // S: пробой предыдущего хая/лоя после касания зоны (книга)
input bool            InpUseBook        = true;         // K: свечи из книги (молот/звезда, поглощение, звёзды, 3 солдата/вороны, повешенный)
input bool            InpBookConfirm    = true;         // K: молот/падающая звезда — вход после свечи-подтверждения
input bool            InpUseTrigE       = false;        // E: отбой от зоны всегда (при слабых подтверждениях включается сам)
input double          InpTouchWickPct   = 25.0;         // E: мин. тень в сторону зоны, %
input bool            InpDirectEntry    = true;         // Прямой вход: BIW + Doji

input group "Слабые подтверждения"
input bool            InpWeakSignals    = true;         // Входить и на слабых подтверждениях весь день (FU класс C, балл −1, отбой E)

input group "Книга: дисциплина"
input bool            InpDojiWait       = true;         // Сигнальная свеча Doji / волчок (не FU) — не входить, ждать
input double          InpImpulsePt      = 12.0;         // 3 свечи подряд в зону на N+ пунктов — ждать ретест (0 = выкл)
input bool            InpQuota          = true;         // Добор (если слабые подтверждения выкл.): после времени правила мягче
input string          InpQuotaFrom      = "16:00";      // Добор с (Бишкек)

input group "Стоп и тейк [§8]"
input double          InpMinSLPt        = 5.0;          // Мин. стоп, пунктов
input double          InpMaxSLPt        = 15.0;         // Макс. стоп, пунктов
input double          InpSLBufferPt     = 0.5;          // Буфер стопа сверх спреда
input bool            InpTrueStop       = true;         // Стоп за дальним краем зоны (True Stop), если влезает
input ENUM_TP_MODE    InpTPMode         = TP_RR;        // Тейк: TP_RR = стоп × RR, TP_LAL = по ликвидности
input double          InpRR             = 2.0;          // RR: тейк = стоп × N (1:2)
input double          InpTPMinPt        = 10.0;         // LAL: тейк мин., пунктов
input double          InpTPMaxPt        = 30.0;         // LAL: тейк макс., пунктов
input double          InpMinRR          = 2.0;          // LAL: мин. тейк/стоп (нет цели — стоп × RR)
input double          InpBEFrac         = 0.66;         // Безубыток после доли пути до тейка (0 = выкл)
input double          InpBEOffsetPt     = 0.5;          // Стоп в безубытке = вход + N
input bool            InpEarlyExit      = true;         // Ранний выход по Negation на M15
input bool            InpPartial        = false;        // 50% на первой LAL (лот от 0.02)
input int             InpFridayMin      = 30;           // Закрыть позиции за N минут до закрытия в пятницу

input group "Фильтры и лимиты [§9]"
input int             InpMaxTradesDay   = 5;            // Сделок в день
input int             InpMaxCounterDay  = 2;            // Из них COUNTER
input double          InpDailyTargetPt  = 0.0;          // Дневная цель, пунктов (0 = выкл)
input int             InpMaxLossStreak  = 3;            // Стопов подряд до паузы (0 = выкл)
input double          InpMaxSpreadPt    = 0.5;          // Макс. спред
input double          InpMaxSlipPt      = 0.3;          // Проскальзывание (deviation)
input string          InpSessionStart   = "10:00";      // Начало входов (Бишкек)
input string          InpSessionEnd     = "23:00";      // Конец входов (Бишкек)
input string          InpLondonStart    = "13:00";      // Метка LONDON с (Бишкек)
input string          InpNYStart        = "18:00";      // Метка NY с (Бишкек)
input int             InpUtcOffset      = 6;            // Часовой пояс Бишкека относительно UTC
input int             InpServerUtc      = -99;          // UTC-сдвиг сервера в часах; -99 = авто по TimeGMT
input string          InpNewsTimes      = "";           // Новости "ГГГГ.ММ.ДД ЧЧ:ММ;..." (Бишкек)
input int             InpNewsMinutes    = 30;           // Окно вокруг новости, минут

input group "Лог, график, уведомления [§10]"
input int             InpLogLevel       = 1;            // 0 сделки, 1 зоны и пропуски, 2 подробно
input bool            InpPush           = true;         // Push в MT5 на iPhone
input string          InpTgToken        = "";           // Telegram bot token (только запасной канал)
input string          InpTgChatId       = "";           // Telegram chat id (только запасной канал)
input bool            InpDraw           = true;         // Рисовать зоны на графике
input string          InpLogFile        = "MrCasino_v32_trades.csv"; // CSV-лог (MQL5/Files)

input group "Telegram-бот (мост) [v3.3]"
input bool            InpBridge         = true;         // Вести файловый мост (status, events, команды)
input string          InpBridgeKey      = "";           // Секрет команд; пусто = команды не принимаются
input int             InpStatusSec      = 10;           // Как часто обновлять status.json, секунд
input bool            InpShots          = true;         // Скрины графика к событиям
input int             InpShotW          = 1280;         // Ширина скрина, пикселей
input int             InpShotH          = 720;          // Высота скрина, пикселей
input string          InpMorningHM      = "09:30";      // Утренний брифинг (Бишкек), пусто = выкл
input string          InpReportHM       = "23:05";      // Дневной отчёт (Бишкек), пусто = выкл
input bool            InpTgFallback     = true;         // Бот молчит > 3 мин: критичное напрямую в Telegram
input double          InpAlertDDPct     = 3.0;          // Тревога: эквити ниже баланса начала дня на N%
input double          InpAlertSpreadX   = 3.0;          // Тревога: спред > N × макс. спреда дольше 2 минут

//--- робот только для золота: 1 пункт = $1, множители = 1
double     g_k      = 1.0;
double     g_ks     = 1.0;
bool       g_crypto = false;
#define PT(v) ((v) * g_k)
#define InpZoneRadius      (InpZoneRadiusPt * g_k)
#define InpFUZoneMinWick   (InpFUZoneMinWickPt * g_k)
#define InpSweepZone       (InpSweepZonePt * g_k)
#define InpEqTol           (InpEqTolPt * g_k)
#define InpZoneMax         (InpZoneMaxPt * g_k)
#define InpZoneMin         (InpZoneMinPt * g_k)
#define InpConfirmAway     (InpConfirmAwayPt * g_k)
#define InpInvalidBeyond   (InpInvalidBeyondPt * g_k)
#define InpLqMin           (InpLqMinPt * g_k)
#define InpHCSTol          (InpHCSTolPt * g_k)
#define InpPierceMax       (InpPierceMaxPt * g_k)
#define InpSweepMax        (InpSweepMaxPt * g_k)
#define InpMaxSL           (InpMaxSLPt * g_k)
#define InpMinSL           (InpMinSLPt * g_k)
#define InpSLBuffer        (InpSLBufferPt * g_k)
#define InpTPMin           (InpTPMinPt * g_k)
#define InpTPMax           (InpTPMaxPt * g_k)
#define InpBEOffset        (InpBEOffsetPt * g_k)
#define InpDailyTarget     (InpDailyTargetPt * g_k)
#define InpMaxSpread       (InpMaxSpreadPt * g_ks)
#define InpMaxSlip         (InpMaxSlipPt * g_k)
#define InpImpulse         (InpImpulsePt * g_k)

//--- константы
#define ZONE_NEW        0
#define ZONE_CONFIRMED  1
#define ZONE_USED       2
#define ZONE_INVALID    3
#define T_WITH          0
#define T_MIXED         1
#define T_COUNTER       2
#define T_NEUTRAL       3
#define OBJ_PREFIX      "MC_"
#define TRADES_HEADER   "date_open,date_close,ticket,side,type,trigger,mark,fu_class,score,zone_sources,zone_width,session,entry,sl,tp,lot,risk_usd,result_pts,result_usd,rr_real,exit,strength,weak,manual,duration_min"

//--- структуры
struct SCand                       // кандидат в зону, зеркальные координаты
  {
   int               src;          // 1 OB, 2 тень FU, 3 ликвидность
   ENUM_TIMEFRAMES   tf;
   double            lo, hi;
   datetime          t0;           // конец свечи-источника
   int               pts;
   bool              fuAfter;
   double            lvl;          // зеркальный уровень ликвидности
   string            label;
  };

struct SZone
  {
   int               dir;          // +1 BUY, -1 SELL
   double            lo, hi;       // торговая зона, реальные цены
   double            ulo, uhi;     // объединение источников (идентификация)
   datetime          born;         // конец самого раннего источника
   datetime          newest;       // конец самого свежего источника
   ENUM_TIMEFRAMES   topTF;
   int               status;
   bool              htf;
   double            lvl4;
   int               srcScore;
   int               score;
   bool              hasHCS;
   bool              tradable;
   bool              notified;
   bool              touched;      // v3.3: цена уже заходила в зону (событие zone_touch)
   string            srcs;
   int               nLvl;
   double            lvl[6];       // реальные цены снятой ликвидности
   string            lastSkip;
   datetime          lastSkipT;
   int               updId;        // номер пересчёта, в котором зона обновлена
  };

struct SSig
  {
   bool              ok;
   string            trig;
   int               cls;
   bool              strong;
   int               pts;          // вклад триггера и класса FU в силу сигнала
   double            ext;          // самая дальняя точка манипуляции (зеркально)
   string            mark;
   datetime          t;
  };

struct SDay
  {
   int               trades;
   int               counter;
   double            pts;
   double            usd;          // закрытый результат дня в валюте счёта (с комиссией и свопом)
   int               lossStreak;
   datetime          lastLossBuy;
   datetime          lastLossSell;
  };

// Настройки, которые можно менять из Telegram [v3.3, §5.4]; c* = значение изменено из Telegram
struct SSet
  {
   bool              paused;
   double            riskPct;       // < 0: риск в день / сделок в день
   double            dailyRiskPct;
   int               maxTrades;
   double            rr;
   bool              weak;
   bool              tradeBuy;
   bool              tradeSell;
   bool              cRisk;
   bool              cDaily;
   bool              cMax;
   bool              cRr;
   bool              cWeak;
   bool              cSide;
  };

//--- глобальные переменные
CTrade     g_trade;
string     g_sym;
int        g_digits;
double     g_point;
SZone      g_zones[];
int        g_d1         = 0;
int        g_h4         = 0;
double     g_lv[8];                // [0..3] BUY, [4..7] SELL
bool       g_hasLv[2];
datetime   g_lastM5     = 0;
datetime   g_lastM15    = 0;
datetime   g_lastH1     = 0;
datetime   g_news[];
int        g_sessStart  = 0;
int        g_sessEnd    = 0;
int        g_londonMin  = 0;
int        g_nyMin      = 0;
SDay       g_day;
bool       g_isTester   = false;
int        g_markIdx    = 0;
string     g_lastSkip   = "-";
double     g_liq[];                // неснятые уровни ликвидности, реальные цены
int        g_liqDir[];
bool       g_liqMajor[];               // Major: равные уровни, Doji (цель для тейка)
string     g_lk[];
int        g_cntDayKey  = -1;
int        g_cntToday   = 0;
int        g_quotaMin   = 0;        // начало добора, минуты Бишкека
int        g_rebuildId  = 0;

// мост v3.3
SSet       g_set;
string     g_pauseReason  = "";
int        g_rejectStreak = 0;
string     g_robotId      = "";
string     g_dir          = "";        // MrCasino\<login>_<magic> в Common\Files
long       g_seq          = 0;
bool       g_bridgeOn     = false;
bool       g_botAlive     = false;
datetime   g_botHb        = 0;         // heartbeat бота, UTC
datetime   g_startUtc     = 0;
bool       g_fallback     = false;     // бот молчит > 3 минут
string     g_tgQueue[];
string     g_alKey[];
bool       g_alOn[];
datetime   g_discSince    = 0;
datetime   g_spreadSince  = 0;
ulong      g_lastTickMs   = 0;
ulong      g_lastStatusMs = 0;
ulong      g_lastAlertMs  = 0;
string     g_lastMorningDay = "";
string     g_lastReportDay  = "";
int        g_noSlTries    = 0;
datetime   g_noSlLast     = 0;
bool       g_biasInit     = false;
bool       g_zoneCreating = false;   // зона создаётся: zone_invalid не шлём
bool       g_sigThisPass  = false;   // в этом проходе TryEntry триггер уже был (для signal_skipped)

// открытая позиция робота
long       g_pId        = 0;
int        g_pDir       = 0;
string     g_pTrig      = "";
int        g_pCls       = 0;
string     g_pStrength  = "";
string     g_pType      = "";
int        g_pScore     = 0;
string     g_pSrcs      = "";
string     g_pSess      = "";
double     g_pEntry     = 0;
double     g_pSL        = 0;
double     g_pTP        = 0;
double     g_pLot       = 0;
double     g_pRisk      = 0;        // риск сделки в валюте счёта
double     g_pZoneW     = 0;
double     g_pZLo       = 0;
double     g_pZHi       = 0;
double     g_pLal       = 0;
double     g_pBEAt      = 0;
bool       g_beDone     = false;
bool       g_partDone   = false;
string     g_exitTag    = "";
int        g_closeRetry = 0;
datetime   g_pOpenT     = 0;
string     g_pMark      = "";
bool       g_pManual    = false;
bool       g_pWeak      = false;
int        g_pStrN      = 1;

//+------------------------------------------------------------------+
//| Лог и уведомления [§10]                                          |
//+------------------------------------------------------------------+
void Lg(int lvl, string s)
  {
   if(lvl <= InpLogLevel) Print(s);
  }

bool LogOnce(string k)
  {
   for(int i = ArraySize(g_lk) - 1; i >= 0; i--)
      if(g_lk[i] == k) return false;
   int n = ArraySize(g_lk);
   if(n >= 3000) { ArrayResize(g_lk, 0); n = 0; }
   ArrayResize(g_lk, n + 1);
   g_lk[n] = k;
   return true;
  }

string UrlEncode(string s)
  {
   uchar b[];
   int n = StringToCharArray(s, b, 0, -1, CP_UTF8);
   string out = "";
   for(int i = 0; i < n - 1; i++)
     {
      uchar c = b[i];
      if((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
         c == '-' || c == '_' || c == '.' || c == '~')
         out += CharToString(c);
      else
         out += StringFormat("%%%02X", c);
     }
   return out;
  }

void SendTG(string text)
  {
   if(g_isTester || InpTgToken == "" || InpTgChatId == "") return;
   string url = "https://api.telegram.org/bot" + InpTgToken + "/sendMessage";
   string body = "chat_id=" + UrlEncode(InpTgChatId) + "&text=" + UrlEncode(text);
   uchar data[], res[];
   int n = StringToCharArray(body, data, 0, -1, CP_UTF8);
   if(n > 0) ArrayResize(data, n - 1);
   string hdr = "Content-Type: application/x-www-form-urlencoded\r\n", rh;
   ResetLastError();
   int code = WebRequest("POST", url, hdr, 5000, data, res, rh);
   if(code != 200)
      Print("Telegram: ошибка отправки, код ", code, ", err ", GetLastError());
  }

void Notify(string text)               // v3.3: обычное сообщение идёт событием, прямой Telegram только в запасном режиме
  {
   Emit("note", 1, "info", "", text, -1);
  }

void LogTrade(string line)
  {
   int h = FileOpen(InpLogFile, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
     {
      Print("CSV: не открыть файл ", InpLogFile, ", err ", GetLastError());
      return;
     }
   if(FileSize(h) == 0)
      FileWriteString(h, "date,side,type,trigger,fu_class,score,zone_sources,zone_width,session,entry,sl,tp,lot,result_pts,result_usd,exit,strength,risk_usd\r\n");
   FileSeek(h, 0, SEEK_END);
   FileWriteString(h, line + "\r\n");
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| Зеркальные аксессоры: для SELL цены отражаются                   |
//+------------------------------------------------------------------+
double Ov(const MqlRates &r[], int i, int d) { return d > 0 ? r[i].open  : -r[i].open;  }
double Cv(const MqlRates &r[], int i, int d) { return d > 0 ? r[i].close : -r[i].close; }
double Hv(const MqlRates &r[], int i, int d) { return d > 0 ? r[i].high  : -r[i].low;   }
double Lv(const MqlRates &r[], int i, int d) { return d > 0 ? r[i].low   : -r[i].high;  }
double BTop(const MqlRates &r[], int i, int d) { return MathMax(Ov(r, i, d), Cv(r, i, d)); }
double BBot(const MqlRates &r[], int i, int d) { return MathMin(Ov(r, i, d), Cv(r, i, d)); }
double Rng(const MqlRates &r[], int i, int d)  { return Hv(r, i, d) - Lv(r, i, d); }

double ZLo(const SZone &z) { return z.dir > 0 ? z.lo : -z.hi; }
double ZHi(const SZone &z) { return z.dir > 0 ? z.hi : -z.lo; }

int Load(ENUM_TIMEFRAMES tf, int n, MqlRates &r[])
  {
   ArraySetAsSeries(r, true);
   return CopyRates(g_sym, tf, 0, n, r);
  }

string TFName(ENUM_TIMEFRAMES tf) { return StringSubstr(EnumToString(tf), 7); }
string ClsName(int c) { return c == 3 ? "A" : (c == 2 ? "B" : (c == 1 ? "C" : "-")); }
string DirName(int d) { return d > 0 ? "BUY" : "SELL"; }
string TypeName(int t) { return t == T_WITH ? "WITH" : (t == T_MIXED ? "MIXED" : (t == T_COUNTER ? "COUNTER" : "NEUTRAL")); }
string BiasName(int b) { return b > 0 ? "BUY" : (b < 0 ? "SELL" : "NONE"); }
string StatusName(int s) { return s == ZONE_NEW ? "NEW" : (s == ZONE_CONFIRMED ? "CONFIRMED" : (s == ZONE_USED ? "USED" : "INVALID")); }
string Stars(int p) { return p >= 7 ? "★★★" : (p >= 5 ? "★★" : "★"); }
string Px(double v) { return DoubleToString(v, g_digits); }

//+------------------------------------------------------------------+
//| Паттерны [§5]                                                    |
//+------------------------------------------------------------------+
bool IsDoji(const MqlRates &r[], int i)
  {
   if(i < 0 || i >= ArraySize(r)) return false;
   double rg = r[i].high - r[i].low;
   if(rg <= 0) return false;
   return MathAbs(r[i].close - r[i].open) <= InpDojiBodyPct / 100.0 * rg;
  }

bool IsFU(const MqlRates &r[], int i, int d)
  {
   if(i < 0 || i + 1 >= ArraySize(r)) return false;
   return Lv(r, i, d) < Lv(r, i + 1, d) - InpLqMin && Cv(r, i, d) > Lv(r, i + 1, d);
  }

bool IsEngulf(const MqlRates &r[], int i, int d)
  {
   return Cv(r, i, d) > BTop(r, i + 1, d) && Ov(r, i, d) <= BBot(r, i + 1, d);
  }

bool IsBodyCover(const MqlRates &r[], int i, int d)
  {
   double b1 = BTop(r, i + 1, d) - BBot(r, i + 1, d);
   if(b1 <= 0) return false;
   double ov = MathMin(BTop(r, i, d), BTop(r, i + 1, d)) - MathMax(BBot(r, i, d), BBot(r, i + 1, d));
   return ov >= 0.5 * b1;
  }

bool IsLowerWick(const MqlRates &r[], int i, int d)
  {
   double rg = Rng(r, i, d);
   if(rg <= 0) return false;
   return (BBot(r, i, d) - Lv(r, i, d)) >= InpWickPct / 100.0 * rg;
  }

bool IsBIW(const MqlRates &r[], int i, int d)
  {
   return BBot(r, i, d) >= Lv(r, i + 1, d) && BTop(r, i, d) <= BBot(r, i + 1, d);
  }

// Класс FU: 3=A (SFU), 2=B, 1=C, 0=не FU
int FUClass(const MqlRates &r[], int i, int d)
  {
   if(!IsFU(r, i, d)) return 0;
   bool bull = Cv(r, i, d) > Ov(r, i, d);
   if(IsBIW(r, i, d) && bull) return 3;
   if(IsEngulf(r, i, d)) return 3;
   if(IsBodyCover(r, i, d) && IsLowerWick(r, i, d)) return 2;
   return 1;
  }

// 0 нет, 1 обычный Negation, 2 Strong Negation. Сигнальная свеча i, отрицаемая i+1
int Negation(const MqlRates &r[], int i, int d)
  {
   if(i < 0 || i + 2 >= ArraySize(r)) return 0;
   if(!(Cv(r, i + 1, d) < Ov(r, i + 1, d))) return 0;
   double rg1 = Rng(r, i + 1, d);
   if(rg1 <= 0) return 0;
   if(BBot(r, i + 1, d) - Lv(r, i + 1, d) > 0.15 * rg1) return 0;
   if(!(Cv(r, i + 1, d) < Lv(r, i + 2, d))) return 0;
   if(!(Cv(r, i, d) > Ov(r, i, d))) return 0;
   if(!(Lv(r, i, d) < Lv(r, i + 1, d))) return 0;
   if(Cv(r, i, d) > Hv(r, i + 1, d)) return 2;
   if(Cv(r, i, d) > BTop(r, i + 1, d)) return 1;
   return 0;
  }

// Swing low: ниже двух свечей слева и двух справа (i >= 3: слева новее)
bool IsSwingLow(const MqlRates &r[], int i, int d)
  {
   if(i < 3 || i + 2 >= ArraySize(r)) return false;
   double L = Lv(r, i, d);
   return L < Lv(r, i - 1, d) && L < Lv(r, i - 2, d) && L < Lv(r, i + 1, d) && L < Lv(r, i + 2, d);
  }

//+------------------------------------------------------------------+
//| Время [§3, §9]                                                   |
//+------------------------------------------------------------------+
int GmtOffset()
  {
   if(InpServerUtc != -99) return InpServerUtc * 3600;
   double off = (double)(TimeCurrent() - TimeGMT());
   return (int)(MathRound(off / 900.0) * 900.0);
  }
datetime ToLocal(datetime srv)   { return srv - GmtOffset() + InpUtcOffset * 3600; }
datetime FromLocal(datetime loc) { return loc + GmtOffset() - InpUtcOffset * 3600; }
datetime LocalNow()
  {
   if(InpServerUtc != -99) return ToLocal(TimeCurrent());
   return TimeGMT() + InpUtcOffset * 3600;     // UTC + 6, не время сервера
  }

datetime DayStartSrv()
  {
   datetime l = LocalNow();
   return FromLocal(l - (l % 86400));
  }

int ParseHM(string s)
  {
   string p[];
   if(StringSplit(s, ':', p) < 2) return 0;
   return (int)StringToInteger(p[0]) * 60 + (int)StringToInteger(p[1]);
  }

int LocalMinutes() { return (int)((LocalNow() % 86400) / 60); }

bool InSession()
  {
   int mins = LocalMinutes();
   if(g_sessStart <= g_sessEnd) return mins >= g_sessStart && mins < g_sessEnd;
   return mins >= g_sessStart || mins < g_sessEnd;
  }

// Мягкие правила: FU класс C для A, балл зоны −1, вход E (отбой от зоны), COUNTER по Negation
// или FU класса B. v3.2: при InpWeakSignals — всю сессию (слабые подтверждения тоже работают);
// иначе — добор после InpQuotaFrom, если сделок меньше лимита. Зона и сигнал нужны всегда.
bool Relaxed()
  {
   if(g_day.trades >= g_set.maxTrades) return false;
   if(!InSession()) return false;
   if(g_set.weak) return true;
   return InpQuota && LocalMinutes() >= g_quotaMin;
  }

string SessionName()
  {
   int m = LocalMinutes();
   if(m < g_londonMin) return "ASIA";
   if(m < g_nyMin) return "LONDON";
   return "NY";
  }

string LocalHM()
  {
   datetime l = LocalNow();
   return StringFormat("%02d:%02d", (int)((l % 86400) / 3600), (int)((l % 3600) / 60));
  }

void ParseNews()
  {
   ArrayResize(g_news, 0);
   string s = InpNewsTimes;
   StringReplace(s, ",", ";");
   StringReplace(s, "\n", ";");
   string p[];
   int n = StringSplit(s, ';', p);
   for(int i = 0; i < n; i++)
     {
      StringTrimLeft(p[i]);
      StringTrimRight(p[i]);
      if(StringLen(p[i]) < 10) continue;
      datetime t = StringToTime(p[i]);
      if(t <= 0) continue;
      int k = ArraySize(g_news);
      ArrayResize(g_news, k + 1);
      g_news[k] = FromLocal(t);
     }
  }

bool NewsBlock()
  {
   datetime now = TimeCurrent();
   for(int i = 0; i < ArraySize(g_news); i++)
      if(MathAbs((double)(now - g_news[i])) <= InpNewsMinutes * 60.0) return true;
   return false;
  }

bool FridayCloseTime()
  {
   if(g_crypto) return false;                 // крипта торгуется 24/7
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   if(t.day_of_week != 5) return false;
   datetime from, to;
   int maxTo = 0;
   for(uint k = 0; SymbolInfoSessionTrade(g_sym, FRIDAY, k, from, to); k++)
      maxTo = MathMax(maxTo, (int)to);
   if(maxTo == 0) maxTo = 86400;
   int now = t.hour * 3600 + t.min * 60 + t.sec;
   return now >= maxTo - InpFridayMin * 60;
  }

//+------------------------------------------------------------------+
//| Мост с ассистентом v3.3 [§5, §6]: JSON, файлы, события, настройки|
//+------------------------------------------------------------------+
string IsoUtc(datetime t)
  {
   MqlDateTime m;
   TimeToStruct(t, m);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ", m.year, m.mon, m.day, m.hour, m.min, m.sec);
  }

string LocalStamp(datetime loc)                 // loc уже в часовом поясе Бишкека
  {
   MqlDateTime m;
   TimeToStruct(loc, m);
   return StringFormat("%04d-%02d-%02d %02d:%02d:%02d", m.year, m.mon, m.day, m.hour, m.min, m.sec);
  }

string LocalDayKey(datetime loc)
  {
   MqlDateTime m;
   TimeToStruct(loc, m);
   return StringFormat("%04d%02d%02d", m.year, m.mon, m.day);
  }

datetime IsoToTime(string s)                    // "2026-11-02T08:15:10Z" -> datetime (UTC)
  {
   if(StringLen(s) < 19) return 0;
   string d = StringSubstr(s, 0, 4) + "." + StringSubstr(s, 5, 2) + "." + StringSubstr(s, 8, 2) + " " + StringSubstr(s, 11, 8);
   return StringToTime(d);
  }

string JEsc(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\r", "");
   StringReplace(s, "\n", "\\n");
   StringReplace(s, "\t", " ");
   return s;
  }

string JS(string key, string v) { return "\"" + key + "\":\"" + JEsc(v) + "\""; }
string JN(string key, double v, int digits)
  {
   if(!MathIsValidNumber(v) || MathAbs(v) > 1.0e12) v = 0.0;
   return "\"" + key + "\":" + DoubleToString(v, digits);
  }
string JI(string key, long v) { return "\"" + key + "\":" + IntegerToString(v); }
string JB(string key, bool v) { return "\"" + key + "\":" + (v ? "true" : "false"); }

// Разбор JSON-объекта в плоские пары. Вложенные объекты дают ключи вида "args.pct".
// Значения: строки (с разэкранированием), числа, true/false как текст. Массивы не поддерживаются.
void JAdd(string &keys[], string &vals[], const string &path[], string key, string val)
  {
   string full = "";
   for(int q = 0; q < ArraySize(path); q++) full += path[q] + ".";
   full += key;
   int k = ArraySize(keys);
   ArrayResize(keys, k + 1);
   ArrayResize(vals, k + 1);
   keys[k] = full;
   vals[k] = val;
  }

int JParse(const string s, string &keys[], string &vals[])
  {
   ArrayResize(keys, 0);
   ArrayResize(vals, 0);
   string path[];
   ArrayResize(path, 0);
   string pending = "";
   int n = StringLen(s);
   int i = 0;
   while(i < n)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '{')
        {
         if(pending != "")
           {
            int d = ArraySize(path);
            ArrayResize(path, d + 1);
            path[d] = pending;
            pending = "";
           }
         i++;
        }
      else if(c == '}')
        {
         int d = ArraySize(path);
         if(d > 0) ArrayResize(path, d - 1);
         i++;
        }
      else if(c == '"')
        {
         string str = "";
         i++;
         while(i < n)
           {
            ushort ch = StringGetCharacter(s, i);
            if(ch == '\\' && i + 1 < n)
              {
               ushort nx = StringGetCharacter(s, i + 1);
               if(nx == 'n') str += "\n";
               else if(nx == 't') str += " ";
               else str += ShortToString(nx);
               i += 2;
              }
            else if(ch == '"') { i++; break; }
            else { str += ShortToString(ch); i++; }
           }
         int j = i;
         while(j < n && (StringGetCharacter(s, j) == ' ' || StringGetCharacter(s, j) == '\r' ||
                         StringGetCharacter(s, j) == '\n' || StringGetCharacter(s, j) == '\t')) j++;
         if(pending == "" && j < n && StringGetCharacter(s, j) == ':')
           {
            pending = str;
            i = j + 1;
           }
         else if(pending != "")
           {
            JAdd(keys, vals, path, pending, str);
            pending = "";
           }
        }
      else if(c == ',' || c == ' ' || c == '\r' || c == '\n' || c == '\t' || c == ':')
         i++;
      else
        {
         string tok = "";
         while(i < n)
           {
            ushort ch = StringGetCharacter(s, i);
            if(ch == ',' || ch == '}' || ch == ' ' || ch == '\r' || ch == '\n' || ch == '\t') break;
            tok += ShortToString(ch);
            i++;
           }
         if(pending != "")
           {
            JAdd(keys, vals, path, pending, tok);
            pending = "";
           }
        }
     }
   return ArraySize(keys);
  }

bool JHas(const string &keys[], string key)
  {
   for(int i = 0; i < ArraySize(keys); i++)
      if(keys[i] == key) return true;
   return false;
  }

string JGet(const string &keys[], const string &vals[], string key, string def = "")
  {
   for(int i = 0; i < ArraySize(keys); i++)
      if(keys[i] == key) return vals[i];
   return def;
  }

//--- файлы моста (общая папка Common\Files, UTF-8 без BOM) [§6]
void EnsureDirs()
  {
   FolderCreate("MrCasino", FILE_COMMON);
   FolderCreate(g_dir, FILE_COMMON);
   FolderCreate(g_dir + "\\events", FILE_COMMON);
   FolderCreate(g_dir + "\\cmd", FILE_COMMON);
   FolderCreate(g_dir + "\\shots", FILE_COMMON);
  }

// Атомарная запись: во временный файл, затем переименование с перезаписью
bool WriteAtomic(string rel, string text)
  {
   string tmp = rel + ".tmp";
   int h = FileOpen(tmp, FILE_WRITE | FILE_BIN | FILE_COMMON);
   if(h == INVALID_HANDLE) return false;
   uchar b[];
   int n = StringToCharArray(text, b, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 1) FileWriteArray(h, b, 0, n - 1);          // без завершающего нуля
   FileClose(h);
   return FileMove(tmp, FILE_COMMON, rel, FILE_REWRITE | FILE_COMMON);
  }

// Дозапись строки в конец файла; header пишется, если файл пуст
bool AppendUtf8(string rel, string text, string header)
  {
   int h = FileOpen(rel, FILE_READ | FILE_WRITE | FILE_BIN | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return false;
   ulong sz = FileSize(h);
   FileSeek(h, 0, SEEK_END);
   uchar b[];
   if(sz == 0 && header != "")
     {
      int nh = StringToCharArray(header + "\n", b, 0, WHOLE_ARRAY, CP_UTF8);
      if(nh > 1) FileWriteArray(h, b, 0, nh - 1);
     }
   int n = StringToCharArray(text + "\n", b, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 1) FileWriteArray(h, b, 0, n - 1);
   FileClose(h);
   return true;
  }

bool ReadUtf8(string rel, string &out)
  {
   out = "";
   int h = FileOpen(rel, FILE_READ | FILE_BIN | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return false;
   int sz = (int)FileSize(h);
   uchar b[];
   if(sz > 0)
     {
      ArrayResize(b, sz);
      FileReadArray(h, b, 0, sz);
      out = CharArrayToString(b, 0, sz, CP_UTF8);
     }
   FileClose(h);
   return true;
  }

//--- настройки, меняющиеся из Telegram [§5.4]
void SettingsFromInputs()
  {
   g_set.paused = false;
   g_set.riskPct = InpRiskPercent > 0 ? InpRiskPercent : -1.0;     // < 0: риск в день / сделок в день
   g_set.dailyRiskPct = InpDailyRiskPct;
   g_set.maxTrades = InpMaxTradesDay;
   g_set.rr = InpRR;
   g_set.weak = InpWeakSignals;
   g_set.tradeBuy = InpTradeBuy;
   g_set.tradeSell = InpTradeSell;
   g_set.cRisk = false;
   g_set.cDaily = false;
   g_set.cMax = false;
   g_set.cRr = false;
   g_set.cWeak = false;
   g_set.cSide = false;
  }

void SaveSettings()
  {
   if(!g_bridgeOn) return;
   string st = "";
   if(g_set.cRisk) st += (st == "" ? "" : ",") + JN("risk_pct", g_set.riskPct, 2);
   if(g_set.cDaily) st += (st == "" ? "" : ",") + JN("daily_risk_pct", g_set.dailyRiskPct, 2);
   if(g_set.cMax) st += (st == "" ? "" : ",") + JI("max_trades", g_set.maxTrades);
   if(g_set.cRr) st += (st == "" ? "" : ",") + JN("rr", g_set.rr, 2);
   if(g_set.cWeak) st += (st == "" ? "" : ",") + JB("weak", g_set.weak);
   if(g_set.cSide) st += (st == "" ? "" : ",") + JB("buy", g_set.tradeBuy) + "," + JB("sell", g_set.tradeSell);
   if(g_set.paused) st += (st == "" ? "" : ",") + JB("paused", true) + "," + JS("pause_reason", g_pauseReason);
   string s = "{" + JI("seq", g_seq) + ",\"settings\":{" + st + "}," + JS("morning_day", g_lastMorningDay) + "," +
              JS("report_day", g_lastReportDay) + "}";
   WriteAtomic(g_dir + "\\settings.json", s);
  }

void LoadSettings()
  {
   string t;
   if(!ReadUtf8(g_dir + "\\settings.json", t) || t == "") return;
   string k[], v[];
   JParse(t, k, v);
   g_seq = StringToInteger(JGet(k, v, "seq", "0"));
   g_lastMorningDay = JGet(k, v, "morning_day");
   g_lastReportDay = JGet(k, v, "report_day");
   if(JHas(k, "settings.risk_pct"))
     { g_set.riskPct = StringToDouble(JGet(k, v, "settings.risk_pct")); g_set.cRisk = true; }
   if(JHas(k, "settings.daily_risk_pct"))
     { g_set.dailyRiskPct = StringToDouble(JGet(k, v, "settings.daily_risk_pct")); g_set.cDaily = true; }
   if(JHas(k, "settings.max_trades"))
     { g_set.maxTrades = (int)StringToInteger(JGet(k, v, "settings.max_trades")); g_set.cMax = true; }
   if(JHas(k, "settings.rr"))
     { g_set.rr = StringToDouble(JGet(k, v, "settings.rr")); g_set.cRr = true; }
   if(JHas(k, "settings.weak"))
     { g_set.weak = (JGet(k, v, "settings.weak") == "true"); g_set.cWeak = true; }
   if(JHas(k, "settings.buy") && JHas(k, "settings.sell"))
     {
      g_set.tradeBuy = (JGet(k, v, "settings.buy") == "true");
      g_set.tradeSell = (JGet(k, v, "settings.sell") == "true");
      g_set.cSide = true;
     }
   if(JGet(k, v, "settings.paused") == "true")
     { g_set.paused = true; g_pauseReason = JGet(k, v, "settings.pause_reason", "Telegram"); }
  }

//--- торговая сессия символа открыта (по расписанию брокера, серверное время)
bool SymbolSessionOpen()
  {
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   int now = t.hour * 3600 + t.min * 60 + t.sec;
   datetime from, to;
   for(uint k = 0; SymbolInfoSessionTrade(g_sym, (ENUM_DAY_OF_WEEK)t.day_of_week, k, from, to); k++)
      if(now >= (int)from && now < (int)to) return true;
   return false;
  }

//--- скрины графика [§5.7]
void CopyChartObjects(long cid)
  {
   int total = ObjectsTotal(0, 0, OBJ_RECTANGLE);
   for(int i = 0; i < total; i++)
     {
      string nm = ObjectName(0, i, 0, OBJ_RECTANGLE);
      if(StringFind(nm, OBJ_PREFIX + "Z") != 0) continue;
      datetime t1 = (datetime)ObjectGetInteger(0, nm, OBJPROP_TIME, 0);
      datetime t2 = (datetime)ObjectGetInteger(0, nm, OBJPROP_TIME, 1);
      double p1 = ObjectGetDouble(0, nm, OBJPROP_PRICE, 0);
      double p2 = ObjectGetDouble(0, nm, OBJPROP_PRICE, 1);
      if(!ObjectCreate(cid, nm, OBJ_RECTANGLE, 0, t1, p1, t2, p2)) continue;
      ObjectSetInteger(cid, nm, OBJPROP_COLOR, ObjectGetInteger(0, nm, OBJPROP_COLOR));
      ObjectSetInteger(cid, nm, OBJPROP_FILL, ObjectGetInteger(0, nm, OBJPROP_FILL));
      ObjectSetInteger(cid, nm, OBJPROP_BACK, true);
     }
   string lines[2] = {"SL", "TP"};
   for(int q = 0; q < 2; q++)
     {
      string nm = OBJ_PREFIX + lines[q];
      if(ObjectFind(0, nm) < 0) continue;
      if(!ObjectCreate(cid, nm, OBJ_HLINE, 0, 0, ObjectGetDouble(0, nm, OBJPROP_PRICE))) continue;
      ObjectSetInteger(cid, nm, OBJPROP_COLOR, ObjectGetInteger(0, nm, OBJPROP_COLOR));
      ObjectSetInteger(cid, nm, OBJPROP_STYLE, STYLE_DASH);
     }
  }

// M5 (и текущий график): ChartScreenShot прямо с графика робота. Другие ТФ: временный график,
// на него копируются зоны, скрин, закрытие. Только из OnTimer (там можно ждать подгрузку истории).
bool TakeShot(ENUM_TIMEFRAMES tf, string name)
  {
   string tmp = "MrCasino_shot.png";
   long cid = ChartID();
   long tmpChart = 0;
   if(tf != PERIOD_M5 && tf != _Period)
     {
      tmpChart = ChartOpen(g_sym, tf);
      if(tmpChart == 0) return false;
      CopyChartObjects(tmpChart);
      for(int k = 0; k < 10; k++)
        {
         if(ChartGetInteger(tmpChart, CHART_VISIBLE_BARS) > 0) break;
         Sleep(200);
        }
      ChartRedraw(tmpChart);
      Sleep(300);
      cid = tmpChart;
     }
   else
      ChartRedraw(cid);
   FileDelete(tmp);
   bool ok = ChartScreenShot(cid, tmp, InpShotW, InpShotH, ALIGN_RIGHT);
   if(tmpChart != 0) ChartClose(tmpChart);
   if(!ok) return false;
   return FileMove(tmp, 0, g_dir + "\\shots\\" + name, FILE_COMMON | FILE_REWRITE);
  }

//--- запасной канал Telegram: очередь, отправка только из OnTimer (в OnTick сети нет) [§5.3]
void QueueTG(string text)
  {
   int n = ArraySize(g_tgQueue);
   if(n >= 20) return;
   ArrayResize(g_tgQueue, n + 1);
   g_tgQueue[n] = text;
  }

void DrainTG()
  {
   int n = ArraySize(g_tgQueue);
   if(n == 0) return;
   string t = g_tgQueue[0];
   for(int i = 1; i < n; i++) g_tgQueue[i - 1] = g_tgQueue[i];
   ArrayResize(g_tgQueue, n - 1);
   SendTG(t);
  }

void PushMT5(string text)
  {
   if(!InpPush || g_isTester) return;
   string p = text;
   StringReplace(p, "\n", " | ");
   if(StringLen(p) > 250) p = StringSubstr(p, 0, 250);
   if(!SendNotification(p)) Print("Push: ошибка ", GetLastError());
  }

//--- Emit: событие в ленту, status.json, Push; запасной Telegram [§5.3, §7]
// fields — содержимое объекта data без фигурных скобок; shotTf >= 0 — приложить скрин (PERIOD_M5 = график робота)
void Emit(string type, int level, string severity, string fields, string text, int shotTf)
  {
   g_seq++;
   if(level <= 0 || type == "zone_confirmed") Print("[NOTIFY] ", text);
   if(g_bridgeOn)
     {
      string data = fields;
      if(shotTf >= 0)
        {
         string shot = "";
         if(InpShots)
           {
            string nm = IntegerToString(g_seq) + ".png";
            if(TakeShot((ENUM_TIMEFRAMES)shotTf, nm)) shot = nm;
           }
         data += (data == "" ? "" : ",") + JS("shot", shot);
        }
      string line = "{" + JI("seq", g_seq) + "," + JS("ts_utc", IsoUtc(TimeGMT())) + "," + JS("ts_local", LocalStamp(LocalNow())) + "," +
                    JS("type", type) + "," + JI("level", level) + "," + JS("severity", severity) + "," + JS("robot", g_robotId) +
                    ",\"data\":{" + data + "}," + JS("text", text) + "}";
      if(!AppendUtf8(g_dir + "\\events\\" + LocalDayKey(LocalNow()) + ".jsonl", line, ""))
         Print("Мост: не записать событие ", type, ", err ", GetLastError());
      SaveSettings();
      WriteStatus();
     }
   if((level <= 0 && type != "cmd_result" && type != "ea_started" && type != "ea_stopped") || type == "zone_confirmed")
      PushMT5(text);
   if(g_fallback && InpTgFallback && (severity == "critical" || type == "trade_opened" || type == "trade_closed"))
      QueueTG(text);
  }

void EmitBias(string tf, int from, int to)
  {
   Emit("bias_changed", 1, "info", JS("tf", tf) + "," + JS("from", BiasName(from)) + "," + JS("to", BiasName(to)),
        "🔄 Bias " + tf + ": " + BiasName(from) + " → " + BiasName(to), -1);
  }

//--- тревоги: событие alert один раз на начало и один раз на конец [§5.8]
void AlertSet(string key, bool on, string severity, string text)
  {
   int idx = -1;
   for(int i = 0; i < ArraySize(g_alKey); i++)
      if(g_alKey[i] == key) { idx = i; break; }
   if(idx < 0)
     {
      idx = ArraySize(g_alKey);
      ArrayResize(g_alKey, idx + 1);
      ArrayResize(g_alOn, idx + 1);
      g_alKey[idx] = key;
      g_alOn[idx] = false;
     }
   if(g_alOn[idx] == on) return;
   g_alOn[idx] = on;
   Emit("alert", 0, severity, JS("key", key) + "," + JS("state", on ? "open" : "closed") + "," + JS("severity", severity), text, -1);
  }

bool AlertIsOn(string key)
  {
   for(int i = 0; i < ArraySize(g_alKey); i++)
      if(g_alKey[i] == key) return g_alOn[i];
   return false;
  }

// Отказ ордера: одиночный — событие order_rejected; 3 подряд — автопауза и критичная тревога [§5.5]
void RegisterReject(uint rc, string descr, string trig)
  {
   g_rejectStreak++;
   Emit("order_rejected", 0, "warning", JI("retcode", (long)rc) + "," + JS("descr", descr) + "," + JS("trigger", trig),
        StringFormat("⚠️ Ордер отклонён: %u %s (триггер %s)", rc, descr, trig), -1);
   if(g_rejectStreak >= 3 && !g_set.paused)
     {
      g_set.paused = true;
      g_pauseReason = "автопауза: 3 отказа ордеров подряд";
      SaveSettings();
      AlertSet("orders_rejected", true, "critical",
               "3 отказа ордеров подряд. Робот поставлен на паузу. Последний отказ: " + descr + ". Снять паузу можно командой resume.");
     }
  }

//+------------------------------------------------------------------+
//| Bias и тип сделки [§4]                                           |
//+------------------------------------------------------------------+
int CalcBias(ENUM_TIMEFRAMES tf)
  {
   MqlRates r[];
   if(Load(tf, 40, r) < 30) return 0;
   for(int i = 1; i <= 20; i++)
     {
      int nb = Negation(r, i, 1), ns = Negation(r, i, -1);
      if(nb > 0 && ns == 0) return 1;
      if(ns > 0 && nb == 0) return -1;
      bool fb = IsFU(r, i, 1), fs = IsFU(r, i, -1);
      if(fb && !fs) return 1;
      if(fs && !fb) return -1;
      if(fb && fs) return r[i].close > r[i].open ? 1 : -1;
     }
   return 0;
  }

void UpdateBias()
  {
   int o1 = g_d1, o4 = g_h4;
   g_d1 = CalcBias(PERIOD_D1);
   g_h4 = CalcBias(PERIOD_H4);
   if(g_biasInit)
     {
      if(g_d1 != o1) EmitBias("D1", o1, g_d1);
      if(g_h4 != o4) EmitBias("H4", o4, g_h4);
     }
   g_biasInit = true;
  }

int TradeType(int d)
  {
   if(g_d1 == 0) return T_NEUTRAL;
   if(d == g_d1 && d == g_h4) return T_WITH;
   if(d == g_d1 || d == g_h4) return T_MIXED;
   return T_COUNTER;
  }

int MinScore(int tp)
  {
   int m = InpMinScoreMixed;
   if(tp == T_WITH) m = InpMinScoreWith;
   else if(tp == T_COUNTER) m = InpMinScoreCounter;
   if(Relaxed()) m = MathMax(1, m - 1);
   return m;
  }

//+------------------------------------------------------------------+
//| HTF Re-test Levels на H4                                         |
//+------------------------------------------------------------------+
void FindHTFLevels(int d)
  {
   int base = d > 0 ? 0 : 4, hi = d > 0 ? 0 : 1;
   g_hasLv[hi] = false;
   MqlRates r[];
   int n = Load(PERIOD_H4, 60, r);
   if(n < 20) return;
   for(int k = 1; k <= 40 && k + 1 < n; k++)
     {
      if(!IsDoji(r, k + 1)) continue;
      double rgD = r[k + 1].high - r[k + 1].low;
      double rgB = r[k].high - r[k].low;
      if(rgB < 1.5 * rgD) continue;
      if(BBot(r, k, d) - Lv(r, k, d) < InpWickPct / 100.0 * rgB) continue;
      double m1 = Hv(r, k + 1, d), m3 = Lv(r, k + 1, d);
      g_lv[base]     = d * m1;
      g_lv[base + 1] = d * (m1 + m3) / 2.0;
      g_lv[base + 2] = d * m3;
      g_lv[base + 3] = d * (Lv(r, k, d) + BBot(r, k, d)) / 2.0;
      g_hasLv[hi] = true;
      return;
     }
  }

bool HtfMatch(int d, double lo, double hi, double &lvl4)
  {
   int base = d > 0 ? 0 : 4, h = d > 0 ? 0 : 1;
   lvl4 = 0;
   if(!g_hasLv[h]) return false;
   lvl4 = g_lv[base + 3];
   for(int k = 0; k < 4; k++)
      if(g_lv[base + k] >= lo - PT(2.0) && g_lv[base + k] <= hi + PT(2.0)) return true;
   return false;
  }

//+------------------------------------------------------------------+
//| FVG и ликвидность [§5, §7, §8]                                   |
//+------------------------------------------------------------------+
// Реальная зона целиком внутри незакрытого FVG M5 шириной 3+ пункта
bool ZoneInFVG(double lo, double hi)
  {
   MqlRates r[];
   int n = Load(PERIOD_M5, 150, r);
   for(int k = 3; k < n - 1; k++)
     {
      double bot = r[k + 1].high, top = r[k - 1].low;           // бычий
      if(top - bot >= PT(3.0))
        {
         bool filled = false;
         for(int q = k - 2; q >= 1; q--)
            if(r[q].low <= bot) { filled = true; break; }
         if(!filled && lo >= bot && hi <= top) return true;
        }
      bot = r[k - 1].high; top = r[k + 1].low;                  // медвежий
      if(top - bot >= PT(3.0))
        {
         bool filled = false;
         for(int q = k - 2; q >= 1; q--)
            if(r[q].high >= top) { filled = true; break; }
         if(!filled && lo >= bot && hi <= top) return true;
        }
     }
   return false;
  }

// Разворот не должен начинаться из имбаланса [§9]: точка разворота (крайняя точка манипуляции)
// внутри незакрытого FVG M5 шириной 3+ пункта. Закрытие внутри дыры не мешает — дыра это цель.
bool InsideFVG(const MqlRates &r[], double price)
  {
   int n = ArraySize(r);
   double c = price;
   for(int k = 3; k < MathMin(n - 1, 45); k++)
     {
      double bot = r[k + 1].high, top = r[k - 1].low;
      if(top - bot >= PT(3.0))
        {
         bool filled = false;
         for(int q = k - 2; q >= 1; q--)
            if(r[q].low <= bot) { filled = true; break; }
         if(!filled && c >= bot && c <= top) return true;
        }
      bot = r[k - 1].high; top = r[k + 1].low;
      if(top - bot >= PT(3.0))
        {
         bool filled = false;
         for(int q = k - 2; q >= 1; q--)
            if(r[q].high >= top) { filled = true; break; }
         if(!filled && c >= bot && c <= top) return true;
        }
     }
   return false;
  }

// Ближайшая Major ликвидность в сторону сделки (Doji, ровные уровни, верх FU, FVG); 0 если нет
double FindMajorLiq(int d, double entry)
  {
   double e = d * entry + PT(3.0);
   double best = DBL_MAX;
   ENUM_TIMEFRAMES tfs[3] = {PERIOD_M15, PERIOD_H1, PERIOD_H4};
   int cnts[3] = {200, 120, 60};
   for(int t = 0; t < 3; t++)
     {
      MqlRates r[];
      int n = Load(tfs[t], cnts[t], r);
      if(n < 10) continue;
      for(int i = 1; i < n - 1; i++)
        {
         double lvl = Hv(r, i, d);
         bool swept = false;
         for(int k = 1; k < i; k++)
            if(Hv(r, k, d) > lvl) { swept = true; break; }
         if(swept) continue;
         if(IsDoji(r, i) && !IsFU(r, i, 1) && !IsFU(r, i, -1))
            if(lvl > e && lvl < best) best = lvl;
         if(i >= 2 && Hv(r, i, d) > Hv(r, i - 1, d) && Hv(r, i, d) > Hv(r, i + 1, d))
            for(int j = i + 2; j < n - 1; j++)
              {
               if(!(Hv(r, j, d) > Hv(r, j - 1, d) && Hv(r, j, d) > Hv(r, j + 1, d))) continue;
               if(MathAbs(Hv(r, j, d) - lvl) <= InpEqTol)
                 {
                  if(lvl > e && lvl < best) best = lvl;
                  break;
                 }
              }
         if(tfs[t] != PERIOD_M15 && IsFU(r, i, d))
            if(lvl > e && lvl < best) best = lvl;
        }
     }
   ENUM_TIMEFRAMES ft[3] = {PERIOD_M5, PERIOD_M15, PERIOD_H1};
   for(int t = 0; t < 3; t++)
     {
      MqlRates r[];
      int n = Load(ft[t], 150, r);
      for(int k = 2; k < n - 1; k++)
        {
         double bottom = Hv(r, k - 1, d), topg = Lv(r, k + 1, d);
         if(topg - bottom < PT(1.0)) continue;
         bool filled = false;
         for(int q = k - 2; q >= 1; q--)
            if(Hv(r, q, d) >= topg) { filled = true; break; }
         if(filled) continue;
         if(bottom > e && bottom < best) best = bottom;
        }
     }
   return best == DBL_MAX ? 0.0 : d * best;
  }

void AddTarget(double &c[], string &cn[], double lvl, string name)
  {
   int n = ArraySize(c);
   ArrayResize(c, n + 1);
   ArrayResize(cn, n + 1);
   c[n] = lvl;
   cn[n] = name;
  }

// Тейк по стратегии (LAL — первая крупная ликвидность после манипуляции [§9]): ближайшая цель
// в сторону сделки — равные хаи/лои, Doji, незакрытый имбаланс, край встречной зоны CONFIRMED —
// в пределах [InpTPMin, InpTPMax] и с RR не ниже InpMinRR. Нет цели — стоп × InpRR
// в тех же пределах. Возвращает дистанцию тейка, src — что за цель. Только режим TP_LAL.
double ChooseTP(int d, double entry, double dist, string &src)
  {
   double e = d * entry;
   double c[];
   string cn[];
   for(int k = 0; k < ArraySize(g_liq); k++)
      if(g_liqDir[k] == -d && g_liqMajor[k]) AddTarget(c, cn, d * g_liq[k], "ликвидность " + Px(g_liq[k]));
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].dir != -d || g_zones[i].status != ZONE_CONFIRMED) continue;
      double edge = d > 0 ? g_zones[i].lo : g_zones[i].hi;
      AddTarget(c, cn, d * edge, StringFormat("зона %s %s–%s", DirName(-d), Px(g_zones[i].lo), Px(g_zones[i].hi)));
     }
   ENUM_TIMEFRAMES ft[3] = {PERIOD_M5, PERIOD_M15, PERIOD_H1};
   for(int t = 0; t < 3; t++)
     {
      MqlRates r[];
      int n = Load(ft[t], 150, r);
      for(int k = 2; k < n - 1; k++)
        {
         double bottom = Hv(r, k - 1, d), topg = Lv(r, k + 1, d);
         if(topg - bottom < PT(1.0)) continue;
         bool filled = false;
         for(int q = k - 2; q >= 1; q--)
            if(Hv(r, q, d) >= topg) { filled = true; break; }
         if(!filled) AddTarget(c, cn, bottom, "имбаланс " + TFName(ft[t]) + " " + Px(d * bottom));
        }
     }
   double best = DBL_MAX;
   src = "";
   for(int k = 0; k < ArraySize(c); k++)
     {
      double dd = c[k] - e - PT(0.3);                 // тейк чуть раньше уровня
      if(dd < InpTPMin || dd > InpTPMax || dd < InpMinRR * dist) continue;
      if(dd < best) { best = dd; src = cn[k]; }
     }
   if(best < DBL_MAX) return best;
   double fb = MathMin(InpTPMax, MathMax(InpTPMin, g_set.rr * dist));
   src = StringFormat("целей нет — стоп × %.1f", g_set.rr);
   return fb;
  }

//+------------------------------------------------------------------+
//| Зоны: общее [§6]                                                 |
//+------------------------------------------------------------------+
string ZoneTag(const SZone &z)
  {
   return StringFormat("%s %s–%s", DirName(z.dir), Px(z.lo), Px(z.hi));
  }

string ZoneLine(const SZone &z)
  {
   return StringFormat("%s | %s | балл %d", ZoneTag(z), z.srcs, z.score);
  }

void SetStatus(SZone &z, int ns, string why = "")
  {
   if(z.status == ns) return;
   string old = StatusName(z.status);
   z.status = ns;
   Lg(1, ZoneLine(z) + " | " + old + "→" + StatusName(ns) + (why != "" ? " (" + why + ")" : ""));
   if(ns == ZONE_INVALID && !g_zoneCreating)
      Emit("zone_invalid", 2, "info", JS("dir", DirName(z.dir)) + "," + JN("lo", z.lo, g_digits) + "," + JN("hi", z.hi, g_digits) + "," +
           JS("why", why), "✖ Зона снята: " + ZoneTag(z) + (why != "" ? " (" + why + ")" : ""), -1);
  }

long LifeSec(ENUM_TIMEFRAMES tf)
  {
   return tf == PERIOD_M15 ? (long)InpZoneLifeM15 * 3600 : (long)InpZoneLifeHours * 3600;
  }

// Зона рядом с ценой: SELL выше цены или на ней, BUY ниже или на ней, не дальше радиуса
bool NearPrice(int d, double lo, double hi)    // зеркальные lo/hi
  {
   double pm = d * SymbolInfoDouble(g_sym, SYMBOL_BID);
   return lo <= pm && (pm - hi) <= InpZoneRadius;
  }

double ZoneDist(const SZone &z)
  {
   double pm = z.dir * SymbolInfoDouble(g_sym, SYMBOL_BID);
   if(pm > ZHi(z)) return pm - ZHi(z);
   if(pm < ZLo(z)) return ZLo(z) - pm;
   return 0;
  }

bool ZoneInRadius(const SZone &z)
  {
   return NearPrice(z.dir, ZLo(z), ZHi(z));
  }

// Status NEW -> CONFIRMED: после образования цена ушла от зоны на InpConfirmAway+ [§6.3]
void AdvanceNew(SZone &z, const MqlRates &r[], int j)
  {
   if(z.status != ZONE_NEW) return;
   if(Hv(r, j, z.dir) >= ZHi(z) + InpConfirmAway)
      SetStatus(z, ZONE_CONFIRMED, "цена ушла на " + DoubleToString(InpConfirmAway, 0) + "+ пунктов");
  }

void CheckInvalidM15(SZone &z, const MqlRates &m[], int j)
  {
   if(z.status > ZONE_CONFIRMED) return;
   int d = z.dir;
   double c = Cv(m, j, d);
   if(c < ZLo(z) - InpInvalidBeyond) { SetStatus(z, ZONE_INVALID, "M15 закрылась за дальним краем"); return; }
   if(z.htf && c < d * z.lvl4) SetStatus(z, ZONE_INVALID, "пробит Level #4");
  }

// Статус по истории свечей от момента образования зоны (исправление v1)
void ReplayZone(SZone &z)
  {
   z.status = ZONE_NEW;
   MqlRates m5[], m15[];
   int sh5 = iBarShift(g_sym, PERIOD_M5, z.born);
   int n5 = Load(PERIOD_M5, MathMin(MathMax(sh5 + 2, 5), 900), m5);
   for(int j = n5 - 1; j >= 1; j--)
      if(m5[j].time >= z.born && z.status == ZONE_NEW) AdvanceNew(z, m5, j);
   int sh15 = iBarShift(g_sym, PERIOD_M15, z.born);
   int n15 = Load(PERIOD_M15, MathMin(MathMax(sh15 + 2, 5), 400), m15);
   for(int j = n15 - 1; j >= 1; j--)
      if(m15[j].time >= z.born) CheckInvalidM15(z, m15, j);
   if(z.status <= ZONE_CONFIRMED && TimeCurrent() - z.newest > LifeSec(z.topTF))
      SetStatus(z, ZONE_INVALID, "возраст");
  }

void ZoneNotify(SZone &z)
  {
   if(z.status != ZONE_CONFIRMED || z.notified) return;
   z.notified = true;
   Emit("zone_confirmed", 1, "info", JS("dir", DirName(z.dir)) + "," + JN("lo", z.lo, g_digits) + "," + JN("hi", z.hi, g_digits) + "," +
        JN("width", z.hi - z.lo, 1) + "," + JI("score", z.score) + "," + JS("srcs", z.srcs) + "," + JB("htf", z.htf),
        StringFormat("%s Зона %s %s–%s CONFIRMED · балл %d · %s", z.dir > 0 ? "🟩" : "🟥", DirName(z.dir), Px(z.lo), Px(z.hi),
                     z.score, z.srcs), -1);
  }

// Участок региона, где перекрывается больше всего теней FU (первый снизу при равенстве)
bool MaxOverlap(int d, ENUM_TIMEFRAMES tf, datetime since, double rl, double rh, double &ol, double &oh)
  {
   MqlRates m[];
   int sh = iBarShift(g_sym, tf, since);
   int n = Load(tf, MathMin(MathMax(sh + 2, 5), 700), m);
   double A[], B[];
   int k = 0;
   for(int j = 1; j < n - 1; j++)
     {
      if(m[j].time < since) break;
      if(FUClass(m, j, d) < 1) continue;
      double a = MathMax(Lv(m, j, d), rl), b = MathMin(BBot(m, j, d), rh);
      if(b - a < 0.01) continue;
      ArrayResize(A, k + 1);
      ArrayResize(B, k + 1);
      A[k] = a; B[k] = b; k++;
     }
   if(k == 0) return false;
   double P[];
   ArrayResize(P, 2 * k);
   for(int i = 0; i < k; i++) { P[2 * i] = A[i]; P[2 * i + 1] = B[i]; }
   ArraySort(P);
   int best = 0;
   double bl = 0, bh = 0;
   for(int i = 0; i < 2 * k - 1; i++)
     {
      double x1 = P[i], x2 = P[i + 1];
      if(x2 - x1 < 1e-9) continue;
      int cov = 0;
      for(int q = 0; q < k; q++)
         if(A[q] <= x1 + 1e-9 && B[q] >= x2 - 1e-9) cov++;
      if(cov > best) { best = cov; bl = x1; bh = x2; }
      else if(cov == best && cov > 0 && MathAbs(x1 - bh) < 1e-9) bh = x2;
     }
   if(best == 0) return false;
   ol = bl; oh = bh;
   return true;
  }

// Сужение до 3..10 внутри самого сильного источника группы [§6.2, True Stop]
void NarrowGroup(int d, double alo, double ahi, datetime aT0, double &lo, double &hi)
  {
   if(hi - lo > InpZoneMax)
     {
      double rl = MathMax(lo, alo), rh = MathMin(hi, ahi);
      if(rh - rl < 0.01) { rl = lo; rh = hi; }
      ENUM_TIMEFRAMES tfs[2] = {PERIOD_M15, PERIOD_M5};
      for(int t = 0; t < 2 && rh - rl > InpZoneMax; t++)
        {
         double ol, oh;
         if(MaxOverlap(d, tfs[t], aT0, rl, rh, ol, oh)) { rl = ol; rh = oh; }
        }
      lo = rl; hi = rh;
     }
   if(hi - lo > InpZoneMax) hi = lo + InpZoneMax;
   if(hi - lo < InpZoneMin) hi = lo + InpZoneMin;
  }

//+------------------------------------------------------------------+
//| Источники зон [§6.1]                                             |
//+------------------------------------------------------------------+
void AddCand(SCand &c[], int src, ENUM_TIMEFRAMES tf, double lo, double hi, datetime t0,
             int pts, bool fuAfter, double lvl, string label)
  {
   int k = ArraySize(c);
   ArrayResize(c, k + 1);
   c[k].src = src; c[k].tf = tf; c[k].lo = lo; c[k].hi = hi; c[k].t0 = t0;
   c[k].pts = pts; c[k].fuAfter = fuAfter; c[k].lvl = lvl; c[k].label = label;
  }

// Грубый отбор кандидатов (3 радиуса); точный радиус проверяется по готовой зоне
bool NearWide(int d, double lo, double hi)
  {
   double pm = d * SymbolInfoDouble(g_sym, SYMBOL_BID);
   return lo <= pm && (pm - hi) <= 3.0 * InpZoneRadius;
  }

// Источник 1: Order Block (odd-свеча против движения, зона = тело)
void GenOB(ENUM_TIMEFRAMES tf, int d, SCand &c[])
  {
   MqlRates r[];
   int n = Load(tf, tf == PERIOD_H4 ? 70 : (tf == PERIOD_H1 ? 130 : 200), r);
   int cOdd = 0, cWick = 0, cMove = 0, cDead = 0, cFar = 0, cNoFU = 0, cOk = 0, cChk = 0;
   int pts = tf == PERIOD_H4 ? 3 : (tf == PERIOD_H1 ? 2 : 1);
   for(int i = 2; i < n - 1; i++)
     {
      datetime t0 = r[i].time + PeriodSeconds(tf);
      if(TimeCurrent() - t0 > LifeSec(tf)) break;
      cChk++;
      double rg = Rng(r, i, d);
      if(rg <= 0 || !(Cv(r, i, d) < Ov(r, i, d))) { cOdd++; continue; }
      double top = BTop(r, i, d), bot = BBot(r, i, d);
      string tag = StringFormat("OB %s %s %s", TFName(tf), DirName(d), TimeToString(r[i].time, TIME_DATE | TIME_MINUTES));
      if(rg - (top - bot) > InpOBWickPct / 100.0 * rg)
        { cWick++; if(LogOnce(tag + "w")) Lg(2, "Отброшен " + tag + ": тени"); continue; }
      bool moved = false;
      for(int k = i - 1; k >= MathMax(1, i - InpOBMoveBars); k--)
         if(Cv(r, k, d) > Hv(r, i, d)) { moved = true; break; }
      if(!moved)
        { cMove++; if(LogOnce(tag + "m")) Lg(2, "Отброшен " + tag + ": moved"); continue; }
      bool dead = false;
      for(int k = i - 1; k >= 1; k--)
         if(Cv(r, k, d) < bot) { dead = true; break; }
      if(dead)
        { cDead++; if(LogOnce(tag + "d")) Lg(2, "Отброшен " + tag + ": dead"); continue; }
      bool fu = false;
      for(int j = i - 1; j >= MathMax(1, i - 5); j--)
         if(IsFU(r, j, d) && Lv(r, j, d) <= top) { fu = true; break; }
      if(InpOBNeedFU && !fu)
        { cNoFU++; if(LogOnce(tag + "f")) Lg(2, "Отброшен " + tag + ": нет FU"); continue; }
      if(!NearWide(d, bot, top))
        { cFar++; if(LogOnce(tag + "r")) Lg(2, "Отброшен " + tag + ": дальше радиуса"); continue; }
      AddCand(c, 1, tf, bot, top, t0, pts, fu, 0, TFName(tf) + " OB");
      cOk++;
     }
   Lg(2, StringFormat("OB %s %s: проверено %d, odd-отказ %d, тени %d, moved %d, dead %d, нет FU %d, радиус %d, принято %d",
                      TFName(tf), DirName(d), cChk, cOdd, cWick, cMove, cDead, cNoFU, cFar, cOk));
  }

// Источник 2: тень FU на H1 и M15
void GenFU(ENUM_TIMEFRAMES tf, int d, SCand &c[])
  {
   MqlRates r[];
   int n = Load(tf, tf == PERIOD_H1 ? 80 : 200, r);
   int pts = tf == PERIOD_H1 ? 2 : 1;
   for(int i = 1; i < n - 1; i++)
     {
      datetime t0 = r[i].time + PeriodSeconds(tf);
      if(TimeCurrent() - t0 > LifeSec(tf)) break;
      if(FUClass(r, i, d) < 1) continue;
      double a = Lv(r, i, d), b = BBot(r, i, d);
      if(b - a < InpFUZoneMinWick) continue;
      if(!NearWide(d, a, b)) continue;
      AddCand(c, 2, tf, a, b, t0, pts, false, 0, TFName(tf) + " FU");
     }
  }

void AddLiq(int d, double price, bool major)
  {
   for(int k = 0; k < ArraySize(g_liq); k++)
      if(g_liqDir[k] == d && MathAbs(g_liq[k] - price) < PT(0.05))
        {
         if(major) g_liqMajor[k] = true;
         return;
        }
   int n = ArraySize(g_liq);
   ArrayResize(g_liq, n + 1);
   ArrayResize(g_liqDir, n + 1);
   ArrayResize(g_liqMajor, n + 1);
   g_liq[n] = price;
   g_liqDir[n] = d;
   g_liqMajor[n] = major;
  }

// Источник 3: ещё не снятая ликвидность (равные уровни, swing, Doji) -> зона за уровнем
void GenLiq(ENUM_TIMEFRAMES tf, int d, SCand &c[])
  {
   MqlRates r[];
   int n = Load(tf, tf == PERIOD_H1 ? 80 : 200, r);
   for(int i = 1; i < n - 1; i++)
     {
      datetime t0 = r[i].time + PeriodSeconds(tf);
      if(TimeCurrent() - t0 > LifeSec(tf)) break;
      bool sw = IsSwingLow(r, i, d);
      bool dj = !sw && IsDoji(r, i) && !IsFU(r, i, 1) && !IsFU(r, i, -1);
      if(!sw && !dj) continue;
      double L = Lv(r, i, d);
      bool swept = false;
      for(int k = 1; k < i; k++)
         if(Lv(r, k, d) < L) { swept = true; break; }
      if(swept) continue;
      int pts = 2;
      string nm = d > 0 ? (sw ? "SWL" : "DOJI") : (sw ? "SWH" : "DOJI");
      if(sw)
        {
         datetime lim = (datetime)((long)TimeCurrent() - (long)InpZoneLifeHours * 3600);
         for(int j = i + 1; j < n - 2; j++)
           {
            if(r[j].time < lim) break;
            if(IsSwingLow(r, j, d) && MathAbs(Lv(r, j, d) - L) <= InpEqTol)
              {
               pts = 3;
               nm = d > 0 ? "EQL" : "EQH";
               break;
              }
           }
        }
      AddLiq(d, d * L, pts == 3 || dj);
      double lo = L - InpSweepZone, hi = L + PT(1.0);
      if(!NearWide(d, lo, hi)) continue;
      AddCand(c, 3, tf, lo, hi, t0, pts, false, L, StringFormat("%s %s %s", TFName(tf), nm, Px(d * L)));
     }
  }

//+------------------------------------------------------------------+
//| Слияние, баллы, учёт зон [§6.2]                                  |
//+------------------------------------------------------------------+
int CandStrength(const SCand &c)
  {
   if(c.src == 1 || c.src == 3) return 10 + c.pts;   // OB H4/H1, EQH/EQL сильнее теней FU
   return c.pts;
  }

void UpsertGroup(int d, SCand &c[], int &mem[], int ai, double glo, double ghi)
  {
   double nlo = glo, nhi = ghi;
   NarrowGroup(d, c[ai].lo, c[ai].hi, c[ai].t0, nlo, nhi);

   // в баллы и подпись идут только источники, попавшие в суженную зону
   int obP = 0, fuP = 0, nFU = 0, liqP = 0;
   bool fuAfter = false;
   datetime formed = 0;
   ENUM_TIMEFRAMES topTF = PERIOD_M15;
   int topRank = 0;
   string sOb = "", sFu = "", sLq = "";
   double lv[6];
   int nl = 0;
   for(int q = 0; q < ArraySize(mem); q++)
     {
      int k = mem[q];
      if(k != ai && (c[k].hi < nlo || c[k].lo > nhi)) continue;
      if(c[k].src == 1)
        {
         obP = MathMax(obP, c[k].pts);
         if(c[k].fuAfter) fuAfter = true;
         if(StringFind(sOb, c[k].label) < 0) sOb += (sOb == "" ? "" : " + ") + c[k].label;
        }
      else if(c[k].src == 2)
        {
         fuP = MathMax(fuP, c[k].pts);
         nFU++;
         if(StringFind(sFu, c[k].label) < 0) sFu += (sFu == "" ? "" : " + ") + c[k].label;
        }
      else
        {
         liqP = MathMax(liqP, c[k].pts);
         if(StringFind(sLq, c[k].label) < 0) sLq += (sLq == "" ? "" : " + ") + c[k].label;
         if(nl < 6 && c[k].lvl >= nlo - PT(2.0) && c[k].lvl <= nhi + PT(2.0)) { lv[nl] = d * c[k].lvl; nl++; }
        }
      formed = MathMax(formed, c[k].t0);          // зона образовалась с последним попавшим в неё источником
      int rk = c[k].tf == PERIOD_H4 ? 3 : (c[k].tf == PERIOD_H1 ? 2 : 1);
      if(rk > topRank) { topRank = rk; topTF = c[k].tf; }
     }
   string srcs = sOb;
   if(sFu != "") srcs += (srcs == "" ? "" : " + ") + sFu;
   if(sLq != "") srcs += (srcs == "" ? "" : " + ") + sLq;
   int srcScore = obP + fuP + MathMin(3, MathMax(0, nFU - 1)) + liqP + (fuAfter ? 1 : 0);

   double rulo = d > 0 ? glo : -ghi, ruhi = d > 0 ? ghi : -glo;
   double zlo = NormalizeDouble(d > 0 ? nlo : -nhi, g_digits);
   double zhi = NormalizeDouble(d > 0 ? nhi : -nlo, g_digits);

   // поиск существующей зоны по объединению источников; одна зона обновляется один раз за пересчёт
   int act = -1;
   bool deadCover = false;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].dir != d || g_zones[i].updId == g_rebuildId) continue;
      if(g_zones[i].uhi < rulo - PT(1.0) || g_zones[i].ulo > ruhi + PT(1.0)) continue;
      if(g_zones[i].status <= ZONE_CONFIRMED) { act = i; break; }
      if(g_zones[i].newest >= formed) deadCover = true;
     }
   if(act < 0 && deadCover)
     {
      string tag = StringFormat("%s %s–%s", DirName(d), Px(zlo), Px(zhi));
      if(LogOnce("dup" + tag + TimeToString(formed))) Lg(2, "Кандидат " + tag + " | " + srcs + ": уже построена");
      return;
     }
   if(act < 0 && !NearPrice(d, d > 0 ? zlo : -zhi, d > 0 ? zhi : -zlo))
     {
      string tag = StringFormat("%s %s–%s", DirName(d), Px(zlo), Px(zhi));
      if(LogOnce("far" + tag + TimeToString(formed))) Lg(2, "Кандидат " + tag + " | " + srcs + ": дальше радиуса");
      return;
     }

   bool isNew = (act < 0);
   int idx = act;
   if(isNew)
     {
      idx = ArraySize(g_zones);
      ArrayResize(g_zones, idx + 1);
      g_zones[idx].dir = d;
      g_zones[idx].hasHCS = false;
      g_zones[idx].notified = false;
      g_zones[idx].touched = false;
      g_zones[idx].lastSkip = "";
      g_zones[idx].lastSkipT = 0;
      g_zones[idx].status = ZONE_NEW;
      g_zones[idx].tradable = false;
      g_zones[idx].lo = zlo;
      g_zones[idx].hi = zhi;
      g_zones[idx].born = formed;
      g_zones[idx].newest = formed;
     }
   bool rangeChanged = isNew || (g_zones[idx].status == ZONE_NEW &&
                                 (MathAbs(g_zones[idx].lo - zlo) > PT(0.005) || MathAbs(g_zones[idx].hi - zhi) > PT(0.005)));
   if(!isNew && rangeChanged)
     {
      g_zones[idx].lo = zlo;
      g_zones[idx].hi = zhi;
      g_zones[idx].born = formed;
      g_zones[idx].newest = formed;
     }
   g_zones[idx].updId = g_rebuildId;
   g_zones[idx].ulo = rulo;
   g_zones[idx].uhi = ruhi;
   g_zones[idx].newest = MathMax(g_zones[idx].newest, formed);
   g_zones[idx].topTF = topTF;
   g_zones[idx].srcs = srcs;
   g_zones[idx].srcScore = srcScore;
   g_zones[idx].nLvl = nl;
   for(int k = 0; k < nl; k++) g_zones[idx].lvl[k] = lv[k];
   g_zones[idx].htf = HtfMatch(d, g_zones[idx].lo, g_zones[idx].hi, g_zones[idx].lvl4);
   int bonus = (g_zones[idx].htf ? 1 : 0) + (ZoneInFVG(g_zones[idx].lo, g_zones[idx].hi) ? -2 : 0) +
               (g_zones[idx].hasHCS ? 2 : 0);
   g_zones[idx].score = srcScore + bonus;
   g_zoneCreating = isNew;                       // при создании зоны событие zone_invalid не шлём
   if(rangeChanged) ReplayZone(g_zones[idx]);
   g_zoneCreating = false;
   if(isNew)
     {
      if(g_zones[idx].status == ZONE_INVALID)
         Lg(2, ZoneLine(g_zones[idx]) + " | создана и сразу INVALID");
      else
         Lg(1, ZoneLine(g_zones[idx]) + " | новая → " + StatusName(g_zones[idx].status));
     }
   ZoneNotify(g_zones[idx]);
  }

void SelectTradable()
  {
   for(int i = 0; i < ArraySize(g_zones); i++) g_zones[i].tradable = false;
   for(int side = -1; side <= 1; side += 2)
      for(int pick = 0; pick < InpMaxZonesSide; pick++)
        {
         int bi = -1;
         double bd = DBL_MAX;
         for(int i = 0; i < ArraySize(g_zones); i++)
           {
            if(g_zones[i].dir != side || g_zones[i].tradable) continue;
            if(g_zones[i].status > ZONE_CONFIRMED || !ZoneInRadius(g_zones[i])) continue;
            double dist = ZoneDist(g_zones[i]);
            if(dist < bd) { bd = dist; bi = i; }
           }
         if(bi >= 0) g_zones[bi].tradable = true;
        }
  }

void PruneZones()
  {
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
      if(TimeCurrent() - g_zones[i].newest > 5 * 86400)
        {
         int last = ArraySize(g_zones) - 1;
         if(i != last) g_zones[i] = g_zones[last];
         ArrayResize(g_zones, last);
        }
  }

// Пересчёт зон на каждой новой свече M15 [§6.3]
void RebuildZones()
  {
   g_rebuildId++;
   ArrayResize(g_liq, 0);
   ArrayResize(g_liqDir, 0);
   ArrayResize(g_liqMajor, 0);
   for(int s = 0; s < 2; s++)
     {
      int d = s == 0 ? 1 : -1;
      if((d > 0 && !g_set.tradeBuy) || (d < 0 && !g_set.tradeSell)) continue;
      FindHTFLevels(d);
      SCand c[];
      ArrayResize(c, 0);
      GenOB(PERIOD_H4, d, c);
      GenOB(PERIOD_H1, d, c);
      if(InpUseM15OB) GenOB(PERIOD_M15, d, c);
      GenFU(PERIOD_H1, d, c);
      GenFU(PERIOD_M15, d, c);
      GenLiq(PERIOD_H1, d, c);
      GenLiq(PERIOD_M15, d, c);
      int n = ArraySize(c);
      bool used[];
      ArrayResize(used, n);
      for(int k = 0; k < n; k++) used[k] = false;
      while(true)
        {
         // якорь кластера: самый сильный кандидат, при равенстве самый свежий
         int ai = -1, bst = -1;
         for(int k = 0; k < n; k++)
           {
            if(used[k]) continue;
            int st = CandStrength(c[k]);
            if(st > bst || (st == bst && ai >= 0 && c[k].t0 > c[ai].t0)) { bst = st; ai = k; }
           }
         if(ai < 0) break;
         double glo = c[ai].lo, ghi = c[ai].hi;
         double limit = MathMax(2.0 * InpZoneMax, ghi - glo);   // группа не шире 2×InpZoneMax
         int mem[];
         ArrayResize(mem, 1);
         mem[0] = ai;
         used[ai] = true;
         bool added = true;
         while(added)
           {
            added = false;
            for(int k = 0; k < n; k++)
              {
               if(used[k]) continue;
               if(c[k].lo > ghi + PT(1.0) || c[k].hi < glo - PT(1.0)) continue;
               double nl2 = MathMin(glo, c[k].lo), nh2 = MathMax(ghi, c[k].hi);
               if(nh2 - nl2 > limit) continue;
               glo = nl2; ghi = nh2;
               used[k] = true;
               int m = ArraySize(mem);
               ArrayResize(mem, m + 1);
               mem[m] = k;
               added = true;
              }
           }
         UpsertGroup(d, c, mem, ai, glo, ghi);
        }
     }
   PruneZones();
   SelectTradable();
   int ns = 0, nb = 0;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].status > ZONE_CONFIRMED || !ZoneInRadius(g_zones[i])) continue;
      if(g_zones[i].dir > 0) nb++; else ns++;
     }
   Lg(1, StringFormat("Bias D1=%s H4=%s | зон SELL: %d, BUY: %d", BiasName(g_d1), BiasName(g_h4), ns, nb));
  }

// Статусы на каждой новой свече M5
void UpdateZoneStates()
  {
   MqlRates m5[], m15[];
   bool h5 = Load(PERIOD_M5, 5, m5) >= 3;
   bool h15 = Load(PERIOD_M15, 5, m15) >= 3;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].status > ZONE_CONFIRMED) continue;
      if(TimeCurrent() - g_zones[i].newest > LifeSec(g_zones[i].topTF))
        {
         SetStatus(g_zones[i], ZONE_INVALID, "возраст");
         continue;
        }
      if(h15) CheckInvalidM15(g_zones[i], m15, 1);
      if(g_zones[i].status > ZONE_CONFIRMED) continue;
      if(h5) AdvanceNew(g_zones[i], m5, 1);
      ZoneNotify(g_zones[i]);
     }
   SelectTradable();
  }

//+------------------------------------------------------------------+
//| Триггеры A-D [§7]                                                |
//+------------------------------------------------------------------+
int TrigPts(string trig, bool strong)
  {
   if(trig == "A") return 1;
   if(trig == "B") return strong ? 3 : 2;            // HCS x2/x3 — старт Order Flow
   if(trig == "C") return strong ? 2 : 0;
   if(trig == "K" || trig == "F" || trig == "S") return 1;
   return 0;
  }

void FillSig(SSig &s, string trig, int cls, bool strong, double ext, string mark, datetime t)
  {
   s.ok = true; s.trig = trig; s.cls = cls; s.strong = strong; s.ext = ext; s.mark = mark; s.t = t;
   s.pts = TrigPts(trig, strong) + (cls == 3 ? 2 : (cls == 2 ? 1 : 0));
  }

void AddWhy(string &why, string m)
  {
   if(StringFind(why, m) >= 0) return;
   why += (why == "" ? "" : "; ") + m;
  }

// A: Manipulation Cycle (точки 1 и 2 ждём, вход на 3)
bool TriggerA(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir, n = ArraySize(r);
   double zl = ZLo(z), zh = ZHi(z);
   int k = -1;
   for(int x = 3; x <= 30 && x + 1 < n; x++)
     {
      double L = Lv(r, x, d);
      if(L < Lv(r, x - 1, d) && L < Lv(r, x + 1, d) && L >= zl - InpPierceMax && L <= zh) { k = x; break; }
     }
   if(k < 0) { AddWhy(why, "A: нет точки 1 в зоне"); return false; }
   double prevLow = Lv(r, k, d);
   double pierce = DBL_MAX;
   int jp = -1;
   for(int q = 1; q < k; q++)
      if(Lv(r, q, d) < pierce) { pierce = Lv(r, q, d); jp = q; }
   if(jp < 0 || !(pierce < prevLow)) { AddWhy(why, "A: точка 1 " + Px(d * prevLow) + " не проколота"); return false; }
   if(pierce < zl - InpSweepMax)
     {
      AddWhy(why, StringFormat("A: прокол %s дальше допуска %s", Px(d * pierce), Px(d * (zl - InpSweepMax))));
      return false;
     }
   if(jp > InpTrigLookback) { AddWhy(why, "A: прокол старше " + IntegerToString(InpTrigLookback) + " свечей"); return false; }
   if(!(Cv(r, 1, d) > prevLow)) { AddWhy(why, "A: нет закрытия обратно за " + Px(d * prevLow)); return false; }
   int cls = MathMax(FUClass(r, 1, d), FUClass(r, jp, d));
   if(cls < 1 && (IsLowerWick(r, jp, d) || IsLowerWick(r, 1, d))) cls = 1;
   int need = Relaxed() ? 1 : (int)InpMinFUClass;
   if(cls < need)
     {
      AddWhy(why, StringFormat("A: FU класс %s < %s", ClsName(cls), ClsName(need)));
      return false;
     }
   FillSig(s, "A", cls, false, pierce, "FU", r[1].time);
   return true;
  }

// B: HCS — FU-2 вернулся в тень FU-1 в зоне
bool TriggerB(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir, n = ArraySize(r);
   double zl = ZLo(z), zh = ZHi(z);
   int cls = FUClass(r, 1, d);
   if(cls < 1) return false;
   double L2 = Lv(r, 1, d), ext = L2;
   int cnt = 0;
   for(int m = 3; m <= 60 && m + 1 < n; m++)
     {
      if(!IsFU(r, m, d)) continue;
      double L1 = Lv(r, m, d), b1 = BBot(r, m, d);
      if(L1 < zl - InpPierceMax || L1 > zh) continue;
      if(L2 >= L1 - InpHCSTol && L2 <= b1) { cnt++; ext = MathMin(ext, L1); }
     }
   if(cnt == 0) return false;
   // HCS x2 / x3: FU перетестирует сразу несколько FU в зоне — уже старт Order Flow [§7]
   FillSig(s, "B", cls, cnt >= 2, ext, cnt >= 2 ? "HCSx" + IntegerToString(MathMin(cnt, 3)) : "HCS", r[1].time);
   return true;
  }

// C: Negation в зоне
bool TriggerC(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir;
   int ng = Negation(r, 1, d);
   if(ng == 0) return false;
   double zl = ZLo(z), zh = ZHi(z);
   double deep = MathMin(Lv(r, 1, d), Lv(r, 2, d));
   if(deep < zl - InpSweepMax || deep > zh) return false;
   FillSig(s, "C", FUClass(r, 1, d), ng == 2, deep, "NEG", r[1].time);
   return true;
  }

// D: снятие ликвидности у зоны — Manipulation Cycle [§8]. Точка 2: цена ушла за уровень
// ликвидности (равные хаи/лои, swing, Doji) одной или несколькими свечами подряд (до InpSweepBars),
// точка 3: закрытие M5 обратно за уровень — вход. Если свеча возврата была Doji (книга: ждать),
// вход ещё на следующей свече, если она тоже закрылась за уровнем. Стоп — за крайней точкой захода.
bool TriggerD(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir, n = ArraySize(r);
   if(n < InpSweepBars + 20) return false;
   double zl = ZLo(z), zh = ZHi(z);
   double lv[];
   int nl = 0;
   for(int k = 0; k < z.nLvl; k++) { ArrayResize(lv, nl + 1); lv[nl] = d * z.lvl[k]; nl++; }
   for(int k = 0; k < ArraySize(g_liq); k++)
      if(g_liqDir[k] == d) { ArrayResize(lv, nl + 1); lv[nl] = d * g_liq[k]; nl++; }
   bool anyNear = false;
   for(int k = 0; k < nl; k++)
     {
      double m = lv[k];
      if(m < zl - InpPierceMax || m > zh + InpPierceMax) continue;
      anyNear = true;
      // точка 3: свеча возврата jr (последняя или предыдущая)
      int jr = 0;
      if(Cv(r, 1, d) > m)
        {
         if(Cv(r, 2, d) <= m || Lv(r, 1, d) < m - InpLqMin) jr = 1;
         else if(Cv(r, 3, d) <= m || Lv(r, 2, d) < m - InpLqMin) jr = 2;
        }
      else if(Lv(r, 1, d) < m)
         AddWhy(why, "D: после снятия " + Px(d * m) + " нет закрытия обратно");
      if(jr == 0) continue;
      // точка 2: свечи за уровнем до возврата (закрытие за уровнем или прокол)
      int ks = jr;
      bool tooLong = false;
      for(int j = jr + 1; j < n - 1; j++)
        {
         if(!(Cv(r, j, d) <= m || Lv(r, j, d) < m - InpLqMin)) break;
         ks = j;
         if(j - jr > InpSweepBars) { tooLong = true; break; }
        }
      if(tooLong)
        {
         AddWhy(why, StringFormat("D: за уровнем %s дольше %d свечей — пробой, не заход", Px(d * m), InpSweepBars));
         continue;
        }
      bool old = false;                       // уровень уже снимали в последний час до этого захода
      for(int j = ks + 1; j <= ks + 12 && j < n; j++)
         if(Lv(r, j, d) < m - InpLqMin) { old = true; break; }
      if(old) continue;
      double ext = DBL_MAX;
      int cls = 0;
      bool wick = false;
      for(int j = 1; j <= ks; j++)
        {
         ext = MathMin(ext, Lv(r, j, d));
         cls = MathMax(cls, FUClass(r, j, d));
         if(IsLowerWick(r, j, d)) wick = true;
        }
      if(ext < zl - InpSweepMax)
        {
         AddWhy(why, StringFormat("D: заход до %s дальше допуска %s", Px(d * ext), Px(d * (zl - InpSweepMax))));
         continue;
        }
      if(cls < 1 && !wick) { AddWhy(why, "D: заход за " + Px(d * m) + " без FU и тени"); continue; }
      if(cls < 1) cls = 1;
      FillSig(s, "D", cls, false, ext, "SWEEP", r[1].time);
      return true;
     }
   if(!anyNear) AddWhy(why, "D: нет уровней ликвидности у зоны");
   return false;
  }

// E: отбой от зоны — свеча коснулась зоны и закрылась в сторону сделки с тенью к зоне
bool TriggerE(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir;
   double zl = ZLo(z), zh = ZHi(z);
   double L = Lv(r, 1, d);
   if(L > zh) return false;                                   // зону не задела
   if(L < zl - InpPierceMax) { AddWhy(why, "E: свеча ушла за зону дальше допуска"); return false; }
   if(!(Cv(r, 1, d) > Ov(r, 1, d))) { AddWhy(why, "E: свеча не развернулась"); return false; }
   double rg = Rng(r, 1, d);
   if(rg <= 0) return false;
   double wick = BBot(r, 1, d) - L;
   if(wick < InpTouchWickPct / 100.0 * rg) { AddWhy(why, "E: тень к зоне меньше " + DoubleToString(InpTouchWickPct, 0) + "%"); return false; }
   if(!(Cv(r, 1, d) >= (zl + zh) / 2.0)) { AddWhy(why, "E: закрытие глубоко в зоне"); return false; }
   FillSig(s, "E", FUClass(r, 1, d), false, L, "TOUCH", r[1].time);
   return true;
  }

// F: FU из трёх свечей [§4 «3 Candle FU»] — свечи [3], [2], [1] как одна «склеенная» свеча:
// её лой снял лой свечи [4], закрытие [1] вернулось выше этого лоя, тело в сторону сделки,
// нижняя тень склеенной свечи не меньше InpWickPct. Крайняя точка — в зоне.
bool TriggerF(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir;
   if(ArraySize(r) < 8) return false;
   double o = Ov(r, 3, d), c = Cv(r, 1, d);
   double h = MathMax(Hv(r, 1, d), MathMax(Hv(r, 2, d), Hv(r, 3, d)));
   double l = MathMin(Lv(r, 1, d), MathMin(Lv(r, 2, d), Lv(r, 3, d)));
   double rg = h - l;
   if(rg <= 0) return false;
   if(!(l < Lv(r, 4, d) - InpLqMin)) return false;                 // ликвидность свечи [4] не снята
   if(!(c > Lv(r, 4, d) && c > o)) return false;                    // нет возврата / тело не в сторону сделки
   if(MathMin(o, c) - l < InpWickPct / 100.0 * rg) { AddWhy(why, "F: короткая тень у FU из 3 свечей"); return false; }
   if(l < ZLo(z) - InpSweepMax || l > ZHi(z)) return false;
   FillSig(s, "F", 1, false, l, "3FU", r[1].time);
   return true;
  }

// S: книга — на касании не входить, ждать пробой предыдущего хая/лоя. Крайняя точка последних
// 12 свечей M5 лежит в зоне (касание), затем свеча закрылась в сторону сделки выше ближайшего
// локального хая перед этой точкой — первый такой пробой. Стоп — за крайней точкой.
bool TriggerS(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir, n = ArraySize(r);
   if(n < 30) return false;
   double zl = ZLo(z), zh = ZHi(z);
   int jx = -1;
   double ext = DBL_MAX;
   for(int j = 1; j <= 12; j++)
      if(Lv(r, j, d) < ext) { ext = Lv(r, j, d); jx = j; }
   if(jx < 2) return false;                                         // крайняя точка на текущей свече
   if(ext < zl - InpSweepMax || ext > zh) return false;
   double ph = 0;
   bool found = false;
   for(int x = jx + 1; x <= jx + 10 && x + 1 < n; x++)
      if(Hv(r, x, d) >= Hv(r, x - 1, d) && Hv(r, x, d) >= Hv(r, x + 1, d)) { ph = Hv(r, x, d); found = true; break; }
   if(!found) return false;
   if(!(Cv(r, 1, d) > ph && Cv(r, 1, d) > Ov(r, 1, d)))
     {
      AddWhy(why, "S: нет пробоя хая " + Px(d * ph));
      return false;
     }
   for(int k = 2; k < jx; k++)
      if(Cv(r, k, d) > ph) return false;                            // пробой уже был раньше
   int cls = 0;
   for(int j = 1; j <= jx; j++) cls = MathMax(cls, FUClass(r, j, d));
   FillSig(s, "S", cls, false, ext, "BOS", r[1].time);
   return true;
  }

// Книга «Oddiy savdo kitobi»: молот (d=+1) / падающая звезда (d=-1) — маленькое тело,
// длинная тень в сторону зоны, противоположная тень короткая
bool IsPin(const MqlRates &r[], int i, int d)
  {
   double rg = Rng(r, i, d);
   if(rg <= 0) return false;
   double body = BTop(r, i, d) - BBot(r, i, d);
   double tail = BBot(r, i, d) - Lv(r, i, d);
   double nose = Hv(r, i, d) - BTop(r, i, d);
   return body <= 0.35 * rg && tail >= 2.0 * body && tail >= 0.55 * rg && nose <= 0.2 * rg;
  }

// Книга: утренняя (d=+1) / вечерняя (d=-1) звезда на свечах [3], [2], [1]
bool IsStar3(const MqlRates &r[], int d)
  {
   double rg3 = Rng(r, 3, d), b3 = BTop(r, 3, d) - BBot(r, 3, d);
   if(rg3 <= 0 || b3 < 0.5 * rg3 || !(Cv(r, 3, d) < Ov(r, 3, d))) return false;
   double b2 = BTop(r, 2, d) - BBot(r, 2, d);
   if(b2 > 0.35 * b3) return false;
   if(!(Cv(r, 1, d) > Ov(r, 1, d))) return false;
   return Cv(r, 1, d) > (Ov(r, 3, d) + Cv(r, 3, d)) / 2.0;
  }

// Книга: три белых солдата (d=+1) / три чёрных вороны (d=-1) на свечах [3], [2], [1]: каждая
// в сторону сделки, тело не меньше половины свечи, каждое закрытие дальше предыдущего
bool Is3Soldiers(const MqlRates &r[], int d)
  {
   for(int i = 1; i <= 3; i++)
     {
      double rg = Rng(r, i, d);
      if(rg <= 0 || !(Cv(r, i, d) > Ov(r, i, d))) return false;
      if(BTop(r, i, d) - BBot(r, i, d) < 0.5 * rg) return false;
     }
   return Cv(r, 1, d) > Cv(r, 2, d) && Cv(r, 2, d) > Cv(r, 3, d);
  }

// Книга: повешенный (только SELL) — маленькое тело наверху, длинная нижняя тень, верхняя тень короткая
bool IsHangingMan(const MqlRates &r[], int i)
  {
   double rg = r[i].high - r[i].low;
   if(rg <= 0) return false;
   double top = MathMax(r[i].open, r[i].close), bot = MathMin(r[i].open, r[i].close);
   double body = top - bot, tail = bot - r[i].low;
   return body <= 0.35 * rg && tail >= 2.0 * body && tail >= 0.55 * rg && (r[i].high - top) <= 0.2 * rg;
  }

// K: свечные модели из книги, только внутри зоны Mr. Casino
bool TriggerK(const SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   int d = z.dir;
   if(ArraySize(r) < 6) return false;
   double zl = ZLo(z), zh = ZHi(z);
   double ext = 0;
   // молот / падающая звезда: по книге вход после свечи-подтверждения
   if(InpBookConfirm)
     {
      if(IsPin(r, 2, d) && Cv(r, 1, d) > Ov(r, 1, d) && Cv(r, 1, d) > BTop(r, 2, d))
        {
         ext = MathMin(Lv(r, 2, d), Lv(r, 1, d));
         if(ext >= zl - InpSweepMax && ext <= zh)
           {
            FillSig(s, "K", MathMax(FUClass(r, 2, d), 1), false, ext, d > 0 ? "HAMMER" : "STAR", r[1].time);
            return true;
           }
        }
     }
   else if(IsPin(r, 1, d))
     {
      ext = Lv(r, 1, d);
      if(ext >= zl - InpSweepMax && ext <= zh)
        {
         FillSig(s, "K", MathMax(FUClass(r, 1, d), 1), false, ext, d > 0 ? "HAMMER" : "STAR", r[1].time);
         return true;
        }
     }
   // поглощение
   if(Cv(r, 2, d) < Ov(r, 2, d) && IsEngulf(r, 1, d))
     {
      ext = MathMin(Lv(r, 1, d), Lv(r, 2, d));
      if(ext >= zl - InpSweepMax && ext <= zh)
        {
         FillSig(s, "K", MathMax(FUClass(r, 1, d), 1), false, ext, "ENGULF", r[1].time);
         return true;
        }
     }
   // утренняя / вечерняя звезда
   if(IsStar3(r, d))
     {
      ext = MathMin(Lv(r, 1, d), MathMin(Lv(r, 2, d), Lv(r, 3, d)));
      if(ext >= zl - InpSweepMax && ext <= zh)
        {
         FillSig(s, "K", MathMax(FUClass(r, 1, d), 1), false, ext, d > 0 ? "MSTAR" : "ESTAR", r[1].time);
         return true;
        }
     }
   // три белых солдата / три чёрных вороны от зоны
   if(Is3Soldiers(r, d))
     {
      ext = MathMin(Lv(r, 3, d), Lv(r, 4, d));
      if(ext >= zl - InpSweepMax && ext <= zh)
        {
         FillSig(s, "K", 1, false, ext, d > 0 ? "3SOLD" : "3CROW", r[1].time);
         return true;
        }
     }
   // повешенный в зоне продаж + медвежья свеча-подтверждение, закрытие ниже его тела
   if(d < 0 && IsHangingMan(r, 2) && r[1].close < r[1].open && r[1].close < MathMin(r[2].open, r[2].close))
     {
      ext = MathMin(Lv(r, 2, d), Lv(r, 1, d));
      if(ext >= zl - InpSweepMax && ext <= zh)
        {
         FillSig(s, "K", 1, false, ext, "HANGMAN", r[1].time);
         return true;
        }
     }
   AddWhy(why, "K: нет модели из книги");
   return false;
  }

// Прямой вход: BIW, за которым сразу Doji
bool TriggerDirect(const SZone &z, const MqlRates &r[], SSig &s)
  {
   int d = z.dir;
   if(!IsDoji(r, 1) || !IsBIW(r, 2, d)) return false;
   double deep = MathMin(Lv(r, 1, d), Lv(r, 2, d));
   if(deep < ZLo(z) - InpPierceMax || deep > ZHi(z)) return false;
   FillSig(s, "DIRECT", 0, false, deep, "BIW", r[1].time);
   return true;
  }

bool EvalZone(SZone &z, const MqlRates &r[], SSig &s, string &why)
  {
   s.ok = false; s.pts = 0; s.cls = 0; s.trig = ""; s.ext = 0; s.mark = ""; s.t = 0; s.strong = false;
   SSig best, t;
   best.ok = false; best.pts = -1;
   string w = "";
   t.ok = false;
   if(InpUseTrigB && TriggerB(z, r, t, w))
     {
      z.hasHCS = true;
      z.score = z.srcScore + (z.htf ? 1 : 0) + 2 + (ZoneInFVG(z.lo, z.hi) ? -2 : 0);
      if(t.pts > best.pts) best = t;
     }
   t.ok = false;
   if(InpUseTrigA && TriggerA(z, r, t, w) && t.pts > best.pts) best = t;
   t.ok = false;
   if(InpUseTrigD && TriggerD(z, r, t, w) && t.pts > best.pts) best = t;
   t.ok = false;
   if(InpUseTrigC && TriggerC(z, r, t, w) && t.pts > best.pts) best = t;
   t.ok = false;
   if(InpUseTrigF && TriggerF(z, r, t, w) && t.pts > best.pts) best = t;
   t.ok = false;
   if(InpUseBook && TriggerK(z, r, t, w) && t.pts > best.pts) best = t;
   t.ok = false;
   if(InpUseTrigS && TriggerS(z, r, t, w) && t.pts > best.pts) best = t;
   t.ok = false;
   if((InpUseTrigE || Relaxed()) && !best.ok && TriggerE(z, r, t, w)) best = t;
   t.ok = false;
   if(InpDirectEntry && !best.ok && TriggerDirect(z, r, t)) best = t;
   if(!best.ok)
     {
      why = w == "" ? "нет триггера (A, B, C, D, F, K, S)" : w;
      return false;
     }
   s = best;
   return true;
  }

//+------------------------------------------------------------------+
//| Позиции, лот, маржа [§2, §8]                                     |
//+------------------------------------------------------------------+
bool FindPos(ulong &tk, long &id, int &d, double &vol, double &op, double &sl, double &tp)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      tk = t;
      id = PositionGetInteger(POSITION_IDENTIFIER);
      d = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      vol = PositionGetDouble(POSITION_VOLUME);
      op = PositionGetDouble(POSITION_PRICE_OPEN);
      sl = PositionGetDouble(POSITION_SL);
      tp = PositionGetDouble(POSITION_TP);
      return true;
     }
   return false;
  }

bool HasPos()
  {
   ulong tk; long id; int d; double v, o, s, t;
   return FindPos(tk, id, d, v, o, s, t);
  }

ENUM_RISK_MODE EffectiveRiskMode()
  {
   ENUM_RISK_MODE m = InpRiskMode;
   if((m == RISK_HALF || m == RISK_FULL) &&
      AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL)
      m = RISK_PERCENT;
   return m;
  }

// Риск на сделку, % баланса. PERCENT с InpRiskPercent = 0: риск в день / сделок в день (5% / 5 = 1%)
double TradeRiskPct(ENUM_RISK_MODE mode)
  {
   if(mode == RISK_HALF) return 50.0;
   if(mode == RISK_FULL) return 100.0;
   if(g_set.riskPct > 0) return g_set.riskPct;                       // задан явно (входной параметр или Telegram)
   if(g_set.dailyRiskPct > 0 && g_set.maxTrades > 0) return g_set.dailyRiskPct / g_set.maxTrades;
   return 1.0;
  }

// Баланс на начало дня (Бишкек) = текущий баланс минус закрытый результат робота за сегодня
double DayStartBalance()
  {
   return AccountInfoDouble(ACCOUNT_BALANCE) - g_day.usd;
  }

// Остаток дневного риска в валюте счёта: InpDailyRiskPct от баланса начала дня минус убыток дня
double DailyRiskLeft()
  {
   if(g_set.dailyRiskPct <= 0) return DBL_MAX;
   return DayStartBalance() * g_set.dailyRiskPct / 100.0 - MathMax(0.0, -g_day.usd);
  }

// Лот [§2]: по риску на сделку, но не больше остатка дневного риска. Если лот по риску меньше
// минимального — минимальный лот, когда он влезает в дневной риск (InpMinLotIfFits).
// Возвращает 0 и причину в why, если сделку открыть нельзя; riskUsd — риск сделки в валюте счёта.
double CalcLot(double slDist, int d, double entry, double &riskUsd, string &why, double riskOverride = -1.0)
  {
   riskUsd = 0;
   why = "";
   double step = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_STEP);
   double minv = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN);
   double maxv = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MAX);
   if(step <= 0) step = 0.01;
   double tv = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_VALUE);
   double ts = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_SIZE);
   double lossPerLot = (tv > 0 && ts > 0) ? slDist / ts * tv : 0;
   ENUM_RISK_MODE mode = EffectiveRiskMode();
   double lot;
   if(mode == RISK_FIXED)
      lot = InpFixedLot;
   else
     {
      if(lossPerLot <= 0) { why = "нет стоимости тика от брокера"; return 0; }
      lot = AccountInfoDouble(ACCOUNT_BALANCE) * (riskOverride > 0 ? riskOverride : TradeRiskPct(mode)) / 100.0 / lossPerLot;
     }
   lot = MathFloor(lot / step + 1e-9) * step;
   double capLot = DBL_MAX;
   if(g_set.dailyRiskPct > 0 && lossPerLot > 0)
      capLot = MathFloor(DailyRiskLeft() / lossPerLot / step + 1e-6) * step;
   lot = MathMin(lot, capLot);
   if(lot < minv - 1e-12)
     {
      if(capLot < minv - 1e-12)
        {
         why = StringFormat("мин. лот %.2f = риск %.2f, остаток дневного риска %.2f", minv, minv * lossPerLot,
                            MathMax(0.0, DailyRiskLeft()));
         return 0;
        }
      if(!InpMinLotIfFits)
        {
         why = StringFormat("лот по риску меньше мин. %.2f (риск мин. лота %.2f)", minv, minv * lossPerLot);
         return 0;
        }
      lot = minv;
     }
   lot = MathMin(lot, maxv);
   ENUM_ORDER_TYPE ot = d > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   while(lot >= minv - 1e-12)
     {
      double m = 0;
      if(OrderCalcMargin(ot, g_sym, lot, entry, m) && m * (1.0 + InpMarginReserve / 100.0) <= free)
        {
         lot = NormalizeDouble(lot, 2);
         riskUsd = lot * lossPerLot;
         return lot;
        }
      lot -= step;
     }
   why = "не хватает маржи на минимальный лот";
   return 0;
  }

double MinStopDist()
  {
   double lvl = (double)MathMax(SymbolInfoInteger(g_sym, SYMBOL_TRADE_STOPS_LEVEL),
                                SymbolInfoInteger(g_sym, SYMBOL_TRADE_FREEZE_LEVEL));
   return lvl * g_point + g_point;
  }

//+------------------------------------------------------------------+
//| Статистика дня по истории счёта [§9]                             |
//+------------------------------------------------------------------+
SDay CalcDay()
  {
   SDay s;
   s.trades = 0; s.counter = 0; s.pts = 0; s.usd = 0; s.lossStreak = 0; s.lastLossBuy = 0; s.lastLossSell = 0;
   if(!HistorySelect(DayStartSrv(), TimeCurrent() + 86400)) return s;
   long ids[];
   double inP[], inV[], outW[], outV[], pnl[];
   datetime outT[];
   int dirs[];
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0) continue;
      if(HistoryDealGetInteger(tk, DEAL_MAGIC) != InpMagic) continue;
      if(HistoryDealGetString(tk, DEAL_SYMBOL) != g_sym) continue;
      long dt = HistoryDealGetInteger(tk, DEAL_TYPE);
      if(dt != DEAL_TYPE_BUY && dt != DEAL_TYPE_SELL) continue;
      long pid = HistoryDealGetInteger(tk, DEAL_POSITION_ID);
      int idx = -1;
      for(int q = 0; q < ArraySize(ids); q++)
         if(ids[q] == pid) { idx = q; break; }
      if(idx < 0)
        {
         idx = ArraySize(ids);
         ArrayResize(ids, idx + 1);  ArrayResize(inP, idx + 1);  ArrayResize(inV, idx + 1);
         ArrayResize(outW, idx + 1); ArrayResize(outV, idx + 1); ArrayResize(pnl, idx + 1);
         ArrayResize(outT, idx + 1); ArrayResize(dirs, idx + 1);
         ids[idx] = pid; inP[idx] = 0; inV[idx] = 0; outW[idx] = 0; outV[idx] = 0; pnl[idx] = 0;
         outT[idx] = 0; dirs[idx] = 0;
        }
      double vol = HistoryDealGetDouble(tk, DEAL_VOLUME);
      double px = HistoryDealGetDouble(tk, DEAL_PRICE);
      pnl[idx] += HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_SWAP) +
                  HistoryDealGetDouble(tk, DEAL_COMMISSION);
      long en = HistoryDealGetInteger(tk, DEAL_ENTRY);
      if(en == DEAL_ENTRY_IN)
        {
         inP[idx] = px; inV[idx] = vol;
         dirs[idx] = dt == DEAL_TYPE_BUY ? 1 : -1;
         s.trades++;
         if(StringFind(HistoryDealGetString(tk, DEAL_COMMENT), "COUNTER") >= 0) s.counter++;
        }
      else if(en == DEAL_ENTRY_OUT || en == DEAL_ENTRY_OUT_BY)
        {
         outW[idx] += px * vol; outV[idx] += vol;
         datetime tm = (datetime)HistoryDealGetInteger(tk, DEAL_TIME);
         if(tm > outT[idx]) outT[idx] = tm;
        }
     }
   for(int q = 0; q < ArraySize(ids); q++)
      s.usd += pnl[q];                 // всё закрытое сегодня, в т.ч. частичные закрытия и комиссии
   int cl[];
   for(int q = 0; q < ArraySize(ids); q++)
      if(inV[q] > 0 && outV[q] >= inV[q] - 1e-9)
        {
         int k = ArraySize(cl);
         ArrayResize(cl, k + 1);
         cl[k] = q;
         s.pts += dirs[q] * (outW[q] / outV[q] - inP[q]);
        }
   for(int a = 0; a < ArraySize(cl); a++)
      for(int b = a + 1; b < ArraySize(cl); b++)
         if(outT[cl[b]] < outT[cl[a]]) { int tmp = cl[a]; cl[a] = cl[b]; cl[b] = tmp; }
   for(int a = 0; a < ArraySize(cl); a++)
     {
      int q = cl[a];
      if(pnl[q] < 0)
        {
         s.lossStreak++;
         if(dirs[q] > 0) s.lastLossBuy = MathMax(s.lastLossBuy, outT[q]);
         else s.lastLossSell = MathMax(s.lastLossSell, outT[q]);
        }
      else
         s.lossStreak = 0;
     }
   // запасной счётчик COUNTER, если брокер обрезал комментарий
   int key = (int)(DayStartSrv() / 86400);
   if(key != g_cntDayKey) { g_cntDayKey = key; g_cntToday = 0; }
   s.counter = MathMax(s.counter, g_cntToday);
   return s;
  }

//+------------------------------------------------------------------+
//| Отрисовка [§10]                                                  |
//+------------------------------------------------------------------+
void DrawZones()
  {
   if(!InpDraw) return;
   ObjectsDeleteAll(0, OBJ_PREFIX + "Z");
   int drawn = 0;
   datetime tEnd = TimeCurrent() + 4 * 3600;
   for(int i = 0; i < ArraySize(g_zones) && drawn < 2 * InpMaxZonesSide; i++)
     {
      SZone z = g_zones[i];
      if(z.status > ZONE_CONFIRMED || !z.tradable) continue;
      string nm = OBJ_PREFIX + "Z" + IntegerToString(drawn);
      color col = z.dir > 0 ? clrGreen : clrRed;
      ObjectCreate(0, nm, OBJ_RECTANGLE, 0, z.born, z.hi, tEnd, z.lo);
      ObjectSetInteger(0, nm, OBJPROP_COLOR, col);
      ObjectSetInteger(0, nm, OBJPROP_FILL, z.status == ZONE_CONFIRMED);
      ObjectSetInteger(0, nm, OBJPROP_STYLE, z.status == ZONE_CONFIRMED ? STYLE_SOLID : STYLE_DOT);
      ObjectSetInteger(0, nm, OBJPROP_BACK, true);
      string tn = OBJ_PREFIX + "ZT" + IntegerToString(drawn);
      ObjectCreate(0, tn, OBJ_TEXT, 0, tEnd, z.hi);
      ObjectSetString(0, tn, OBJPROP_TEXT, StringFormat("%s · %s–%s · балл %d · %s", DirName(z.dir),
                      Px(z.lo), Px(z.hi), z.score, z.status == ZONE_CONFIRMED ? "CONF" : "NEW"));
      ObjectSetInteger(0, tn, OBJPROP_COLOR, col);
      ObjectSetInteger(0, tn, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, tn, OBJPROP_ANCHOR, ANCHOR_RIGHT_LOWER);
      drawn++;
     }
   ChartRedraw();
  }

void DrawMark(datetime t, double price, string text, color col)
  {
   if(!InpDraw) return;
   string nm = OBJ_PREFIX + "M" + IntegerToString(g_markIdx);
   g_markIdx = (g_markIdx + 1) % 4;
   ObjectDelete(0, nm);
   ObjectCreate(0, nm, OBJ_TEXT, 0, t, price);
   ObjectSetString(0, nm, OBJPROP_TEXT, text);
   ObjectSetInteger(0, nm, OBJPROP_COLOR, col);
   ObjectSetInteger(0, nm, OBJPROP_FONTSIZE, 9);
  }

void DrawLine(string name, double price, color col)
  {
   if(!InpDraw) return;
   string nm = OBJ_PREFIX + name;
   if(ObjectFind(0, nm) < 0) ObjectCreate(0, nm, OBJ_HLINE, 0, 0, price);
   ObjectSetDouble(0, nm, OBJPROP_PRICE, price);
   ObjectSetInteger(0, nm, OBJPROP_COLOR, col);
   ObjectSetInteger(0, nm, OBJPROP_STYLE, STYLE_DASH);
  }

void ClearLines()
  {
   ObjectDelete(0, OBJ_PREFIX + "SL");
   ObjectDelete(0, OBJ_PREFIX + "TP");
  }

string NearestZoneText(int d)
  {
   int bi = -1;
   double bd = DBL_MAX;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].dir != d || g_zones[i].status > ZONE_CONFIRMED || !ZoneInRadius(g_zones[i])) continue;
      double dist = ZoneDist(g_zones[i]);
      if(dist < bd) { bd = dist; bi = i; }
     }
   if(bi < 0) return "нет";
   return StringFormat("%s–%s %s (балл %d)", Px(g_zones[bi].lo), Px(g_zones[bi].hi),
                       g_zones[bi].status == ZONE_CONFIRMED ? "CONF" : "NEW", g_zones[bi].score);
  }

void DrawPanel()
  {
   if(!InpDraw) return;
   double sp = SymbolInfoDouble(g_sym, SYMBOL_ASK) - SymbolInfoDouble(g_sym, SYMBOL_BID);
   ENUM_RISK_MODE rm = EffectiveRiskMode();
   string riskTxt = rm == RISK_FIXED ? "лот " + DoubleToString(InpFixedLot, 2)
                    : StringFormat("%.2f%% на сделку", TradeRiskPct(rm));
   if(g_set.cRisk) riskTxt += " (TG)";
   if(g_set.dailyRiskPct > 0)
      riskTxt += StringFormat(" · в день %.1f%%%s, осталось %.2f %s", g_set.dailyRiskPct, g_set.cDaily ? " (TG)" : "", MathMax(0.0, DailyRiskLeft()),
                              AccountInfoString(ACCOUNT_CURRENCY));
   string tpTxt = InpTPMode == TP_RR ? StringFormat("тейк RR 1:%.1f", g_set.rr)
                  : StringFormat("тейк %.0f–%.0f (LAL, RR от %.1f)", InpTPMin, InpTPMax, InpMinRR);
   string mode = Relaxed() ? (g_set.weak ? " · слабые сигналы вкл." : " · добор") : "";
   if(g_set.cWeak) mode += " (TG)";
   Comment("Mr. Casino v3.3 (золото)\n", BridgeLine(),
           "Bias: D1=", BiasName(g_d1), "  H4=", BiasName(g_h4), "\n",
           "Сессия: ", InSession() ? "открыта" : "закрыта", " (Бишкек ", LocalHM(), ")", mode, "\n",
           "SELL-зона: ", NearestZoneText(-1), "\n",
           "BUY-зона: ", NearestZoneText(1), "\n",
           "Риск: ", EnumToString(rm), ", ", riskTxt, "\n",
           StringFormat("Стоп %.0f–%.0f · %s\n", InpMinSL, InpMaxSL, tpTxt),
           "Сделок сегодня: ", g_day.trades, " / ", g_set.maxTrades, (g_set.cMax ? " (TG)" : ""), "  (COUNTER ", g_day.counter, ")\n",
           "За день: ", DoubleToString(g_day.pts, 1), " pt / ", DoubleToString(g_day.usd, 2), " ", AccountInfoString(ACCOUNT_CURRENCY), "\n",
           "Спред: ", DoubleToString(sp, 2), "\n",
           "Пропуск: ", g_lastSkip);
  }

//+------------------------------------------------------------------+
//| Фильтры и вход [§7, §8, §9]                                      |
//+------------------------------------------------------------------+
string GlobalBlock()
  {
   if(g_set.paused) return "пауза (" + g_pauseReason + ")";
   if(HasPos()) return "позиция уже открыта";
   if(FridayCloseTime()) return "пятница, закрытие рынка";
   if(!InSession()) return "вне сессии (Бишкек " + LocalHM() + ")";
   if(g_day.trades >= g_set.maxTrades) return StringFormat("лимит сделок дня %d", g_set.maxTrades);
   if(InpDailyTarget > 0 && g_day.pts >= InpDailyTarget) return StringFormat("дневная цель %.1f pt", g_day.pts);
   if(InpMaxLossStreak > 0 && g_day.lossStreak >= InpMaxLossStreak)
      return StringFormat("%d стопа подряд, пауза до завтра", g_day.lossStreak);
   if(g_set.dailyRiskPct > 0 && DailyRiskLeft() <= 0)
      return StringFormat("дневной риск %.1f%% исчерпан (%.2f за день), пауза до завтра", g_set.dailyRiskPct, g_day.usd);
   if(NewsBlock()) return "окно новостей";
   double spread = SymbolInfoDouble(g_sym, SYMBOL_ASK) - SymbolInfoDouble(g_sym, SYMBOL_BID);
   if(spread > InpMaxSpread) return StringFormat("спред %.2f > %.2f", spread, InpMaxSpread);
   return "";
  }

// hadSignal: триггер был, но фильтр не пустил (signal_skipped); иначе нет триггера (analysis_skip, уровень 2)
void Skip(int zi, string why, bool hadSignal = false)
  {
   g_lastSkip = why;
   if(g_zones[zi].lastSkip == why && TimeCurrent() - g_zones[zi].lastSkipT < 1800) return;
   g_zones[zi].lastSkip = why;
   g_zones[zi].lastSkipT = TimeCurrent();
   Lg(1, "Пропуск " + ZoneTag(g_zones[zi]) + ": " + why);
   bool sig = hadSignal || g_sigThisPass;
   Emit(sig ? "signal_skipped" : "analysis_skip", sig ? 1 : 2, "info",
        JS("dir", DirName(g_zones[zi].dir)) + "," + JN("lo", g_zones[zi].lo, g_digits) + "," + JN("hi", g_zones[zi].hi, g_digits) + "," +
        JS("why", why), (sig ? "⏭ Пропуск " : "· Нет триггера ") + ZoneTag(g_zones[zi]) + ": " + why, -1);
  }

// Книга: Doji или волчок (spinning top) — нерешительность, вход через свечу. По стратегии свеча,
// которая сняла ликвидность (FU / AFU), — не Doji, и молот / падающая звезда — тоже не нерешительность.
bool IsSpinTop(const MqlRates &r[], int i)
  {
   double rg = r[i].high - r[i].low;
   if(rg <= 0) return false;
   double top = MathMax(r[i].open, r[i].close), bot = MathMin(r[i].open, r[i].close);
   return (top - bot) <= 0.35 * rg && (r[i].high - top) >= 0.25 * rg && (bot - r[i].low) >= 0.25 * rg;
  }

bool Indecision(const MqlRates &r[], int i, int d)
  {
   if(!IsDoji(r, i) && !IsSpinTop(r, i)) return false;
   return !IsFU(r, i, d) && !IsPin(r, i, d);
  }

// Книга (FOMO): цена влетела в зону импульсом — 3 свечи подряд против сделки на InpImpulse+ пунктов.
// Не ловим нож, ждём ретест.
bool ImpulseInto(const MqlRates &r[], int d)
  {
   if(InpImpulse <= 0 || ArraySize(r) < 6) return false;
   for(int i = 2; i <= 4; i++)
      if(!(Cv(r, i, d) < Ov(r, i, d))) return false;
   return Hv(r, 4, d) - MathMin(Lv(r, 2, d), Lv(r, 1, d)) >= InpImpulse;
  }

// Безубыток после доли пути до тейка
double BEAt(double tpDist)
  {
   if(InpBEFrac <= 0 || tpDist <= 0) return DBL_MAX;
   return InpBEFrac * tpDist;
  }

bool InRegion(const SZone &z, const MqlRates &r[])
  {
   int d = z.dir;
   double zl = ZLo(z), zh = ZHi(z);
   for(int i = 1; i <= 2; i++)
      if(Hv(r, i, d) >= zl - InpPierceMax && Lv(r, i, d) <= zh + InpPierceMax) return true;
   return false;
  }

void TryEntry()
  {
   MqlRates r[];
   if(Load(PERIOD_M5, 90, r) < 50) return;
   string gblock = GlobalBlock();
   int bi = -1, bpts = -1, btype = T_NEUTRAL;
   SSig bs;
   bs.ok = false;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].status != ZONE_CONFIRMED || !g_zones[i].tradable) continue;
      int d = g_zones[i].dir;
      g_sigThisPass = false;
      if(!InRegion(g_zones[i], r)) continue;
      if(gblock != "") { Skip(i, gblock); continue; }
      if((d > 0 && !g_set.tradeBuy) || (d < 0 && !g_set.tradeSell)) { Skip(i, "сторона выключена"); continue; }
      int tp = TradeType(d);
      if(tp == T_COUNTER)
        {
         if(!InpAllowCounter) { Skip(i, "COUNTER выключен"); continue; }
         if(g_day.counter >= InpMaxCounterDay) { Skip(i, StringFormat("лимит COUNTER %d в день", InpMaxCounterDay)); continue; }
        }
      datetime ll = d > 0 ? g_day.lastLossBuy : g_day.lastLossSell;
      if(ll > 0 && TimeCurrent() - ll < (long)InpRepeatM15Bars * PeriodSeconds(PERIOD_M15))
        { Skip(i, StringFormat("повтор %s после стопа раньше %d свечей M15", DirName(d), InpRepeatM15Bars)); continue; }
      SSig s;
      string why = "";
      if(!EvalZone(g_zones[i], r, s, why)) { Skip(i, why); continue; }
      g_sigThisPass = true;
      int need = MinScore(tp);
      if(g_zones[i].score < need)
        { Skip(i, StringFormat("балл %d < %d (%s, триггер %s)", g_zones[i].score, need, TypeName(tp), s.trig)); continue; }
      if(tp == T_COUNTER)
        {
         bool cok = s.trig == "B" || (s.trig == "C" && s.strong) || s.cls == 3;
         if(!cok && Relaxed()) cok = s.trig == "C" || s.cls >= 2;          // слабые подтверждения
         if(!cok)
           {
            Skip(i, (Relaxed() ? "COUNTER: нужен HCS, Negation или FU класса A/B (триггер "
                               : "COUNTER: нужен HCS, Strong Negation или FU класса A (триггер ") + s.trig + ")");
            continue;
           }
        }
      int pts = g_zones[i].score + s.pts;
      if(pts > bpts) { bpts = pts; bi = i; bs = s; btype = tp; }
     }
   if(bi < 0) { g_sigThisPass = false; return; }
   g_sigThisPass = true;

   int d = g_zones[bi].dir;
   SZone z = g_zones[bi];
   if(InsideFVG(r, d * bs.ext)) { Skip(bi, "разворот начался внутри незакрытого FVG (триггер " + bs.trig + ")"); return; }
   if(InpDojiWait && bs.trig != "DIRECT" && Indecision(r, 1, d))
     { Skip(bi, "сигнальная свеча Doji / волчок — ждём (книга, триггер " + bs.trig + ")"); return; }
   if(ImpulseInto(r, d))
     { Skip(bi, StringFormat("импульс в зону %.0f+ за 3 свечи — ждём ретест (книга, триггер %s)", InpImpulse, bs.trig)); return; }

   double ask = SymbolInfoDouble(g_sym, SYMBOL_ASK), bid = SymbolInfoDouble(g_sym, SYMBOL_BID);
   double spread = ask - bid;
   double entry = d > 0 ? ask : bid;
   double buf = spread + InpSLBuffer;
   double sl = d * bs.ext - d * buf;                     // за манипуляцией
   double dist = d * (entry - sl);
   string slSrc = "за манипуляцией";
   if(InpTrueStop)                                       // True Stop: за дальним краем зоны [§10]
     {
      double slZ = (d > 0 ? z.lo : z.hi) - d * buf;
      double distZ = d * (entry - slZ);
      if(distZ > dist && distZ <= InpMaxSL) { sl = slZ; dist = distZ; slSrc = "True Stop за зоной"; }
     }
   double minDist = MathMax(MinStopDist(), InpMinSL);
   if(dist < minDist) { dist = minDist; sl = entry - d * dist; slSrc += ", мин. стоп"; }
   if(dist > InpMaxSL)
     { Skip(bi, StringFormat("стоп %.1f > %.1f (триггер %s)", dist, InpMaxSL, bs.trig)); return; }

   string tpSrc = "";
   double tpDist;
   if(InpTPMode == TP_RR)                                // v3.2: тейк = стоп × RR (1:2)
     {
      tpDist = g_set.rr * dist;
      tpSrc = StringFormat("RR 1:%.1f", g_set.rr);
      if(tpDist < MinStopDist()) { Skip(bi, StringFormat("тейк %.1f меньше стоп-уровня брокера", tpDist)); return; }
     }
   else
     {
      tpDist = ChooseTP(d, entry, dist, tpSrc);
      if(tpDist < MinStopDist() || tpDist / dist < InpMinRR)
        {
         Skip(bi, StringFormat("RR %.1f < %.1f (тейк %.1f, стоп %.1f, %s)", tpDist / dist, InpMinRR, tpDist, dist, tpSrc));
         return;
        }
     }
   double major = FindMajorLiq(d, entry);

   sl = NormalizeDouble(sl, g_digits);
   double tp = NormalizeDouble(entry + d * tpDist, g_digits);
   double riskUsd = 0;
   string lotWhy = "";
   double lot = CalcLot(dist, d, entry, riskUsd, lotWhy);
   if(lot <= 0) { Skip(bi, lotWhy + " (триггер " + bs.trig + ")"); return; }

   string tname = TypeName(btype);
   bool weak = bpts < 5;                                 // Signal Strength ★ — слабый сигнал
   string cmt = "MC " + bs.trig + " " + tname + " S" + IntegerToString(bpts >= 7 ? 3 : (bpts >= 5 ? 2 : 1));
   bool ok = d > 0 ? g_trade.Buy(lot, g_sym, 0.0, sl, tp, cmt) : g_trade.Sell(lot, g_sym, 0.0, sl, tp, cmt);
   uint rc = g_trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
     {
      RegisterReject(rc, g_trade.ResultRetcodeDescription(), bs.trig);
      Skip(bi, StringFormat("ордер отклонён: %u %s", rc, g_trade.ResultRetcodeDescription()));
      return;
     }
   g_rejectStreak = 0;

   ulong tk; long id; int pd; double pv, po, psl, ptp;
   if(!FindPos(tk, id, pd, pv, po, psl, ptp)) { po = entry; id = 0; psl = sl; ptp = tp; pv = lot; }
   int total = bpts;
   g_pId = id; g_pDir = d; g_pTrig = bs.trig; g_pCls = bs.cls;
   g_pType = tname; g_pScore = z.score; g_pSrcs = z.srcs; g_pSess = SessionName();
   g_pStrength = Stars(total);
   g_pEntry = po; g_pSL = psl; g_pTP = ptp; g_pLot = pv; g_pLal = major; g_pRisk = riskUsd;
   g_pZoneW = z.hi - z.lo; g_pZLo = z.lo; g_pZHi = z.hi;
   g_pBEAt = BEAt(MathAbs(ptp - po));
   g_pOpenT = TimeCurrent(); g_pMark = bs.mark; g_pManual = false; g_pWeak = weak;
   g_pStrN = bpts >= 7 ? 3 : (bpts >= 5 ? 2 : 1);
   g_beDone = false; g_partDone = false; g_exitTag = ""; g_closeRetry = 0;
   SetStatus(g_zones[bi], ZONE_USED, "сделка");
   if(btype == T_COUNTER) g_cntToday++;
   g_day = CalcDay();

   Lg(0, StringFormat("СДЕЛКА %s | триггер %s | тип %s | балл зоны %d | вход %s SL %s TP %s | лот %.2f | сессия %s | %s",
                      ZoneTag(z), bs.trig, tname, z.score, Px(po), Px(psl), Px(ptp), pv, g_pSess, z.srcs));
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   double riskPct = bal > 0 ? riskUsd / bal * 100.0 : 0;
   Lg(0, StringFormat("Стоп %.1f (%s) | тейк %.1f (%s) | RR 1:%.1f | риск %.2f %s (%.2f%%)%s", dist, slSrc, tpDist, tpSrc,
                      tpDist / dist, riskUsd, cur, riskPct, weak ? " | слабый сигнал" : ""));

   DrawMark(bs.t, d > 0 ? r[1].low : r[1].high, bs.mark, d > 0 ? clrLime : clrRed);
   DrawLine("SL", psl, clrRed);
   DrawLine("TP", ptp, clrLime);
   DrawZones();
   DrawPanel();

   string ef = JS("symbol", g_sym) + "," + JS("side", DirName(d)) + "," + JI("ticket", g_pId) + "," + JN("entry", po, g_digits) + "," +
               JN("sl", psl, g_digits) + "," + JN("tp", ptp, g_digits) + "," + JN("lot", pv, 2) + "," + JN("risk_usd", riskUsd, 2) + "," +
               JN("risk_pct", riskPct, 2) + "," + JS("trigger", bs.trig) + "," + JS("mark", bs.mark) + "," + JS("fu_class", ClsName(bs.cls)) + "," +
               JS("type", tname) + "," + JB("manual", false) + "," + JB("weak", weak) + "," + JI("strength", g_pStrN) + "," +
               JN("zone_lo", z.lo, g_digits) + "," + JN("zone_hi", z.hi, g_digits) + "," + JI("score", z.score) + "," + JS("srcs", z.srcs) + "," +
               JB("htf", z.htf) + "," + JS("session", g_pSess) + "," + JN("rr", MathAbs(ptp - po) / MathMax(MathAbs(po - psl), 0.01), 2) + "," +
               JS("tp_src", tpSrc) + "," + JS("sl_src", slSrc) + "," + JN("sl_pts", dist, 1) + "," + JN("tp_pts", tpDist, 1);
   Emit("trade_opened", 0, "info", ef, StringFormat("%s %s · %s\nEntry: %s\nSL: %s\nTP: %s (%s)\nRR: 1:%.1f\nРиск: %.2f %s (%.2f%%) · лот %.2f\n\n✅ Зона %s–%s · балл %d\n✅ %s\n✅ Триггер %s (%s), FU класс %s\n%sТФ: M5 · сессия %s\nSignal Strength: %s%s",
                       g_sym, DirName(d), tname,
                       Px(po), Px(psl), Px(ptp), tpSrc,
                       MathAbs(ptp - po) / MathMax(MathAbs(po - psl), 0.01),
                       riskUsd, cur, riskPct, pv,
                       Px(z.lo), Px(z.hi), z.score, z.srcs, bs.trig, bs.mark, ClsName(bs.cls),
                       z.htf ? "✅ HTF Re-test Level\n" : "", g_pSess, Stars(total),
                       weak ? "\n⚠️ Слабый сигнал" : ""), (int)PERIOD_M5);
  }

//+------------------------------------------------------------------+
//| Сопровождение и закрытие [§8]                                    |
//+------------------------------------------------------------------+
void ClosePosNow(ulong tk, string tag)
  {
   g_exitTag = tag;
   if(!g_trade.PositionClose(tk))
      Print("Закрытие не удалось: ", g_trade.ResultRetcode(), " ", g_trade.ResultRetcodeDescription());
  }

void OnPositionClosed()
  {
   if(!HistorySelectByPosition(g_pId) || HistoryDealsTotal() == 0)
     {
      if(++g_closeRetry > 300) { g_pId = 0; ClearLines(); }
      return;
     }
   double pnl = 0, inP = 0, outW = 0, outV = 0, inV = 0;
   long reason = -1;
   datetime tOut = 0;
   int dir = g_pDir;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0) continue;
      long dt = HistoryDealGetInteger(tk, DEAL_TYPE);
      if(dt != DEAL_TYPE_BUY && dt != DEAL_TYPE_SELL) continue;
      pnl += HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_SWAP) +
             HistoryDealGetDouble(tk, DEAL_COMMISSION);
      long en = HistoryDealGetInteger(tk, DEAL_ENTRY);
      double vol = HistoryDealGetDouble(tk, DEAL_VOLUME), px = HistoryDealGetDouble(tk, DEAL_PRICE);
      if(en == DEAL_ENTRY_IN) { inP = px; inV = vol; dir = dt == DEAL_TYPE_BUY ? 1 : -1; }
      else if(en == DEAL_ENTRY_OUT || en == DEAL_ENTRY_OUT_BY)
        {
         outW += px * vol; outV += vol;
         reason = HistoryDealGetInteger(tk, DEAL_REASON);
         tOut = (datetime)HistoryDealGetInteger(tk, DEAL_TIME);
        }
     }
   if(outV <= 0 || (inV > 0 && outV < inV - 1e-9)) return;
   double outP = outW / outV;
   double pts = dir * (outP - inP);
   string ex = g_exitTag;
   if(ex == "")
     {
      if(reason == DEAL_REASON_TP) ex = "TP";
      else if(reason == DEAL_REASON_SL) ex = g_beDone && pts >= 0 ? "BE" : "SL";
      else ex = "OTHER";
     }
   string srcs = g_pSrcs;
   StringReplace(srcs, ",", " ");
   string line = StringFormat("%s,%s,%s,%s,%s,%d,%s,%.1f,%s,%s,%s,%s,%.2f,%.2f,%.2f,%s,%s,%.2f",
                              TimeToString(tOut, TIME_DATE | TIME_MINUTES), DirName(dir), g_pType,
                              g_pTrig, ClsName(g_pCls), g_pScore, srcs, g_pZoneW, g_pSess,
                              Px(g_pEntry), Px(g_pSL), Px(g_pTP), g_pLot, pts, pnl, ex, g_pStrength, g_pRisk);
   LogTrade(line);
   // v3.3: trades.csv моста (колонки раздела 13 ТЗ), время Бишкека, запятые в тексте заменены пробелом
   if(g_bridgeOn)
     {
      datetime tOpen = g_pOpenT > 0 ? g_pOpenT : tOut;
      double rrReal = g_pRisk > 0 ? pnl / g_pRisk : 0.0;
      string zs = g_pSrcs;
      StringReplace(zs, ",", " ");
      string mk = g_pMark;
      StringReplace(mk, ",", " ");
      string row = LocalStamp(ToLocal(tOpen)) + "," + LocalStamp(ToLocal(tOut)) + "," + IntegerToString(g_pId) + "," + DirName(dir) + "," +
                   g_pType + "," + g_pTrig + "," + mk + "," + ClsName(g_pCls) + "," + IntegerToString(g_pScore) + "," + zs + "," +
                   DoubleToString(g_pZoneW, 1) + "," + g_pSess + "," + Px(g_pEntry) + "," + Px(g_pSL) + "," + Px(g_pTP) + "," +
                   DoubleToString(g_pLot, 2) + "," + DoubleToString(g_pRisk, 2) + "," + DoubleToString(pts, 2) + "," +
                   DoubleToString(pnl, 2) + "," + DoubleToString(rrReal, 2) + "," + ex + "," + IntegerToString(g_pStrN) + "," +
                   (g_pWeak ? "true" : "false") + "," + (g_pManual ? "true" : "false") + "," + IntegerToString((long)((tOut - tOpen) / 60));
      AppendUtf8(g_dir + "\\trades.csv", row, TRADES_HEADER);
     }
   if(ex == "SL")                      // после стопа повторного входа в зону нет
      for(int zi = 0; zi < ArraySize(g_zones); zi++)
         if(g_zones[zi].dir == dir && MathAbs(g_zones[zi].lo - g_pZLo) < 0.01 && MathAbs(g_zones[zi].hi - g_pZHi) < 0.01)
            SetStatus(g_zones[zi], ZONE_INVALID, "стоп по зоне");
   Lg(0, StringFormat("ЗАКРЫТА %s %s | %s | %+.1f pt / %+.2f $ | вход %s выход %s", DirName(dir), g_pType, ex,
                      pts, pnl, Px(inP), Px(outP)));
   string cf = JS("symbol", g_sym) + "," + JI("ticket", g_pId) + "," + JS("side", DirName(dir)) + "," + JN("entry", inP, g_digits) + "," +
               JN("exit_price", outP, g_digits) + "," + JN("pts", pts, 1) + "," + JN("usd", pnl, 2) + "," + JS("exit", ex) + "," +
               JI("duration_min", (long)((tOut - (g_pOpenT > 0 ? g_pOpenT : tOut)) / 60)) + "," + JS("trigger", g_pTrig) + "," +
               JS("type", g_pType) + "," + JS("session", g_pSess) + "," + JB("manual", g_pManual);
   Emit("trade_closed", 0, "info", cf, StringFormat("%s %s · %s закрыта (%s)\nВход: %s → Выход: %s\nРезультат: %+.1f pt / %+.2f $",
                       g_sym, DirName(dir), g_pType, ex, Px(inP), Px(outP), pts, pnl), (int)PERIOD_M5);
   g_pId = 0; g_exitTag = ""; g_closeRetry = 0; g_pManual = false; g_pOpenT = 0;
   ClearLines();
   g_day = CalcDay();
   // v3.2: дневная цель 0 = выкл (раньше уведомление приходило после каждой сделки в плюс)
   if(g_day.trades >= g_set.maxTrades || (InpDailyTarget > 0 && g_day.pts >= InpDailyTarget) ||
      (InpMaxLossStreak > 0 && g_day.lossStreak >= InpMaxLossStreak) ||
      (g_set.dailyRiskPct > 0 && DailyRiskLeft() <= 0))
      Emit("limit_reached", 0, "info", JI("trades", g_day.trades) + "," + JN("pts", g_day.pts, 1) + "," + JN("usd", g_day.usd, 2) + "," +
           JI("loss_streak", g_day.lossStreak),
           StringFormat("⛔ Дневной лимит достигнут: сделок %d, %+.1f pt / %+.2f %s, стопов подряд %d. Робот стоит до завтра.",
                        g_day.trades, g_day.pts, g_day.usd, AccountInfoString(ACCOUNT_CURRENCY), g_day.lossStreak), -1);
   DrawZones();
   DrawPanel();
  }

void ManagePosition()
  {
   ulong tk; long id; int d; double vol, op, sl, tp;
   if(!FindPos(tk, id, d, vol, op, sl, tp))
     {
      if(g_pId != 0) OnPositionClosed();
      return;
     }
   if(g_pId == 0)                     // подхват позиции после перезапуска терминала
     {
      g_pId = id; g_pDir = d; g_pTrig = "RESTORED"; g_pCls = 0; g_pStrength = "-"; g_pType = "-";
      g_pScore = 0; g_pSrcs = "-"; g_pSess = "-";
      g_pEntry = op; g_pSL = sl; g_pTP = tp; g_pLot = vol; g_pRisk = 0;
      g_pLal = 0; g_beDone = d * (sl - op) >= 0; g_partDone = false; g_exitTag = ""; g_closeRetry = 0;
      g_pBEAt = BEAt(tp > 0 ? MathAbs(tp - op) : 0);
      g_pOpenT = (datetime)PositionGetInteger(POSITION_TIME);
      g_pMark = "-"; g_pWeak = false; g_pStrN = 1;
      g_pManual = StringFind(PositionGetString(POSITION_COMMENT), "MANUAL") >= 0;
      if(g_pManual) { g_pTrig = "MANUAL"; g_pMark = "MANUAL"; }
     }
   if(FridayCloseTime())
     {
      AlertSet("friday_close", true, "info", "Позиция закрыта перед выходными (рынок закрывается).");
      ClosePosNow(tk, "FRIDAY");
      return;
     }
   // v3.3: позиция без стопа — тревога и повторная установка (до 3 попыток), затем автопауза [§5.5, §5.8]
   if(sl <= 0)
     {
      AlertSet("no_sl", true, "critical", "Позиция робота без стопа. Робот пытается поставить стоп заново.");
      if(TimeCurrent() - g_noSlLast >= 2 && g_noSlTries < 3)
        {
         g_noSlLast = TimeCurrent();
         g_noSlTries++;
         double curp = d > 0 ? SymbolInfoDouble(g_sym, SYMBOL_BID) : SymbolInfoDouble(g_sym, SYMBOL_ASK);
         double want = g_pSL > 0 ? g_pSL : op - d * InpMaxSL;
         if(!(d * (curp - want) >= MinStopDist())) want = curp - d * (MinStopDist() + InpMinSL);
         want = NormalizeDouble(want, g_digits);
         if(g_trade.PositionModify(tk, want, tp)) { g_pSL = want; g_noSlTries = 0; }
         else if(g_noSlTries >= 3 && !g_set.paused)
           {
            g_set.paused = true;
            g_pauseReason = "автопауза: позиция без стопа, поставить не удалось";
            SaveSettings();
            Emit("note", 0, "critical", "", "⛔ Не удалось поставить стоп за 3 попытки. Робот поставлен на паузу.", -1);
           }
        }
      return;
     }
   if(AlertIsOn("no_sl")) { AlertSet("no_sl", false, "critical", "стоп выставлен"); g_noSlTries = 0; }

   double bid = SymbolInfoDouble(g_sym, SYMBOL_BID), ask = SymbolInfoDouble(g_sym, SYMBOL_ASK);
   double cur = d > 0 ? bid : ask;
   double profit = d * (cur - op);

   // безубыток: после доли InpBEFrac пути до тейка
   if(!g_beDone && profit >= g_pBEAt)
     {
      double nsl = NormalizeDouble(op + d * InpBEOffset, g_digits);
      if(d * (nsl - sl) > 0 && d * (cur - nsl) >= MinStopDist())
        {
         if(g_trade.PositionModify(tk, nsl, tp))
           {
            g_beDone = true;
            g_pSL = nsl;
            DrawLine("SL", nsl, clrRed);
            Emit("trade_be", 0, "info", JI("ticket", g_pId) + "," + JN("sl", nsl, g_digits),
                 StringFormat("⚪ %s %s: стоп в безубытке\nSL: %s", g_sym, DirName(d), Px(nsl)), -1);
           }
        }
     }

   // частичное закрытие: 50% на первой LAL, только от 0.02 лота
   if(InpPartial && !g_partDone && g_pLal > 0 && vol >= 0.02 - 1e-9 && d * (cur - g_pLal) >= 0)
     {
      double step = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_STEP);
      double minv = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN);
      double cv = MathFloor(vol / 2.0 / step + 1e-9) * step;
      if(cv >= minv && vol - cv >= minv - 1e-12)
        {
         if(g_trade.PositionClosePartial(tk, NormalizeDouble(cv, 2)))
           {
            g_partDone = true;
            Emit("trade_partial", 0, "info", JI("ticket", g_pId) + "," + JN("closed_lot", cv, 2) + "," + JN("left_lot", vol - cv, 2),
                 StringFormat("50%% позиции закрыто на первой LAL: %.2f лота, осталось %.2f", cv, vol - cv), -1);
           }
        }
      else
         g_partDone = true;
     }
  }

// Ранний выход: Negation против позиции на M15
void CheckEarlyExit()
  {
   if(!InpEarlyExit) return;
   ulong tk; long id; int d; double vol, op, sl, tp;
   if(!FindPos(tk, id, d, vol, op, sl, tp)) return;
   MqlRates m[];
   if(Load(PERIOD_M15, 10, m) < 6) return;
   if(Negation(m, 1, -d) > 0) ClosePosNow(tk, "EARLY");
  }

//+------------------------------------------------------------------+
//| Мост v3.3: статус, тревоги, команды, отчёты, таймер [§5, §6, §8] |
//+------------------------------------------------------------------+
string PosBrief()
  {
   ulong tk; long id; int d; double vol, op, sl, tp;
   if(!FindPos(tk, id, d, vol, op, sl, tp)) return "";
   return StringFormat("Открыта %s %.2f с %s, сейчас %+.2f %s.", DirName(d), vol, Px(op), PositionGetDouble(POSITION_PROFIT),
                       AccountInfoString(ACCOUNT_CURRENCY));
  }

string BlockText()
  {
   string b = GlobalBlock();
   if(b == "позиция уже открыта") b = "";
   return b;
  }

string ZonesJson()
  {
   string s = "";
   int cnt = 0;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(!g_zones[i].tradable || g_zones[i].status > ZONE_CONFIRMED) continue;
      if(cnt > 0) s += ",";
      s += "{" + JS("dir", DirName(g_zones[i].dir)) + "," + JN("lo", g_zones[i].lo, g_digits) + "," +
           JN("hi", g_zones[i].hi, g_digits) + "," + JS("status", StatusName(g_zones[i].status)) + "," +
           JI("score", g_zones[i].score) + "," + JS("srcs", g_zones[i].srcs) + "," + JB("htf", g_zones[i].htf) + "," +
           JN("dist", ZoneDist(g_zones[i]), 2) + "}";
      cnt++;
     }
   return s;
  }

string NewsJson()
  {
   string s = "";
   int cnt = 0;
   datetime nowLoc = LocalNow();
   for(int i = 0; i < ArraySize(g_news); i++)
     {
      datetime loc = ToLocal(g_news[i]);
      if(loc + InpNewsMinutes * 60 < nowLoc) continue;                 // уже прошла
      string st = LocalStamp(loc);
      st = StringSubstr(st, 0, 16);
      s += (cnt > 0 ? "," : "") + "\"" + st + " (окно ±" + IntegerToString(InpNewsMinutes) + " мин)\"";
      cnt++;
     }
   return s;
  }

string PositionJson()
  {
   ulong tk; long id; int d; double vol, op, sl, tp;
   if(!FindPos(tk, id, d, vol, op, sl, tp)) return "null";
   datetime ot = (datetime)PositionGetInteger(POSITION_TIME);
   return "{" + JI("ticket", id) + "," + JS("side", DirName(d)) + "," + JN("lot", vol, 2) + "," + JN("entry", op, g_digits) + "," +
          JN("sl", sl, g_digits) + "," + JN("tp", tp, g_digits) + "," + JN("profit", PositionGetDouble(POSITION_PROFIT), 2) + "," +
          JS("trigger", g_pTrig) + "," + JN("risk", g_pRisk, 2) + "," + JB("be", g_beDone) + "," +
          JS("opened_utc", IsoUtc(ot - GmtOffset())) + "," + JB("manual", g_pManual) + "}";
  }

void WriteStatus()
  {
   if(!g_bridgeOn) return;
   double bid = SymbolInfoDouble(g_sym, SYMBOL_BID), ask = SymbolInfoDouble(g_sym, SYMBOL_ASK);
   long mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string modeS = mode == ACCOUNT_TRADE_MODE_REAL ? "real" : (mode == ACCOUNT_TRADE_MODE_CONTEST ? "contest" : "demo");
   datetime lastTick = (datetime)SymbolInfoInteger(g_sym, SYMBOL_TIME);
   double left = g_set.dailyRiskPct > 0 ? DailyRiskLeft() : 0.0;
   long pingUs = (long)TerminalInfoInteger(TERMINAL_PING_LAST);
   ENUM_RISK_MODE rm = EffectiveRiskMode();
   string s = "{" + JI("schema", 1) + "," + JS("robot", g_robotId) + "," + JS("version", "3.30") + "," +
              JS("ts_utc", IsoUtc(TimeGMT())) + "," + JS("ts_local", LocalStamp(LocalNow())) + ",";
   s += "\"terminal\":{" + JB("connected", (bool)TerminalInfoInteger(TERMINAL_CONNECTED)) + "," +
        JB("algo_allowed", (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) + "," +
        JB("ea_trade_allowed", (bool)MQLInfoInteger(MQL_TRADE_ALLOWED)) + "," + JI("ping_ms", pingUs > 0 ? pingUs / 1000 : -1) + "},";
   s += "\"account\":{" + JI("login", AccountInfoInteger(ACCOUNT_LOGIN)) + "," + JS("server", AccountInfoString(ACCOUNT_SERVER)) + "," +
        JS("currency", AccountInfoString(ACCOUNT_CURRENCY)) + "," + JS("mode", modeS) + "," +
        JN("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2) + "," + JN("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2) + "," +
        JN("margin_free", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + "," +
        JN("margin_level", AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 1) + "},";
   s += "\"symbol\":{" + JS("name", g_sym) + "," + JN("bid", bid, g_digits) + "," + JN("ask", ask, g_digits) + "," +
        JN("spread", ask - bid, 2) + "," + JS("last_tick_utc", IsoUtc(lastTick - GmtOffset())) + "," +
        JB("market_open", SymbolSessionOpen()) + "},";
   s += "\"session\":{" + JB("open", InSession()) + "," + JS("name", SessionName()) + "," + JB("relaxed", Relaxed()) + "},";
   s += JB("paused", g_set.paused) + "," + JS("pause_reason", g_set.paused ? g_pauseReason : "") + ",";
   s += "\"bias\":{" + JS("d1", BiasName(g_d1)) + "," + JS("h4", BiasName(g_h4)) + "},";
   s += "\"zones\":[" + ZonesJson() + "],";
   s += "\"day\":{" + JI("trades", g_day.trades) + "," + JI("max_trades", g_set.maxTrades) + "," + JI("counter", g_day.counter) + "," +
        JN("pts", g_day.pts, 1) + "," + JN("usd", g_day.usd, 2) + "," + JI("loss_streak", g_day.lossStreak) + "," +
        JN("risk_left", left, 2) + "," + JS("blocked", BlockText()) + "},";
   s += "\"position\":" + PositionJson() + ",";
   s += "\"settings\":{" + JN("risk_pct", TradeRiskPct(rm), 2) + "," + JN("daily_risk_pct", g_set.dailyRiskPct, 2) + "," +
        JI("max_trades", g_set.maxTrades) + "," + JN("rr", g_set.rr, 2) + "," + JB("weak", g_set.weak) + "," +
        JB("buy", g_set.tradeBuy) + "," + JB("sell", g_set.tradeSell) + "},";
   s += "\"bridge\":{" + JB("bot_alive", g_botAlive) + "," + JS("bot_heartbeat_utc", g_botHb > 0 ? IsoUtc(g_botHb) : "") + "},";
   s += "\"news\":[" + NewsJson() + "],";
   s += JS("last_skip", g_lastSkip) + "}";
   WriteAtomic(g_dir + "\\status.json", s);
  }

//--- heartbeat бота и запасной канал [§5.2, §5.3]
void ReadHeartbeat()
  {
   string t;
   bool alive = false;
   if(ReadUtf8(g_dir + "\\bot_heartbeat.json", t) && t != "")
     {
      string k[], v[];
      JParse(t, k, v);
      datetime hb = IsoToTime(JGet(k, v, "ts_utc"));
      if(hb > 0)
        {
         g_botHb = hb;
         alive = (TimeGMT() - hb) <= 30;
        }
     }
   g_botAlive = alive;
   datetime ref = g_botHb > 0 ? g_botHb : g_startUtc;
   bool silent = (TimeGMT() - ref) > 180;
   if(InpTgFallback && silent && !g_fallback)
     {
      g_fallback = true;
      QueueTG("⚠️ Бот не отвечает, пишу напрямую. Критичные события и сделки идут сюда.");
     }
   else if(!silent && g_fallback)
      g_fallback = false;
  }

//--- тревоги, которые видит робот [§5.8]; вызывается раз в 5 секунд
void CheckAlerts()
  {
   datetime now = TimeGMT();
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   // disconnect: нет связи с брокером дольше 60 с
   bool conn = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   if(!conn) { if(g_discSince == 0) g_discSince = now; }
   else g_discSince = 0;
   bool disc = !conn && g_discSince > 0 && (now - g_discSince) >= 60 && (SymbolSessionOpen() || HasPos());
   AlertSet("disconnect", disc, "critical",
            disc ? StringFormat("Нет связи с брокером с %s (%d мин). %s Стоп стоит у брокера. Проверь интернет на ПК и MT5.",
                                StringSubstr(LocalStamp(g_discSince + InpUtcOffset * 3600), 11, 5), (int)((now - g_discSince) / 60), PosBrief())
                 : "связь с брокером есть");
   // algo_off: алго-торговля выключена
   bool algo = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   AlertSet("algo_off", !algo, "critical",
            !algo ? "Алго-торговля выключена: робот не может открывать и закрывать сделки. Включи кнопку «Алго-торговля» в MT5."
                  : "алго-торговля включена");
   // day_dd: эквити ниже баланса начала дня на InpAlertDDPct
   double base = DayStartBalance();
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   bool dd = InpAlertDDPct > 0 && base > 0 && eq < base * (1.0 - InpAlertDDPct / 100.0);
   AlertSet("day_dd", dd, "warning",
            dd ? StringFormat("Эквити %.2f %s ниже баланса начала дня %.2f на %.1f%%.", eq, cur, base, (base - eq) / base * 100.0)
               : "просадка за день ниже порога");
   // day_limit: дневной риск исчерпан, робот стоит до завтра
   bool lim = g_set.dailyRiskPct > 0 && DailyRiskLeft() <= 0;
   AlertSet("day_limit", lim, "critical",
            lim ? StringFormat("Дневной риск %.1f%% исчерпан (%.2f %s за день). Робот стоит до завтра.", g_set.dailyRiskPct, g_day.usd, cur)
                : "новый день: дневной лимит снова доступен");
   // spread: спред выше нормы дольше 2 минут при открытой позиции
   double sp = SymbolInfoDouble(g_sym, SYMBOL_ASK) - SymbolInfoDouble(g_sym, SYMBOL_BID);
   bool wide = HasPos() && InpAlertSpreadX > 0 && sp > InpAlertSpreadX * InpMaxSpread;
   if(wide) { if(g_spreadSince == 0) g_spreadSince = now; }
   else g_spreadSince = 0;
   bool spAlert = wide && g_spreadSince > 0 && (now - g_spreadSince) >= 120;
   AlertSet("spread", spAlert, "warning",
            spAlert ? StringFormat("Спред %.2f выше нормы %.2f больше 2 минут при открытой позиции.", sp, InpMaxSpread * InpAlertSpreadX)
                    : "спред в норме");
   // no_ticks: нет тиков 5 минут, а сессия символа открыта
   bool noTicks = SymbolSessionOpen() && (GetTickCount64() - g_lastTickMs) >= 300000;
   AlertSet("no_ticks", noTicks, "warning", noTicks ? "Нет тиков больше 5 минут при открытой торговой сессии." : "тики идут");
   // margin: уровень маржи ниже 200%
   double ml = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   bool mg = AccountInfoDouble(ACCOUNT_MARGIN) > 0 && ml < 200.0;
   AlertSet("margin", mg, "warning", mg ? StringFormat("Уровень маржи %.0f%% ниже 200%%.", ml) : "уровень маржи в норме");
   // friday_close: закрываем событие после пятничного окна
   if(AlertIsOn("friday_close") && !FridayCloseTime())
      AlertSet("friday_close", false, "info", "рынок открыт, торговля возобновлена");
  }

//--- зона: цена впервые зашла в торгуемую CONFIRMED-зону [§5.6]
void CheckZoneTouch()
  {
   double bid = SymbolInfoDouble(g_sym, SYMBOL_BID);
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].touched || !g_zones[i].tradable || g_zones[i].status != ZONE_CONFIRMED) continue;
      if(bid < g_zones[i].lo || bid > g_zones[i].hi) continue;
      g_zones[i].touched = true;
      Emit("zone_touch", 1, "info", JS("dir", DirName(g_zones[i].dir)) + "," + JN("lo", g_zones[i].lo, g_digits) + "," +
           JN("hi", g_zones[i].hi, g_digits) + "," + JI("score", g_zones[i].score),
           StringFormat("👀 Цена в %s-зоне %s–%s (балл %d), жду триггер", DirName(g_zones[i].dir), Px(g_zones[i].lo),
                        Px(g_zones[i].hi), g_zones[i].score), -1);
     }
  }

//--- утренний брифинг и дневной отчёт [§5.2]
void SendMorning()
  {
   datetime loc = LocalNow();
   string txt = StringFormat("☀️ План на %s · %s\nBias: D1 %s · H4 %s\nSELL-зона: %s\nBUY-зона: %s\nРиск %.2f%% на сделку, %.1f%% в день · до %d сделок · RR 1:%.1f",
                             StringSubstr(LocalStamp(loc), 8, 2) + "." + StringSubstr(LocalStamp(loc), 5, 2), g_sym, BiasName(g_d1), BiasName(g_h4),
                             NearestZoneText(-1), NearestZoneText(1), TradeRiskPct(EffectiveRiskMode()), g_set.dailyRiskPct,
                             g_set.maxTrades, g_set.rr);
   Emit("morning_brief", 0, "info", JS("bias_d1", BiasName(g_d1)) + "," + JS("bias_h4", BiasName(g_h4)), txt, (int)PERIOD_M5);
  }

void SendDailyReport()
  {
   g_day = CalcDay();
   datetime loc = LocalNow();
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   string txt = StringFormat("📊 Итог дня %s · %s\nСделок %d/%d · %+.1f pt · %+.2f %s\nБаланс: %.2f %s",
                             StringSubstr(LocalStamp(loc), 8, 2) + "." + StringSubstr(LocalStamp(loc), 5, 2), g_sym, g_day.trades,
                             g_set.maxTrades, g_day.pts, g_day.usd, cur, AccountInfoDouble(ACCOUNT_BALANCE), cur);
   Emit("daily_report", 0, "info", JI("trades", g_day.trades) + "," + JN("pts", g_day.pts, 1) + "," + JN("usd", g_day.usd, 2),
        txt, (int)PERIOD_M5);
  }

void CheckScheduled()
  {
   datetime loc = LocalNow();
   MqlDateTime t;
   TimeToStruct(loc, t);
   bool weekday = t.day_of_week >= 1 && t.day_of_week <= 5;
   if(!weekday) return;                                   // в субботу и воскресенье брифинг и отчёт не шлём
   string day = StringFormat("%04d.%02d.%02d", t.year, t.mon, t.day);
   int mins = t.hour * 60 + t.min;
   if(InpMorningHM != "" && day != g_lastMorningDay)
     {
      int at = ParseHM(InpMorningHM);
      if(mins >= at && mins < at + 60) { g_lastMorningDay = day; SaveSettings(); SendMorning(); }
     }
   if(InpReportHM != "" && day != g_lastReportDay)
     {
      int at = ParseHM(InpReportHM);
      if(mins >= at && mins < at + 60) { g_lastReportDay = day; SaveSettings(); SendDailyReport(); }
     }
  }

//--- команды [§8]
bool ParseNum(const string &k[], const string &v[], string key, double &out)
  {
   if(!JHas(k, key)) return false;
   out = StringToDouble(JGet(k, v, key));
   return true;
  }

bool ExecSet(const string &k[], const string &v[], string &msg)
  {
   string allowed[7] = {"risk_pct", "daily_risk_pct", "max_trades", "rr", "weak", "buy", "sell"};
   bool any = false;
   for(int i = 0; i < ArraySize(k); i++)
     {
      if(StringFind(k[i], "args.") != 0) continue;
      string name = StringSubstr(k[i], 5);
      bool known = false;
      for(int q = 0; q < 7; q++)
         if(allowed[q] == name) known = true;
      if(!known) { msg = "неизвестная настройка: " + name; return false; }
      any = true;
     }
   if(!any) { msg = "не указана настройка"; return false; }
   double risk = 0, daily = 0, rr = 0, mx = 0;
   bool hRisk = ParseNum(k, v, "args.risk_pct", risk), hDaily = ParseNum(k, v, "args.daily_risk_pct", daily);
   bool hRr = ParseNum(k, v, "args.rr", rr), hMax = ParseNum(k, v, "args.max_trades", mx);
   if(hRisk && (risk < 0.1 || risk > 5.0)) { msg = "риск на сделку: допустимо от 0.1 до 5"; return false; }
   if(hDaily && (daily < 1.0 || daily > 20.0)) { msg = "дневной риск: допустимо от 1 до 20"; return false; }
   if(hMax && (mx < 1 || mx > 10 || MathAbs(mx - MathRound(mx)) > 1e-9)) { msg = "максимум сделок: целое от 1 до 10"; return false; }
   if(hRr && (rr < 1.0 || rr > 5.0)) { msg = "RR: допустимо от 1 до 5"; return false; }
   bool hWeak = JHas(k, "args.weak"), hBuy = JHas(k, "args.buy"), hSell = JHas(k, "args.sell");
   string sw = JGet(k, v, "args.weak"), sb = JGet(k, v, "args.buy"), ss = JGet(k, v, "args.sell");
   if(hWeak && sw != "true" && sw != "false") { msg = "слабые сигналы: нужно да или нет"; return false; }
   if(hBuy && sb != "true" && sb != "false") { msg = "BUY: нужно да или нет"; return false; }
   if(hSell && ss != "true" && ss != "false") { msg = "SELL: нужно да или нет"; return false; }
   bool nb = hBuy ? (sb == "true") : g_set.tradeBuy, ns = hSell ? (ss == "true") : g_set.tradeSell;
   if(!nb && !ns) { msg = "нельзя выключить обе стороны"; return false; }
   string parts = "";
   if(hRisk) { g_set.riskPct = risk; g_set.cRisk = true; parts += (parts == "" ? "" : ", ") + StringFormat("риск %g%% на сделку", risk); }
   if(hDaily) { g_set.dailyRiskPct = daily; g_set.cDaily = true; parts += (parts == "" ? "" : ", ") + StringFormat("дневной риск %g%%", daily); }
   if(hMax) { g_set.maxTrades = (int)MathRound(mx); g_set.cMax = true; parts += (parts == "" ? "" : ", ") + StringFormat("максимум %d сделок в день", g_set.maxTrades); }
   if(hRr) { g_set.rr = rr; g_set.cRr = true; parts += (parts == "" ? "" : ", ") + StringFormat("RR 1:%g", rr); }
   if(hWeak) { g_set.weak = (sw == "true"); g_set.cWeak = true; parts += (parts == "" ? "" : ", ") + "слабые сигналы " + (g_set.weak ? "вкл" : "выкл"); }
   if(hBuy || hSell)
     {
      g_set.tradeBuy = nb;
      g_set.tradeSell = ns;
      g_set.cSide = true;
      parts += (parts == "" ? "" : ", ") + "стороны: " + (nb && ns ? "BUY и SELL" : (nb ? "только BUY" : "только SELL"));
     }
   SaveSettings();
   msg = parts;
   return true;
  }

bool ExecOpen(const string &k[], const string &v[], string &msg)
  {
   string side = JGet(k, v, "args.side");
   double slPts = 0, riskArg = 0, rrArg = 0;
   bool hSl = ParseNum(k, v, "args.sl_pts", slPts);
   bool hRisk = ParseNum(k, v, "args.risk_pct", riskArg), hRr = ParseNum(k, v, "args.rr", rrArg);
   if(side != "BUY" && side != "SELL") { msg = "side: BUY или SELL"; return false; }
   if(!hSl || slPts <= 0 || slPts > 50.0) { msg = "стоп обязателен: от 0 до 50 пунктов"; return false; }
   if(hRisk && (riskArg < 0.1 || riskArg > 5.0)) { msg = "риск на сделку: допустимо от 0.1 до 5"; return false; }
   if(hRr && (rrArg < 1.0 || rrArg > 5.0)) { msg = "RR: допустимо от 1 до 5"; return false; }
   if(HasPos()) { msg = "позиция уже открыта"; return false; }
   if(FridayCloseTime()) { msg = "пятница, закрытие рынка"; return false; }
   if(g_day.trades >= g_set.maxTrades) { msg = StringFormat("лимит сделок дня %d исчерпан", g_set.maxTrades); return false; }
   if(g_set.dailyRiskPct > 0 && DailyRiskLeft() <= 0) { msg = "дневной риск исчерпан"; return false; }
   double ask = SymbolInfoDouble(g_sym, SYMBOL_ASK), bid = SymbolInfoDouble(g_sym, SYMBOL_BID);
   if(ask - bid > InpMaxSpread) { msg = StringFormat("спред %.2f > %.2f", ask - bid, InpMaxSpread); return false; }
   int d = side == "BUY" ? 1 : -1;
   double dist = slPts;
   if(dist < MinStopDist()) { msg = StringFormat("стоп %.1f меньше стоп-уровня брокера", dist); return false; }
   double rr = hRr ? rrArg : g_set.rr;
   double entry = d > 0 ? ask : bid;
   double sl = NormalizeDouble(entry - d * dist, g_digits);
   double tp = NormalizeDouble(entry + d * rr * dist, g_digits);
   double riskUsd = 0;
   string why = "";
   double lot = CalcLot(dist, d, entry, riskUsd, why, hRisk ? riskArg : -1.0);
   if(lot <= 0) { msg = why; return false; }
   bool ok = d > 0 ? g_trade.Buy(lot, g_sym, 0.0, sl, tp, "MC MANUAL") : g_trade.Sell(lot, g_sym, 0.0, sl, tp, "MC MANUAL");
   uint rc = g_trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
     {
      RegisterReject(rc, g_trade.ResultRetcodeDescription(), "MANUAL");
      msg = StringFormat("ордер отклонён: %u %s", rc, g_trade.ResultRetcodeDescription());
      return false;
     }
   g_rejectStreak = 0;
   ulong tk; long id; int pd; double pv, po, psl, ptp;
   if(!FindPos(tk, id, pd, pv, po, psl, ptp)) { po = entry; id = 0; psl = sl; ptp = tp; pv = lot; }
   string tname = TypeName(TradeType(d));
   g_pId = id; g_pDir = d; g_pTrig = "MANUAL"; g_pCls = 0; g_pType = tname; g_pScore = 0; g_pSrcs = "-";
   g_pSess = SessionName(); g_pStrength = Stars(1); g_pStrN = 1;
   g_pEntry = po; g_pSL = psl; g_pTP = ptp; g_pLot = pv; g_pLal = 0; g_pRisk = riskUsd;
   g_pZoneW = 0; g_pZLo = 0; g_pZHi = 0;
   g_pBEAt = BEAt(MathAbs(ptp - po));
   g_pOpenT = TimeCurrent(); g_pMark = "MANUAL"; g_pManual = true; g_pWeak = false;
   g_beDone = false; g_partDone = false; g_exitTag = ""; g_closeRetry = 0;
   g_day = CalcDay();
   DrawLine("SL", psl, clrRed);
   DrawLine("TP", ptp, clrLime);
   DrawPanel();
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskPct = bal > 0 ? riskUsd / bal * 100.0 : 0;
   Lg(0, StringFormat("РУЧНАЯ СДЕЛКА %s | вход %s SL %s TP %s | лот %.2f | риск %.2f %s", DirName(d), Px(po), Px(psl), Px(ptp), pv, riskUsd, cur));
   string fields = JS("symbol", g_sym) + "," + JS("side", DirName(d)) + "," + JI("ticket", g_pId) + "," + JN("entry", po, g_digits) + "," +
                   JN("sl", psl, g_digits) + "," + JN("tp", ptp, g_digits) + "," + JN("lot", pv, 2) + "," + JN("risk_usd", riskUsd, 2) + "," +
                   JN("risk_pct", riskPct, 2) + "," + JS("trigger", "MANUAL") + "," + JS("mark", "MANUAL") + "," + JS("type", tname) + "," +
                   JB("manual", true) + "," + JB("weak", false) + "," + JI("strength", 1) + "," + JS("session", g_pSess) + "," +
                   JN("rr", MathAbs(ptp - po) / MathMax(MathAbs(po - psl), 0.01), 2) + "," + JN("sl_pts", dist, 1);
   Emit("trade_opened", 0, "info", fields,
        StringFormat("%s %s · ручная сделка\nEntry: %s\nSL: %s\nTP: %s\nRR: 1:%.1f\nРиск: %.2f %s (%.2f%%) · лот %.2f\n✋ Ручная сделка из Telegram",
                     g_sym, DirName(d), Px(po), Px(psl), Px(ptp), rr, riskUsd, cur, riskPct, pv), (int)PERIOD_M5);
   msg = StringFormat("открыта %s %.2f лота, SL %s, TP %s", DirName(d), pv, Px(psl), Px(ptp));
   return true;
  }

bool ExecCmd(string cmd, const string &k[], const string &v[], string &msg, int &shotTf)
  {
   shotTf = -1;
   if(cmd == "ping") { msg = "pong"; return true; }
   if(cmd == "status") { WriteStatus(); msg = "статус обновлён"; return true; }
   if(cmd == "pause")
     {
      g_set.paused = true;
      g_pauseReason = "Telegram";
      SaveSettings();
      msg = "пауза включена, новые входы остановлены";
      return true;
     }
   if(cmd == "resume")
     {
      g_set.paused = false;
      g_pauseReason = "";
      g_rejectStreak = 0;
      g_noSlTries = 0;
      SaveSettings();
      AlertSet("orders_rejected", false, "critical", "пауза снята командой resume, отказы ордеров сброшены");
      msg = "пауза снята, снова ищу входы";
      return true;
     }
   if(cmd == "reset")
     {
      bool wasPaused = g_set.paused;
      SettingsFromInputs();
      g_set.paused = wasPaused;                    // reset возвращает параметры, а не снимает паузу
      SaveSettings();
      DrawPanel();
      msg = "настройки возвращены к значениям входных параметров";
      return true;
     }
   if(cmd == "chart")
     {
      string tfs = JGet(k, v, "args.tf", "M5");
      ENUM_TIMEFRAMES tf = PERIOD_M5;
      if(tfs == "M15") tf = PERIOD_M15;
      else if(tfs == "H1") tf = PERIOD_H1;
      else if(tfs == "H4") tf = PERIOD_H4;
      else if(tfs != "M5") { msg = "таймфрейм " + tfs + " не поддерживается (M5, M15, H1, H4)"; return false; }
      if(!InpShots) { msg = "скрины выключены (InpShots)"; return false; }
      shotTf = (int)tf;
      msg = "скрин " + tfs;
      return true;
     }
   if(cmd == "set") return ExecSet(k, v, msg);
   if(cmd == "open") return ExecOpen(k, v, msg);

   ulong tk; long id; int d; double vol, op, sl, tp;
   if(cmd == "close" || cmd == "be" || cmd == "modify")
     {
      if(!FindPos(tk, id, d, vol, op, sl, tp)) { msg = "открытой позиции нет"; return false; }
      double cur = d > 0 ? SymbolInfoDouble(g_sym, SYMBOL_BID) : SymbolInfoDouble(g_sym, SYMBOL_ASK);
      if(cmd == "close")
        {
         g_exitTag = "MANUAL";
         if(!g_trade.PositionClose(tk))
           {
            g_exitTag = "";
            msg = "брокер отклонил закрытие: " + g_trade.ResultRetcodeDescription();
            return false;
           }
         msg = "позиция закрыта по рынку";
         return true;
        }
      if(cmd == "be")
        {
         double nsl = NormalizeDouble(op + d * InpBEOffset, g_digits);
         if(d * (nsl - sl) <= 0) { msg = "стоп уже в безубытке или выше"; return false; }
         if(d * (cur - nsl) < MinStopDist()) { msg = "прибыль меньше стоп-уровня брокера: безубыток не поставить"; return false; }
         if(!g_trade.PositionModify(tk, nsl, tp)) { msg = "брокер отклонил: " + g_trade.ResultRetcodeDescription(); return false; }
         g_beDone = true;
         g_pSL = nsl;
         DrawLine("SL", nsl, clrRed);
         Emit("trade_be", 0, "info", JI("ticket", g_pId) + "," + JN("sl", nsl, g_digits),
              StringFormat("⚪ %s %s: стоп в безубытке по команде\nSL: %s", g_sym, DirName(d), Px(nsl)), -1);
         msg = "стоп в безубытке: " + Px(nsl);
         return true;
        }
      // modify: новый стоп и/или тейк
      double nsl = sl, ntp = tp;
      bool hS = ParseNum(k, v, "args.sl", nsl), hT = ParseNum(k, v, "args.tp", ntp);
      if(!hS && !hT) { msg = "нужен новый sl или tp"; return false; }
      if(hS)
        {
         if(nsl <= 0) { msg = "стоп убрать нельзя"; return false; }
         if(!(d * (cur - nsl) >= MinStopDist())) { msg = "стоп с неправильной стороны от цены или ближе стоп-уровня"; return false; }
        }
      if(hT && !(d * (ntp - cur) >= MinStopDist())) { msg = "тейк с неправильной стороны от цены или ближе стоп-уровня"; return false; }
      nsl = NormalizeDouble(nsl, g_digits);
      ntp = NormalizeDouble(ntp, g_digits);
      if(!g_trade.PositionModify(tk, nsl, ntp)) { msg = "брокер отклонил: " + g_trade.ResultRetcodeDescription(); return false; }
      g_pSL = nsl;
      g_pTP = ntp;
      if(d * (nsl - op) >= 0) g_beDone = true;
      DrawLine("SL", nsl, clrRed);
      DrawLine("TP", ntp, clrLime);
      Emit("trade_modified", 0, "info", JI("ticket", g_pId) + "," + JN("sl", nsl, g_digits) + "," + JN("tp", ntp, g_digits),
           StringFormat("Стоп/тейк изменены: SL %s, TP %s", Px(nsl), Px(ntp)), -1);
      msg = StringFormat("новые уровни: SL %s, TP %s", Px(nsl), Px(ntp));
      return true;
     }
   msg = "неизвестная команда: " + cmd;
   return false;
  }

void RunCommand(string body)
  {
   string k[], v[];
   JParse(body, k, v);
   string id = JGet(k, v, "id");
   string cmd = JGet(k, v, "cmd");
   bool ok = false;
   string msg = "";
   int shotTf = -1;
   if(InpBridgeKey == "") msg = "команды не принимаются: InpBridgeKey пуст";
   else if(JGet(k, v, "key") != InpBridgeKey) msg = "неверный ключ моста";
   else
     {
      datetime expT = IsoToTime(JGet(k, v, "expires_utc"));
      if(expT == 0) msg = "нет срока действия команды";
      else if(expT < TimeGMT()) msg = "команда устарела";
      else ok = ExecCmd(cmd, k, v, msg, shotTf);
     }
   if(!ok) shotTf = -1;
   Emit("cmd_result", 0, "info", JS("id", id) + "," + JB("ok", ok) + "," + JS("msg", msg),
        (ok ? "✔️ Готово: " : "✖️ Не выполнено: ") + msg, shotTf);
  }

// Каждую секунду: файлы cmd\*.json по порядку ts_utc; выполненный или отклонённый файл удаляется
void ProcessCommands()
  {
   string files[];
   string name;
   long fh = FileFindFirst(g_dir + "\\cmd\\*.json", name, FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   do
     {
      int c = ArraySize(files);
      ArrayResize(files, c + 1);
      files[c] = name;
     }
   while(FileFindNext(fh, name));
   FileFindClose(fh);
   int n = ArraySize(files);
   if(n == 0) return;
   string bodies[], stamps[];
   ArrayResize(bodies, n);
   ArrayResize(stamps, n);
   for(int i = 0; i < n; i++)
     {
      string t = "";
      ReadUtf8(g_dir + "\\cmd\\" + files[i], t);
      bodies[i] = t;
      string k[], v[];
      JParse(t, k, v);
      stamps[i] = JGet(k, v, "ts_utc");
     }
   bool done[];
   ArrayResize(done, n);
   for(int i = 0; i < n; i++) done[i] = false;
   for(int step = 0; step < n; step++)
     {
      int best = -1;
      for(int i = 0; i < n; i++)
        {
         if(done[i]) continue;
         if(best < 0 || stamps[i] < stamps[best]) best = i;
        }
      if(best < 0) break;
      done[best] = true;
      RunCommand(bodies[best]);
      FileDelete(g_dir + "\\cmd\\" + files[best], FILE_COMMON);
     }
  }

void OnTimer()
  {
   if(!g_bridgeOn) return;
   ProcessCommands();
   ulong ms = GetTickCount64();
   if(ms - g_lastStatusMs >= (ulong)MathMax(1, InpStatusSec) * 1000)
     {
      g_lastStatusMs = ms;
      ReadHeartbeat();
      WriteStatus();
     }
   if(ms - g_lastAlertMs >= 5000)
     {
      g_lastAlertMs = ms;
      CheckAlerts();
      CheckScheduled();
     }
   CheckZoneTouch();
   DrainTG();
  }

string BridgeLine()
  {
   string s = "";
   if(g_set.paused) s += "ПАУЗА (" + g_pauseReason + ")\n";
   if(!g_bridgeOn) return s + "Бот: мост выключен\n";
   if(InpBridgeKey == "") s += "⚠️ InpBridgeKey пуст: команды не принимаются\n";
   if(g_botAlive) s += "Бот: онлайн\n";
   else
     {
      datetime ref = g_botHb > 0 ? g_botHb : g_startUtc;
      s += "Бот: нет связи " + IntegerToString((long)((TimeGMT() - ref) / 60)) + " мин\n";
     }
   return s;
  }

string DeinitText(int reason)
  {
   switch(reason)
     {
      case REASON_REMOVE:      return "снят с графика";
      case REASON_RECOMPILE:   return "перекомпиляция";
      case REASON_CHARTCHANGE: return "смена символа или периода графика";
      case REASON_CHARTCLOSE:  return "график закрыт";
      case REASON_PARAMETERS:  return "изменены параметры";
      case REASON_ACCOUNT:     return "смена счёта";
      case REASON_TEMPLATE:    return "применён шаблон";
      case REASON_INITFAILED:  return "ошибка запуска";
      case REASON_CLOSE:       return "закрытие терминала";
      case REASON_PROGRAM:     return "советник остановил сам себя";
     }
   return "причина " + IntegerToString(reason);
  }

//+------------------------------------------------------------------+
//| Основной цикл                                                    |
//+------------------------------------------------------------------+
void OnNewM5()
  {
   g_day = CalcDay();

   datetime h1 = iTime(g_sym, PERIOD_H1, 0);
   if(h1 != g_lastH1)
     {
      g_lastH1 = h1;
      UpdateBias();
     }

   datetime m15 = iTime(g_sym, PERIOD_M15, 0);
   if(m15 != g_lastM15)
     {
      g_lastM15 = m15;
      RebuildZones();
      CheckEarlyExit();
     }

   UpdateZoneStates();
   DrawZones();
   DrawPanel();
   TryEntry();
  }

int OnInit()
  {
   g_sym = InpSymbol == "" ? _Symbol : InpSymbol;
   if(_Symbol != g_sym)
     {
      Print("Робот торгует только ", g_sym, ". Откройте график ", g_sym, " (M5), сейчас: ", _Symbol);
      return INIT_FAILED;
     }
   if(!SymbolSelect(g_sym, true))
     {
      Print("Символ не найден в Обзоре рынка: ", g_sym);
      return INIT_FAILED;
     }
   g_digits = (int)SymbolInfoInteger(g_sym, SYMBOL_DIGITS);
   g_point = SymbolInfoDouble(g_sym, SYMBOL_POINT);
   g_isTester = (bool)MQLInfoInteger(MQL_TESTER);
   string up = g_sym;
   StringToUpper(up);
   if(StringFind(up, "XAU") < 0 && StringFind(up, "GOLD") < 0)
     {
      Print("Mr. Casino v3 торгует только золото (XAUUSD / GOLD#). Символ графика: ", g_sym, " — робот не запущен.");
      return INIT_FAILED;
     }
   g_k = 1.0;
   g_ks = 1.0;
   g_crypto = false;
   g_bridgeOn = InpBridge && !g_isTester;
   g_robotId = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "_" + IntegerToString(InpMagic);
   g_dir = "MrCasino\\" + g_robotId;
   g_startUtc = TimeGMT();
   g_lastTickMs = GetTickCount64();
   SettingsFromInputs();
   if(g_bridgeOn)
     {
      EnsureDirs();
      LoadSettings();
      if(InpBridgeKey == "")
         Print("Внимание: InpBridgeKey пуст, команды из Telegram не принимаются.");
     }
   g_quotaMin = ParseHM(InpQuotaFrom);
   Print(StringFormat("Стоп %.1f–%.1f (True Stop %s), %s, безубыток после %.2f пути, сделок в день %d, слабые подтверждения %s",
                      InpMinSL, InpMaxSL, InpTrueStop ? "вкл" : "выкл",
                      InpTPMode == TP_RR ? StringFormat("тейк RR 1:%.1f", g_set.rr)
                                         : StringFormat("тейк %.1f–%.1f (LAL, RR от %.1f)", InpTPMin, InpTPMax, InpMinRR),
                      InpBEFrac, g_set.maxTrades, g_set.weak ? "вкл" : "выкл (добор с " + InpQuotaFrom + ")"));
   Print(StringFormat("Риск: %.2f%% на сделку, в день до %.1f%% баланса%s", TradeRiskPct(EffectiveRiskMode()), g_set.dailyRiskPct,
                      EffectiveRiskMode() == RISK_FIXED ? " (режим FIXED: лот " + DoubleToString(InpFixedLot, 2) + ", дневной лимит действует)" : ""));
   g_sessStart = ParseHM(InpSessionStart);
   g_sessEnd = ParseHM(InpSessionEnd);
   g_londonMin = ParseHM(InpLondonStart);
   g_nyMin = ParseHM(InpNYStart);
   ParseNews();
   if(Period() != PERIOD_M5)
      Print("Внимание: робот рассчитан на график M5.");

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints((ulong)MathMax(1.0, MathRound(InpMaxSlip / g_point)));
   g_trade.SetTypeFillingBySymbol(g_sym);

   if(EffectiveRiskMode() != InpRiskMode)
      Print("Реальный счёт: режим риска ", EnumToString(InpRiskMode), " заменён на PERCENT.");

   ArrayResize(g_zones, 0);
   ArrayResize(g_lk, 0);
   g_hasLv[0] = false; g_hasLv[1] = false;
   g_lastM5 = 0; g_lastM15 = 0; g_lastH1 = 0;
   g_day = CalcDay();
   ManagePosition();
   Print("Mr. Casino v3.3 запущен: ", g_sym, ", риск ", EnumToString(EffectiveRiskMode()), ", магик ", InpMagic,
         ", лог-уровень ", InpLogLevel, ", Бишкек ", LocalHM());
   if(g_bridgeOn)
     {
      EventSetTimer(1);
      Emit("ea_started", 0, "info", JS("version", "3.30") + "," + JI("login", AccountInfoInteger(ACCOUNT_LOGIN)) + "," + JS("symbol", g_sym) + "," +
           JN("risk_pct", TradeRiskPct(EffectiveRiskMode()), 2) + "," + JN("daily_risk_pct", g_set.dailyRiskPct, 2) + "," +
           JI("max_trades", g_set.maxTrades) + "," + JN("rr", g_set.rr, 2),
           StringFormat("▶️ Робот запущен: v3.3 · %s · %s · %s · риск %g%% / %g%% в день", g_sym, AccountInfoString(ACCOUNT_SERVER),
                        AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL ? "реальный счёт" : "демо",
                        TradeRiskPct(EffectiveRiskMode()), g_set.dailyRiskPct), -1);
     }
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(g_bridgeOn)
     {
      EventKillTimer();
      Emit("ea_stopped", 0, "info", JS("reason", DeinitText(reason)), "⏹ Робот остановлен: " + DeinitText(reason), -1);
     }
   ObjectsDeleteAll(0, OBJ_PREFIX);
   Comment("");
  }

void OnTick()
  {
   g_lastTickMs = GetTickCount64();
   ManagePosition();
   datetime t = iTime(g_sym, PERIOD_M5, 0);
   if(t == 0) return;
   if(t != g_lastM5)
     {
      g_lastM5 = t;
      OnNewM5();
     }
  }
//+------------------------------------------------------------------+
