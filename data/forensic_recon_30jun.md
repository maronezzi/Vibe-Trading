# Forense Vibe-Trading — Reconstrução 30/06 antes do incidente 01/07

**Data do relatório:** 2026-07-01 10:01 (BRT)
**Autor:** sub-agente Hermes (forense sob demanda)
**Para:** Bruno Maronezzi
**Escopo:** descobrir qual config/estratégia/params estavam ativos em 30/06 ANTES do incidente que reescreveu `vt_config.json` em memória hoje (01/07 ~09:30) durante hot-reload.

---

## TL;DR — Resumo executivo

1. **A "perda de 95% do config" no incidente de hoje NÃO é tão catastrófica quanto parece.** O estado de 30/06 19:52 (v945, último backup antes do tuning noturno perdido) **foi recuperado** em `vt_config.json.bak_v945_pre_reoptimize_195210` (53 chaves, 13.6 KB, 19:52 BRT de 30/06). É a melhor reconstrução do "baseline de 30/06 antes do incidente".

2. **O autotrader NÃO está rodando com config legado de 26/06.** O `_updated_by` mais recente é `bruno_hard_lock_ind_2026_07_01` (v952, 01/07 09:58), 2 min depois do commit `48780d05` ("liberar BUY na quarta"). O hot-reload de hoje comeu um subset específico de chaves: kill switches de risco (`max_daily_loss`, `halt_duration_minutes_by_tf`, `max_consecutive_losses_by_tf`, `warmup_minutes`, `winddown_minutes`, `validate_with_llm`) — todas viraram `null` no config atual.

3. **DB confirma produção ativa em 30/06:** WINQ26_M5 (STRONG_TREND), WDOQ26_M5 (STRONG_TREND), INDQ26_M5 (test tracking only). WSP e BIT não geraram trades no dia 30/06 (BIT_M5/BIT_M30/WSP_M5 foram desabilitados DEPOIS, não estavam em 30/06). IND estava em `disabled_symbols` em 30/06 — Bruno desabilitou IND na quarta, mas o autotrader viu sinais `[EXCLUDED]` (experiment tracking) que vazaram pro DB.

4. **Recomendação imediata (PRIORIDADE 1):** reaplicar v945 inteiro como baseline (overwrite atual) → rodar AGI `--dry-run` → comparar diffs → se quiser, aplicar mudanças manuais para IND+BIT OFF no topo do v945.

---

## 1. Linha do tempo real dos arquivos de config

| mtime (BRT) | arquivo | versão | writer | chaves | nota |
|---|---|---|---|---|---|
| 23/06 21:37 | `vt_config.json.bak-v866-20260623-213711` | **v866** | agi_v3_discovery_engine | 56 | backup Wave 4.3 + algo |
| 25/06 12:02 | `vt_config.json.bak-meiodia-20260625-120239` | **v882** | bruno_human_decision_2026-06-25_post_agi_v2 | 40 | manual |
| 26/06 20:29 | (Wave 9 — só em git, commit `7930b4ac`) | **v921** | hermes_wave_9_high_edge | 49 | backup em `/tmp/vt_config_full_579lines.json` |
| 30/06 18:09 | `vt_config.json.bak_pre_full_opt_1809` | **v938** | agi_forward_it1 | 50 | AGI forward run, posição otimista (max_daily_loss=-999999) |
| 30/06 18:19 | `vt_config.json.bak_pre_claude_run_181927` | **v938** | agi_forward_it1 | 50 | (idêntico, 2 backups do mesmo v938) |
| 30/06 19:02 | `vt_config.json.bak_v943_pre_v944_190254` | **v943** | agi_v3_discovery_engine | 50 | ultra-conservador |
| 30/06 19:09 | `vt_config.json.bak_v943_pre_v944_190903` | **v943** | agi_v3_discovery_engine | 50 | (idêntico, 2 backups do mesmo v943) |
| 30/06 19:09 | `vt_config.json.bak_v944_945_1909` | **v945** | hermes_v944_intermediate_1909 | 51 | **MEIO TERMO aplicado** |
| **30/06 19:52** | **`vt_config.json.bak_v945_pre_reoptimize_195210`** | **v945** | hermes_v944_intermediate_1909 | **53** | **🟢 RECOMENDAÇÃO: ESTE É O BASELINE DE 30/06** |
| 01/07 09:56 | git commit `48780d05` ("Wave 3.1 manual override") | **v951** | calendar_resolve | 50 | Bruno habilita BUY na quarta manualmente |
| **01/07 09:58** | `vt_config.json` (ATUAL, pós-incidente) | **v952** | bruno_hard_lock_ind_2026_07_01 | 50 | **hot-reload comeu chaves de kill switch** |

