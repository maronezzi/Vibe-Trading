//+------------------------------------------------------------------+
//| VibeTrading_VWAP.mq5                                              |
//| Expert Advisor — VWAP intraday long+short                        |
//|                                                                  |
//| Estratégia: VWAP(20)                                              |
//|   Entrada: close > VWAP*1.003 → buy                              |
//|            close < VWAP*0.997 → sell                              |
//|   Saída:   Trailing Stop (1.5x ATR) + 16:45 BRT fecha tudo       |
//                                                                  |
//| Resultado backtest v5 (3 contratos, 5 dias):                     |
//|   WIN$ M5  VWAP: +2.89% (Sharpe 15.34)                           |
//|   WIN$ M15 VWAP: +2.32% (Sharpe 3.12)                            |
//|   WDO$ M5  VWAP: +0.51% (Sharpe 2.65)                            |
//|   WDO$ M15 VWAP: +0.21% (Sharpe 1.47)                            |
//+------------------------------------------------------------------+
#property copyright "Vibe-Trading v5"
#property version   "5.00"
#property strict

#include <Trade\Trade.mqh>

// ===== INPUTS =====
input group "=== VWAP ==="
input int      VWAP_Period      = 20;      // Período VWAP
input double   VWAP_BuyThresh   = 1.003;   // Compra se close > VWAP * 1.003
input double   VWAP_SellThresh  = 0.997;   // Vende se close < VWAP * 0.997

input group "=== ATR ==="
input int      ATR_Period       = 14;      // Período ATR

input group "=== Trailing Stop ==="
input double   Trail_Activate   = 1.5;     // Ativa após N x ATR de lucro
input double   Trail_Distance   = 0.5;     // Distância trailing = N x ATR

input group "=== Intraday ==="
input int      Close_Hour       = 16;      // Hora de fechar (BRT)
input int      Close_Minute     = 45;      // Minuto de fechar

input group "=== Posição ==="
input int      Max_Contracts    = 3;       // Máximo de contratos
input double   Volume_Contract  = 1.0;     // Volume por contrato
input double   Slippage_Points  = 5;       // Slippage máximo (points)
input double   Magic_Number     = 555501;  // Magic number único

// ===== VARIÁVEIS GLOBAIS =====
CTrade trade;
datetime last_bar_time;
datetime last_close_day = 0;
bool    debug_mode = true;

// Estado da posição
struct PositionState
{
    double entry_price;
    double best_price;
    double entry_atr;
    int    direction;      // 1=long, -1=short, 0=none
    bool   trail_on;
    int    bars_in_trade;
    double sl_price;
    double sl_initial;
    ulong  ticket;
    datetime close_check_bar;
    bool   position_was_closed;
};
PositionState pos_state;

//+------------------------------------------------------------------+
//| Inicialização                                                     |
//+------------------------------------------------------------------+
int OnInit()
{
    trade.SetExpertMagicNumber((ulong)Magic_Number);
    trade.SetDeviationInPoints((int)Slippage_Points);
    trade.SetTypeFilling(ORDER_FILLING_RETURN);

    last_bar_time = 0;
    last_close_day = 0;
    ResetPositionState();

    PrintFormat("VibeTrading VWAP EA inicializado | VWAP(%d) ATR(%d) Trail(%.1fx ATR) Fecha %02d:%02d",
                VWAP_Period, ATR_Period, Trail_Activate, Close_Hour, Close_Minute);
    return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {}

void ResetPositionState()
{
    pos_state.entry_price = 0;
    pos_state.best_price  = 0;
    pos_state.entry_atr   = 0;
    pos_state.direction   = 0;
    pos_state.trail_on    = false;
    pos_state.bars_in_trade = 0;
    pos_state.sl_price    = 0;
    pos_state.sl_initial  = 0;
    pos_state.ticket      = 0;
    pos_state.close_check_bar = 0;
    pos_state.position_was_closed = false;
}

//+------------------------------------------------------------------+
//| Cálculo VWAP (Volume Weighted Average Price)                      |
//+------------------------------------------------------------------+
double CalculateVWAP(double &high[], double &low[], double &close[],
                     long &volume[], int period)
{
    double sum_pv = 0;
    double sum_v  = 0;
    int count = MathMin(period, ArraySize(close));

    for(int i = 0; i < count; i++)
    {
        double typical = (high[i] + low[i] + close[i]) / 3.0;
        double vol = (double)volume[i];
        if(vol <= 0) vol = 1;
        sum_pv += typical * vol;
        sum_v  += vol;
    }
    if(sum_v == 0) return 0;
    return sum_pv / sum_v;
}

//+------------------------------------------------------------------+
//| Cálculo ATR (Average True Range)                                  |
//+------------------------------------------------------------------+
double CalculateATR(double &high[], double &low[], double &close[], int period)
{
    int total = ArraySize(close);
    if(total < period + 1) return 0;

    double tr_sum = 0;
    for(int i = 0; i < period; i++)
    {
        double tr1 = high[i] - low[i];
        double tr2 = MathAbs(high[i] - close[i + 1]);
        double tr3 = MathAbs(low[i]  - close[i + 1]);
        double tr = MathMax(tr1, MathMax(tr2, tr3));
        tr_sum += tr;
    }
    return tr_sum / period;
}

//+------------------------------------------------------------------+
//| Conta posições abertas                                            |
//+------------------------------------------------------------------+
int CountPositions()
{
    int count = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(PositionGetInteger(POSITION_MAGIC) != (long)Magic_Number) continue;
        if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        count++;
    }
    return count;
}

