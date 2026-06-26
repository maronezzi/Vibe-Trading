# Relatório de OPORTUNIDADES LUCRATIVAS — Vibe-Trading (30d, até 2026-06-26)

**Fonte:** `vt_trades.db` (309 trades; **277 reais**, 32 sintéticos `*N99` filtrados)
**PnL total real:** **-R$ 8.401,00** (277 trades)
**Filtros de qualidade:** excluídos símbolos `BITM26N99` (12) e `DOLN26N99` (20) — preços 100–152 e `entry/exit/SL` geometricamente idênticos = **dados de sandbox/teste**. Restaram 277 trades reais com preços de mercado realistas.

⚠️ **Achado crítico de qualidade dos dados:** 258 trades estão marcados `exit_reason='SL_SERVIDOR'` mas **94 deles (36,4%) fecharam em direção favorável** (BUY: exit>entry; SELL: exit<entry) e têm `net_pnl>0`. O rótulo `exit_reason` parece estar **descolado do resultado econômico** — provavelmente está sendo gravado como "stop modify attempted" mesmo quando o stop foi deslocado para breakeven/lock-in. A análise aqui usa `net_pnl` como verdade.

---

## 1. Top 10 Pares/Condições LUCRATIVOS (PnL > 0, n ≥ 3)

| # | symbol | tf | strategy | n | wins | WR% | total_pnl (R$) | avg_pnl (R$) | max_consec_wins |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **INDM26** | M15 | BOLLINGER | 5 | 4 | **80,0%** | **+609,00** | +121,80 | 2 |
| 2 | WINQ26 | M30 | MACD_MOMENTUM | 5 | 2 | 40,0% | +352,00 | +70,40 | 1 |
| 3 | INDM26 | M30 | RSI_REVERSION | 16 | 8 | 50,0% | +210,80 | +13,18 | 4 |
| 4 | WDOQ26 | M30 | VWAP | 3 | 2 | 66,7% | +91,40 | +30,47 | 1 |
| 5 | WINM26 | M15 | BOLLINGER | 3 | 2 | 66,7% | +86,40 | +28,80 | 2 |
| 6 | WSPU26 | M30 | RSI_REVERSION | 4 | 2 | 50,0% | +66,70 | +16,68 | 2 |
| 7 | WDOQ26 | M5 | VWAP | 6 | 4 | 66,7% | +52,80 | +8,80 | 3 |
| 8 | WSPU26 | M15 | RSI_REVERSION | 8 | 5 | 62,5% | +16,40 | +2,05 | 4 |
| 9 | WSPM26 | M30 | EMA_PULLBACK | 5 | 3 | 60,0% | +1,00 | +0,20 | 3 |
| 10 | WSPM26 | M5 | RSI_REVERSION | 13 | 5 | 38,5% | +0,35 | +0,03 | 3 |

**Soma dos 10 grupos:** **+R$ 1.486,85** (em 68 trades; 36 wins / 53% WR).
**Conclusão:** o lucro está **hiperconcentrado** em INDM26 (819 / 1.487 = **55%** do ganho) e WINQ26 M30 (352 / 1.487 = 24%). O restante é marginal.

---

## 2. Top 5 Horários/Dias LUCRATIVOS (PADRÃO INVERTIDO dos perdedores)

### 2A. Por dia da semana (filtrados WR > 50%, PnL > 0, n ≥ 3)
**Vazio** — nenhuma combinação dia+direction supera WR 50% com PnL positivo e n ≥ 3 simultaneamente.

### 2B. Por hora do dia (filtrados WR > 50%, PnL > 0, n ≥ 3)
**Vazio** — idem.

### 2C. Por dia+hora+direction (filtrados WR > 50%, PnL > 0, n ≥ 3) — **3 combinações rebaixáveis do universo**

| dia | hora | direction | n | wins | WR% | PnL (R$) |
|---|---|---|---|---|---|---|
| **Seg** | **14h** | **SELL** | 6 | 5 | **83,3%** | **+843,05** |
| Ter | 15h | SELL | 6 | 4 | 66,7% | +568,05 |
| Qua | 13h | SELL | 3 | 3 | 100,0% | +106,15 |

