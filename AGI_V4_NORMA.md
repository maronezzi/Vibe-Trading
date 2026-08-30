# AGI V4 — Norma de Operação e Intervenção

> **Documento permanente (rastreado no git).** Memória institucional do estado
> do AGI v4 após a revisão de 01/08/2026. Consulte este doc quando o AGI der erro,
> regressão, ou antes de mexer no pipeline de otimização. Mantenha atualizado a
> cada mudança material.
>
> **Mantenedor:** Bruno Maronezzi. Estilo PT-BR. Edite só com contexto live-system.

---

## 1. Propósito

O AGI v4 é o otimizador autônomo do autotrader B3. Roda via cron (12:00 e 17:10,
seg–sex), testa todas as estratégias disponíveis contra cada par SYM_TF, otimiza
parâmetros, gera estratégias novas via LLM quando precisa, e **decide sozinho**
quais pares operam (soberania simétrica: ativa lucrativos, desativa failing).

Este doc documenta: (a) o estado operacional atual, (b) os bugs corrigidos em
01/08 (para diagnóstico de regressão), (c) os invariantes que NÃO podem ser
quebrados, (d) como intervir quando algo quebra.

---

## 2. Estado operacional atual (snapshot 01/08/2026, config v1166)

**13 pares ativos lucrativos** (P&L simulado 30d, mult B3 corrigido):

| Índice | M5 | M15 | M30 | H1 | Total |
|---|---|---|---|---|---|
| WIN | SMART_EMA +R$5.535 | HTF_BIAS_LTF_ENTRY +R$7.260 | HTF_BIAS_LTF_ENTRY +R$8.667 | 🔒 failing | +R$20.462 |
| BIT | RANGE_TRADING +R$135 | RANGE_TRADING +R$180 | BOLLINGER +R$50 | 🔒 failing | +R$365 |
| WSP | BOLLINGER +R$1.082 | ADX_TREND +R$2.633 | EMA_PULLBACK +R$3.654 | EMA_PULLBACK +R$1.490 | +R$8.859 |
| WDO | ADX_TREND +R$585 | EMA_PULLBACK +R$261 | ADX_TREND +R$145 | 🔒 failing | +R$925 |
| **Total** | +R$7.337 | +R$10.334 | +R$12.516 | +R$678 | **+R$30.611** |

**3 pares H1 bloqueados** (AGI desativou — sem edge): `WIN_H1`, `BIT_H1`, `WDO_H1`.
Estão em `disabled_timeframes` e `day_trade_intent=false`. O Stage 4 tenta gerar
estratégias para eles a cada run; se um dia achar edge, o AGI o reativa sozinho.

---

## 3. Bugs corrigidos em 01/08/2026 (diagnóstico de regressão)

Se um destes sintomas reaparecer, a causa é a regressão correspondente.

### Bug 1 — Stage 4 gera código com TypeError (`'float' object is not subscriptable`)
- **Causa-raiz**: prompt do Stage 4 listava assinaturas das funções `utils` mas
  omitia os tipos de retorno. A LLM alucinava séries (`rsi_values[-1]`) quando
  `calculate_rsi` retorna `float` escalar.
- **Correção**: `optimization/agi_v4/stage4_generate.py` — prompt agora especifica
  tipos de retorno completos + exemplo canônico + regra "NUNCA indexe com [-1]".
- **Defesa**: `_runtime_smoke_gate` executa `check_entry` com barras sintéticas
  entre `ast_gate` e backtest. Captura TypeError de runtime com mensagem real.

### Bug 2 — Stage 3 não testava as 43 estratégias (pares "travados")
- **Causa-raiz**: `consecutive_rejects` (limite 50) era inicializado FORA do
  loop de estratégias e só resetava ao achar aprovado. Como `ALL_STRATEGIES` é
  alfabético (ADX_TREND primeiro), uma estratégia ruim saturava o contador e as
  outras 42 testavam só 1 combo cada.
- **Correção**: `optimization/agi_v4/stage3_exhaustive.py` — reset por estratégia
  + `MAX_ATTEMPTS` 300→600.

### Bug 3 — WDO/WSP com `mult` errado (PF=0 garantido para TODAS as estratégias)
- **Causa-raiz**: `CONTRACT_SPECS` do WDO era `mult=0.0015` (erro 6667×) e o WSP
  era cópia do BIT (`mult=0.01`, erro 1350×). A comissão fixa de R$1,20 superava
  qualquer lucro possível.
