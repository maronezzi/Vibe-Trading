//+------------------------------------------------------------------+
//| VibeTrading_TradeLogger.mq5                                       |
//| EA logger — captura OnTradeTransaction() e escreve CSV            |
//|                                                                   |
//| Propósito: registrar TODOS os eventos de trade da conta em        |
//| tempo real, direto da fonte (sem polling). O watcher Linux        |
//| faz tail do CSV e alimenta o SQLite.                              |
//|                                                                   |
//| Uso: anexar em UM chart qualquer (ex: WIN$ M1).                   |
//| Captura eventos de TODOS os símbolos da conta.                    |
//|                                                                   |
//| CSV: MQL5/Files/vt_trade_events.csv (pipe-delimited)              |
//+------------------------------------------------------------------+
#property copyright "Vibe-Trading"
#property version   "1.00"
#property strict
#property description "Logger de eventos de trade via OnTradeTransaction -> CSV"

// ===== INPUTS =====
input group "=== Logger ==="
input bool     Log_Orders      = true;       // Logar ORDER_ADD/UPDATE/DELETE
input bool     Log_Deals       = true;       // Logar DEAL_ADD (fills)
input bool     Log_Requests    = false;      // Logar REQUEST (verboso)
input int      Heartbeat_Min   = 5;          // Heartbeat a cada N minutos (0=off)
input int      Backfill_Days   = 1;          // Backfill de deals historicos ao iniciar (0=off)

// ===== GLOBAIS =====
int      g_file_handle = INVALID_HANDLE;
string   g_filename    = "vt_trade_events.csv";
datetime g_last_heartbeat = 0;
ulong    g_event_seq   = 0;

//+------------------------------------------------------------------+
//| Nomes legíveis para enums                                         |
//+------------------------------------------------------------------+
string TransTypeName(ENUM_TRADE_TRANSACTION_TYPE t)
{
   switch(t)
   {
      case TRADE_TRANSACTION_ORDER_ADD:    return "ORDER_ADD";
      case TRADE_TRANSACTION_ORDER_UPDATE: return "ORDER_UPDATE";
      case TRADE_TRANSACTION_ORDER_DELETE: return "ORDER_DELETE";
      case TRADE_TRANSACTION_DEAL_ADD:     return "DEAL_ADD";
      case TRADE_TRANSACTION_REQUEST:      return "REQUEST";
      // Build 5660 (2026): tipos adicionais fora do enum classico 0-4. O enum
      // nao esta em include legivel, entao os nomes abaixo sao INFERIDOS pela
      // assinatura empirica dos eventos (DumpEnum() no OnInit confirma os
      // valores reais no journal). Blast radius zero: nenhum consumidor usa
      // esses tipos (copilot so le DEAL_ADD + deal_entry='OUT').
      case 3:  return "POSITION_UPDATE";   // FILLED, carrega SL, comment "[sl X]"
      case 9:  return "ORDER_REQUEST";     // STARTED, order_type, ticket=0 (pre-ticket)
      default:                             return "UNKNOWN_" + IntegerToString((int)t);
   }
}

string OrderTypeName(ENUM_ORDER_TYPE t)
{
   switch(t)
   {
      case ORDER_TYPE_BUY:             return "BUY";
      case ORDER_TYPE_SELL:            return "SELL";
      case ORDER_TYPE_BUY_LIMIT:       return "BUY_LIMIT";
      case ORDER_TYPE_SELL_LIMIT:      return "SELL_LIMIT";
      case ORDER_TYPE_BUY_STOP:        return "BUY_STOP";
      case ORDER_TYPE_SELL_STOP:       return "SELL_STOP";
      case ORDER_TYPE_BUY_STOP_LIMIT:  return "BUY_STOP_LIMIT";
      case ORDER_TYPE_SELL_STOP_LIMIT: return "SELL_STOP_LIMIT";
      case ORDER_TYPE_CLOSE_BY:        return "CLOSE_BY";
      default:                         return "TYPE_" + IntegerToString((int)t);
   }
}

string OrderStateName(ENUM_ORDER_STATE s)
{
   switch(s)
   {
      case ORDER_STATE_STARTED:          return "STARTED";
      case ORDER_STATE_PLACED:           return "PLACED";
      case ORDER_STATE_CANCELED:         return "CANCELED";
      case ORDER_STATE_PARTIAL:          return "PARTIAL";
      case ORDER_STATE_FILLED:           return "FILLED";
      case ORDER_STATE_REJECTED:         return "REJECTED";
      case ORDER_STATE_EXPIRED:          return "EXPIRED";
      case ORDER_STATE_REQUEST_ADD:      return "REQUEST_ADD";
      case ORDER_STATE_REQUEST_MODIFY:   return "REQUEST_MODIFY";
      case ORDER_STATE_REQUEST_CANCEL:   return "REQUEST_CANCEL";
      default:                           return "STATE_" + IntegerToString((int)s);
   }
}