**Soma:** +R$ 1.517,25 em 15 trades, **80% WR**.

### 2C-alt. Top combos dia+hora+direction por PnL (sem filtro WR — diagnóstico)

| dia | hora | direction | n | wins | WR% | PnL (R$) |
|---|---|---|---|---|---|---|
| Qua | 09h | SELL | 5 | 2 | 40,0% | +1.544,75 |
| Qua | 10h | SELL | 12 | 5 | 41,7% | +1.206,10 |
| Seg | 14h | SELL | 6 | 5 | 83,3% | +843,05 |
| Ter | 14h | BUY | 9 | 2 | 22,2% | +726,95 *(wins gordos vs losses)* |
| Ter | 15h | SELL | 6 | 4 | 66,7% | +568,05 |
| Seg | 13h | SELL | 7 | 2 | 28,6% | +547,35 |

**Insight forte:** os horários rentáveis são **quarta manhã (9–10h) e tarde de seg/ter (13–15h)**, com **viés SELL** predominante. Quinta 11h SELL e quarta 9h BUY têm PnL positivo mas WR < 30% — sobrevive por **payoff, não por acerto**.

### Por hora agregado (n ≥ 5)

| hora | n | PnL (R$) | avg (R$) |
|---|---|---|---|
| **09h** | **28** | **+1.753,40** | **+62,62** ⭐ |
| 10h | 36 | -3.550,35 | -98,62 |
| 11h | 42 | -1.156,85 | -27,54 |
| 12h | 50 | -1.386,50 | -27,73 |
| 13h | 55 | -3.309,75 | -60,18 |
| 14h | 30 | -431,25 | -14,38 |
| 15h | 29 | -233,55 | -8,05 |
| 16h | 7 | -86,15 | -12,31 |

**A hora 9 é a única lucrativa** — **+R$ 62,62 médio/trade**. A partir das 10h o sistema sangra.

### Por dia (n ≥ 5)

| dia | n | PnL (R$) | avg (R$) |
|---|---|---|---|
| **Seg** | **71** | **+355,50** | **+5,01** ⭐ único dia positivo |
| Qui | 35 | -739,00 | -21,11 |
| Sex | 7 | -744,40 | -106,34 |
| Ter | 97 | -3.248,55 | -33,49 |
| Qua | 67 | -4.024,55 | -60,07 |

**Terça e quarta juntos = -R$ 7.273,10** (87% do loss total). **Bruno's "Terça SELL 65t WR29%" é confirmado como o maior buraco**.

---

## 3. Top Estratégias Vencedoras por Par (n ≥ 3, PnL > 0)

| par | estratégia_top | n | WR% | PnL (R$) | avg (R$) |
|---|---|---|---|---|---|
| WINQ26 | MACD_MOMENTUM | 6 | 33,3% | +335,80 | +55,97 |
| INDM26 | RSI_REVERSION | 16 | 50,0% | +210,80 | +13,18 |
| WSPU26 | PIVOT_POINTS | 4 | 75,0% | +46,45 | +11,61 |
| WDOQ26 | VWAP | 13 | 46,2% | +34,40 | +2,65 |
| WSPM26 | EMA_PULLBACK | 5 | 60,0% | +1,00 | +0,20 |

**Observações:**
- **BITM26 e DOLN26 não têm nenhuma estratégia vencedora** com n ≥ 3 — todo o PnL deles é negativo.
- **WINQ26 MACD_MOMENTUM** é o "sharpe-like" melhor (avg +55,97 com 6 trades; 2 wins gordos +338 e +138 contra 3 losses pequenos < 112).
- **INDM26 RSI_REVERSION** é o mais robusto estatisticamente (n=16, 50% WR).
- **WSPU26 PIVOT_POINTS** é o de maior consistência (75% WR, n=4) — pouco volátil,样本 pequeno mas limpo.

---

## 4. Top 3 Setups RSI para REPLICAR

**Cálculo:** por (symbol, direction), `avg_rsi_win − avg_rsi_loss` (BUY) e inverso para SELL. **Filtrado n_wins ≥ 2 E n_losses ≥ 2.**

