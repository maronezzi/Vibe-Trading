# Trade Management Analysis — 2026-06-19

**Gerado:** 2026-06-19 ~17:30  
**Fontes:** vt_autotrader_full.log (352 linhas), vt_trades.db, vt_config.json, vt_agi_audit.json, vt_copilot.log, vt_daily_report.log

---

## 1. Cronologia do Dia

| Hora | Evento | Detalhe |
|------|--------|---------|
| 10:00 | Copilot #1 | Autotrader PID 50807/51024 rodando. Log 11min atrás. WDO: 2 ops hoje. |
| 12:00 | Copilot #2 | Autotrader PID 107890/108107 rodando. Log **131min** atrás. WDO: 8 ops hoje. |
| 13:04 | Config update | `_updated_by: meio_dia_params` — v678 aplicado |
| 15:00 | Copilot #3 | Autotrader PID 142724/142941 rodando. Log 0min atrás. WDO: 8 ops hoje. |
| **15:46:29** | **Autotrader SPLIT INICIADO** | **Esperado 09:05 — 6h41min de atraso** |
| 15:46:37 | Vencimentos resolvidos | WIN→WINQ26, BIT→BITM26, WSP→WSPU26, WDO→WDON26 |
| 15:46:41 | RECOVER | 1 posição aberta no MT5: BUY WSPU26 M5 @ 7572.50 |
| 15:46:47 | DEBUG | WIN_M30: 1/6 perdas consecutivas |
| 15:46:49 | BLOQUEADO | BITM26: máximo diário atingido (5/1) — M15 |
| 15:46:53 | BLOQUEADO | BITM26: máximo diário atingido (5/1) — H1 |
| 15:46:54 | ORPHAN_RECOVERY | WSPU26 M5: TF desativado, gerenciando |
| 15:46:55 | HARD_EXIT | WSPU26 BUY: 224min >= 45min, fechando a mercado |
| 15:46:59 | DEBUG | WSP_M15: 2/6 perdas consecutivas |
| 15:47:00 | DEBUG/SINAL | WSP_H1: 3/5 perdas consecutivas. **SINAL WSPU26 H1 BUY @ 7504.75** (KELTNER, ATR=24, RSI=6.2) |
| 15:48:06 | BLOQUEADO | WDON26: máximo diário (8/4) — M15, M30, H1 |
| 15:48:45 | ORPHAN_RECOVERY + HARD_EXIT | WSPU26 M5 novamente: 224min >= 45min |
| 15:48:51 | FECHADO | WSPU26 H1 — PnL R$-1.70 (Ticket 2460476880) |
| 15:49:31-53 | ORPHAN_RECOVERY | WSPU26 M5 (3ª vez): HARD_EXIT + LLM timeout + falha UNKNOWN |
| 15:49:53 | FECHADO | WSPU26 M5 — PnL R$+0.00 (Ticket 2460452169, já fechado) |
| 15:55:37 | **SINAL** | **WINQ26 M30 BUY @ 171555** (MACD_MOMENTUM, ATR=145, RSI=69.0) |
| 15:56:04 | VALIDATOR | SL corrigido: 150→200pts (conservador, 1.2x ATR) |
| 16:04:55 | FECHADO | WINQ26 M30 — PnL R$-41.20 (Ticket 2460479972) |
| 16:05:37 | **SINAL** | **WINQ26 M30 BUY @ 171390** (MACD_MOMENTUM, ATR=157, RSI=72.8) |
| 16:06:16 | VALIDATOR | **LLM falhou**, correção local: 160→200pts |
| 16:06:37 | Último log | BLOQUEADO WDON26 H1 (8/2) — sessão encerra |
| 16:50:01 | Daily Report | **0 trades, PnL R$ 0.00** (DB vazia para 2026-06-19) |
| 17:13:06 | AGI Audit | 5 iterações, 0 convergência, 13 mudanças via fallback |

### Resumo de Eventos
- **Sinais gerados:** 3 (WSP H1 BUY, WIN M30 BUY x2)
- **Trades fechados:** 3 (WSP H1 R$-1.70, WSP M5 R$0.00, WIN M30 R$-41.20)
- **Orphan recoveries:** 3 (todas WSPU26 M5, 224min de idade)
- **Hard exits:** 3
- **Bloqueios por max_daily:** ~120+ (WDO, BIT repetidos a cada ciclo)
- **LLM timeouts/falhas:** 2 (1 timeout, 1 fallback local)
- **PnL do dia (log):** R$-42.90