string DealTypeName(ENUM_DEAL_TYPE t)
{
   switch(t)
   {
      case DEAL_TYPE_BUY:              return "BUY";
      case DEAL_TYPE_SELL:             return "SELL";
      case DEAL_TYPE_BALANCE:          return "BALANCE";
      case DEAL_TYPE_CREDIT:           return "CREDIT";
      case DEAL_TYPE_CHARGE:           return "CHARGE";
      case DEAL_TYPE_CORRECTION:       return "CORRECTION";
      case DEAL_TYPE_BONUS:            return "BONUS";
      case DEAL_TYPE_COMMISSION:       return "COMMISSION";
      case DEAL_TYPE_COMMISSION_DAILY: return "COMMISSION_DAILY";
      case DEAL_TYPE_COMMISSION_MONTHLY: return "COMMISSION_MONTHLY";
      case DEAL_TYPE_INTEREST:         return "INTEREST";
      case DEAL_TYPE_BUY_CANCELED:     return "BUY_CANCELED";
      case DEAL_TYPE_SELL_CANCELED:    return "SELL_CANCELED";
      default:                         return "DEAL_" + IntegerToString((int)t);
   }
}

string DealEntryName(ENUM_DEAL_ENTRY e)
{
   switch(e)
   {
      case DEAL_ENTRY_IN:            return "IN";
      case DEAL_ENTRY_OUT:           return "OUT";
      case DEAL_ENTRY_INOUT:         return "INOUT";
      case DEAL_ENTRY_OUT_BY:        return "OUT_BY";
      default:                       return "ENTRY_" + IntegerToString((int)e);
   }
}

//+------------------------------------------------------------------+
//| Garante que o arquivo está aberto (re-abre se necessário)         |
//+------------------------------------------------------------------+
bool EnsureFileOpen()
{
   if(g_file_handle != INVALID_HANDLE)
      return true;

   g_file_handle = FileOpen(g_filename, FILE_READ|FILE_WRITE|FILE_ANSI|FILE_TXT, 0, CP_UTF8);

   if(g_file_handle == INVALID_HANDLE)
   {
      PrintFormat("ERRO: nao consegui abrir %s (err=%d)", g_filename, GetLastError());
      return false;
   }

   // Se arquivo vazio, escreve header
   if(FileSize(g_file_handle) == 0)
   {
      string header = "seq|event_time|trans_type|order_ticket|deal_ticket|symbol|"
                      "order_type|order_state|volume|price|sl|tp|"
                      "deal_type|deal_entry|deal_profit|deal_commission|deal_swap|"
                      "deal_price|deal_volume|position_ticket|comment";
      FileWriteString(g_file_handle, header + "\r\n");
      FileFlush(g_file_handle);
   }

   // Sempre posiciona no final para append
   FileSeek(g_file_handle, 0, SEEK_END);
   return true;
}

//+------------------------------------------------------------------+
//| Escreve uma linha de evento no CSV                                |
//+------------------------------------------------------------------+
void WriteEvent(string trans_type,
                ulong order_ticket, ulong deal_ticket,
                string symbol, string order_type, string order_state,
                double volume, double price, double sl, double tp,
                string deal_type, string deal_entry,
                double deal_profit, double deal_commission, double deal_swap,
                double deal_price, double deal_volume,
                ulong position_ticket, string comment)
{
   if(!EnsureFileOpen()) return;

   g_event_seq++;

   // Timestamp ISO
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   string ts = StringFormat("%04d-%02d-%02dT%02d:%02d:%02d",
                            dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec);

   string line = StringFormat("%d|%s|%s|%d|%d|%s|%s|%s|%.6f|%.6f|%.6f|%.6f|%s|%s|%.6f|%.6f|%.6f|%.6f|%.6f|%d|%s",
                              g_event_seq, ts, trans_type,
                              order_ticket, deal_ticket,
                              symbol, order_type, order_state,
                              volume, price, sl, tp,
                              deal_type, deal_entry,
                              deal_profit, deal_commission, deal_swap,
                              deal_price, deal_volume,
                              position_ticket, comment);

   // Verifica se o arquivo ainda existe (watcher pode rotacionar)
   if(!FileIsExist(g_filename))
   {
      FileClose(g_file_handle);
      g_file_handle = INVALID_HANDLE;
      if(!EnsureFileOpen()) return;
   }

   FileSeek(g_file_handle, 0, SEEK_END);
   FileWriteString(g_file_handle, line + "\r\n");
   FileFlush(g_file_handle);
}