- **Correção**: `backtest/backtest_v944.py` `CONTRACT_SPECS` calibrado pela B3:
  - WDO: `mult=10.0` (R$10/pt, [fonte B3](https://borainvestir.b3.com.br/glossario/mini-dolar/))
  - WSP: `mult=13.5` (USD 2,50/pt × câmbio 5,4, [fonte B3](https://www.b3.com.br/en_us/products-and-services/trading/equities/micro-s-p-500-futures-contract.htm))
- **Validação**: WDO_M5+ADX_TREND passou de PF=0/−R$70 para PF=1.36/+R$585.

### Bug 4 — AGI buscava contrato resolvido (sem histórico pós-rolagem)
- **Causa-raiz**: `backtest_evaluator._fetch_30d_bars` pedia `BITQ26` (resolvido)
  em vez de `BIT$` (perpétua). Pós-rolagem, BITQ26 tinha 93 barras M15 vs 2501 do BIT$.
- **Correção**: `optimization/agi_v4/backtest_evaluator.py:131-137` — usa
  `f"{sym_root}$"` (perpétua). Path LIVE (vt_autotrader) continua com resolvido.

### Bug 5 — Stage 4 morria em timeout de 59s
- **Causa-raiz**: qwen leva 40–70s para gerar código, mas o budget era 60s.
- **Correção**: `core/vt_hermes_helper.py` `_ASK_LLM_PROVIDERS` qwen 60→180s;
  Stage 4 budget 60→200s; Stage 2 budget 45→120s. validator_v2 (live) tem
  implementação própria, não afetada.

### Bug 6 — AGI não decidia entra/sai (pares otimizados ficavam bloqueados)
- **Causa-raiz**: Lei 2 antiga — "AGI só pausa, unpause é humano". Pares
  lucrativos otimizados permaneciam em `disabled_timeframes`.
- **Correção**: soberania simétrica no Stage 5 (ver seção 4 abaixo).

---

## 4. Soberania simétrica do AGI (invariante central)

> **Bruno 01/08:** "se ele decidir que deve entrar deve entrar, se deve sair deve
> sair, a cada iteração do AGI ele decide o que fazer."

O AGI decide autônomo, a cada iteração:

| Condição (sim 30d) | Decisão do AGI |
|---|---|
| Par lucrativo (PnL > 0) E bloqueado | 🔓 **REATIVA**: remove `disabled_timeframes`, liga `day_trade_intent` (sempre, desde a iteração 1) |
| Par failing (PnL ≤ 0) E ativo E **≥5 tentativas esgotadas** | 🔒 **DESATIVA**: adiciona `disabled_timeframes`, desliga `day_trade_intent` |
| Par failing (PnL ≤ 0) E ativo E **<5 tentativas** | ⏳ **MANTÉM ATIVO**: AGI continua otimizando (busca + geração) |
| Par lucrativo E já ativo | mantém (nada a fazer) |
| Par failing E já bloqueado | mantém (nada a fazer) |

> **Wave 882 (Bruno 04/08):** a desativação só ocorre após o AGI tentar
> otimizar pelo menos `VT_AGI_MIN_ITERS_BEFORE_DEACTIVATE` (default 5) vezes,
> OU quando o loop de convergência se esgota (`ctx["_loop_exhausted"]=True`).
> Antes deste gate, o AGI desabilitava pares failing já na iteração 1 com
> base num PnL≤0 instantâneo instável — incidente 12h42 de 04/08 desabilitou
> 13 pares que eram lucrativos em re-simulação. A reativação (lado entrada)
> permanece sem gate: um par lucrativo nunca deve ficar bloqueado.

**Implementação** (não regredir):
- `stage1_collect._identify_failing_simulated` popula `ctx["profitable_pairs"]`
  (lista de str) e `ctx["failing_pairs"]` (pode ser str OU dict).
- `stage5_apply._reactivate_profitable_pairs` e `_deactivate_failing_pairs` são
  os dois lados. Ambos persistem via `save_full_config` (Stage 5 é o ÚNICO
  writer autorizado do AGI v4 — ver `core/vt_config_loader.py:154`).
- `_deactivate_failing_pairs` NORMALIZA failing_pairs (str|dict) antes de iterar
  (bug do `unhashable dict` corrigido — `ctx["failing_pairs"]` vem como
  `list[dict]` do `_check_convergence_simulated`).
- `guardrails.classify_disabled_timeframes_change`: AGI pode pausar E despausar
  (era só pausar). `day_trade_intent` adicionado a `SAFE_WRITE_TARGETS`.

---

## 5. Pipeline e freios (não regredir para timeouts curtos)

Ordem dos stages no loop de convergência (`optimization/agi_v4/pipeline.py`):
1. **Stage 1** (collect) — simula baselines, identifica failing/profitable.
2. **Stage 2** (intel) — web search + LLM gera hipóteses.
3. **Stage 3** (search) — busca exaustiva: 43 estratégias × ~14 params cada.
4. **Stage 5** (apply) — aplica candidatos + **soberania simétrica**.
5. **Convergência** — re-simula todos os pares.
6. **Stage 4** (generate) — gera estratégias novas via LLM para pares ainda failing.
7. **Stage 5** novamente.
8. **Pós-loop**: sweep _pending → tune incumbentes → **risk_calibrator** →
   **backfill_intel** (Wave AGI-backfill, 16/08 — ver seção 11).
9. **Stage 6** (report) — Telegram + audit JSON. Sempre roda.

**Freios (configuráveis via env var):**
- `VT_AGI_DEADLINE_MINS` (default 480 = 8h) — deadline hard. Bruno: "tempo não é
  problema". O AGI roda às 17:10 com a madrugada toda.
- `VT_AGI_MAX_STAGNATION` (default 3) — iterações sem progresso antes de parar.
- `VT_AGI_MAX_ATTEMPTS` (default 600) — combos por par no Stage 3.
- `VT_AGI_MIN_ITERS_BEFORE_DEACTIVATE` (default 5) — Wave 882 (Bruno 04/08):
  mínimo de iterações de otimização antes de o AGI poder desativar um par
  failing. Antes disto, pares failing permanecem ativos enquanto o AGI tenta
  otimizar. Ver seção 4.

**NÃO retorne estes para os valores antigos** (90min/2/300) — cortavam o AGI
antes de testar as 43 estratégias e gerar código.

---

## 6. Cron 12:00 + 17:10 (referência)

- Wrapper: `scripts/run_agi_v4_cron.sh` — snapshot do config + run async (nohup).
- Crontab: `10 17 * * 1-5` e `00 12 * * 1-5` (12:00 e 17:10, seg–sex).
- Usa `.venv/bin/python3` (tem pandas etc.) + `export PYTHONPATH`.
- **Wave 882 (Bruno 04/08):** ambos fazem a MESMA lógica (iterar, otimizar,
  reativar/desativar). A única diferença é operacional — o wrapper detecta
  o horário:
  - **12h** (pregão aberto): `VT_MAX_WORKERS=1`, `VT_AGI_DEADLINE_MINS=120`
    (CPU limitada para não atrapalhar o autotrader ao vivo).
  - **17h10** (pós-close 16:45): sem limite de workers, deadline 8h
    (madrugada toda para testar exaustivamente).
- Args: `--days 7 --mode auto` (max-iterations default 1000, freios naturais).
- Logs: `/tmp/vt_agi_v4_<TS>.log` + symlink `/tmp/vt_agi_v4_latest.log`.
- Lock anti-colisão: `/tmp/vt_agi_v4.lock` (fcntl no runner) + `.pid` no wrapper.

---

## 7. Notificação Hermes (como funciona e como depurar)

- Transport: `core/vt_hermes_helper.hermes_send` → subprocess `hermes send`.
- `find_hermes()` procura `~/.local/bin/hermes`, `~/.hermes/.../hermes`, `which`.
- **Chunking**: mensagens >4096 chars viram N chunks prefixados "[N/M]".
- **Stderr logado**: se um chunk falha, o stderr vai pro log do caller (logger
  `vt_hermes`). Antes era 100% silencioso.
- **Stage 6 retry**: 3 tentativas com 10s de backoff. Antes, uma falha de rede
  silenciava a notificação da noite.
- **Mensagem** (`stage6_report._build_telegram_message`): mostra decisões
  soberanas ("🔓 REATIVOU" / "🔒 DESATIVOU"), PnL por símbolo, shadow do pregão,
  mudanças aplicadas/rejeitadas, failing pairs.

**Depuração se Telegram não chegar:**
1. Verifique `/tmp/vt_ask_llm.log` e o log da run (`/tmp/vt_agi_v4_latest.log`)
   por linhas `vt_hermes WARNING`.
2. Teste manual: `python3 -c "from core.vt_hermes_helper import hermes_send; print(hermes_send('telegram:-1004284773048:1', 'teste'))"`.
3. Confirme que `find_hermes()` acha o binário.

---

## 8. Como intervir (passos quando algo quebra)

### Sintoma: "todas as estratégias falham com PF=0 num símbolo"
→ **Bug 3 (mult errado)**. Verifique `CONTRACT_SPECS` em
`backtest/backtest_v944.py`. Valide o `mult` contra a especificação B3 do ativo.
Custo fixo R$1,20 + slippage deve ser < que lucro médio de 1 trade.

### Sintoma: "par X está travado na estratégia Y, AGI nunca troca"
→ **Bug 2 (early-stop)**. Verifique que `consecutive_rejects` reseta POR
estratégia em `stage3_exhaustive._search_pair_worker` (dentro do loop externo).

### Sintoma: "estratégia gerada dá TypeError em runtime"
→ **Bug 1 (contrato)**. Verifique que o prompt do Stage 4 especifica tipos de
retorno e que `_runtime_smoke_gate` está entre `ast_gate` e `_simulate_generated`.

### Sintoma: "BIT/WSP/WDO aparece como 'sem barras MT5'"
→ **Bug 4 (símbolo)**. Verifique que `_fetch_30d_bars` usa `f"{sym_root}$"`
(perpétua), não `resolved_symbols`.

### Sintoma: "Stage 4 nunca gera código (timeout)"
→ **Bug 5 (LLM timeout)**. Verifique `_ASK_LLM_PROVIDERS` (qwen ≥180s) e
`ask_llm(prompt, timeout=200)` no Stage 4.

### Sintoma: "par lucrativo não opera / par failing continua operando"
→ **Bug 6 (soberania)**. Verifique `_reactivate_profitable_pairs` e
`_deactivate_failing_pairs` no Stage 5, e que `ctx["profitable_pairs"]` /
`ctx["failing_pairs"]` são populados pelo Stage 1.

### Erro: `NameError: name 'os' is not defined` no pipeline
→ O `import os as _os` é LOCAL em `pipeline.run()`. Use `_os.environ`, nunca
`os.environ` nesse escopo (o módulo não importa `os` no topo).

### Erro: `TypeError: unhashable type: 'dict'` no Stage 5
→ `ctx["failing_pairs"]` vem como `list[dict]` do `_check_convergence_simulated`.
`_deactivate_failing_pairs` deve normalizar (str|dict → str) antes de iterar.

---

## 9. Pontos de atenção conhecidos (não são bugs, mas monitorar)

1. **WSP tem câmbio embutido no `mult`** (USD 2,50 × 5,4 = R$13,50). Se o dólar
   mudar significativamente, o `mult` do WSP precisa recalibração. WIN/WDO/BIT
   são em R$ direto (não afetados por câmbio).

2. **Os 3 H1 failing** (WIN_H1, BIT_H1, WDO_H1) têm poucos trades (3–8 em 30d).
   O Stage 4 tenta gerar estratégias a cada run mas nenhuma teve edge até agora.
   Se um dia o AGI achar edge, reativa sozinho. Se quiser forçar, aumente
   `VT_AGI_MAX_STAGNATION` para dar mais iterações.

3. **Comissão fixa R$1,20/trade** (`backtest_v944.py:322`). Se a corretora mudar
   a tabela de custos, recalibre este valor junto com `slip_r`.

4. **`_notes` stale no config** diz "WDOU26 → WDOQ26" mas resolved é WDOU26.
   É só comentário (não afeta operação). Não corrigir via write direto (violaria
   `ALLOWED_WRITERS`).

5. **`htf_ema_pullback_tight.py:102,110`** chama `calc_sl` com assinatura errada
   (`price, atr, mult` em vez de `symbol, atr, params`). Não está mapeada em
   `strategy_by_tf` (inofensiva), mas se for reativada, quebra no SL. Corrigir
   antes de mapear.

---

## 10. Snapshot dos commits de referência (01/08/2026)

Os 14 commits desta revisão (em ordem cronológica):

```
bfce4e1a Wave 1110.C — profit lock verificado no startup do daemon
bc7dbb43 Wave rolagem/TFs — BITQ26 + WIN_M30/H1 reativados + writers
1630ad08 Wave fix-contract — Stage 4 gera código que executa + bug visível
75fc4ebc Wave noturno-generoso — timeouts do LLM sobem p/ a madrugada toda
9b177d61 Wave limpeza-sandbox — remove estratégias _pending com bug de contrato
bbbd403f Wave perpétua+stage3-justo — AGI usa WIN$/WDO$/BIT$/WSP$ + 43 estratégias
d35fb3ad Wave custo-real — CONTRACT_SPECS WDO/WSP calibrados pela B3
1cb7806b Wave AGI-soberano — reativa pares lucrativos bloqueados
91f68c7e Wave soberania-simétrica — desativa pares failing ativos
a8ad5e31 Wave config-soberano — v1166: 13 ativos, 3 H1 bloqueados
afea33c0 Wave noturno-robusto — deadline 8h + notificação Hermes confiável
a1aef10c Wave limpeza-sandbox — remove última _pending com bug
413a8b91 fix: NameError os→_os no pipeline
59cd6b31 fix: TypeError unhashable dict no _deactivate_failing_pairs
```

Use `git show <hash>` para ver o diff de qualquer correção.

---

## 11. backfill_intel — calibração autônoma de sessão por replay (16/08/2026)

**O que é:** fase pós-loop (`optimization/agi_v4/backfill_intel.py`) que fecha o ciclo
da "história melhor". O forward_walker ganhou modo `--backfill` (replay histórico com
a semântica EXATA do daemon: mesma check_entry, gestão TP1/breakeven/trailing/hard/
time, EOD na virada de data, gate `aggregate_blackout`). O backfill_intel usa esse
replay para o AGI decidir sozinho filtros de horário (`time_blocks`).

**Ciclo autônomo (a cada cron 17h10):**
1. Replay baseline da janela rolante (default 30d, `VT_BACKFILL_INTEL_DAYS`) com o
   config atual → tabela isolada `forward_backfill_trades` (o stage6 shadow NÃO lê).
2. Analisa (root × hora) na MESMA escala do gate do daemon (hora do ts da barra —
   hoje 06h renderizado ≈ 09h BRT de abertura; consistente por construção).
3. Só nasce candidato de hora NEGATIVA (PnL < 0, n ≥ 12) — horas positivas nunca
   são tocadas (anti-overfit).
4. Contrafactual: re-replay da mesma janela com o bloco da hipótese IN-MEMORY
   (`--config-override` do walker; config em disco intocado).
5. Aplica via `save_full_config(updated_by="agi_v4_backfill_intel")` só se:
   ΔPnL ≥ R$20, ≥ 10 dias de evidência, bloqueio cortou trades, e não sobrepõe
   bloco manual (manual sempre vence). Blocks próprios são marcados
   `reason="agi_backfill: ..."` — churn controlado.

**STATUS (Bruno 16/08): AUTO-APPLY DESATIVADO — modo análise-only.** Walk-forward
out-of-sample (descoberta mai-jul vs validação cega de agosto) deu INCONCLUSIVO:
prometia +R$87, entregou +R$7 sem estragar nada. O default do código é OFF —
nenhum run do AGI (cron ou manual) aplica blocks. Reativar quando houver
evidência melhor: `export VT_BACKFILL_INTEL=1` no wrapper do cron (linha
comentada em `scripts/run_agi_v4_cron.sh`). Análise manual segue disponível:
`python3 optimization/agi_v4/backfill_intel.py` (sempre dry-run) ou o walker
direto (`--backfill`, `--config-override`).

**Guardas:** só roda pós-close (≥ 17h ou fim de semana — o do meio-dia pula;
o walker recusa dia útil 08–17h para não colidir com o cron 09:01). Auto-apply
é OPT-IN (`VT_BACKFILL_INTEL=1`; default OFF — probação). Fail-safe: erro aqui
NUNCA derruba o pipeline.

**Invariantes (não quebrar):**
- Backfill NUNCA escreve em `forward_sim_trades` (sinal shadow do meio-dia é só
  do pregão ao vivo) nem em `trades`.
- Cenários A/B só via override in-memory; escrever config live direto de
  "experimento" é proibido — só o passo 5 (com evidência) escreve, pelo writer
  autorizado.
- Uso manual: `python3 optimization/forward_walker.py --backfill --from ...`
  fora do pregão; `run_id` distinto por cenário; A/B compara por run_id.
- Limitação conhecida (v1): replay é in-sample p/ params de estratégia (o AGI os
  afinou no mesmo período) — por isso valida GESTÃO e FILTROS, não estratégia.
  Multiplier do walker (0.20) é uniforme p/ todos os símbolos (escala relativa).

---

## 12. Gates de não-regressão + profit lock variável (Wave 880.I, 19/08/2026)

**Diretiva Bruno 19/08:** "o AGI não pode piorar; precisa ter todas as super
informações e garantir um sistema ideal". Porta para DENTRO do pipeline a
régua validada no apply noturno W880 (18/08 — `scripts/
w880_nightly_super_agi_apply_20260818.py`, que em seu 1º dia validou
forward +R$395/PF 2.42 e foi verde live +R$90.60).

**Incidentes que motivaram (18–19/08):**
1. WIN_M30 trocado pelo AGI 12h logo após fazer +R$57 LIVE de manhã —
   o pipeline não olhava histórico live multiday do par;
2. BIT_H1 ligado 18/08 12h → desligado 18/08 17h → ligado 19/08 17h —
   churn de soberania em U-turn sem evidência nova;
3. Trocas aplicadas com melhoria marginal de simulação (sem fator mínimo).

### Módulo novo: `optimization/agi_v4/non_regression.py`
- **Gate A (walk_forward):** consistência ≥ `VT_AGI_WF_MIN_CONSISTENCY`
  (0.75) E ≥3 janelas positivas (janela com <3 trades não é julgada).
  Régua do super_agi_v5 — MAIS dura que a do evaluator (0.65);
- **Gate B (fator):** baseline positivo exige `cand_score ≥ 1.3x baseline`
  (`VT_AGI_FACTOR`; regra Wave 877 "<30% não troca");
- **Gate C (live_winner):** par com ≥ R$100 live em 10 pregões (tabela
  `trades`, `VT_AGI_LIVE_WINNER_MIN`) exige fator **2.0x** E WF **100%**;
- **Gate D (churn):** par trocado em SESSÃO anterior há < `VT_AGI_CHURN_DAYS`
  (2d) exige evidência ≥ 2x a alegada na troca anterior. Iterações internas
  da mesma execução são isentas (session id em `ctx["_nr_session"]`);
- **Gate E (flip):** U-turn enable↔disable há < `VT_AGI_FLIP_DAYS` (5d) é
  suprimido na soberania (`_reactivate`/`_deactivate_failing_pairs`).

**Journal** `optimization/agi_v4/state/pair_change_journal.json`: toda
troca/enable/disable registra ts, from→to e PnL alegado. Primeira carga
AUTO-SEMEIA diffs dos snapshots `vt_config.json.snapshot_pre_cron_*`
(19/08: 116 eventos; BIT_H1 já nasce com o histórico do U-turn para o
gate E agir). Não deletar à mão — é a memória de churn do AGI.

### Wiring no Stage 5 (`stage5_apply.py`)
- `_apply_one`: gates A–D logo após o gate de baseline existente.
  **Fail-closed**: erro no módulo REJEITA o candidato (nunca aplica sem
  exame). Sucesso → `append_journal` (best-effort pós-escrita);
- `run()`: teto de `VT_AGI_MAX_SWAPS` (default **4**) trocas de
  estratégia/params por EXECUÇÃO (blast radius, régua W880). Rejeita com
  gate `max_swaps_run`. Soberania (enable/disable) NÃO conta no teto;
- Soberania: gate E fail-open (reativar lucrativo segue sempre possível —
  norma §4; desativar failing também — proteção de capital primeiro).

### Profit lock VARIÁVEL (`risk_calibrator.py`)
Problema: calibrar `profit_lock_min_target` só no live é **autocensurado**
— o lock corta os trades que provariam que um alvo maior renderia mais
(19/08: live travou +R$90 às 10:06 com target 100; shadow fez +R$395).
Agora:
1. `_load_shadow_trades` lê `forward_sim_trades` (walker NÃO arma o lock
   → sequência não-censurada);
2. `_merge_with_shadow` reescala shadow→live pela razão mediana
   live/shadow dos dias comparáveis (clamp [0.2, 2.0]) e RECONSTRÓI os
   dias censurados (pico live ≥ 0.95× target) com o shadow reescalado —
   contrafactual honesto em escala live;
3. Histerese: alvo novo clamped em [0.5x, 2x] do atual (variável sem
   salto) + `MIN_GAIN_R` de sempre.
Primeira calibração real (19/08): ótimo bruto 300, clamp leva 100→200
(ganho +R$84 na janela, ratio 0.613, 5 dias reconstruídos).

### Invariantes (não quebrar)
- Gates A–D são fail-closed POR CANDIDATO; gates de soberania são fail-open;
- O journal é acumulativo (append-only na prática); re-semar só se o arquivo
  sumir (auto-seed é idempotente por construção — diffs de snapshot);
- Live-winner usa a tabela `trades` (broker-truth do DB), nunca simulação;
- `VT_AGI_MAX_SWAPS=0` desliga o teto (não recomendado em produção);
- Testes: `tests/test_agi_v4_non_regression.py` (19 casos, herméticos).

---

## 13. Wave 880.II — netting-aware: exposição, SL e kill-switch live (26/08/2026)

**Incidentes 24–26/08 (motivação):**
1. **26/08 (o grave):** WDO M15+M30+H1 entraram SELL em WDOU26 em 9 min.
   A conta é netting → UMA posição de 4 contratos; o SL (last-writer-wins,
   terminou no do ÚLTIMO TF a entrar) fechou tudo: **-R$285 num único
   stop**, estourando o stop diário de WDO (-R$250) no primeiro trade do
   dia. Cada par "arriscava" ~R$50 sozinho.
2. O reconcile marcava as sub-entradas como GHOST (PnL 0) ENQUANTO a
   posição consolidada seguia viva — o que (a) distorcia toda estatística
   por par e (b) **sumia com o slot do state e derrotava o guard
   anti-duplicação** (por isso o M30 entrou 2×). A perda inteira caía
   numa linha só (close_source `RECONCILE_HISTORY`).
3. **Soberania cega ao live:** WDO_M15/ADX_TREND com sim 30d +R$450 e
   live **-R$337/14d** — nenhum mecanismo desativava; o gate `live_reality`
   congela MUDANÇAS no dia do sangramento, mas o par segue operando.

### Módulos novos (26/08)
- **`core/vt_risk_governor.py`** (puro) — governador de risco por
  símbolo-root: antes de enviar entrada, soma pior caso em aberto do root
  (posições do bot, `|entry−sl| × mult × vol`) + risco da nova entrada; se
  passar do orçamento (`|max_daily_loss_by_symbol[root]|` com buffer de
  slippage `execution_guards.risk_buffer`, default 25%), BLOQUEIA
  (`reason=RISK_BUDGET`, sinal vai pro `signal_blocked_log`). Entrada que
  REDUZ exposição líquida (hedge) é liberada. Posição sem SL = orçamento
  inteiro consumido. **Fail-open** (erro nunca segura entrada).
  Kill-switch: env `VT_RISK_GOVERNOR=0` ou
  `execution_guards.risk_budget_enabled=false`.
  Também exporta `should_restore_prev_sl` (tightest-SL-wins).
- **`core/vt_netting.py`** (puro) — split do PnL da posição consolidada:
  cada sub-entrada recebe `(preço_saída − sua_entrada) × vol × mult ×
  direção`; o "pai" (dono do ticket da posição) recebe o resíduo para
  Σ linhas == broker truth. Testes reproduzem o incidente (4 SELLs
  5149/5149.5/5150.5/5152.5 fechados @5157.5 = −85/−80/−70/−50).
- **`optimization/agi_v4/live_kill_switch.py`** (puro) — decisões do
  kill-switch live: **live_bleed** (n≥10 E PnL ≤ −R$200 em 10 pregões) e
  **live_churn** (n≥30 E PnL ≤ −R$20 — morte por comissão). Lê a tabela
  `trades` (sem GHOSTs, espelho do risk_calibrator). Env: `VT_AGI_LIVE_KILL`
  (default 1), `VT_AGI_LIVE_KILL_DAYS/_MIN_TRADES/_PNL`,
  `VT_AGI_LIVE_CHURN_MIN_TRADES/_PNL`, `VT_AGI_LIVE_QUARANTINE_DAYS`.

### Wiring (não regredir)
- `_execute_entry` (vt_autotrader): volume resolvido ANTES do governador;
  após FILLED, se a entrada LARGOU o SL da posição (netting
  last-writer-wins), o SL anterior mais apertado é restaurado via
  `safe_modify_sl_with_emergency_close` (só mesma direção).
- Reconcile (`reconcile_positions_with_mt5`): índices
  `_mt5_open_symbols`/`_mt5_pos_by_symbol`; **netting-hold** (contrato
  ainda aberto → sub-ticket segue aberto no state/DB com nota
  `NETTING_CHILD`; NUNCA mais GHOST em posição viva) e
  **netting-settle** (posição fechou + ≥2 slots no state do contrato →
  reparte PnL via `settle_netting_group`, close_source
  `RECONCILE_NETTING`/`_SPLIT`). Símbolo com 1 posição segue 100% no
  caminho legado. Erro no settle → cai no legado por ticket (fail-safe).
- Pipeline: `_run_live_kill_switch(ctx)` roda pós-loop (ambos os ramos),
  depois do risk_calibrator. O WRITE mora em
  `stage5_apply.live_kill_switch_pass` (stage5 continua o ÚNICO writer
  autorizado; `updated_by="agi_v4_stage5_live_kill"`).
- **Quarentena:** `_reactivate_profitable_pairs` suprime reativação de par
  com `kind="live_kill"` no journal há < `VT_AGI_LIVE_QUARANTINE_DAYS`
  (default 10d) — a sim que o live contradisse não religa sozinha.
  Fail-open (norma §4 preservada: lucrativo nunca fica bloqueado por BUG).
- Testes: `tests/test_wave880_2_risk_netting.py` (21 casos, herméticos —
  incidente reproduzido em 3 níveis: governador, split, kill-switch).

### Verificação com dado real (26/08)
`live_kill_switch.evaluate()` sobre o DB real (leitura): decidiria
**WDO_M15 live_bleed (-R$405, 11t/10d)** — dispara sozinho no cron 17:10.

### Invariantes (não quebrar)
- O governador e o kill-switch são FAIL-OPEN: erro interno deles NUNCA
  segura entrada legítima nem derruba o pipeline;
- kill-switch NÃO é treino com trades passados (house rule intacta): é
  gestão de risco — mesmo princípio do risk_calibrator que já lia `trades`;
- netting-settle nunca reabre linha fechada (`WHERE exit_time IS NULL` em
  todo UPDATE) e Σ das linhas == broker truth;
- `close_source` novos: `RECONCILE_NETTING` (pai) e
  `RECONCILE_NETTING_SPLIT` (filhos) — não alterar os existentes.

---

## 14. Wave 883 — saúde do LLM, scorecard de trocas, trava sintonizada e guarda de sinal (29–30/08/2026)

Origem: auditoria multi-agente (4 agentes, somente leitura) das operações
14–28/08, das entradas/lucro e do AGI v4. Decisões do Bruno em 29/08.

### O que a auditoria achou (contexto das mudanças)
- Execução de ordens SÓLIDA (Lei 3/4 ok nas 162 operações); camada de
  registro frágil (crash do reconcile 27–28/08 corrigido em hotfix P0).
- Stages 2/4 (LLM) mortos ≥5 dias SEM alerta — o "AGI" virou só busca em
  grade; 10 runs seguidos `applied=0` com ~140 mil simulações sem info nova.
- `pnl_claimed` dos swaps NUNCA era cobrado (57 swaps no journal); live
  -R$211/14d enquanto todas as sims 30d positivas; shadow -R$6.083.
- Entradas tardias em M30/H1 (H1 p50 = 22min dentro da barra seguinte);
  71 sinais descartados/15d pelo SLIPPAGE-GUARD por preço andado.
- Trava de lucro assimétrica: 5/10 dias verdes pararam entre 10h–13h.

### Mudanças (Bruno: "a LLM atual fica; adaptar o sistema a funcionar COM
ou SEM ela"; "a trava fica, mas o AGI sintoniza — número sem chute")
1. **Saúde do LLM** (`de53fe61`): validator_v2 com circuit-breaker por
   modelo (2 falhas → cooldown 10min; cascata pula sem custo de timeout —
   cada entrada queimava 15-20s) e gate anti-resposta-erro (hermes CLI
   devolve rc=0 com erro no stdout; nunca mais vira "LLM OK ... HTTP 403").
   `ask_llm` grava `/tmp/vt_llm_health.json`; Stage 6 mostra banner 🔴 se
   cair 2+ vezes seguidas. Kill-switch não precisa: é observabilidade.
2. **swap_scorecard** (`014399b0`): conferidor de recibos — para cada swap
   do journal com ≥5 pregões, PnL entregue (live+shadow na janela) vs
   `pnl_claimed`. **MODO OBSERVAÇÃO**: só reporta (linha 📋 no Telegram +
   audit). Escalonamento de Gate B/quarentena automática fica para wave
   futura após ~2 semanas de dados. `VT_AGI_SCORECARD=0` desliga.
   House rule intacta: accountability de decisão já tomada, não treino.
3. **Trava sintonizada** (`4073984c`): `calibrate_lock_activation` no
   risk_calibrator — `trailing_activation_pct` por counterfactual 21d (grid
   0.4–1.0 do trailing_target_per_lot, mesmos dias-shadow do alvo, empate →
   trava cedo, histerese de passo [0.7×, 1.3×], ganho mínimo R$15/5d).
   Dry-run real (14d): ótimo 0.8, primeiro passo 0.5→0.7 (arma R$175 em
   vez de R$125). Aplicação automática no próximo run do calibrador.
4. **Guarda de sinal expirado** (`24b74727`): `core/vt_signal_guard.py` —
   M30/H1 com sinal visto depois de 25% da barra atual → entrada
   descartada com log `[SINAL-EXPIRADO]`. Escopo só M30/H1 (M5/M15
   seguem). `VT_SIGNAL_AGE_GUARD=0` desliga. EFEITO SÓ APÓS RESTART do
   daemon (cron 09:00).
5. **Quarentena manual BIT_M15** (`b5e59217`, config v1333): DIVERGENCE_RSI
   -17,3R/15d, WR 22%, perda média -1,92R. Script whitelisted
   `scripts/w883_quarantine_bit_m15_20260829.py` + journal `kind=live_kill`
   (`rule=manual_quarantine_883`) → 10d de quarentena contra reativação por
   sim (a sim 30d do par é POSITIVA — sem journal o AGI religaria).
   Executado e verificado em 30/08 (fora do pregão).

### Commits de referência da wave
`de53fe61` (LLM) · `014399b0` (scorecard) · `4073984c` (trava) ·
`24b74727` (sinal) · `b5e59217` (quarentena). Ondas de commit do drift
pré-existente: `275bd027` (W880.II core) · `81cac88a` (W880.I AGI) ·
`c79474f0` · `8a206e6e` · `f8f46564` · `3ab890ba` (lint).

### Wave 883.B5 (30/08): series_divergence por MOVIMENTO — WIN destravado
A comparação por NÍVEL blockava o WIN PARA SEMPRE: a perpétua costurada
carrega basis de rolagem estrutural (WIN$ 177.805 vs WINZ26 181.790 = Δ2,04%)
e o gate rejeitava candidato bom 5×/run (incidente real no run de 30/08:
WIN_H1/ADX_TREND +R$1.046 barrado 5×). Correção (Bruno aprovou): gate agora
compara RETORNO DIÁRIO mediano perpétua-vs-contrato — basis constante some
da conta. Calibração com 24d de dado real: legítimos ≤0,085%; ativo ERRADO
(WIN$×WDOU26) ≥0,694% → default `VT_AGI_SERIES_DIVERGENCE_PCT` 0.25% (3×
folga p/ cima, 2,8× p/ baixo), janela 900 barras M15 (~24d, ≥3 dias comuns).
Validado: WIN_M15/WIN_M30 `allow_changes=True`; freeze/grace do WDO intactos;
teste negativo (ativo errado) bloqueia. Nível (diff_pct) segue no audit
como diagnóstico, rotulado "basis (normal)". Testes:
`tests/test_rollover_series_returns.py` (5 casos herméticos).

### Dívidas conhecidas desta wave (não feitas — decidir depois)
- WSP cooldown herdado 1800s (chaves mortas `wsp_m30`/`wdo_m15` no config);
- volume 2 no WIN_M15 (governador comporta; precisa OK do Bruno);
- loop de convergência conta pares disabled como failing (desperdício);
- walker com multiplier uniforme 0.20 (WDO 50× fora de escala);
- `backtest/strategies/AGI4_BIT_202313.py` untracked (mirror stale).

### Aprendizados do gerador manual (Wave 883.B — 10 estratégias W883_*, 30/08)
Estudos feitos à mão (o que o Stage 4/LLM deveria saber quando voltar):
- **Barras do backtest NÃO têm "open" nem "time"** (só high/low/close/volume —
  `backtest_v944.py:557`); o live tem tudo. Plugin que usa `b["open"]` vive no
  live e morre no backtest como "0 trades" (KeyError engolido). Corpo de
  candle no plugin: `close[0] − close[1]`.
- **Barras do smoke-test não têm "volume"** — `calculate_vwap` no plugin
  precisa de try/except (senão o runtime_smoke_gate rejeita o arquivo).
- **`max_dd_ratio > 2.5` é o bloqueio dominante** de candidatos lucrativos
  (não é PF) — os params de gestão (max_consecutive_losses=3 + halt 60min +
  trail 1,2/0,3 + profit_lock_r 0,8) resolvem a maioria dos casos.
- **n_trades≥20 é a régua que mais derruba no limite** (16-19 trades) —
  1 param de frequência (adx_min, squeeze_atr) resolve sem degradar PF.
- Resultado: 5-7 combinações (estratégia × par) aprovadas nos gates COMPLETOS
  (destaques: W883_RSI_CROSS_DI WIN_M30 +R$1.245/PF 28 com adx_min=8;
  W883_SQUEEZE_BREAK WIN_M30 +R$997; DONCHIAN_COMPRESS WIN_M30 +R$815~917).
  Arquivos ficam em `_pending/` (sandbox, gitignored) — o sweep das 12h testa,
  afina params por par e promove pelos gates normais do Stage 5.