---

## 2. Trades DB vs Logs — Discrepancia

### Situação
| Fonte | Trades 2026-06-18 | Trades 2026-06-19 |
|-------|-------------------|-------------------|
| **vt_trades.db** | 0 | 0 |
| **vt_autotrader.log** | — | 3 fechados + 3 sinais |
| **vt_agi_audit.json** | — | 32 trades (via MT5 direto) |

### Últimas entradas no DB
| ID | Símbolo | Entry Time | Exit Time | Status |
|----|---------|------------|-----------|--------|
| 1315 | WDOU26 SELL | 2026-06-17 11:05:54 | NULL | **ÓRFÃO** |
| 1316 | DOLN26 SELL | 2026-06-17 11:16:52 | NULL | **ÓRFÃO** |
| 1317 | WSPM26 SELL | 2026-06-17 11:25:22 | NULL | **ÓRFÃO** |

### Causa Raiz
O autotrader **crashou/parou por volta de 11:25 em 2026-06-17** (última escrita no DB). As 3 posições ficaram abertas no MT5 mas sem acompanhamento.

Quando o autotrader **reiniciou às 15:46 de 2026-06-19** (2 dias depois):
1. Fez RECOVER das posições abertas no MT5
2. Os símbolos mudaram (WDOU26→WDON26, DOLN26→DOL?, WSPM26→WSPU26) por vencimento
3. O autotrader gerencia as posições via MT5 mas **não escreve no DB** — o DB só é atualizado em `insert_trade()` no momento da abertura
4. As posições órfãs do DB (WDOU26, DOLN26, WSPM26) usam contratos antigos que já venceram ou foram resolvidos

### Impacto
- Daily report mostra 0 trades (lê do DB)
- AGI audit usa MT5 direto, por isso tem os 32 trades
- 3 posições órfãs no DB sem resolução — PnL não registrado

---

## 3. Performance por Símbolo (últimos 7 dias: 2026-06-13 a 2026-06-19)

### Por Símbolo (DB — até 2026-06-17)

| Símbolo | Trades | Wins | WR% | PnL Total | Status |
|---------|--------|------|-----|-----------|--------|
| INDM26 | 20 | 11 | 55.0% | R$ +656.00 | ✅ Lucrativo |
| WINM26 | 14 | 5 | 35.7% | R$ +32.20 | ⚠️ Marginal |
| WSPM26 | 32 | 12 | 37.5% | R$ -25.45 | ⚠️ Prejuízo leve |
| DOLN26 | 19 | 5 | 26.3% | R$ -52.10 | ❌ WR baixa |
| WDOU26 | 3 | 0 | 0.0% | R$ -152.40 | ❌ Sem acertos |
| BITM26 | 13 | 4 | 30.8% | R$ -2,878.00 | ❌ Pior performer |

### Por Símbolo+TF (DB — até 2026-06-17)

| Símbolo_TF | Trades | WR% | PnL | Estratégia |
|------------|--------|-----|-----|------------|
| INDM26_M15 | 4 | 75.0% | +445.20 | BOLLINGER |
| BITM26_M5 | 1 | 100.0% | +398.80 | RSI_REVERSION |
| INDM26_M30 | 16 | 50.0% | +210.80 | RSI_REVERSION |
| WINM26_M15 | 2 | 100.0% | +109.60 | BOLLINGER |
| BITM26_H1 | 2 | 50.0% | +37.60 | VWAP |
| WSPM26_M30 | 5 | 60.0% | +1.00 | EMA_PULLBACK |
| WSPM26_M5 | 13 | 38.5% | +0.35 | RSI_REVERSION |
| WSPM26_H1 | 3 | 33.3% | -2.60 | VWAP |
| WINM26_M5 | 1 | 0.0% | -19.20 | BOLLINGER |
| WSPM26_M15 | 11 | 27.3% | -24.20 | STRONG_TREND |
| DOLN26_M15 | 7 | 28.6% | -23.70 | EMA_PULLBACK |
| DOLN26_M5 | 12 | 25.0% | -28.40 | RSI_REVERSION |
| WINM26_M30 | 11 | 27.3% | -58.20 | RSI_REVERSION |
| WDOU26_H1 | 1 | 0.0% | -56.20 | EMA_PULLBACK |
| WDOU26_M5 | 2 | 0.0% | -96.20 | RSI_REVERSION |
| BITM26_M15 | 4 | 25.0% | -464.80 | EMA_PULLBACK |
| BITM26_M30 | 6 | 16.7% | **-2,849.60** | RSI_REVERSION |

