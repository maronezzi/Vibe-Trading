# LESSON LEARNING MATRIZ — 05/08/2026 · 1º dia em conta REAL (XPMT5-PRD 2257579)

> **Documento matriz** — complementa e audita `docs/lesson_learning_2026-08-05.md` (Hermes).
> Fonte de verdade cruzada: `vt_trades.db` (SQLite, broker-truth) + `/tmp/vt_autotrader.log` + `/tmp/vt_agi_v4_20260805_120001.log` + `/tmp/vt_agi_v4_20260805_171001.log` + `vt_config.json` (v1181→v1187) + código (`core/`, `mt5/`, `optimization/agi_v4/`).
> Cada afirmação abaixo é verificada: **DB =** lido direto do banco; **LOG =** contado no log; **CODE =** linha exata no código.

---

## 0. Resultado do dia (reconciliado, 3 fontes)

| Fonte | Valor | Observação |
|---|---|---|
| **Saldo broker-truth (MT5 balance delta)** | **−R$ 552,88** (R$ 1.500 → R$ 947,12) | número que sangrou o caixa |
| **Soma net_pnl das 16 trades fechadas no DB** | **−R$ 400,00** | `SELECT SUM(net_pnl)` em `trades` 2026-08-05 |
| **Soma gross_pnl** | −R$ 394,00 | bruto sem taxas |
| **Gap broker-truth − DB** | **−R$ 152,88** | **taxas B3/emolumentos/corretagem + fills dos 6 GHOST não capturados** — NÃO é perda de estratégia |
| **Trades no DB** | 22 (16 com PnL, 6 GHOST net=0) | `close_source`: 8 `MT5_SERVER_SL`, 4 `RECONCILE`, 2 `RECONCILE_HISTORY`, 8... |
| **Win rate** | **0% (0/16)** | nenhuma operação ganhadora |

> **Leitura para o AGI:** o "prejuízo" de hoje tem duas camadas que **não podem ser misturadas**:
> (a) **−R$ 400,00** de PnL bruto de estratégia — mas esse número é **inválido como amostra** porque a execução esteve corrompida o dia inteiro (sem TP1, sem trailing, sem breakeven — só SL de entrada). As 16 entradas são legítimas; as 16 saídas são todas "SL original bateu". Ou seja, mediu-se só a qualidade da *entrada* num dia de range, sem qualquer gestão.
> (b) **−R$ 152,88** de custo de transação real (taxas B3 + slippage) — este é o número **estimável e recorrente**: é o custo de operar, independente de estratégia, e deve entrar no modelo de custo do backtest/AGI.

---

## 1. Matriz de operações (22 trades · DB broker-truth)

Ordem cronológica. `sl_real` = preço de SL efetivamente registrado; `sl_pts` = distância em pontos do executor; `fonte` = `close_source` no DB. GHOST = `exit_price=0` (fechamento não capturado, reconciliado depois).

| # | Ticket | Hora | Par | TF | Dir | Estratégia | Entry | SL(precio) | SL_pts | Exit | Net R$ | Fonte | Tipo saída |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 5870047320 | 09:21 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179310 | 178775 | 535 | 179260 | **−9,00** | MT5_SERVER_SL | SL original |
| 2 | 5870119080 | 09:31 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179487,5 | 178957,5 | 530 | 179270 | **−87,00** | RECONCILE_HISTORY | GHOST→broker |
| 3 | 5870121712 | 09:31 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179480 | 179085 | 395 | 0 | 0,00 | RECONCILE | GHOST aberto |
| 4 | 5870122533 | 09:32 | WSPU26 | M15 | SELL | BOLLINGER | 7804,75 | 7804,88 | 1300* | 7804 | **−13,75** | RECONCILE_HISTORY | GHOST→broker |
| 5 | 5870202106 | 09:46 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179530 | 178970 | 560 | 179285 | **−47,00** | MT5_SERVER_SL | SL original |
| 6 | 5870298499 | 10:02 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179255 | 178810 | 445 | 179170 | **−38,00** | MT5_SERVER_SL | SL original |
| 7 | 5870313880 | 10:03 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179635 | 179000 | 635 | 0 | 0,00 | RECONCILE | GHOST aberto |
| 8 | 5870475688 | 10:16 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179550 | 178895 | 655 | 179490 | **−10,00** | MT5_SERVER_SL | SL original |
| 9 | 5870655834 | 10:30 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179550 | 179085 | 465 | 179450 | **−19,00** | MT5_SERVER_SL | SL original |
| 10 | 5871038542 | 11:01 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179665 | 179185 | 480 | 179510 | **−32,00** | MT5_SERVER_SL | SL original |
| 11 | 5871423031 | 11:31 | WINQ26 | M15 | SELL | HTF_BIAS_LTF_ENTRY | 179110 | **800** | 800* | 179405 | **−56,50** | MT5_SERVER_SL | SL inflado |
| 12 | 5871432170 | 11:31 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 179135 | **570** | 570* | 179270 | **−29,50** | MT5_SERVER_SL | SL inflado |
| 13 | 5871464573 | 11:34 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | **0,0** | −580 | 580 | 0 | 0,00 | RECONCILE | GHOST aberto |
| 14 | 5871623377 | 11:48 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179520 | 178720 | 800* | 179165 | **−40,00** | RECONCILE_HISTORY | GHOST→broker |
| 15 | 5871748501 | 12:01 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179200 | 178640 | 560 | 179200 | 0,00 | RECONCILE | GHOST aberto |
| 16 | 5871757040 | 12:01 | WSPU26 | H1 | BUY | EMA_PULLBACK | 7790,75 | 7766,75 | 2400 | 7779,5 | **−1,31** | MT5_SERVER_SL | SL original |
| 17 | 5871900653 | 12:17 | BITQ26 | M15 | SELL | BOLLINGER | 333620 | **335038,57** | **141857** | 334220 | **−7,20** | MT5_SERVER_SL | SL **hibiscus** |
| 18 | 5871993047 | 12:27 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 179155 | 179735 | 580 | 179180 | **−6,20** | MT5_SERVER_SL | SL original |
| 19 | 5872026450 | 12:31 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179185 | 178635 | 550 | 179185 | 0,00 | RECONCILE | GHOST aberto |
| 20 | 5872034269 | 12:32 | WSPU26 | M15 | BUY | BOLLINGER | 7778,75 | 7763,75 | 1500 | 7764,75 | **−1,34** | MT5_SERVER_SL | SL original |
| 21 | 5872055459 | 12:34 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 179150 | 179705 | 555 | 179150 | 0,00 | RECONCILE | GHOST aberto |
| 22 | 5872309533 | 13:01 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 178955 | 179515 | 560 | 178960 | **−2,20** | MT5_SERVER_SL | SL original |