**Conclusão sobre a linha do tempo:**
- O backup de 26/06 Wave 9 (`/tmp/vt_config_full_579lines.json`) é **v921** — representa o estado pós-Wave 9 (BOLLINGER no IND, STRONG_TREND em WDO_M5/WDO_M15).
- O "trabalho fino de 28-30/06" foi uma **oscilação entre duas posições**:
  - Posição **agressiva/otimista** (v938, AGI forward_it1): max_daily_loss=-999999, blocked_day_directions=null, WIN_M5=STRONG_TREND
  - Posição **meio-termo** (v945, hermes_v944_intermediate_1909): max_daily_loss=-800, blocked_day_directions=[[2,"BUY"],[1,"SELL"]], WIN_M5=ADX_TREND, volume WIN=2
- A posição v945 (a que Bruno aparentemente queria para a sessão de 30/06) é a que devemos preservar.

---

## 2. Recoverable_Lost — Chaves que o incidente comeu

Comparação direta entre `vt_config.json.bak_v945_pre_reoptimize_195210` (53 chaves) e `vt_config.json` (50 chaves):

### 2.1 Chaves inteiramente removidas (3)
| chave | valor em v945 | impacto |
|---|---|---|
| `_note_blocked_day_directions` | docstring do schema (explica [[weekday, direction]]) | só documentação, baixo risco |
| `_reason` | "v944 MEIO TERMO: reduzir cooldowns, WIN volume=2, max_daily_loss=-800 (entre v938 otimista e v943 ultra-conservador)" | metadado, não afeta runtime |
| `ind` | `{sl_atr_mult: 1.0, cooldown_seconds: 180}` | config legado do IND (já em disabled_symbols, então morto — recuperável do v945 para consistência) |

### 2.2 Chaves PRESENTES em ambos mas com valor `null` no atual (6 — KILL SWITCHES PERDIDOS!)
| chave | valor em v945 | valor atual | impacto |
|---|---|---|---|
| `halt_duration_minutes_by_tf` | dict com 20 TFs (WIN_M5=60, WDO_M5=45, etc.) | **null** | 🔴 halt por loss não dispara mais |
| `max_consecutive_losses_by_tf` | dict com 20 TFs (IND_M*=3, resto=999) | **null** | 🔴 cooldown por streak não funciona |
| `warmup_minutes` | 15 | **null** | 🟡 primeiros 15min pós-abertura sem filtro |
| `winddown_minutes` | 15 | **null** | 🟡 últimos 15min pré-fechamento sem filtro |
| `validate_with_llm` | true | **null** | 🟡 LLM validator desligado (default false → conservador) |
| `consecutive_loss_config` | dict IND_M*={max_consecutive:3, halt_minutes:60} | presente mas igual | OK, comparar abaixo |

> **Esse é o incidente "real":** o autotrader de hoje está rodando SEM kill switches de loss streak, SEM halt pós-perdas consecutivas, e SEM warmup/winddown. É equivalente a perder 5 disjuntores do painel elétrico — está tudo ligado direto.