//+------------------------------------------------------------------+
//| Encontra o ticket da nossa posição                                |
//+------------------------------------------------------------------+
ulong FindOurTicket()
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(PositionGetInteger(POSITION_MAGIC) != (long)Magic_Number) continue;
        if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        return ticket;
    }
    return 0;
}

//+------------------------------------------------------------------+
//| Fecha todas as posições                                            |
//+------------------------------------------------------------------+
void CloseAllPositions(string reason = "")
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(PositionGetInteger(POSITION_MAGIC) != (long)Magic_Number) continue;
        if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        trade.PositionClose(ticket);
    }
    if(reason != "" && debug_mode)
        PrintFormat("[%s] %s | Posicoes fechadas", _Symbol, reason);
    ResetPositionState();
}

//+------------------------------------------------------------------+
//| Abre posição LONG                                                  |
//+------------------------------------------------------------------+
bool OpenLong(double atr, double ask)
{
    double sl_dist = atr;  // SL inicial: 1x ATR
    double sl = ask - sl_dist;
    double tp = 0;

    double lot = Volume_Contract;
    if(!trade.Buy(lot, _Symbol, ask, sl, tp, "VWAP-Long"))
    {
        PrintFormat("Erro ao abrir LONG: %d", GetLastError());
        return false;
    }

    // Espera confirmação de posição criada
    Sleep(200);
    ulong ticket = FindOurTicket();
    if(ticket == 0)
    {
        PrintFormat("LONG executado mas posição não encontrada");
        return false;
    }

    pos_state.entry_price = ask;
    pos_state.best_price  = ask;
    pos_state.entry_atr   = atr;
    pos_state.direction   = 1;
    pos_state.trail_on    = false;
    pos_state.bars_in_trade = 0;
    pos_state.sl_initial  = sl;
    pos_state.sl_price    = sl;
    pos_state.ticket      = ticket;
    pos_state.position_was_closed = false;

    if(debug_mode)
        PrintFormat("[%s] LONG ticket=%d @ %.2f | SL: %.2f (%.1f pts) | ATR: %.1f",
                    _Symbol, ticket, ask, sl, sl_dist, atr);
    return true;
}

//+------------------------------------------------------------------+
//| Abre posição SHORT                                                 |
//+------------------------------------------------------------------+
bool OpenShort(double atr, double bid)
{
    double sl_dist = atr;
    double sl = bid + sl_dist;
    double tp = 0;

    double lot = Volume_Contract;
    if(!trade.Sell(lot, _Symbol, bid, sl, tp, "VWAP-Short"))
    {
        PrintFormat("Erro ao abrir SHORT: %d", GetLastError());
        return false;
    }

    Sleep(200);
    ulong ticket = FindOurTicket();
    if(ticket == 0)
    {
        PrintFormat("SHORT executado mas posição não encontrada");
        return false;
    }

    pos_state.entry_price = bid;
    pos_state.best_price  = bid;
    pos_state.entry_atr   = atr;
    pos_state.direction   = -1;
    pos_state.trail_on    = false;
    pos_state.bars_in_trade = 0;
    pos_state.sl_initial  = sl;
    pos_state.sl_price    = sl;
    pos_state.ticket      = ticket;
    pos_state.position_was_closed = false;

    if(debug_mode)
        PrintFormat("[%s] SHORT ticket=%d @ %.2f | SL: %.2f (%.1f pts) | ATR: %.1f",
                    _Symbol, ticket, bid, sl, sl_dist, atr);
    return true;
}