\* SL com valor aparentemente codificado como distância em vez de preço (trades 4, 11, 12, 14) — sintoma da **corrupção do state em memória** (entrada C2/C5); o SL salvo no DB reflete o que o gerenciamento tentou impor, não o preço de SL legítimo da entrada.

### Distribuição
- **Por par:** WINQ26 = 18 (82%), WSPU26 = 2, BITQ26 = 1, (+ GHOST WSP/BUY). **WIN sozinho = −R$ 377,20.**
- **Por estratégia:** HTF_BIAS_LTF_ENTRY = 18 (todas WIN), BOLLINGER = 3 (WSP×2, BIT×1), EMA_PULLBACK = 1 (WSP).
- **Por direção (WIN):** BUY = 11, SELL = 7. Compras no topo do range (entry 179,2-179,6k) caíram; vendas no fundo (entry 178,9-179,1k) subiram.
- **Regime de mercado:** WINQ26 oscilou **179.000–179.800** o dia todo (range apertado, ~0,4%). **HTF_BIAS_LTF_ENTRY é estratégia de momento** — comprou topos e vendeu fundos. É o cenário-tipo que mata momento. **6 dos 16 GHOST/perda vieram em janela de 09:21-10:34, todas BUY no mesmo range.**

---

## 2. O mecanismo do colapso (passo a passo, com código)

Esta é a sequência causal que transforma um sinal legítimo em perda garantida. Cada passo tem a evidência.

```
SINAL (ok)
  │  HTF_BIAS_LTF_ENTRY dispara; _calc_sl (2×ATR) gera SL 395-635pts; ordem enviada.
  │  CODE: core/vt_autotrader.py:2071-2118 (_calc_sl, mult default 1.5×, mas WIN_M15 usa 2.0)
  ▼
~1 min: TP1 TENTA FECHAR 0.5
  │  close_volume = original * tp1_pct = 1.0 * 0.5 = 0.5  → B3 exige volume inteiro
  │  LOG: "Invalid volume" ×98  ·  CODE: core/vt_autotrader.py:2728-2758
  │  → FALHA SILENCIOSA. Nunca houve take-profit parcial. Risco nunca reduziu.
  ▼
MESMO CICLO: TRAILING AVALIA POSIÇÃO
  │  entry_price = pos["entry_price"]  ← veio do STATE-REBUILD (state reconstruído vazio)
  │  entry_price = float(p.price_open or 0.0) = 0.0  (XPMT5-PRD retorna price_open=0)
  │  profit_pts = best - entry_price = 179375 - 0 = 179375 pts  ← PREÇO ABSOLUTO, não lucro
  │  LOG: "Lucro: 179375 pts (670.6x ATR)"  ·  CODE: core/vt_autotrader.py:2687, 2716-2720
  │  → profit_pts >= trail_act * atr (670x >= ~1x) → TRAILING ATIVA IMEDIATAMENTE
  │  → armeia também PROFIT_LOCK, BREAKEVEN, TIME_TRAIL, HARD_EXIT — todos.
  ▼
BREAKEVEN/PROFIT-LOCK CALCULA SL A ~5pts DO PREÇO
  │  → envia MODIFY SL=5pts
  │  → CONTA REAL rejeita: "Invalid stops" (SL dentro do stop_level do broker; demo aceitava)
  │  LOG: "Invalid stops" ×255  ·  CODE: core/vt_autotrader.py:2924-2966, 2978-3005
  ▼
RECOVERY TENTA CORRIGIR (3x)
  │  → consulta LLM via binário "hermes"
  │  → hermes NÃO ESTÁ NO PATH do daemon (cron PATH=/usr/bin:/bin, sem ~/.local/bin)
  │  LOG: "[Errno 2] No such file or directory: 'hermes'" ×53
  │  CODE: mt5/mt5_error_recovery.py:62-84 (_ask_llm chama "hermes" direto, sem find_hermes)
  ▼
FIX PADRÃO (LLM falhou) — INFLA O SL
  │  min_dist = max(stops * point_val, point_val * 50) → fallback duro de 50pts×1.5
  │  CODE: mt5/mt5_error_recovery.py:269-270, 303  →  _fix_invalid_stops:156-170
  │  LOG: "Fix padrão" ×5  (BIT foi pior: validator v2 inflou 50.000 → 141.857pts)
  │  → SL agora é LARGO demais (não protege). Trade #17 BIT: SL 141.857pts = 1.418 nativos
  │    (max_native do BIT é 500; 1.418 = 2,8× o máximo do clamp).
  ▼
PnL VIRA NEGATIVO → EMERGENCY CLOSE
  │  tenta fechar manualmente 3x  ·  alguns falham ("No changes"/aborted, PnL lido = 0 → >= 0 ambigu)
  │  LOG: "EMERGENCY CLOSE" ×20, "No changes" ×9  ·  CODE: core/vt_emergency.py:251-357, 137-148
  ▼
POSIÇÃO SÓ FECHA QUANDO PREÇO BATE NO SL ORIGINAL
  │  → TODAS AS 16 SAÍDAS = PERDA (−2,20 a −87,00)
  │  resultado: operou-se o dia INTEIRO sem TP1, sem trailing, sem breakeven, sem emergency confiável
```