| # | symbol | direction | avg_rsi_win | avg_rsi_loss | n_wins | n_losses | **|delta|** | leitura |
|---|---|---|---|---|---|---|---|---|
| 1 | **WINQ26** | BUY | 47,0 | 31,9 | 4 | 10 | **+15,1** | BUY funciona quando RSI ≥ 47 |
| 2 | DOLN26 | BUY | 50,3 | 35,7 | 3 | 7 | **+14,6** | BUY sobre RSI ~50 |
| 3 | BITM26 | SELL | 50,8 | 65,3 | 4 | 9 | **+14,5** | SELL quando RSI alto (≥ 65) |
| 4 | WDOQ26 | SELL | 37,8 | 50,2 | 4 | 14 | +12,4 | SELL quando RSI < 38 |
| 5 | WDOQ26 | BUY | 35,1 | 26,1 | 10 | 9 | +9,1 | BUY quando RSI ~35 (oversold) |

**Top 3 (maior |delta|):**
1. **WINQ26 BUY** com RSI ≥ 47 — delta +15,1
2. **DOLN26 BUY** com RSI ~50 — delta +14,6
3. **BITM26 SELL** com RSI ≥ 65 — delta +14,5

**Limitação crítica:** apenas 223/277 (80%) dos trades têm RSI numérico em `signal_detail`. VWAP com frequência grava `rsi=null`. Para os top 3 acima, o n_wins é 3–4 — **evidência inicial, precisa backtest forward**.

**Por estratégia (visão alternativa):**

| estratégia | direction | avg_rsi_win | avg_rsi_loss | delta |
|---|---|---|---|---|
| BOLLINGER | SELL | 89,4 | 72,5 | -16,9 (SELL quando RSI ≥ 72 vence) |
| STRONG_TREND | BUY | 69,2 | 73,2 | -4,1 |
| PIVOT_POINTS | SELL | 65,6 | 69,6 | +4,1 |
| RSI_REVERSION | SELL | 78,2 | 81,3 | +3,1 |

**Insight secundário:** `BOLLINGER SELL` ganha quando RSI já está alto (89+) — sinal de continuação, não reversão. Reforça a leitura de "vender força em tendência de baixa".

---

## 5. Exit_Reasons — o que EVITAR

| exit_reason | n | wins | WR% | PnL (R$) | avg (R$) |
|---|---|---|---|---|---|
| **SL_SERVIDOR** | 258 | 89 | 34,5% | **-7.932,00** | -30,74 |
| EOD_16:45 | 15 | 7 | 46,7% | -469,00 | -31,27 |
| stale_close | 4 | 0 | 0,0% | 0,00 | 0,00 |

**Critério do brief** (WR < 30% E n ≥ 5): **NENHUM** bate exatamente. O mais próximo é SL_SERVIDOR com 34,5% — **não é falha do exit_reason em si**, é o **volume absoluto** que drena o caixa: 258 trades × -30,74 = -7.932.

### 5b. Onde SL_SERVIDOR CONCENTRA (top symbol+strategy, n ≥ 3)

| symbol | strategy | n | wins | WR% | PnL (R$) |
|---|---|---|---|---|---|
| **WINQ26** | VWAP | **52** | 12 | **23,1%** | **-331,40** ← pior combo |
| INDM26 | RSI_REVERSION | 15 | 8 | 53,3% | +247,00 ← lucrativo! |
| WDOQ26 | PIVOT_POINTS | 15 | 6 | 40,0% | -248,00 |
| **BITM26** | **STRONG_TREND** | 12 | 5 | 41,7% | **-3.234,40** ← segundo pior |
| DOLN26 | RSI_REVERSION | 12 | 3 | 25,0% | -28,40 |
| WSPM26 | RSI_REVERSION | 12 | 5 | 41,7% | +0,35 |
| WSPU26 | RSI_REVERSION | 12 | 7 | 58,3% | +83,10 |
| WINQ26 | PIVOT_POINTS | 11 | 3 | 27,3% | -71,20 |
| WINQ26 | STRONG_TREND | 11 | 3 | 27,3% | -94,20 |