//+------------------------------------------------------------------+
//| Heartbeat periódico                                               |
//+------------------------------------------------------------------+
void WriteHeartbeat()
{
   if(Heartbeat_Min <= 0) return;

   datetime now = TimeCurrent();
   if(now - g_last_heartbeat < Heartbeat_Min * 60) return;
   g_last_heartbeat = now;

   WriteEvent("HEARTBEAT", 0, 0, _Symbol, "", "", 0, 0, 0, 0,
              "", "", 0, 0, 0, 0, 0, 0, "alive");
}

//+------------------------------------------------------------------+
//| Formata datetime -> ISO (mesmo formato do WriteEvent)             |
//+------------------------------------------------------------------+
string IsoTime(datetime t)
{
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d",
                       dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec);
}

//+------------------------------------------------------------------+
//| Imprime os valores reais do enum de transacoes (uma vez, no init) |
//| Confirma no journal o mapeamento dos tipos adicionais (case 3/9). |
//+------------------------------------------------------------------+
void DumpEnum()
{
   PrintFormat("ENUM_TRANS_TYPE: ORDER_ADD=%d ORDER_UPDATE=%d ORDER_DELETE=%d "
               "DEAL_ADD=%d REQUEST=%d",
               (int)TRADE_TRANSACTION_ORDER_ADD,
               (int)TRADE_TRANSACTION_ORDER_UPDATE,
               (int)TRADE_TRANSACTION_ORDER_DELETE,
               (int)TRADE_TRANSACTION_DEAL_ADD,
               (int)TRADE_TRANSACTION_REQUEST);
}

//+------------------------------------------------------------------+
//| Escreve um deal historico (backfill) no CSV                       |
//| seq = deal_ticket (deterministico -> dedup estavel no watcher:    |
//| re-rodar o backfill gera a mesma linha -> INSERT OR IGNORE)       |
//+------------------------------------------------------------------+
void WriteBackfillDeal(ulong deal_ticket)
{
   if(!EnsureFileOpen()) return;

   long   dtype   = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
   long   dentry  = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
   string symbol  = HistoryDealGetString(deal_ticket, DEAL_SYMBOL);
   double vol     = HistoryDealGetDouble(deal_ticket, DEAL_VOLUME);
   double price   = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
   double profit  = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
   double comm    = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
   double swap    = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
   long   pos     = HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
   long   order   = HistoryDealGetInteger(deal_ticket, DEAL_ORDER);
   datetime dtime = (datetime)HistoryDealGetInteger(deal_ticket, DEAL_TIME);
   string comment = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
   StringReplace(comment, "|", "/");
   StringReplace(comment, "\r", "");
   StringReplace(comment, "\n", " ");
   comment = comment + " [backfill]";

   string line = StringFormat("%d|%s|%s|%d|%d|%s|%s|%s|%.6f|%.6f|%.6f|%.6f|%s|%s|%.6f|%.6f|%.6f|%.6f|%.6f|%d|%s",
                              (int)deal_ticket, IsoTime(dtime), "DEAL_ADD",
                              (int)order, (int)deal_ticket,
                              symbol, "", "FILLED",
                              vol, price, 0.0, 0.0,
                              DealTypeName((ENUM_DEAL_TYPE)dtype),
                              DealEntryName((ENUM_DEAL_ENTRY)dentry),
                              profit, comm, swap,
                              price, vol,
                              (int)pos, comment);

   FileSeek(g_file_handle, 0, SEEK_END);
   FileWriteString(g_file_handle, line + "\r\n");
   FileFlush(g_file_handle);
}