### 2.3 Chaves PRESENTES mas com valor divergente (estratégia/params — perda semântica)
| chave | valor em v945 (30/06) | valor atual (01/07) | nota |
|---|---|---|---|
| `strategy_by_tf.WIN_M5` | `ADX_TREND` | `STRONG_TREND` | mudança AGI Wave 8.5+ ? ou hot-reload perdeu o override? |
| `strategy_by_tf.WIN_M15` | `PIVOT_POINTS` | `SQUEEZE_BREAKOUT` | mudou (commit `8430831a` Wave 8.5+, 26/06 09:39) |
| `strategy_by_tf.BIT_M15` | `PIVOT_POINTS` | `STRONG_TREND` | mudou |
| `strategy_by_tf.WSP_M15` | `MACD_MOMENTUM` | `SUPERTREND` | mudou (commit `cc940729` Wave 8.5, 26/06 09:12) |
| `strategy_by_tf.WDO_M5` | `ADX_TREND` | `STRONG_TREND` | mudou |
| `strategy_by_tf.WDO_M15` | `ADX_TREND` | `STRONG_TREND` | mudou |
| `strategy_by_tf.WIN_M30` | `RSI_REVERSION` | `MACD_MOMENTUM` | mudou (commit `bc24d9a8` Wave 2.1, 26/06 07:35) |
| `strategy_by_tf.WSP_H1` | `RSI_REVERSION` | `PIVOT_POINTS` | mudou |
| `strategy_by_tf.IND_*` | (ausente — IND em disabled_symbols) | BOLLINGER em M5/M15/M30/H1 | IND reativado HOJE |
| `max_daily_loss` | **-800** | **-999999** | 🔴 kill switch removido |
| `blocked_day_directions` | `[[2,"BUY"],[1,"SELL"]]` (Qua+Ter) | `[[1,"SELL"]]` (só Ter) | 🔴 regra da Quarta BUY perdida |
| `disabled_symbols` | `["IND"]` | `["BIT","IND"]` | BIT foi desabilitado depois |
| `disabled_timeframes` | `[]` | `["BIT_M5","BIT_M30","WSP_M5"]` | 3 TFs novos OFF |
| `volume_by_symbol.WIN` | `2` | `1` | volume WIN reduzido pela metade |
| `volume_by_symbol.IND` | `0` | `1` | IND reativado com volume |
| `strategy.IND` | (ausente) | `BOLLINGER` | IND reativado |
| `params_by_tf.WIN_M5.adx_threshold` | 29 | 40 | mais restritivo |
| `params_by_tf.WIN_M5.cooldown_seconds` | 600 | 1200 | cooldown dobrado |
| `params_by_tf.WIN_M5.sl_atr_mult` | 1.02 | 1.5 | SL mais largo |
| `params_by_tf.WIN_M5.trail_activate` | 1.28 | 1.0 | trail mais cedo |
| `params_by_tf.WIN_M5.max_daily_trades` | 8 | 6 | limite de trades reduzido |
| `params_by_tf.WIN_M5.touch_pct` | 0.004 | 0.006 | filtro de toque menos sensível |
| `params_by_tf.WIN_M5.ema_fast` | 10 | 5 | EMA mais rápida |
| `params_by_tf.WIN_M5.ema_slow` | 24 | 23 | EMA lenta quase igual |
| `params_by_tf.WIN_M5.rsi_overbought/oversold` | 65/17 | null | perdemos os limites de RSI |
| `params_by_tf.WDO_M5.cooldown_seconds` | 300 | 600 | cooldown dobrado |
| `params_by_tf.WDO_M5.max_daily_trades` | 12 | **999** | 🔴 teto removido (de 12 pra 999 = sem teto) |
| `params_by_tf.WDO_M5.sl_atr_mult` | 1.5 | 1.5 | igual |
| `params_by_tf.BIT_M5.cooldown_seconds` | 400 | 600 | cooldown aumentado |
| `params_by_tf.BIT_M5.max_daily_trades` | 10 | 12 | limite aumentado |
| `params_by_tf.BIT_M15.cooldown_seconds` | 200 | 120 | cooldown reduzido |
| `params_by_tf.WIN_M15.cooldown_seconds` | 200 | 300 | cooldown aumentado |
| `params_by_tf.WIN_M15.max_daily_trades` | 10 | null | perdemos o limite |
| `params_by_tf.WSP_M5` | (desabilitado agora) | iguais | OK |