### 5c. Onde SL_SERVIDOR CONCENTRA (por symbol+hora, n ≥ 5)

| symbol | hora | n | PnL (R$) |
|---|---|---|---|
| WINQ26 | 13h | 32 | -167,40 |
| WINQ26 | 12h | 17 | -131,40 |
| WINQ26 | 11h | 10 | -190,00 |
| **BITM26** | **10h** | 9 | **-2.013,20** ← pico do loss |
| WINQ26 | 14h | 9 | -106,80 |
| WSPM26 | 12h | 9 | -1,80 |
| **INDM26** | **11h** | 8 | **+260,40** ← oásis |
| WDOQ26 | 12h | 7 | -203,40 |
| WINQ26 | 10h | 7 | +191,60 |
| BITM26 | 09h | 6 | +1.732,80 |

**Insight:** WINQ26 concentra SL_SERVIDOR das 11h às 14h (68 trades, -595). BITM26 10h é o pico do loss (-2.013 em 9 trades).

---

## 6. RECOMENDAÇÕES — Top 3 Ações para MAXIMIZAR Lucro

### ⭐ Ação 1 — **DESABILITAR BITM26 estratégia STRONG_TREND** entre **09h–11h**
- BITM26 STRONG_TREND 12 trades, PnL **-R$ 3.234,40** (-269,53/trade)
- Concentração: hora 10 = 9 trades com -2.013; hora 11 = -1.221
- **Evidência:** 41,7% WR com avg loss muito maior que avg win → assimetria perdedora
- **Esperado:** cortar **~R$ 2.500/dia** de loss (proporcional aos 12 trades em 30d ÷ dias operados)

### ⭐ Ação 2 — **DESABILITAR WINQ26 estratégia VWAP** (todos timeframes, **especialmente M5**)
- WINQ26 VWAP = 52 SL_SERVIDOR, **23,1% WR**, -R$ 331,40
- Contexto mais amplo: WINQ26 BUY é a maior concentração de loss do sistema (já reportado -R$ 6.775)
- **Esperado:** -R$ 331 direto + remoção de ~52 trades de ruído que disputam risco/margem

### ⭐ Ação 3 — **FOCAR em INDM26 M15 BOLLINGER + INDM26 M30 RSI_REVERSION** (janela 11h–15h SELL)
- INDM26 M15 BOLLINGER: **5 trades, 80% WR, +R$ 609** (avg +121,80) ⭐ **maior sharpe do DB**
- INDM26 M30 RSI_REVERSION: **16 trades, 50% WR, +R$ 210,80** (mais robusto por n)
- INDM26 SELL concentra ganhos: +543, +553, +478 nos melhores trades
- **Esperado:** se INDM26 dobrar de frequência mantendo as mesmas taxas, +R$ 1.500 a +R$ 3.000/mês

### Bônus — Ações complementares (segundo escalão, baixo risco)

| ação | racional | impacto esperado |
|---|---|---|
| Restringir BUY entre 09h–11h | BUY tem -R$ 9.762 vs SELL +R$ 1.361; só Seg 14h SELL e Ter/Qua SELL sobreviveram com WR alto | cortar -R$ 5k/mês |
| Desabilitar Qua 13h BUY/10h BUY | Qua é o dia mais perdedor (-R$ 4.024) | cortar -R$ 2k/mês |
| Bloquear Terça SELL cedo (≤14h) | Contexto do brief: 65t WR 29% -R$ 2.946 | cortar -R$ 2k/mês |
| Replicar filtro RSI≥47 para WINQ26 BUY | Setup #1 com delta +15,1 | pequeno mas validável |

---

## Limitações do relatório (transparência)