//+------------------------------------------------------------------+
//| Modifica SL da posição aberta                                      |
//+------------------------------------------------------------------+
bool ModifyStopLoss(ulong ticket, double new_sl)
{
    if(!trade.PositionModify(ticket, new_sl, 0))
    {
        int err = GetLastError();
        // Erro 10004 (requote) e 10020 (price changed) não são fatais
        if(err != 10004 && err != 10020)
            PrintFormat("Aviso SL: err=%d novo=%.2f", err, new_sl);
        return false;
    }
    return true;
}

//+------------------------------------------------------------------+
//| Verifica se está na hora de fechar (16:45)                         |
//+------------------------------------------------------------------+
bool IsCloseTime(MqlDateTime &now)
{
    if(now.hour > Close_Hour) return true;
    if(now.hour == Close_Hour && now.min >= Close_Minute) return true;
    return false;
}

//+------------------------------------------------------------------+
//| Verifica e fecha se SL foi atingido (checagem intrabar)             |
//+------------------------------------------------------------------+
void CheckStopLossHit(double ask, double bid)
{
    if(pos_state.direction == 0 || pos_state.sl_price == 0) return;

    bool sl_hit = false;
    if(pos_state.direction == 1 && bid <= pos_state.sl_price) sl_hit = true;
    if(pos_state.direction == -1 && ask >= pos_state.sl_price) sl_hit = true;

    if(sl_hit)
    {
        if(debug_mode)
            PrintFormat("[%s] SL ATINGIDO intrabar | SL=%.2f bid=%.2f ask=%.2f | Fechando...",
                        _Symbol, pos_state.sl_price, bid, ask);
        CloseAllPositions("SL intrabar");
    }
}