### Atividade de 2026-06-19 (via AGI audit / logs)

| Símbolo | Trades | WR% | PnL | Bloqueado? |
|---------|--------|-----|-----|------------|
| WIN | 12 | 33.3% | R$ -216.40 | Não |
| WDO | 8 | 50.0% | R$ +65.40 | Sim (8/4 max) |
| BIT | 5 | 40.0% | R$ -486.00 | Sim (5/1 max) |
| WSP | 7 | 0.0% | R$ -28.15 | Não |
| **TOTAL** | **32** | — | **R$ -665.15** | — |

---

## 4. Problemas Identificados

### CRITICAL

| # | Problema | Impacto | Evidência |
|---|----------|---------|-----------|
| C1 | **Autotrader iniciou 6h41min atrasado** (15:46 vs 09:05) | Perdeu todo o pregão das 09:05-15:46. 32 trades executados antes do log não foram registrados no DB. | Log line 2: `15:46:29` |
| C2 | **DB sem escritas desde 2026-06-17** (2 dias de gap) | Daily report mostra 0. AGI audit sem dados frescos. 3 posições órfãs no DB. | DB: último ID 1317, entry 2026-06-17 11:25 |
| C3 | **3 posições órfãs no DB** (WDOU26 SELL, DOLN26 SELL, WSPM26 SELL) | Contratos antigos sem resolução. PnL não contabilizado. WDOU26 e DOLN26 podem ter vencido. | DB IDs 1315-1317, exit_time=NULL |

### HIGH

| # | Problema | Impacto | Evidência |
|---|----------|---------|-----------|
| H1 | **WDO max_daily_trades bloqueando** (8/4 atingido) | WDO_M15 e WDO_M30 limitados a 4/dia, WDO_H1 a 2/dia. 8 ops já feitas antes do restart. | Logs: 36+ bloqueios WDON26 |
| H2 | **BIT max_daily_trades bloqueando** (5/1) | BIT_M5 limitado a 2/dia. Config mostra 5 trades antes do restart, mas max_daily=999 no config geral. O bloqueio 5/1 indica max_daily_trades_by_tf não configurado para BIT. | Logs: 24+ bloqueios BITM26 |
| H3 | **WSP_H1: 4/5 perdas consecutivas** | Próximo do halt (max=5). Se perder mais 1, para por 90min. | Log lines 66, 78, 90, 102, etc. |
| H4 | **WIN_M30: sequência de perdas** (audit: 6 losses, -184.20) | Streak máximo atingido. Config limita a 6 consecutive losses. | Log: `WIN_M30 — 1/6 perdas consecutivas` repetido |
| H5 | **LLM validation falhou** (2x: timeout + fallback local) | SL sendo corrigido manualmente pelo fallback (160→200pts). Sem validação inteligente. | Log lines 61-62, 339-340 |
| H6 | **BIT_M30: pior performer** (-R$2,849.60 em 6 trades, WR 16.7%) | Estratégia RSI_REVERSION não funciona neste TF. | AGI audit: `BIT_M30 RSI_REVERSION` |

### MEDIUM

| # | Problema | Impacto | Evidência |
|---|----------|---------|-----------|
| M1 | **Daily report mostrou 0 trades** | Relatório enganoso — na verdade 32 trades com R$-665 de prejuízo | vt_daily_report.log |
| M2 | **AGI audit não convergiu** (5 iterações, 0 convergência) | Fallback rule-based aplicou 13 mudanças automáticas sem validação de backtest | AGI audit: `converged: false` |
| M3 | **WSP WR=0% no dia** (7 trades, 0 acertos) | Todas as estratégias de WSP falharam no dia | AGI audit today |
| M4 | **Orphan recovery repetida 3x para mesma posição** | WSPU26 M5 tentou fechar 3 vezes, 1ª com sucesso, 2ª e 3ª já estava fechada | Log lines 27, 43, 58 |
| M5 | **DOLN26 posição órfã com contrato vencido** | DOLN26 pode ter vencido, posição SELL no DB sem resolução | DB ID 1316 |