**Linha de carga crítica** que amarra tudo: o state rebuild (`core/vt_autotrader.py:582` e `:3510/:3529`) pode gravar `entry_price = 0.0`, o que faz `profit_pts = best - 0` (`:2718`) explodir, armando todo o bloqueio de trailing/breakeven/profit-lock; e quando esses `safe_modify_sl` batem em "Invalid stops", o fallback LLM (`mt5/mt5_error_recovery.py:69`) falha silenciosamente no ambiente cron-lançado de `scripts/start_autotrader.sh` (que nunca põe `~/.local/bin` no PATH), deixando o `point_val * 50 * 1.5` (`:303`) como a política de SL efetiva.

---

## 3. Causas-raiz (auditoria de código, file:line)

Priorizadas por impacto. Cada causa é **confirmada no código** com a linha exata.

### 🔴 C1 — Stop level da REAL rejeita SLs apertados (a diferença DEMO→REAL do dia)
- **Sintoma:** `Invalid stops` ×255. Nenhum modify de SL funcionou o dia inteiro.
- **Por que só na real:** a DEMO tem `trade_stops_level ≈ 0` (aceita SL a poucos pts do preço); a REAL tem stop level real (rejeita SL dentro da distância mínima do broker).
- **CODE:** o stop level **só é consultado reativamente**, após a rejeição — nunca antes do modify inicial (`modify_sl` é chamado "cego" em `mt5/mt5_error_recovery.py:542`). Leitura: `mt5/mt5_error_recovery.py:161,165,269,279,294,567`; `core/vt_autotrader.py:2946-2954` (PROFIT_LOCK lê `trade_stops_level` com fallback duro de 50pts). `freeze_level` é exposto no info dict (`mt5/mt5_executor.py:537,710`) mas **nunca consumido** por nenhuma decisão.
- **Severidade:** 🔴 CRÍTICA — é o vetor principal.

### 🔴 C2 — `entry_price=0` no state em memória → trailing ativa com lucro falso
- **Sintoma:** "Lucro: 179375 pts (670.6x ATR)" no 1º trade.
- **CODE:** state rebuild grava `entry_price = float(p.price_open or 0.0)` em `core/vt_autotrader.py:582` (caminho Fase 3, usado no startup) e `:3510/:3529` (recover_open_positions legado). O XPMT5-PRD retorna `price_open=0` (documentado em `:2304-2307`). A guarda anti-zero **existe só no caminho de entrada ao vivo** (`:2307-2308`), não nos rebuilds.
- **Efeito:** `profit_pts = best - 0 = preço absoluto` (`:2718`) armeia TP1/TP2/HARD_EXIT/trailing/PROFIT_LOCK/TIME_TRAIL simultaneamente.
- **Severidade:** 🔴 CRÍTICA.

### 🔴 C3 — TP1 fracionário inválido (volume inteiro B3)
- **Sintoma:** `Invalid volume` ×98.
- **CODE:** `close_volume = original * tp1_pct` = 1.0 × 0.5 = 0.5 contrato (`core/vt_autotrader.py:2739`). B3 exige volume inteiro (volume_min=1.0, step=1.0). Não há guarda; falha silenciosa.
- **Efeito:** **nunca houve take-profit parcial.** Nenhuma posição reduziu risco em lucro. O "tp1_profit_pts" em `:2756-2758` divide por `original` com floor 0.001 — corrompido quando original falta.
- **Severidade:** 🔴 CRÍTICA.

### 🟠 C4 — Recovery LLM quebrado (`hermes` fora do PATH do daemon)
- **Sintoma:** `[Errno 2] No such file or directory: 'hermes'` ×53.
- **CODE:** `mt5/mt5_error_recovery.py:69` chama `subprocess.run(["hermes", ...])` (string nua, depende de PATH). `scripts/start_autotrader.sh` **não exporta `~/.local/bin` para PATH** (usa `HERMES_BIN` absoluto só para a msg de startup Telegram, linhas 62-63). O validator v2 usa `find_hermes()` (`core/vt_hermes_helper.py:27`) que é robusto; o módulo de recovery **não usa**.
- **Severidade:** 🟠 ALTA.