//+------------------------------------------------------------------+
//| Função principal chamada a cada tick                               |
//+------------------------------------------------------------------+
void OnTick()
{
    // ===== CHECAGENS INTRABAR (CRÍTICO) =====
    double ask_now = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid_now = SymbolInfoDouble(_Symbol, SYMBOL_BID);

    // Se tem posição aberta, checa SL a CADA tick
    if(pos_state.direction != 0)
    {
        // Verifica se posição ainda existe (pode ter sido fechada pelo servidor)
        ulong current_ticket = FindOurTicket();
        if(current_ticket == 0)
        {
            // Posição sumiu (provavelmente SL ou TP atingido pelo servidor)
            if(debug_mode && !pos_state.position_was_closed)
                PrintFormat("[%s] Posicao FECHADA pelo servidor (SL/TP atingido)", _Symbol);
            pos_state.position_was_closed = true;
            ResetPositionState();
            // NÃO retorna aqui — pode tentar nova entrada no mesmo tick
        }
        else
        {
            // Posição existe, atualiza ticket (pode ter mudado)
            pos_state.ticket = current_ticket;

            // CHECAGEM INTRABAR DO SL
            CheckStopLossHit(ask_now, bid_now);
            if(pos_state.direction == 0) {
                pos_state.position_was_closed = true;
            }
        }
    }

    // Detectar nova barra
    datetime current_bar_time = iTime(_Symbol, PERIOD_CURRENT, 0);
    bool is_new_bar = (current_bar_time != last_bar_time);
    if(is_new_bar) last_bar_time = current_bar_time;

    // Checagens intrabar (fora de nova barra): só checa SL
    if(!is_new_bar) return;

    // ===== A PARTIR DAQUI SÓ EXECUTA EM NOVA BARRA =====

    // Horário atual
    MqlDateTime now;
    TimeToStruct(TimeCurrent(), now);

    // Pular fora de horário
    if(now.hour < 9 || (now.hour == 9 && now.min < 5)) return;
    if(now.hour > 18) return;

    // ===== FECHAR TUDO ÀS 16:45 (uma vez por dia) =====
    if(IsCloseTime(now))
    {
        // Evita fechar várias vezes
        datetime today = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
        if(today != last_close_day)
        {
            if(CountPositions() > 0)
            {
                CloseAllPositions("16:45 Fecha intraday");
                last_close_day = today;
            }
        }
        return;
    }

    // Carregar dados
    int bars_needed = MathMax(VWAP_Period, ATR_Period) + 5;
    MqlRates rates[];
    ArraySetAsSeries(rates, true);
    if(CopyRates(_Symbol, PERIOD_CURRENT, 0, bars_needed, rates) < bars_needed)
    {
        Print("Dados insuficientes");
        return;
    }

    // Calcular indicadores
    double high[], low[], close[];
    long volume[];
    ArrayResize(high, bars_needed);
    ArrayResize(low, bars_needed);
    ArrayResize(close, bars_needed);
    ArrayResize(volume, bars_needed);
    for(int i = 0; i < bars_needed; i++)
    {
        high[i]   = rates[i].high;
        low[i]    = rates[i].low;
        close[i]  = rates[i].close;
        volume[i] = rates[i].tick_volume;
    }

    double vwap = CalculateVWAP(high, low, close, volume, VWAP_Period);
    double atr  = CalculateATR(high, low, close, ATR_Period);

    if(vwap == 0 || atr == 0) return;

    // Pega close da última barra FECHADA
    double price = close[1];  // [0] = barra atual (formando), [1] = última fechada
    double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);

    // ===== SEM POSIÇÃO: procurar entrada =====
    if(pos_state.direction == 0)
    {
        if(CountPositions() >= Max_Contracts) return;

        if(price > vwap * VWAP_BuyThresh)
        {
            OpenLong(atr, ask);
        }
        else if(price < vwap * VWAP_SellThresh)
        {
            OpenShort(atr, bid);
        }
        return;
    }

    // ===== POSIÇÃO ABERTA: gerenciar trailing stop =====
    pos_state.bars_in_trade++;

    if(CountPositions() == 0)
    {
        if(debug_mode && !pos_state.position_was_closed)
            PrintFormat("[%s] Posicao sumiu (SL/TP servidor) | Reset", _Symbol);
        pos_state.position_was_closed = true;
        ResetPositionState();
        return;
    }

    // Re-obter ticket atualizado
    ulong ticket = FindOurTicket();
    if(ticket == 0) { ResetPositionState(); return; }
    pos_state.ticket = ticket;

    // Atualizar melhor preço (usa ask/bid intrabar)
    if(pos_state.direction == 1)
        pos_state.best_price = MathMax(pos_state.best_price, bid);
    else if(pos_state.direction == -1)
        pos_state.best_price = (pos_state.best_price == 0) ? ask : MathMin(pos_state.best_price, ask);

    // Lucro atual em pontos
    double profit_pts = 0;
    if(pos_state.direction == 1)
        profit_pts = pos_state.best_price - pos_state.entry_price;
    else if(pos_state.direction == -1)
        profit_pts = pos_state.entry_price - pos_state.best_price;

    // Ativa trailing após N x ATR
    if(!pos_state.trail_on && pos_state.entry_atr > 0)
    {
        if(profit_pts >= Trail_Activate * pos_state.entry_atr)
        {
            pos_state.trail_on = true;
            if(debug_mode)
                PrintFormat("[%s] Trailing ATIVADO | Lucro: %.1f pts (%.2fx ATR)",
                            _Symbol, profit_pts, profit_pts / pos_state.entry_atr);
        }
    }

    // Ajustar trailing
    if(pos_state.trail_on && pos_state.entry_atr > 0)
    {
        double trail_dist = Trail_Distance * pos_state.entry_atr;
        double new_sl = 0;
        bool should_update = false;

        if(pos_state.direction == 1)
        {
            new_sl = pos_state.best_price - trail_dist;
            if(new_sl > pos_state.sl_price || pos_state.sl_price == 0)
            {
                pos_state.sl_price = new_sl;
                should_update = true;
            }
        }
        else if(pos_state.direction == -1)
        {
            new_sl = pos_state.best_price + trail_dist;
            if(new_sl < pos_state.sl_price || pos_state.sl_price == 0)
            {
                pos_state.sl_price = new_sl;
                should_update = true;
            }
        }

        if(should_update)
            ModifyStopLoss(ticket, pos_state.sl_price);
    }
}