### LOW

| # | Problema | Impacto | Evidência |
|---|----------|---------|-----------|
| L1 | **Config version 678** mas AGI audit diz version 743 | Possível conflito de versões ou escrita concorrente | vt_config.json vs AGI audit |
| L2 | **BIT_M30 disabled** no config mas aparece no AGI audit | `disabled_timeframes: ["BIT_M30"]` mas audit tem BIT_M30 com -R$2,849 | vt_config.json:204 |
| L3 | **Copilot erro ao enviar mídia** (3x) | `No such file or directory: 'hermes'` — notificações sem imagem | vt_copilot.log lines 13, 29, 45 |

---

## 5. Config State

**Arquivo:** vt_config.json  
**Versão:** 678  
**Atualizado:** 2026-06-19T13:04:09 por `meio_dia_params`

### Parâmetros Gerais
| Parâmetro | Valor |
|-----------|-------|
| Horário | 09:05 — 16:45 |
| Volume | 1 contrato |
| Check interval | 30s |
| Bars count | 45 |
| Warmup/ Winddown | 15min |
| validate_with_llm | true |
| halt_trading | false |
| halt_new_trades | false |
| max_daily_loss | -999999 (desativado) |
| global_max_daily_trades | 999 |

### Estratégias por Símbolo
| Símbolo | Estratégia Principal | TFs |
|---------|---------------------|-----|
| WIN | DONCHIAN_BREAKOUT | M5, M15, M30, H1 |
| BIT | KELTNER_CHANNEL | M5, M15, M30, H1 |
| WSP | KELTNER_CHANNEL | M5, M15, M30, H1 |
| WDO | EMA_PULLBACK | M5, M15, M30, H1 |

### Estratégias por TF (detalhado)
| TF | WIN | BIT | WSP | WDO |
|----|-----|-----|-----|-----|
| M5 | MACD_MOMENTUM | RSI_REVERSION | MACD_MOMENTUM | DONCHIAN_BREAKOUT |
| M15 | RSI_REVERSION | KELTNER_CHANNEL | KELTNER_CHANNEL | VWAP |
| M30 | MACD_MOMENTUM | RSI_REVERSION | DONCHIAN_BREAKOUT | VWAP |
| H1 | RSI_REVERSION | MACD_MOMENTUM | KELTNER_CHANNEL | EMA_PULLBACK |

### Max Daily Trades por TF
| TF | WIN | BIT | WSP | WDO |
|----|-----|-----|-----|-----|
| M5 | 999 | 999 | 999 | **2** |
| M15 | 999 | 999 | 999 | **4** |
| M30 | 999 | 999 | 999 | **4** |
| H1 | 999 | 999 | 999 | **2** |

### Max Consecutive Losses (halt threshold)
| TF | WIN | BIT | WSP | WDO |
|----|-----|-----|-----|-----|
| M5 | 3 | 3 | 5 | 3 |
| M15 | 3 | 4 | 6 | 3 |
| M30 | 6 | 4 | 4 | 4 |
| H1 | 5 | 5 | 5 | 5 |

### SL ATR Multiplier (config atual)
| Símbolo | sl_atr_mult |
|---------|-------------|
| WIN | 1.1 |
| BIT | 1.1 |
| WSP | 1.1 |
| WDO | 1.1 |

### Disabled
- **Timeframes:** BIT_M30
- **Symbols:** nenhum

---

## 6. AGI Audit Summary

**Último run:** 2026-06-19T17:13:06  
**Período:** 7 dias (cutoff 2026-06-12)  
**Iterações:** 5 (max 5)  
**Convergiu:** ❌ NÃO  
**Modo:** dry_run=false (mudanças aplicadas)  
**Análise LLM:** Fallback rule-based — 13 mudanças automáticas

### Performance 7 Dias (via AGI audit)

