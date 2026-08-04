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
8. **Stage 6** (report) — Telegram + audit JSON. Sempre roda.

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