//+------------------------------------------------------------------+
//| Backfill: recupera deals do historico ao iniciar (auto-cura apos  |
//| restart do terminal — nao depende de OnTradeTransaction ao vivo). |
//| Grava deals BUY/SELL desde o inicio do periodo como DEAL_ADD.     |
//| O watcher dedup via INSERT OR IGNORE; o copilot dedup por ticket. |
//+------------------------------------------------------------------+
void BackfillHistory()
{
   if(Backfill_Days <= 0) return;

   datetime now = TimeCurrent();
   datetime today_midnight = StringToTime(TimeToString(now, TIME_DATE));
   datetime from = today_midnight - (Backfill_Days - 1) * 86400;

   if(!HistorySelect(from, now))
   {
      PrintFormat("BACKFILL: HistorySelect falhou (err=%d)", GetLastError());
      return;
   }

   int total = HistoryDealsTotal();
   int written = 0;
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long dtype = HistoryDealGetInteger(ticket, DEAL_TYPE);
      // So trades reais (BUY/SELL) — ignora balance/commission/swap puros
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL) continue;
      WriteBackfillDeal(ticket);
      written++;
   }

   PrintFormat("BACKFILL: %d deals gravados (desde %s)",
               written, TimeToString(from, TIME_DATE|TIME_MINUTES));
}

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   g_file_handle = INVALID_HANDLE;
   g_last_heartbeat = 0;
   g_event_seq = 0;

   if(!EnsureFileOpen())
   {
      Print("FATAL: nao consegui abrir arquivo de log");
      return(INIT_FAILED);
   }

   PrintFormat("TradeLogger iniciado | Arquivo: %s | Heartbeat: %d min",
               g_filename, Heartbeat_Min);

   // Evento de inicialização
   WriteEvent("LOGGER_START", 0, 0, _Symbol, "", "", 0, 0, 0, 0,
              "", "", 0, 0, 0, 0, 0, 0,
              "EA attached to " + _Symbol + " " + EnumToString(Period()));

   DumpEnum();  // confirma valores reais do enum no journal (1x)

   // Recupera deals do histórico (auto-cura após restart do terminal)
   BackfillHistory();

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   WriteEvent("LOGGER_STOP", 0, 0, _Symbol, "", "", 0, 0, 0, 0,
              "", "", 0, 0, 0, 0, 0, 0,
              "reason=" + IntegerToString(reason));

   if(g_file_handle != INVALID_HANDLE)
   {
      FileFlush(g_file_handle);
      FileClose(g_file_handle);
      g_file_handle = INVALID_HANDLE;
   }

   Print("TradeLogger encerrado");
}

//+------------------------------------------------------------------+
//| OnTick — só para heartbeat                                        |
//+------------------------------------------------------------------+
void OnTick()
{
   WriteHeartbeat();
}

//+------------------------------------------------------------------+
//| OnTradeTransaction — o coração do logger                          |
//| Assinatura correta: MqlTradeTransaction + MqlTradeRequest +       |
//| MqlTradeResult. Dados extras via HistoryDealSelect.               |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   string trans_type = TransTypeName(trans.type);

   // Filtro por tipo
   if(trans.type == TRADE_TRANSACTION_REQUEST && !Log_Requests) return;
   if((trans.type == TRADE_TRANSACTION_ORDER_ADD ||
       trans.type == TRADE_TRANSACTION_ORDER_UPDATE ||
       trans.type == TRADE_TRANSACTION_ORDER_DELETE) && !Log_Orders) return;
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD && !Log_Deals) return;

   // Dados da ordem (do struct trans — sempre disponível)
   string o_type  = OrderTypeName(trans.order_type);
   string o_state = OrderStateName(trans.order_state);
   double o_vol   = trans.volume;
   double o_price = trans.price;
   double o_sl    = 0;
   double o_tp    = 0;

   // SL/TP via histórico da ordem (trans.sl/tp não disponível em todas builds)
   if(trans.order > 0 && HistoryOrderSelect(trans.order))
   {
      o_sl = HistoryOrderGetDouble(trans.order, ORDER_SL);
      o_tp = HistoryOrderGetDouble(trans.order, ORDER_TP);
   }

   // Dados do deal — tenta enriquecer via HistoryDealSelect
   string d_type   = "";
   string d_entry  = "";
   double d_profit = 0;
   double d_comm   = 0;
   double d_swap   = 0;
   double d_price  = 0;
   double d_vol    = 0;

   if(trans.type == TRADE_TRANSACTION_DEAL_ADD && trans.deal > 0)
   {
      if(HistoryDealSelect(trans.deal))
      {
         long dtype  = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
         long dentry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
         d_type   = DealTypeName((ENUM_DEAL_TYPE)dtype);
         d_entry  = DealEntryName((ENUM_DEAL_ENTRY)dentry);
         d_profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);
         d_comm   = HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
         d_swap   = HistoryDealGetDouble(trans.deal, DEAL_SWAP);
         d_price  = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
         d_vol    = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
      }
      else
      {
         // Fallback: usa o que tem no trans
         d_type = DealTypeName(trans.deal_type);
      }
   }

   // Comment — da request (se disponível) ou do histórico
   string comment = request.comment;
   if(comment == "" && trans.order > 0)
   {
      if(HistoryOrderSelect(trans.order))
         comment = HistoryOrderGetString(trans.order, ORDER_COMMENT);
   }
   // Sanitiza pipe e newline do comment
   StringReplace(comment, "|", "/");
   StringReplace(comment, "\r", "");
   StringReplace(comment, "\n", " ");

   WriteEvent(trans_type,
              trans.order, trans.deal,
              trans.symbol, o_type, o_state,
              o_vol, o_price, o_sl, o_tp,
              d_type, d_entry, d_profit, d_comm, d_swap,
              d_price, d_vol,
              trans.position, comment);
}
//+------------------------------------------------------------------+