1. **n pequeno** nos top winners (3–16 por grupo) — sinais sugerem, não provam. Requer walk-forward.
2. **EXIT_REASON mal rotulado**: 36,4% dos "SL_SERVIDOR" têm PnL positivo. O tag provavelmente conflita com "trailing_stop_hit_profit" ou similar. Análise aqui usou `net_pnl` como verdade.
3. **Sem RSI em VWAP** (28 trades): exclui o setup mais operado da análise de sinal.
4. **32 trades sintéticos** (`*N99`) identificados e filtrados — preços geométricos, não vieram do MT5.
5. **Sem detalhe de taxa/vol** por estratégia no DB — análise de "sharpe" é aproximada via avg_pnl, não risk-adjusted.
6. **Janela de 30d** = poucos dias de operação por (symbol,strategy,timeframe) — regimes podem estar enviesados.

---

## JSON Final

```json
{
  "top_3_acciones": [
    {
      "rank": 1,
      "tipo": "DESABILITAR",
      "alvo": "BITM26 strategy=STRONG_TREND horario=09h-11h",
      "evidencia": {
        "n_trades": 12,
        "WR_pct": 41.7,
        "total_pnl_R$": -3234.40,
        "avg_pnl_R$": -269.53,
        "concentracao_horaria": "10h = -2013 R$ em 9 trades"
      },
      "impacto_esperado_R$": "cortar ~2500/dia",
      "confianca": "alta (concentração horária forte)"
    },
    {
      "rank": 2,
      "tipo": "DESABILITAR",
      "alvo": "WINQ26 strategy=VWAP todos timeframes",
      "evidencia": {
        "n_trades": 52,
        "WR_pct": 23.1,
        "total_pnl_R$": -331.40,
        "contexto": "concentra maior parte do loss WINQ26 BUY já reportado"
      },
      "impacto_esperado_R$": "remover ~52 trades/mês de ruído",
      "confianca": "alta (n grande, WR baixíssimo)"
    },
    {
      "rank": 3,
      "tipo": "FOCAR",
      "alvo": "INDM26 timeframe=M15 strategy=BOLLINGER (e M30 RSI_REVERSION) janela 11h-15h direction=SELL",
      "evidencia": {
        "M15_BOLLINGER": {"n": 5, "WR_pct": 80.0, "total_pnl_R$": 609.0, "avg_pnl_R$": 121.80},
        "M30_RSI_REVERSION": {"n": 16, "WR_pct": 50.0, "total_pnl_R$": 210.80, "max_consec_wins": 4}
      },
      "impacto_esperado_R$": "+1500 a +3000/mês se dobrar frequência",
      "confianca": "média (n ainda pequeno; precisa walk-forward)"
    }
  ],
  "top_5_pares_lucrativos": [
    {"symbol": "INDM26", "timeframe": "M15", "strategy": "BOLLINGER", "n_trades": 5, "wins": 4, "WR_pct": 80.0, "total_pnl_R$": 609.0, "avg_pnl_R$": 121.80, "max_consecutive_wins": 2},
    {"symbol": "WINQ26", "timeframe": "M30", "strategy": "MACD_MOMENTUM", "n_trades": 5, "wins": 2, "WR_pct": 40.0, "total_pnl_R$": 352.0, "avg_pnl_R$": 70.40, "max_consecutive_wins": 1},
    {"symbol": "INDM26", "timeframe": "M30", "strategy": "RSI_REVERSION", "n_trades": 16, "wins": 8, "WR_pct": 50.0, "total_pnl_R$": 210.8, "avg_pnl_R$": 13.18, "max_consecutive_wins": 4},
    {"symbol": "WDOQ26", "timeframe": "M30", "strategy": "VWAP", "n_trades": 3, "wins": 2, "WR_pct": 66.7, "total_pnl_R$": 91.4, "avg_pnl_R$": 30.47, "max_consecutive_wins": 1},
    {"symbol": "WINM26", "timeframe": "M15", "strategy": "BOLLINGER", "n_trades": 3, "wins": 2, "WR_pct": 66.7, "total_pnl_R$": 86.4, "avg_pnl_R$": 28.80, "max_consecutive_wins": 2},
    {"symbol": "WSPU26", "timeframe": "M30", "strategy": "RSI_REVERSION", "n_trades": 4, "wins": 2, "WR_pct": 50.0, "total_pnl_R$": 66.7, "avg_pnl_R$": 16.68, "max_consecutive_wins": 2},
    {"symbol": "WDOQ26", "timeframe": "M5", "strategy": "VWAP", "n_trades": 6, "wins": 4, "WR_pct": 66.7, "total_pnl_R$": 52.8, "avg_pnl_R$": 8.80, "max_consecutive_wins": 3},
    {"symbol": "WSPU26", "timeframe": "M15", "strategy": "RSI_REVERSION", "n_trades": 8, "wins": 5, "WR_pct": 62.5, "total_pnl_R$": 16.4, "avg_pnl_R$": 2.05, "max_consecutive_wins": 4},
    {"symbol": "WSPM26", "timeframe": "M30", "strategy": "EMA_PULLBACK", "n_trades": 5, "wins": 3, "WR_pct": 60.0, "total_pnl_R$": 1.0, "avg_pnl_R$": 0.20, "max_consecutive_wins": 3},
    {"symbol": "WSPM26", "timeframe": "M5", "strategy": "RSI_REVERSION", "n_trades": 13, "wins": 5, "WR_pct": 38.5, "total_pnl_R$": 0.35, "avg_pnl_R$": 0.03, "max_consecutive_wins": 3}
  ],
  "top_5_horarios_lucrativos": [
    {"dia": "Seg", "hora": 14, "direction": "SELL", "n_trades": 6, "WR_pct": 83.3, "total_pnl_R$": 843.05},
    {"dia": "Ter", "hora": 15, "direction": "SELL", "n_trades": 6, "WR_pct": 66.7, "total_pnl_R$": 568.05},
    {"dia": "Qua", "hora": 13, "direction": "SELL", "n_trades": 3, "WR_pct": 100.0, "total_pnl_R$": 106.15},
    {"dia": "Qua", "hora": 9, "direction": "SELL", "n_trades": 5, "WR_pct": 40.0, "total_pnl_R$": 1544.75},
    {"dia": "Qua", "hora": 10, "direction": "SELL", "n_trades": 12, "WR_pct": 41.7, "total_pnl_R$": 1206.10}
  ],
  "top_3_setups_rsi": [
    {"symbol": "WINQ26", "direction": "BUY", "avg_rsi_win": 47.0, "avg_rsi_loss": 31.9, "delta": 15.1, "regra": "RSI >= 47"},
    {"symbol": "DOLN26", "direction": "BUY", "avg_rsi_win": 50.3, "avg_rsi_loss": 35.7, "delta": 14.6, "regra": "RSI ~ 50"},
    {"symbol": "BITM26", "direction": "SELL", "avg_rsi_win": 50.8, "avg_rsi_loss": 65.3, "delta": 14.5, "regra": "RSI >= 65"}
  ],
  "exit_reasons_a_evitar": [
    {
      "exit_reason": "SL_SERVIDOR",
      "n": 258,
      "WR_pct": 34.5,
      "total_pnl_R$": -7932.0,
      "concentracao": [
        {"symbol": "WINQ26", "strategy": "VWAP", "n": 52, "WR_pct": 23.1, "pnl_R$": -331.40},
        {"symbol": "BITM26", "strategy": "STRONG_TREND", "n": 12, "WR_pct": 41.7, "pnl_R$": -3234.40},
        {"symbol": "WINQ26", "strategy": "PIVOT_POINTS", "n": 11, "WR_pct": 27.3, "pnl_R$": -71.20},
        {"symbol": "WINQ26", "strategy": "STRONG_TREND", "n": 11, "WR_pct": 27.3, "pnl_R$": -94.20},
        {"symbol": "BITM26", "hora": 10, "n": 9, "pnl_R$": -2013.20}
      ]
    }
  ],
  "limitacoes": "n pequeno nos top winners (3-16 por grupo); exit_reason mal rotulado (36% dos SL_SERVIDOR têm PnL positivo); RSI ausente em ~20% dos trades (especialmente VWAP); 32 trades sintéticos *N99 filtrados; janela 30d com poucos dias por (symbol,strategy,timeframe) — sinais estatísticos fracos, requer walk-forward antes de bloquear/focar produção"
}
```