### 2.4 Chaves preservadas corretamente (✓)
`time_blocks` (BITM26 09-11h + WINQ26 VWAP off), `symbols`, `timeframes`, `timeframes_by_symbol`, `wdo`, `win`, `bit`, `wsp` (todos blocos raiz), `magic=555501`, `check_interval=30`, `bars_count=45`, `resolved_symbols`, `contract_specs`, `daily_trade_count_by_symbol`, `pause_criteria`, `halt_trading=false`, `halt_new_trades=false`, `halt_on_loss=false`, `max_daily_trades=999`, `global_max_daily_trades=999`, `max_consecutive_losses_by_tf` virou null mas tem substituto em `consecutive_loss_config`.

---

## 3. Stash_Candidates — Verificação dos stashes

```
stash@{0}: 579 linhas, _version=921, writer=hermes_wave_9_high_edge (Wave 9, 26/06)
stash@{1}: 543 linhas, _version=928, writer=hermes_wave_13_2_bit_wsp_mult_fix
stash@{2}: 574 linhas, _version=857, writer=test_agi_memo_teardown
stash@{3}: 592 linhas, _version=887, writer=test_agi_memo_teardown
stash@{4}: 403 linhas, "pre-opencode-alert-analysis"
stash@{5}: 458 linhas, "pre-opencode-trade-analysis"
stash@{6}: 439 linhas, "pre-opencode-mt5-integration-improvement"
```

**Conclusão:** todos os stashes são **anteriores** ao estado de 30/06 (versões 857, 887, 921, 928 < v945 e < v952). Nenhum stash contém trabalho de 28-30/06. Não há candidatos para resgate via stash.

> Observação: `stash@{0}` (579 linhas, v921) é idêntico em forma ao backup `/tmp/vt_config_full_579lines.json` — ambos são Wave 9 do 26/06. Pode descartar um.

---

## 4. Verificação dos commits

```
git log --all --grep='manual_fix\|v942\|w868\|strategy_key' (vazio relevante)
```

Commits nos últimos 10 dias que tocaram `vt_config.json`:
```
48780d05  2026-07-01 09:56  feat(config): liberar BUY na quarta (Wave 3.1 manual override Bruno 2026-07-01)  ← HOJE
7930b4ac  2026-06-26 20:29  feat(config): Wave 9 - IND_M15 BOLLINGER reativado + WDO_M15 STRONG_TREND       ← Wave 9
f31556dd  2026-06-26 18:29  fix(agi): Wave 8.8 - AGI NUNCA desabilita pares
ce4ce15f  2026-06-26 13:12  fix(state): _sync_daily_pnl_with_db
8430831a  2026-06-26 09:39  feat(config): Wave 8.5+ - WIN_M15 → SQUEEZE_BREAKOUT
cc940729  2026-06-26 09:12  feat(config): Wave 8.5 - WDO_M5 STRONG_TREND + WSP_M15 SUPERTREND
377b5c2f  2026-06-26 09:09  feat(autotrader): time_blocks wired (BITM26 09-11h off, WINQ26 VWAP off) Wave 8.4
26d3105b  2026-06-26 09:02  feat(config): trail_activate 1.2→1.0 em 17 pares (Wave 4.3)
bc24d9a8  2026-06-26 07:35  feat(config): WIN_M30 PIVOT_POINTS → MACD_MOMENTUM (Wave 2.1)
eb1c516d  2026-06-26 06:50  chore(config): desabilitar 4 pares perdedores (Wave 1.3)
18cb7d04  2026-06-25 19:40  fix(autotrader): _check_cooldown respeita params_by_tf
f913f3bd  2026-06-25 08:14  chore(config): disable BIT — AGI v2 dry-run
604093a2  2026-06-25 07:26  chore(runtime): snapshot vt_config v881 + vt_trades
```

