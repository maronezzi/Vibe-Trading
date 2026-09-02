# GO/NO-GO — Migração para conta REAL (plano selado 01/09 · avaliação 02/09 · alvo: quinta 03/09)

> **Regra de ouro: o critério do GO NUNCA é lucro — é execução limpa.**
> 05/08 provou no prejuízo (−36,9% sem edge, por falha de execução) e 01/09 provou
> no ganho: PnL não mede fidelidade. Dia vermelho pequeno com execução limpa avança
> o GO; dia verde com execução suja atrasa.

---

## 1. Estado verificado em 01/09 (pronto de verdade)

| Camada | Estado |
|---|---|
| Gestão/decisão (daemon) | ✅ C1–C6 de 05/08 corrigidos; gate do lock respeita a flag (Wave 889); SL alinhado à grade do tick; kill switch −500; guard de conta (`ALLOWED_ACCOUNT_LOGINS`) |
| Calibração com dados reais | ✅ `trade_stops_level`/`freeze` medidos na XPMT5-PRD ao vivo (= 0); grades de tick (WIN 5, BIT 20, WDO 0.5, WSP 0.25); multiplicadores broker-truth no walker (Wave 888); taxas modeladas R$0,50/perna |
| Observabilidade | ✅ Journal modo conta-real (`data/forward_journal/`), pareamento sim↔live por sinal, seção 9 do fw-report (auditoria LLM diária) |
| Ferramental do dia D | ✅ `scripts/read_stop_levels.py`, `scripts/toggle_profit_lock.py`, roteiro de troca de conta |
| Correções 31/08–01/09 | Waves 885–890: relógio do walker, run_id/dedupe, journal, modo conta-real, multiplicador, BE/hard-exit espelhando o daemon |

## 2. Scorecard do dia 02/09 (5 critérios — preencher no fechamento)

| # | Critério | Como medir | Resultado |
|---|---|---|---|
| 1 | Zero sintomas de 05/08 | log: `INVALID_STOPS`, `Invalid volume`, storm de modifies = 0 | ☐ |
| 2 | Caso WIN validado | sims de WIN contam a mesma história do live (seguram SL quando o live segura; sem arranhão de +5pts aos 3min) | ☐ |
| 3 | Paridade sim↔live | mediana \|Δ\| por par ≤ ~2R; nenhum outlier não explicado | ☐ |
| 4 | Livros coerentes | \|gap tabela↔broker\| < ~R$5 (01/09 deu ~16 — auditar causa: netting split) | ☐ |
| 5 | Trava conforme v1337 | arm/floor/bloqueio exatamente pelos parâmetros (ativação 0,50, target 100) | ☐ |

**GO = 5/5 amanhã + 5/5 no dia anterior de referência (01/09 parcial: critérios 1 e 5 ✅, 2–4 pendentes do fix de hoje).**
Com 5/5 em 02/09 → recomendação formal de GO para **quinta 03/09**.

## 3. Registro da linha de base — 01/09 (leitura corrigida pelo Bruno)

- **Sessão oficial (trava ON, 09:00–11:36): +R$78,28 — 6º dia verde consecutivo.**
- Tarde: **experimento controlado** (trava OFF, autorização Bruno) — −R$89,48 de custo de
  teste que **comprou**: 2 bugs de fidelidade do walker mortos (Wave 890 — BE sem condição
  de lucro arranhava todo WIN a +5pts; HARD_EXIT incondicional), validação das proteções
  sem trava (16 trades extras, zero falha de execução, um SL cheio de −108 absorvido).
- Broker-truth do dia: +R$4,73 (plano). A tarde é custo de teste, não performance do sistema.
- 27 trades live; 31 sims dedup; 17 pares sim↔live casados (mediana Δ +2,60; escalas corrigidas).

## 4. Decisões pendentes do Bruno (antes da abertura de 02/09)

1. **Trava**: rodar v1337 (ativação 0,50 / target 100 — estado atual, sintonia do calibrador)
   **ou** voltar a 0,70 com limite no risk_calibrator para ele não reverter. Se optar por 0,70,
   avisar antes da abertura.
2. **Kill switch da semana real em R$** (sugestão: 5–10% do capital da conta real; aplicar via
   save_params no dia D).
3. (Opcional) Taxas oficiais com a XP — o journal já modela R$0,50/perna.

## 5. Dia D — checklist (15 min, alvo quinta 03/09 pré-abertura)

1. Login da conta REAL 2257579 no MT5 (senha do Bruno).
2. `python3 scripts/read_stop_levels.py` → conferir stops/freeze ao vivo → ajustar
   `stop_level_sim_pts` se divergir do medido em 31/08.
3. `ALLOWED_ACCOUNT_LOGINS += 2257579` (core/vt_autotrader.py — deliberado, com commit).
4. Kill switch da semana em R$ via save_params.
5. Pre-flight 08:55 + confirmação de execução na primeira entrada.

## 6. Critérios de aborto na semana real (LLM intradiário + automáticos)

- `INVALID_STOPS`/`Invalid volume` repetidos em janela curta → **pausa + alerta** (foi o vetor do 05/08).
- Perda diária ≥ kill switch → stop automático (já implementado, com fallback broker-truth).
- Divergência de execução sim↔live acima do ruído → alerta Telegram em minutos.
- Qualquer sintoma de 05/08 → pausa e post-mortem antes de retomar.

## 7. Diário de validação

- **01/09** (parcial — fix do caso WIN aplicado no fim do dia): critérios 1 e 5 ✅; 2–4
  revalidam em 02/09 com o walker corrigido. Verde oficial +78,28 (6º seguido).
- **02/09**: ☐ preencher no fechamento → decisão GO para 03/09.
- **03/09+ (se GO)**: semana real com monitor intradiário; PnL da real será inferior ao demo
  por construção (taxas + slippage) — divergência de DECISÃO é o que dispara alerta.

## 8. Incidente 02/09 10:19 — EMERGENCY CLOSE por consolidação NETTING (fechado, custo R$5)

**Sequência:** WSP M15+M30 SELL no mesmo símbolo → NETTING consolidou a M15 (child
2517895574) na M30 (pai 2517822277) em ~1min → modify de SL no ticket filho falhou
`POSITION_NOT_FOUND` (o filho não existe mais) → recovery abortou corretamente (1
tentativa, sem storm) → PnL do filho = None → **safety-first tratou como contra e
fechou a exposição netted inteira: −R$5,00 broker-truth** (era o risco real vivo).

**O que funcionou:** a camada safety-first agiu defensiva e corretamente (fechou −5
em vez de deixar risco sem visibilidade); o fix Wave 885/889 fez a PRIMEIRA salvada
em produção ao vivo — registrou o fechamento do PAI por history por-ticket com
broker-truth em tempo real. Dano total: R$5.

**Gaps identificados (Wave 891, pré-abertura):**
1. Modify não adota o ticket do PAI quando o filho foi consolidado (o monitor adota;
   o path de modify não) → tighten útil pode ser perdido.
2. PnL None → "contra" fecha a posição NETTED inteira, incluindo o trade de OUTRA
   estratégia (hoje custou −5; se o pai estivesse +200, fecharia um vencedor alheio).
3. Wording do alerta não diz a causa real (consolidação netting) — assusta sem informar.

**Impacto GO:** critério 1 de 02/09 ganha asterisco (novo sintoma da família NETTING,
custo R$5, safety-first funcionou). Wave 891 aplica o fix pré-abertura de 03/09;
pre-flight valida. GO mantido sob esses termos.