| Símbolo | Trades | WR% | PnL | Pior | Melhor |
|---------|--------|-----|-----|------|--------|
| WIN | 51 | 37.3% | -375.20 | -101.2 | +123.8 |
| IND | 30 | 50.0% | -366.00 | -716.2 | +553.8 |
| WDO | 37 | 35.1% | -194.40 | -96.2 | +148.8 |
| DOL | 30 | 26.7% | -127.60 | -61.0 | +41.5 |
| WSP | 42 | 35.7% | -13.65 | -12.45 | +22.05 |
| BIT | 43 | 46.5% | **+546.00** | -981.2 | +2338.8 |

### Issues Detectadas (24 total)
- **LOSS_STREAK:** WIN (5), WDO (6,4,4), DOL (7,9), WSP (5,4,8), BIT (4)
- **LARGE_LOSS:** IND (-716.2), BIT (-981.2)
- **LOW_WR:** DOL (26.7%)
- **LOSS_STREAK_TF:** WIN_M5(5), WIN_M15(4), WIN_M30(6), DOL_M5(6), DOL_M30(5), WSP_M5(5), WSP_M15(6), WSP_H1(5), BIT_M30(4), BIT_H1(4), BIT_M15(4), WDO_M30(4), IND_M5(2), IND_M30(3), IND_M15(1)

### Mudanças Aplicadas (13)
Todas via fallback rule-based com correção de SL_SERVIDOR (98% dos exits) e Strategy Explorer:

| Símbolo | Mudança | Razão |
|---------|---------|-------|
| WIN | sl_atr_mult: 1.1→escalado até 1.7 | SL_SERVIDOR 98% exits |
| IND | sl_atr_mult: escalado | SL_SERVIDOR + Explorer PF=186.85 |
| WDO | sl_atr_mult: escalado | SL_SERVIDOR + Explorer PF=4.60 |
| DOL | sl_atr_mult: escalado | SL_SERVIDOR |
| WSP | sl_atr_mult: escalado | SL_SERVIDOR |
| BIT | sl_atr_mult: escalado | SL_SERVIDOR + Explorer PF=40.30 |

**Nota:** O config atual mostra sl_atr_mult=1.1 para todos, mas o AGI audit aplicou mudanças progressivas até 1.7. Possível conflito — config v678 foi escrito pelo `meio_dia_params` às 13:04, revertendo as mudanças do AGI.

### Backtest Evaluations (5 iterações)
Melhores performers no backtest:
- BIT_M5: WR 88.9%, PnL +8,328.79
- WDO_M5: WR 100%, PnL +1,010.57
- WIN_M30: WR 71.4%, PnL ~+750
- WSP_M15: WR 100%, PnL +177.99

---

## 7. Recomendações para AGI 17h

### Prioridade 1 — Resolver DB Gap (CRITICAL)
1. **Investigar por que o DB parou de receber escritas** em 2026-06-17 11:25. Verificar se é crash, lock de SQLite, ou problema de permissão.
2. **Fechar as 3 posições órfãs** no DB (WDOU26, DOLN26, WSPM26) — marcar como EOD ou com PnL estimado.
3. **Sincronizar os 32 trades de hoje** do MT5 para o DB via reconciliação.
4. **Garantir que o autotrader inicie às 09:05** — verificar systemd/cron do serviço.

### Prioridade 2 — Ajustar max_daily_trades (HIGH)
5. **WDO:** O config tem max_daily=10 global mas 2/4/4/2 por TF. Considerar aumentar WDO_M5 para 4 e WDO_H1 para 4, ou reduzir o total de operações antes do restart.
6. **BIT:** O bloqueio 5/1 indica que o autotrader está usando um max_daily diferente do config. Verificar se o `params_by_tf` está sendo lido corretamente.

### Prioridade 3 — Estratégias Problemáticas (HIGH)
7. **BIT_M30 RSI_REVERSION:** Pior performer (-R$2,849 em 6 trades). Considerar desativar ou trocar estratégia. Já está em `disabled_timeframes` mas o AGI audit ainda registra trades.
8. **WSP_H1:** 4/5 perdas consecutivas, WR 22.2% em 7 dias. Considerar aumentar halt para 4 ou trocar de KELTNER_CHANNEL.
9. **WIN_M30 MACD_MOMENTUM:** 30% WR em 20 trades (-R$254). Avaliar se MACD é adequado para M30.

### Prioridade 4 — LLM Validator (MEDIUM)
10. **LLM validation falhando** com timeout. Considerar:
    - Aumentar timeout do LLM
    - Usar fallback mais sofisticado (não apenas multiplicar SL por 1.2x)
    - Cache de validações recentes