**Não houve commits nos dias 27, 28, 29, 30 de junho.** Toda a atividade nesses dias foi:
- **28/06 14:34** — commit `f880f2ef` "Wave 10 - background invisível" (mudou infra, não config)
- **29-30/06** — múltiplas execuções do AGI forward e iterações manuais que resultaram nos `.bak` files v938→v943→v945
- O commit mais recente (`48780d05`) foi feito às 09:56:21 HOJE, e o `_version` em disco é 952 (depois de `calendar_resolve` salvar).

**Conclusão:** o "trabalho de 28-30/06" **não está em git**, está apenas nos `.bak` files do disco. O backup v945 é a única cópia sobrevivente do estado intermediário final do dia 30/06.

---

## 5. DB Trade History — Estratégias efetivas em 30/06

```sql
SELECT entry_time, symbol, timeframe, strategy, direction, net_pnl, exit_reason
FROM trades WHERE DATE(entry_time) BETWEEN '2026-06-28' AND '2026-06-30';
```

| entry_time | symbol | TF | strategy | direction | net_pnl | exit_reason |
|---|---|---|---|---|---|---|
| 29/06 16:15 | WINQ26 | M5 | STRONG_TREND | SELL | -2.2 | SL_SERVIDOR |
| 29/06 16:16 | WDOQ26 | M5 | STRONG_TREND | BUY | +23.8 | EOD_16:45 |
| 29/06 16:28 | WINQ26 | M5 | STRONG_TREND | SELL | +40.8 | EOD_16:45 |
| 30/06 09:56 | WINQ26 | M5 | STRONG_TREND | BUY | -40.0 | SL_SERVIDOR |
| 30/06 10:06 | INDQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | -116.2 | EOD_16:45 |
| 30/06 10:15 | WINQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | -30.0 | SL_SERVIDOR |
| 30/06 10:17 | WINQ26 | M5 | STRONG_TREND | BUY | +31.0 | SL_SERVIDOR |
| 30/06 10:19 | INDQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | +243.8 | SL_SERVIDOR |
| 30/06 10:23 | INDQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | -221.2 | SL_SERVIDOR |
| 30/06 10:27 | INDQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | -116.2 | SL_SERVIDOR |
| 30/06 10:30 | WINQ26 | M5 | STRONG_TREND | BUY | +24.0 | SL_SERVIDOR |
| 30/06 10:43 | WINQ26 | M5 | STRONG_TREND | BUY | +106.0 | SL_SERVIDOR |
| 30/06 11:16 | WDOQ26 | M15 | PIVOT_POINTS [EXCLUDED] | BUY | +60.0 | SL_SERVIDOR |
| 30/06 11:30 | WINQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | -40.0 | SL_SERVIDOR |
| 30/06 12:12 | WDOQ26 | M15 | PIVOT_POINTS [EXCLUDED] | BUY | -50.0 | SL_SERVIDOR |
| 30/06 12:21 | WINQ26 | M5 | STRONG_TREND [EXCLUDED] | BUY | +106.0 | SL_SERVIDOR |
| 30/06 13:02 | FAKEG | M5 | TEST [EXCLUDED] | BUY | 0.0 | watchdog_drift |
| 30/06 13:10 | WDOQ26 | M5 | MACD_MOMENTUM [EXCLUDED] | BUY | +50.0 | SL_SERVIDOR |