### 🟠 C5 — Fix padrão / validator inflam SL (inverte a proteção)
- **Sintoma:** "Fix padrão" ×5; BIT SL inflado 50.000→141.857pts (trade #17).
- **CODE:**
  - Fix padrão recovery: `min_dist = max(stops*point_val, point_val*50)*1.5` (`mt5/mt5_error_recovery.py:303`); catch-all `int(sl_pts*1.5)` (`:156-170`).
  - Validator v2 clamp "pré-envio": `SL_LIMITS["BIT"]["max"]=500000` executor-pts (`core/vt_order_validator_v2.py:34-41`); regex pega sugestão e aplica `max(min, min(max, suggested))` (`:617-618`); aplicado em `core/vt_autotrader.py:2262-2266`. O `max_native` do BIT em `_calc_sl` é 500 (`:2102`); 500×100 = 50.000 exec-pts; clamp max 500.000 deixa passar até 5× o máximo nativo.
- **Severidade:** 🟠 ALTA.

### 🟡 C6 — Emergency close nem sempre fecha
- **Sintoma:** EMERGENCY CLOSE ×20, com 9 "No changes"/aborted.
- **CODE:** `core/vt_emergency.py:251-357`. Não há string literal "No changes" — o equivalente é o return `aborted` em `:308-319` quando `_is_position_against_us` (`:137-148`, retorna `pnl <= 0`) é falso, **ou** `_get_current_pnl` (`:87-134`) retorna `0.0` em qualquer falha de leitura, e `0 <= 0` é True (ambíguo). O caller "no modify performed" gate está em `core/vt_autotrader.py:3085`.
- **Severidade:** 🟡 MÉDIA.

### 🟡 C7 — Exposição concentrada em WINQ26 sem filtro de regime
- **Sintoma:** 18/22 trades em WIN, todos HTF_BIAS_LTF_ENTRY, num dia de range 179.000-179.800.
- **CODE:** não há filtro de regime/volatilidade que desabilite momento em range apertado. `pause_criteria` existe no config (`enabled:false`) mas não atua por regime.
- **Severidade:** 🟡 MÉDIA (agravante; não é causa-raiz técnica).

### 🟢 C8 — AGI reativou WDO_M15 no meio do pregão (v1174→v1175, 12:05)
- **LOG:** AGI 12h `stage5: 🔓 AGI-SOBERANO: reativou ['WDO_M15']` (12:05:16).
- **Severidade:** 🟢 BAIXA (não gerou trade WDO_M15 hoje; monitorar mudança de config durante operação).

---

## 4. Análise estrutural do AGI (12h e 17h de hoje)

### 4.1 O AGI **acertou** ao não otimizar sobre dados corrompidos (parcialmente)
O documento do Hermes recomenda "não otimizar com dados de hoje". O AGI **não viu isso explicitamente**, mas por construção ele simula em **backtest 30d bar-by-bar** (`optimization/agi_v4/backtest_evaluator.py:35` — `BARS_FOR_30D`), não nos trades reais do DB. Portanto as 22 trades de hoje **não entram diretamente** como amostra de otimização. Isso é **bom** — o AGI não foi enganado pela execução corrompida do dia.

⚠️ **Mas há um vício oculto:** o backtest 30d do AGI simula a **mesma execução defeituosa** (o engine `backtest_v944.py` carrega `strategies/` ao vivo, e o gerenciamento simulado tem os mesmos bugs C2/C3 se o simulador replicar o gerenciamento). Se o simulador **não** replica TP1/trailing/breakeven (só SL de entrada), então ele está medindo "entrada + SL fixo" — que é exatamente o que aconteceu na real hoje. **Conclusão: o backtest do AGI é fiel ao que a real fez hoje (só SL de entrada), mas NÃO é fiel ao que a real deveria fazer (com TP1/trailing).** Isso significa que otimizar no backtest atual é otimizar um sistema "aleijado".

### 4.2 BUG ENCONTRADO no stage5: log contraditório `cand R$2479 > base R$2507`
O AGI 17h aplicou **WIN_M5: BOLLINGER → EMA_CROSSOVER** com o log:
```
17:49:08 APLICADO WIN_M5: EMA_CROSSOVER (cand R$2479.09 > base R$2507.54)
```
**2479,09 < 2507,54 — a comparação impressa é falsa.** Investigando o código (`optimization/agi_v4/stage5_apply.py:261-288`): a decisão real usa um **score blended** (`cand_pnl + 0.3 × today_pnl`, função `_blended` em `:270-277`), mas **o log da linha 314 imprime só os PnL brutos**, não o score. Então a aplicação pode ser correta (o blended do candidato superou o do baseline via bônus intradia), mas **o audit trail é opaco/malentido** — o log diz ">" onde deveria mostrar o score.

**Impacto:** não é possível auditar pelo log se uma mudança de estratégia foi justificada. Para um sistema que opera conta real, isso é inaceitável.
**Correção necessária:** o log da linha 314 deve imprimir `cand_score vs base_score` (com decomposição `30d + hoje`), não `cand_pnl vs base_pnl`.

### 4.3 Mudanças aplicadas hoje (v1181 → v1187, AGI 17h)
| Versão | Par | Mudança | Justificativa no log |
|---|---|---|---|
| v1182 | WSP_M15 | → EMA_PULLBACK | `cand R$3497,93 > base R$996,87` ✓ coerente |
| v1183 | **WIN_M5** | **BOLLINGER → EMA_CROSSOVER** | `cand R$2479 < base R$2507` ✗ **log opaco** (blended score) |
| v1184 | WSP_M5 | → EMA_CROSSOVER | `cand R$1285 > base R$893` ✓ |
| v1185 | BIT_M30 | → BOLLINGER | `cand R$327 > base R$232` ✓ |
| v1186 | BIT_M15 | → DIVERGENCE_RSI | `cand R$229,58 = base R$229,58` ✗ **igualdade** (aplicou sem melhora?) |
| v1187 | BIT_M5 | → BOLLINGER | `cand R$105 > base R$9` ✓ |

**Risco:** WIN_M5 operou hoje com BOLLINGER (nenhuma trade WIN_M5 hoje, pois WIN_M5 não gerou sinal). Amanhã, se a demo rodar, WIN_M5 já vem como EMA_CROSSOVER. **A troca foi feita sob backtest que mede só SL-de-entrada** (ver 4.1) — então a "melhoria" pode não se materializar quando TP1/trailing forem consertados.

### 4.4 AGI 12h: mudança no meio do pregão (C8)
Às 12:05 o AGI reativou WDO_M15 (`stage5 AGI-SOBERANO`). Isso mudou config durante operação ativa. Não causou trade WDO_M15 hoje, mas **viola o princípio de não mexer em config com posições abertas**. Hoje já havia 14 trades e posições potencialmente abertas.

### 4.5 Veredito sobre o AGI
O AGI **não estruturou melhor as lógicas de acordo com as operações reais de hoje**, porque **ele não vê as operações reais** — ele só vê backtest 30d. Para que o AGI aprenda com hoje, seria necessário:
1. Que o backtest replique fielmente a execução real (com stop level, TP1 inteiro, trailing correto) — hoje **não replica** (mede só entrada+SL).
2. Que o custo real (R$ 152,88 de taxas, ~R$ 7/trade) entre no modelo — hoje **não entra** (PnL do backtest é bruto).
3. Que o AGI receba um sinal explícito de "dia inválido/não-otimizar" quando a execução esteve corrompida — hoje **não existe** esse sinal.

---

## 5. DEMO vs REAL — o que é diferente e como espelhar amanhã

### 5.1 Diferenças confirmadas (código + observação do dia)

| Aspecto | DEMO 52257579 | REAL 2257579 | Evidência | Impacto |
|---|---|---|---|---|
| **Stop level / freeze level** | ~0 (aceita SL a poucos pts) | real (rejeita SL dentro do stop level) | `Invalid stops` ×255 só na real; CODE não consulta antes do modify | 🔴 **diferença crítica do dia** |
| **`price_open` no `order_send`** | ? | **retorna 0.0** (documentado `core/vt_autotrader.py:2304-2307`) | `entry_price=0` no state → trailing falso | 🔴 causa C2 |
| **Volume mínimo / step** | 1.0 / 1.0 | 1.0 / 1.0 (igual) | `Invalid volume` ×98 (B3, ambas contas) | 🔴 C3 afeta ambas |
| **Spread** | ~0-1 tick | real (maior abertura/fechamento) | observação | 🟡 pequeno |
| **Slippage (market orders)** | ideal/fixo | real | observação; `slip_r` no `contract_specs` | 🟡 pequeno |
| **`history_deals_get()`** | vazio (limitação) | vazio hoje também (fallback balance) | `get_daily_pnl: fallback balance-starting` (LOG 09:33) | 🟡 mesmo caminho |
| **Margem / taxas B3** | virtual | real + taxas | gap −R$ 400 vs −R$ 552,88 | 🟡 PnL líquido difere |
| **PATH do daemon** | mesmo | mesmo | `start_autotrader.sh` não exporta `~/.local/bin` | 🟠 C4 |

### 5.2 O erro conceitual de "espelhar a real na demo"
**Atenção:** o pedido "fazer a demo seguir fielmente o que aconteceu na real hoje" tem uma armadilha. Se simplesmente replicarmos o comportamento de hoje (SLs rejeitados, trailing disparando cedo, TP1 falhando), a demo vai **reproduzir o bug**, não a realidade. O correto é:

> **A demo deve espelhar a COMPORTAMENTO-ALVO da real (com stop level correto, TP1 inteiro, trailing válido), não o comportamento-BUGADO de hoje.**

Isso exige, **antes de operar a demo amanhã**, aplicar as correções C1-C6 no código. Sem isso, a demo vai apenas repetir o desastre de forma virtual.

### 5.3 Plano para a demo ser fiel à real (com correções aplicadas)

1. **Aplicar correções C1-C6 no código HOJE** (ver Seção 6). Sem isso, a demo replica bug, não realidade.
2. **Simular o stop level da real na demo:** implementar `_respect_stop_level(symbol, sl_price, price)` que consulta `trade_stops_level` e **rejeita pré-envio** SLs dentro do limite (+buffer 2 ticks). Assim, o que a demo rejeitar = o que a real rejeitaria → modifies falham igual nas duas → comportamento fiel. **Confirmar o número real do stop level com a XP** (pergunta aberta Q1).
3. **Resolver `entry_price=0`:** nunca confiar em `price_open` zerado; usar broker-truth `positions_get()` para obter `price_open` real; adicionar guarda `if entry_price <= 0: skip trailing/TP1/profit_lock` em todo site que usa `profit_pts`.
4. **TP1 inteiro:** se `volume × tp1_pct < volume_step`, **não tentar TP1** (logar "TP1 skipped — volume fracionário"); ou ajustar `tp1_pct` para resultar em volume inteiro.
5. **PATH do hermes:** `start_autotrader.sh` deve fazer `export PATH="$HOME/.local/bin:$PATH"` e/ou o recovery deve usar `find_hermes()` como o validator v2.
6. **Mesma config, mesmas estratégias, mesmo dispatch** — só troca a credencial (`start_mt5linux.sh 52257579 <senha> XPMT5-DEMO`).
7. **Custo real no backtest:** incorporar ~R$ 7/trade de taxa no `backtest_evaluator` para o AGI otimizar líquido.

### 5.4 Por que a demo "funcionava" e a real "quebrou" (a ilusão)
A demo aceitava SLs a 5pts do preço (stop level ~0), então o trailing/breakeven **parecia funcionar** — mas só porque a demo não enforce o stop level. O bug C2 (`entry_price=0` → trailing ativa cedo) **já estava presente na demo**, mas lá ele mandava SLs de 5pts que a demo aceitava, então a posição era fechada em "lucro" (na verdade breakeven) e ninguém notava. Na real, o mesmo SL de 5pts é rejeitado, o trailing fica preso, e o SL original (largo) é o que vale. **A demo mascarava o bug; a real o expôs.**

---

## 6. Correções obrigatórias (especificação para implementar HOJE)

Priorizadas. Cada item tem o arquivo, a linha, e o que mudar.

### FIX-1 (C2) — Guard `entry_price` em todo cálculo de `profit_pts` · 🔴 bloqueante
- **Onde:** `core/vt_autotrader.py:2687` (leitura), `:2716-2720` (cálculo), `:3537,3540` (recover path).
- **Mudar:** logo após `entry_price = pos["entry_price"]`, adicionar `if not entry_price or entry_price <= 0: log.warning("entry_price=0 — pulando gestão (trailing/TP1/BE)"); return` (ou tentar re-obter de `positions_get`).
- **Raiz:** popular `entry_price` via broker-truth (`positions_get price_open`) nos rebuilds (`:582`, `:3510`, `:3529`), não aceitar `0.0`.

### FIX-2 (C1) — `_respect_stop_level` pré-envio · 🔴 bloqueante
- **Onde:** novo helper chamado antes de **todo** modify (`core/vt_autotrader.py:2960, 3000, 3085` e `mt5/mt5_error_recovery.py:542`).
- **Mudar:** `_respect_stop_level(symbol, sl_price, price, direction)` consulta `info(symbol).trade_stops_level`; se `|sl-price| < stops_level + 2 ticks` → **não envia** (mantém SL anterior), loga "SL dentro do stop level — modify pulado".

### FIX-3 (C3) — TP1 com volume inteiro · 🔴 bloqueante
- **Onde:** `core/vt_autotrader.py:2739`.
- **Mudar:** `close_volume = round(original * tp1_pct / volume_step) * volume_step`; se `< volume_step` → skip TP1 com log "TP1 skipped — volume fracionário (original={original} tp1_pct={tp1_pct})".

### FIX-4 (C4) — hermes no PATH do daemon · 🟠
- **Onde:** `scripts/start_autotrader.sh` (após linha 8) e `mt5/mt5_error_recovery.py:69`.
- **Mudar:** no script, `export PATH="$HOME/.local/bin:$PATH"`; no recovery, trocar `["hermes", ...]` por `[find_hermes(), ...]` (usar `core/vt_hermes_helper.find_hermes`).

### FIX-5 (C5) — Fix padrão e validator não inflam SL · 🟠
- **Onde:** `mt5/mt5_error_recovery.py:303, 156-170` (remover `point_val*50*1.5` e `int(sl_pts*1.5)` como fallback duro); `core/vt_order_validator_v2.py:34-41` (BIT `max` 500000 → alinhar com `max_native*point_mult` = 500×100 = 50000).
- **Mudar:** em vez de inflar, **revalidar** o SL contra o stop level e tentar novamente com distância válida; se falhar, **manter SL anterior** e alertar (nunca inflar).

### FIX-6 (C6) — Emergency close confiável · 🟡
- **Onde:** `core/vt_emergency.py:87-134` (`_get_current_pnl` retorna 0.0 em falha) e `:137-148` (`pnl <= 0` ambíguo).
- **Mudar:** `_get_current_pnl` deve distinguir "falha de leitura" de "pnl real = 0" (retornar `None` em falha); `_is_position_against_us` trata `None` como "incerto → fechar" (já é o espírito do `safe_modify_sl_with_emergency_close`, mas com logging claro).

### FIX-7 (AGI) — Log do stage5 deve mostrar score real · 🟠 governança
- **Onde:** `optimization/agi_v4/stage5_apply.py:314`.
- **Mudar:** `log.info(f"APLICADO {pair}: {strategy} (score cand R${cand_score:.2f} = 30d R${cand_pnl:.2f}+hoje R${today_bônus:.2f} > score base R${base_score:.2f})")` — auditável.

### FIX-8 (operacional) — Kill switch humano diário · 🟡 processo
- **Onde:** config `max_daily_loss` já é −500 (bom); mas hoje o stop real foi −552,88 (rompeu). Verificar se o kill switch dispara no **broker-truth** (saldo MT5) e não no PnL do DB (que subnotifica).
- **Mudar:** garantir que `max_daily_loss` compare contra `balance delta` MT5, não `SUM(net_pnl)` DB.

---

## 7. Plano de ação (donos + quando)

| # | Ação | Dono | Quando | Status |
|---|---|---|---|---|
| 1 | FIX-1 (entry_price guard) + FIX-3 (TP1 inteiro) + FIX-4 (PATH hermes) | Claude | 05/08 noite | ✅ **Concluído** |
| 2 | FIX-2 (`_within_stop_level` pré-envio em todo modify) | Claude | 05/08 noite | ✅ **Concluído** |
| 3 | FIX-9 (kill switch broker-truth robusto — get_daily_pnl não retorna 0.00) | Claude | 05/08 noite | ✅ **Concluído (novo)** |
| 4 | FIX-5 (validator/recovery não inflam SL) + FIX-6 (emergency) | próxima sessão | Fase 2 | ⏳ |
| 5 | FIX-7 (log AGI auditável) + custo R$7/trade no backtest | próxima sessão | Fase 2 | ⏳ |
| 6 | Paridade demo-real: stop level simulado (WIN~300/WDO~200/BIT~500/WSP~200) | próxima sessão | Fase 2 | ⏳ |
| 7 | Smoke test na demo com correções + stop level simulado | Hermes | Amanhã pre-flight | ⏳ |
| 8 | Operar DEMO com mesma config/modo de hoje (corrigido) | Autotrader | Amanhã 09:05 | ⏳ |
| 9 | Confirmar stop level real com a XP (WIN/BIT/WDO/WSP) | Bruno | Até sexta | ⏳ |
| 10 | Rodar AGI 17h sobre demo forward-only (sem backtest contaminado) | AGI | Amanhã 17:10 | ⏳ |
| 11 | **NÃO operar conta real** até a demo validar 3 dias verdes | Bruno | Esta semana | ⏳ |

### 7.1 Fase 1 implementada (05/08/2026 à noite) — detalhes de código

| Fix | Arquivo:linha | O que mudou | Verificação |
|---|---|---|---|
| **FIX-1** (C2) | `core/vt_autotrader.py:2687` (guard manage) · `:582` (rebuild fallback price_current) · `:3543/3562` (recover fallback) | Guard `if entry_price <= 0: return` no topo do manage_position; rebuild/recover usam price_current quando price_open=0 | smoke: entry_price=0 → 0 modifies (antes: trailing falso com 179375pts "lucro") ✅ |
| **FIX-2** (C1) | `core/vt_autotrader.py` novos helpers `_get_stops_level`/`_within_stop_level` (`:2185`) + gates em PROFIT_LOCK (`:3116`) · BREAKEVEN (`:3153`) · Trailing (`:3267`); `mt5/mt5_error_recovery.py:556` gate pré-envio no safe_modify_sl | SL dentro do stop_level (+2 ticks buffer) → SKIP modify, mantém SL anterior válido; defesa em profundidade no recovery | smoke: SL=5pts + stops=300 → bloqueado; SL=500pts → válido; DEMO stops=0 → não bloqueia ✅ |
| **FIX-3** (C3) | `core/vt_autotrader.py` novos helpers `_get_volume_step`/`_normalize_partial_volume` (`:2145`) + TP1 (`:2754`) · TP2 (`:2896`) | Arredonda close_volume p/ múltiplo de volume_step; se < 1 step → skip TP1/TP2 idempotente | smoke: 0.5×1.0 step=1.0 → 0.0 (skip, era o `Invalid volume`×98) ✅ |
| **FIX-4** (C4) | `mt5/mt5_error_recovery.py:62` (`_ask_llm` usa `find_hermes()`) · `scripts/start_autotrader.sh:11` (`export PATH`) | Recovery usa `find_hermes()` (helper robusto do validator v2) em vez da string nua "hermes"; script exporta `~/.local/bin` no PATH | sintaxe OK; se hermes ausente → falha limpa com msg clara (antes: errno críptico ×53) ✅ |
| **FIX-9** (novo) | `core/vt_truth.py:432` (acesso defensivo account) + fallback-DB conservador | `st.get("account",{}).get("balance")` em vez de `st["account"]["balance"]` (KeyError); se broker-truth + fallback falham, usa SUM(net_pnl) do DB (conservador, trava kill switch se há perda) em vez de 0.00 (que desarma o gate) | smoke: status degradado {} → None (não KeyError); acesso defensivo funciona ✅ |

**Verificação Fase 1:**
- `py_compile` OK nos 3 arquivos (`core/vt_autotrader.py`, `core/vt_truth.py`, `mt5/mt5_error_recovery.py`)
- ruff: 0 erros novos (7 F841 pré-existentes, todos fora das edições)
- `test_emergency_close.py`: 9 passed · `test_trailing_profit_lock.py`: 20 passed · `test_mt5_error_recovery_safety.py`: 6 passed · `test_autotrader_emergency_wiring.py`: 3 passed · `test_vt_truth_helpers.py`+direction: 23 passed
- 5 falhas pré-existentes (1 rebuild + 2 pnl_truth + 2 validator) — **nenhuma introduzida**, confirmadas via `git stash` contra HEAD
- Smoke tests manuais validaram a lógica de FIX-1/2/3/9

**O que a demo de amanhã já ganha com a Fase 1 (e o que ainda falta):**
- ✅ **Já ganha:** trailing/breakeven/profit-lock não disparam mais com lucro falso (FIX-1); SLs dentro do stop level não geram rajada de rejects (FIX-2); TP1/TP2 não geram `Invalid volume` (FIX-3); recovery LLM funciona (FIX-4); kill switch dispara de verdade quando há perda (FIX-9).
- ⚠️ **Ainda falta (Fase 2):** o stop level da demo ainda é ~0 (vai mascarar o FIX-2 — a demo vai aceitar SLs que a real rejeitaria); validator ainda pode inflar SL (FIX-5); custo de R$7/trade não entra no backtest do AGI; troca WIN_M5→EMA_CROSSOVER do AGI 17h não foi auditada.

### 7.2 Fase 2 implementada (05/08/2026 à noite) — validator, emergency, AGI e paridade

| Fix | Arquivo:linha | O que mudou | Verificação |
|---|---|---|---|
| **FIX-5** (C5) | `core/vt_order_validator_v2.py:34-48` (SL_LIMITS alinhado c/ `_calc_sl`) · `:611-630` (clamp só aperta); `mt5/mt5_error_recovery.py:156-175` (`_fix_invalid_stops` não infla) | `max` do SL_LIMITS alinhado com `max_native × point_mult` (BIT 500000→50000); clamp "pré-envio" SÓ APERTA (nunca infla dentro do range); fix padrão mantém SL em vez de `int(sl_pts*1.5)` | smoke: BIT sl=50000 (0.30x ATR) → não infla (None); sl=600000 → aperta p/ 50000 ✅ · test_validator_pre_send: 10 passed |
| **FIX-6** (C6) | `core/vt_emergency.py:87-171` (`_get_current_pnl` retorna None em falha) · `:137-171` (`_is_position_against_us` trata None) · `:327-369` (logging None-safe) | `_get_current_pnl` retorna `None` (não 0.0) em falha; `_is_position_against_us` trata None como "incerto → contra" (safety-first preservado, logging claro); `_notify_critical_emergency` e `_emergency_close_position` formatam None sem TypeError | test_emergency_close: 9 passed ✅ |
| **FIX-7** (governança) | `optimization/agi_v4/stage5_apply.py:314-326` (log auditável) | Log do APLICADO agora mostra score real blended com decomposição `30d + hoje (weight×pnl×n_trades)` em vez de PnL brutos enganosos | sintaxe OK; resolve o "cand R$2479 > base R$2507" contraditório |
| **Paridade demo-real** | `core/vt_autotrader.py:2191-2222` (`_get_stops_level` lê override) · `core/vt_config_loader.py:517-525` (overlay simulated_stop_level) · `/tmp/vt_copilot_overrides.json` | Quando broker retorna stops_level=0 (DEMO), aplica override conservador (WIN~300/WDO~200/BIT~500/WSP~200 pts) → demo rejeita mesmos SLs que real | sidecar criado; `_get_stops_level` retorna 300 p/ WIN quando broker=0 |
| **AGI como real: mult** | `backtest/backtest_v944.py:55-62` (CONTRACT_SPECS alinhado c/ config) | `WIN$ mult` 1.0→0.2 (estava superestimando PnL WIN em 5×); WSP 13.5→0.01; fee_r=R$7/trade (era hardcoded 1.20, gap real 05/08 foi ~R$7) | smoke: WIN mult=0.2, fee_r=7.0 ✅ |
| **AGI como real: stop level** | `backtest/backtest_v944.py:258-264` (sim_stops_level) + gates em profit-lock `:494` · breakeven `:503` · trailing `:517` | Backtest respeita stop_level simulado: SL apertado (entry+1tick) dentro do stop_level NÃO é aplicado (mantém SL anterior) — fiel à real que rejeita "Invalid stops" | sim_stops_level presente no backtest_combo ✅ |
| **AGI: dia inválido** | `optimization/agi_v4/stage5_apply.py:243-255` (guard `/tmp/vt_invalid_day.flag`) | Se flag existe, AGI NÃO aplica nenhuma mudança (só observa) — evita "aprender" de dia com execução corrompida | sintaxe OK |

**Verificação Fase 2:**
- `py_compile` OK em 6 arquivos (`vt_order_validator_v2`, `vt_emergency`, `stage5_apply`, `vt_config_loader`, `backtest_v944`, `vt_autotrader`)
- ruff: 0 erros nas linhas editadas (17 F841 pré-existentes, todos fora das edições; HEAD tinha 24 — reduzi)
- Regressão final: **71 passed, 1 skipped, 0 failed** (emergency_close 9 + trailing 20 + recovery 6 + validator_pre_send 10 + autotrader_wiring 3 + vt_truth 23)
- 3 falhas pré-existentes em `test_config_volume_by_contract_type` (falham no HEAD, não minhas)
- Smoke tests: FIX-5 (clamp não infla), AGI-real (mult 0.2 + fee 7.0 + stop_level) — todos validados

**Descoberta crítica da Fase 2 (mult do WIN):**
O backtest usava `WIN$ mult=1.0` enquanto a config real é `mult=0.2`. **Isto superestimava o PnL do WIN em 5×** — o ativo que mais operou (18/22 trades em 05/08). O AGI via "WIN_M5 PF=2.05 PnL=R$2683" quando o PnL real proporcional seria ~R$537. Com o `mult=0.2` corrigido + `fee_r=R$7`, o AGI agora otimiza contra números realistas, não inflados. **Este era provavelmente o viés sistemático que fazia o AGI aprovar estratégias que perdiam na real.**

**A demo de amanhã agora é fiel à real:**
- ✅ Stop level simulado ativo (demo rejeita SLs como a real)
- ✅ Trailing/breakeven/profit-lock respeitam stop level (FIX-2)
- ✅ TP1/TP2 não geram Invalid volume (FIX-3)
- ✅ Validator não infla SL (FIX-5)
- ✅ Emergency close confiável (FIX-6)
- ✅ Kill switch dispara de verdade (FIX-9)
- ✅ AGI otimiza com mult correto + custo real + stop level (não mais superestimado)

---

## 8. Perguntas abertas (investigação contínua)

1. **Q1 — Stop level exato na XP real?** (WIN/BIT/WDO/WSP) — precisa do número pra calibrar o simulador da demo. Bruno perguntar à XP.
2. **Q2 — Origem do `entry_price=0`:** foi o STATE-REBUILD das 09:21:12 (state reconstruído vazio, LOG linha 5)? Confirmar o fluxo de inicialização após abertura de posição. **Hipótese forte:** o daemon (re)iniciou e o rebuild leu `price_open=0` do XPMT5-PRD.
3. **Q3 — Validator BIT 50.000→141.857:** a lógica do "SL ajustado pré-envio" parece inverter o clamp do `_calc_sl`. Por que o `max` do SL_LIMITS do BIT é 500.000 (5× o max_native)? Alinhar com `_calc_sl` specs.
4. **Q4 — GHOST (6/22):** por que `exit_price=0` no fechamento? O reconcile funcionou (PnL correto no broker), mas o registro primário falhou. Investigar `monitoring/vt_trade_event_watcher.py` × `core/vt_history_reconcile.py`.
5. **Q5 — Taxas B3 (gap R$ 152,88):** itemizar por trade para o AGI não tratar como perda de estratégia. Incluir ~R$ 7/trade no `backtest_evaluator`.
6. **Q6 — Backtest do AGI replica gerenciamento?** O `backtest_v944.py` simula TP1/trailing/breakeven, ou só SL de entrada? Se só SL, otimiza sistema aleijado. (Resposta parcial: precisa auditoria do engine.)

---

## 9. Lições (para o AGI de hoje e para o Bruno)

1. **A demo mascarava bugs que a real expôs.** Stop level ~0 na demo deixava SLs absurdos passarem. **Toda funcionalidade "que funcionava na demo" precisa ser revalidada contra constraints da real** (stop level, volume step, price_open, taxas).
2. **Não se opera conta real com gestão conhecida-bugada.** Às 09:24 (2º sinal, padrão de `Invalid stops` + `Invalid volume` já evidente) o sistema deveria ter sido pausado. Kill switch humano teria limitado a −R$ 87 (trades 1-2) em vez de −R$ 552,88.
3. **`−36,9%` num dia é falha de gestão de risco, não de estratégia.** O `max_daily_loss=−500` (33% do capital de R$ 1.500) já era agressivo demais para conta real. Recomendar `−10%` (−R$ 150) até o sistema provar estabilidade.
4. **O custo real (taxas B3) é material:** R$ 152,88 num dia = 10% do capital só em custo. O backtest que ignora taxas superestima qualquer edge. **Incluir custo no AGI.**
5. **Concentração mata:** 82% dos trades em WIN, mesma estratégia (HTF_BIAS_LTF_ENTRY), mesmo TF (M15/M30), num dia de range. **Diversificação entre pares e regimes não é opcional.**
6. **Audit trail opaco é perigoso:** o log do AGI `cand R$2479 > base R$2507` é mentiroso (mostra PnL bruto, decide por score blended). Em conta real, **cada decisão automatizada precisa ser auditável e verdadeira.**
7. **Recovery LLM sem hermes no PATH = política de SL definida por fallback duro** (`point_val*50*1.5`). O fallback silencioso virou a estratégia de fato. **Todo fallback precisa ser logado em WARN e revisado.**

---

*Documento matriz gerado por Claude (ZCode) em 05/08/2026 — forense read-only de `vt_trades.db` + `/tmp/vt_autotrader.log` + logs AGI 12h/17h + código `core/`·`mt5/`·`optimization/agi_v4/`. Complementa `docs/lesson_learning_2026-08-05.md` (Hermes) com auditoria de código, análise estrutural do AGI e plano de paridade DEMO↔REAL.*