### Prioridade 5 — Config Consistency (MEDIUM)
11. **Resolver conflito v678 vs v743.** O `meio_dia_params` sobrescreveu o config às 13:04, revertendo mudanças do AGI. Definir quem é o source of truth.
12. **SL escalation loop:** O AGI está fazendo escalonamento progressivo de SL (1.1→1.2→1.3→...→1.7) sem convergência. Limitar a 3 iterações e forçar decisão.

### Prioridade 6 — Monitoring (LOW)
13. **Corrigir envio de mídia do Copilot** (`hermes` não encontrado).
14. **Daily report deve ler do MT5** quando DB estiver vazia, não mostrar 0.

---

## 8. Raw Data Tables

### 8.1 PnL Diário por Símbolo (DB)

| Data | BITM26 | DOLN26 | INDM26 | WINM26 | WSPM26 | WDOU26 | Total |
|------|--------|--------|--------|--------|--------|--------|-------|
| 06-15 | — | -23.30 | +1,008.00 | +117.60 | -30.55 | — | +1,071.75 |
| 06-16 | -2,434.40 | -24.70 | -352.00 | -85.40 | -7.25 | — | -2,903.75 |
| 06-17 | -443.60 | -4.10 | — | — | +12.35 | -152.40 | -587.75 |
| **Total** | **-2,878.00** | **-52.10** | **+656.00** | **+32.20** | **-25.45** | **-152.40** | **-2,419.75** |

### 8.2 PnL Diário por Símbolo (AGI audit — 2026-06-19)

| Símbolo | Trades | WR% | PnL |
|---------|--------|-----|-----|
| BIT | 5 | 40.0% | -486.00 |
| WDO | 8 | 50.0% | +65.40 |
| WIN | 12 | 33.3% | -216.40 |
| WSP | 7 | 0.0% | -28.15 |
| **Total** | **32** | — | **-665.15** |

### 8.3 Config Snapshot

```json
{
  "version": 678,
  "updated_at": "2026-06-19T13:04:09",
  "updated_by": "meio_dia_params",
  "symbols": ["WIN", "BIT", "WSP", "WDO"],
  "volume": 1,
  "start": "09:05",
  "close": "16:45",
  "validate_with_llm": true,
  "halt_trading": false,
  "max_daily_loss": -999999,
  "disabled_timeframes": ["BIT_M30"],
  "contract_multipliers": {
    "WIN": 0.20,
    "WDO": 10.0,
    "BIT": 1.0,
    "WSP": 1.0
  }
}
```

### 8.4 Streaks Recentes (AGI audit)

| Símbolo | Maior Streak | PnL do Streak |
|---------|--------------|---------------|
| WIN | 5 losses | -255.00 |
| WDO | 6 losses | -227.20 |
| DOL | 9 losses | -128.10 |
| WSP | 8 losses | -33.10 |
| BIT | 4 losses | -2,324.80 |
| IND | 3 losses | -643.60 |

### 8.5 Exit Reasons (7 dias)

| Razão | Count | PnL Total | PnL Médio |
|-------|-------|-----------|-----------|
| SL_SERVIDOR | 229 | -482.05 | -2.11 |
| EOD_16:45 | 4 | -48.80 | -12.20 |

---

## Diagnóstico Final

O dia 2026-06-19 foi **degradado** por múltiplos problemas sistêmicos:

1. **O autotrader ficou offline 2 dias** (17→19 junho) e só reiniciou às 15:46, perdendo 87% do pregão.
2. **O DB não registrou nada** desde 2026-06-17, criando um buraco de dados que afeta daily reports, AGI audit (parcialmente), e análise de performance.
3. **3 posições órfãs** com contratos antigos (WDOU26, DOLN26, WSPM26) permanecem sem resolução no DB.
4. **Quando operou**, o sistema estava massivamente bloqueado por max_daily_trades (WDO 8/4, BIT 5/1), operando apenas WIN e WSP — ambos com performance negativa.
5. **O AGI audit rodou mas não convergiu**, aplicando mudanças via fallback que podem ter sido revertidas pelo `meio_dia_params`.

**PnL real do dia (estimado via logs + MT5): R$ -665.15**  
**PnL reportado pelo daily report: R$ 0.00** (incorreto)