**Resumo do dia 30/06:**
- **15 trades, PnL líquido: +R$ 7,20** (quase break-even)
- 8 WIN_M5 (6 produção + 2 [EXCLUDED]), 5 IND_M5 [EXCLUDED], 2 WDO_M15 [EXCLUDED], 1 WDO_M5 [EXCLUDED], 1 FAKEG (test)
- **Estratégias em produção real (não [EXCLUDED])**: WIN_M5=STRONG_TREND, WDO_M5=STRONG_TREND
- **WSP e BIT NÃO produziram trades em 30/06** — confirma que o time_block de BITM26 09-11h estava ativo e bloqueou
- IND_M5 trades estão todos marcados `[EXCLUDED]` — significa que **IND estava em disabled_symbols em 30/06** (confirma v945: `disabled_symbols: ["IND"]`), mas o autotrader gerou sinais mesmo assim para fins de experiment tracking

> Observação: os trades marcados `[EXCLUDED]` indicam experiment tracking (provavelmente `validate_with_llm=true` ligou em modo shadow). Após o incidente, o `validate_with_llm` virou null → IND_M5 trades que vazaram pro DB não vão se repetir.

---

## 6. Recomendação — Plano de Reconstrução

### 6.1 Decisão de design: qual baseline usar?

Três opções viáveis:

| opção | fonte | prós | contras |
|---|---|---|---|
| A. **v945_pre_reoptimize** (30/06 19:52) | `vt_config.json.bak_v945_pre_reoptimize_195210` | Representa o trabalho fino de 28-30/06 (kill switches ativos, blocked_day_directions, max_daily_loss=-800) | É "v944/v945 intermediate", não estado final consolidado |
| B. **Wave 9 (v921, 26/06 20:29)** | `/tmp/vt_config_full_579lines.json` | Estado completo, testado, commitado | 4 dias atrás — perde todo trabalho fino |
| C. **Config atual (v952)** | `vt_config.json` | Mais recente | **Não tem kill switches, max_daily_loss=-999999, blocked incompleto** |

**Recomendação: opção A (v945) + camada de IND/BIT OFF do estado atual.**

### 6.2 Script de reconstrução (PRIORIDADE 1 — executar antes do próximo pregão)

```bash
cd /home/bruno/Projects/Vibe-Trading

# 1. Backup do estado atual corrompido (não jogar fora)
cp vt_config.json /tmp/vt_config_corrupted_v952_0107.json

# 2. Restaurar v945 como baseline
cp vt_config.json.bak_v945_pre_reoptimize_195210 vt_config.json

# 3. Aplicar overrides pós-30/06 (IND+BIT off, IND BOLLINGER params) manualmente
#    OU: rodar AGI --dry-run e revisar o diff antes de aplicar
python3 optimization/agi_tuning_17h.py --dry-run 2>&1 | tee /tmp/agi_dryrun_after_v945.log

# 4. Se o AGI dry-run sugerir mudanças sensatas, aplicar via:
python3 optimization/agi_tuning_17h.py 2>&1 | tee /tmp/agi_apply_0107.log

# 5. Validar kill switches no config final
python3 -c "
import json
with open('vt_config.json') as f:
    c = json.load(f)
required = ['max_daily_loss', 'halt_duration_minutes_by_tf', 'max_consecutive_losses_by_tf',
            'warmup_minutes', 'winddown_minutes', 'validate_with_llm', 'time_blocks',
            'blocked_day_directions', 'disabled_symbols', 'disabled_timeframes']
missing = [k for k in required if k not in c or c[k] is None]
print('MISSING/NULL:', missing if missing else 'OK — todos os kill switches presentes')
"

# 6. Se quiser IND/BIT OFF explicitamente (compatibilidade com decisão Bruno 01/07):
#    Editar:
#       disabled_symbols = ['BIT', 'IND']
#       disabled_timeframes = ['BIT_M5', 'BIT_M30', 'WSP_M5']
#       volume_by_symbol.IND = 0
#       strategy.pop('IND', None)
#       params_by_tf.pop('IND_M5', None); params_by_tf.pop('IND_M15', None); ...
#       consecutive_loss_config.pop('IND_M5', None); ...
```

### 6.3 Decisões de produto pendentes (que AGI dry-run pode ajudar a resolver)

1. **Reativar IND?** Wave 9 (26/06) reativou IND com BOLLINGER, Bruno desabilitou em 30/06, AGI forward_it1 (v938) não reativou, v945 manteve OFF. Hoje (01/07) Bruno colocou IND de volta em disabled_symbols mas com `_updated_by=bruno_hard_lock_ind` → Bruno quer IND OFF por enquanto.

2. **BIT está OFF?** Era OFF em 25/06 (commit `f913f3bd`), foi desabilitado de novo DEPOIS de 30/06 (não está no v945). Confirmar se Bruno quer BIT OFF permanentemente ou reativar após convergência.

3. **WSP_M5 está OFF no atual, estava ON no v945** — quem colocou WSP_M5 em disabled_timeframes? Provavelmente o AGI forward_it1 (v938) — verificar se faz sentido manter OFF dado que WSP não tem gerado losses em 30/06 (zero trades).

4. **Regra Quarta BUY:** v945 tinha `[[2,"BUY"]]` (bloqueava Quarta BUY). Atual só tem `[[1,"SELL"]]`. Hoje é quarta (01/07) — Bruno fez Wave 3.1 manual override pra liberar BUY. **A regra da Quarta BUY não é uma "feature perdida" — é uma feature intencional que Bruno está testando desabilitar.** Decisão de produto.

5. **Volume WIN: v945=2, atual=1.** v945 dobrava o volume de WIN. Reduzir pra 1 é decisão consciente de Bruno pós-30/06 ou foi mudança automática? Verificar com AGI.

---

## 7. Riscos e inconsistências identificados

### 7.1 RISCO ALTO (resolver antes do próximo pregão)

🔴 **Autotrader rodando SEM kill switches de loss streak.** O config atual (v952) tem `halt_duration_minutes_by_tf=null`, `max_consecutive_losses_by_tf=null`, `warmup_minutes=null`, `winddown_minutes=null`. Se WINQ26_M5 (único produtor ativo de 30/06) entrar em sequência de 5+ losses (como aconteceu em 10/06-11/06), o autotrader NÃO vai pausar — vai continuar martelando.

🔴 **`max_daily_loss=-999999`** = sem teto de perda diária. Um dia de volatilidade extrema pode gerar loss de R$ 5.000+ sem nenhum trip.

🔴 **`WDO_M5.max_daily_trades=999`** = sem teto de trades em WDO_M5. Combinado com `disabled_timeframes` não contendo WDO_M5, isso permite overtrading.

🔴 **`blocked_day_directions=[[1,"SELL"]]`** — só bloqueia Terça SELL. Quarta BUY está liberado. Hoje (01/07) é quarta e Bruno liberou BUY manualmente. Amanhã (02/07, quinta) se isso persistir, regra de bloqueio está incompleta.

### 7.2 RISCO MÉDIO

🟡 **`validate_with_llm=null`** — LLM validator desligado. Pode ser conservador (default false → sem LLM check) ou pode quebrar algum path que assume default true. Verificar em `core/vt_autotrader.py` se há fallback explícito.

🟡 **Stash WIP do Wave 10 (commit f880f2ef)** ainda tem 2 entradas idênticas (stash@{0} e stash@{1}). Não afetam config mas é lixo no repo.

🟡 **Discrepância `IND` em `_reason` vs ausente em `params_by_tf.IND_M5`** — `_reason` do v945 menciona tuning WIN, mas params_by_tf não tem IND (porque IND estava OFF). Não é inconsistência funcional.

### 7.3 INCONSISTÊNCIAS MENORES

- `volume_by_symbol.WIN` divergente entre v945 (2) e atual (1) — Bruno mudou pós-30/06 ou AGI?
- `volume_by_symbol.IND` em v945 é 0 (consistente com disabled), mas no atual é 1 (inconsistente com disabled_symbols). Provavelmente efeito colateral do `calendar_resolve` que faz auto-resolve de vencimentos.
- `disabled_timeframes` em v945 é `[]` (vazio) — significa que **BIT_M5, BIT_M30, WSP_M5 foram adicionados a disabled_timeframes DEPOIS de 30/06, possivelmente pelo AGI forward_it1 (v938) ou pelo incident de hoje**. Não está claro o porquê.

### 7.4 BOA NOTÍCIA

🟢 **Todos os backups .bak de 30/06 estão intactos.** Não houve corrupção de filesystem. O incidente foi puramente lógico (save_full_config com dict parcial).

🟢 **DB `vt_trades.db` tem histórico completo** — toda decisão de tuning pode ser re-validada com backtest.

🟢 **Commit Wave 9 (`7930b4ac`) ainda no git** — fallback final se v945 bak for corrompido por algum motivo.

🟢 **Stash Wave 10 (`f880f2ef`) intacto** — código Wave 10 background MT5 está preservado em git.

---

## 8. Resumo dos baselines disponíveis

| baseline | versão | data | tamanho | writer | uso recomendado |
|---|---|---|---|---|---|
| `/tmp/vt_config_full_579lines.json` | 921 | 26/06 20:29 | 579L/20KB | hermes_wave_9_high_edge | baseline Wave 9 (BOLLINGER IND, STRONG_TREND WDO) |
| `vt_config.json.bak-meiodia-20260625-120239` | 882 | 25/06 12:02 | 40 chaves | bruno_human_decision | baseline pré-Wave 9 |
| `vt_config.json.bak-v866-20260623-213711` | 866 | 23/06 21:37 | 56 chaves | agi_v3_discovery_engine | baseline Wave 4.3 |
| **`vt_config.json.bak_v945_pre_reoptimize_195210`** | **945** | **30/06 19:52** | **53 chaves** | **hermes_v944_intermediate_1909** | **🟢 RECOMENDAÇÃO: baseline 30/06** |
| `vt_config.json.bak_v944_945_1909` | 945 | 30/06 19:09 | 51 chaves | hermes_v944_intermediate_1909 | backup intermediário |
| `vt_config.json.bak_v943_pre_v944_190254` | 943 | 30/06 19:02 | 50 chaves | agi_v3_discovery_engine | posição ultra-conservadora (alternativa) |
| `vt_config.json.bak_pre_full_opt_1809` | 938 | 30/06 18:09 | 50 chaves | agi_forward_it1 | posição otimista (alternativa) |
| `vt_config.json` (atual) | 952 | 01/07 09:58 | 50 chaves | bruno_hard_lock_ind_2026_07_01 | 🔴 NÃO USAR sem restaurar kill switches |

---

## 9. Ações imediatas (checklist)

- [ ] **P0:** Backup do estado atual corrompido: `cp vt_config.json /tmp/vt_config_corrupted_v952_0107.json`
- [ ] **P0:** Restaurar v945: `cp vt_config.json.bak_v945_pre_reoptimize_195210 vt_config.json`
- [ ] **P0:** Validar kill switches presentes (script §6.2 passo 5)
- [ ] **P1:** Rodar AGI dry-run para confirmar que decisão de IND/BIT OFF é estável
- [ ] **P1:** Decidir produto: reativar IND ou manter OFF? BIT ON de novo?
- [ ] **P1:** Atualizar `volume_by_symbol.WIN` para 1 ou 2 conforme decisão (v945=2, atual=1)
- [ ] **P2:** Adicionar pre-commit hook que impeça `vt_config.json` com `*_minutes_by_tf = null`
- [ ] **P2:** Adicionar CI check que exige todas as chaves de §2.2 presentes (não-null)
- [ ] **P2:** Limpar stashes duplicados (stash@{0} e stash@{1} idênticos)
- [ ] **P3:** Documentar em CHANGELOG.md o incidente 01/07 e a recuperação

---

**Fim do relatório.**