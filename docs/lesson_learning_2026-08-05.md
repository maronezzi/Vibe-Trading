# LESSON LEARNING — 05/08/2026 · Primeiro dia em conta REAL (XPMT5-PRD 2257579)

> **Resultado: R$ 1.500,00 → R$ 947,12 (−R$ 552,88 · −36,9%)**
> 16 operações reais · **0 vencedoras (WR 0%)** · todas fechadas por perda
> Fonte: broker-truth (MT5 balance delta) + vt_trades.db + /tmp/vt_autotrader.log
> Conta: **2257579 / XPMT5-PRD / XP Investimentos** (1º dia de operação real)

---

## 1. TL;DR executivo (para o AGI de hoje)

O dia foi perdido **não por falta de edge, mas por 3 falhas operacionais encadeadas**:

1. **`Invalid stops` ×155** — o gerenciamento (breakeven/trailing) enviava SLs de **~5pts** do preço, que a conta REAL rejeita (stop level do broker). Na DEMO isso era aceito (stop level ~0). **Nenhum modify de SL funcionou o dia inteiro.**
2. **Trailing dispara na hora errada (bug)** — `profit_pts = best − entry_price` com `entry_price=0` no state em memória → lucro calculado = preço absoluto (ex: "Lucro: 179375 pts = 670x ATR") → ativa trailing/profit-lock imediatamente em toda posição, gerando a enxurrada de modifies que falhavam.
3. **TP1 fracionário inválido (`Invalid volume` ×98)** — `tp1_pct=0.5` com volume 1.0 → tenta fechar 0.5 contrato → B3 exige volume inteiro (volume_min=1.0) → **nunca houve take-profit parcial**. Nenhuma posição reduziu risco em lucro.

Reforços que agravaram:
4. **Recovery LLM quebrado ×106** — o fallback que consultaria o LLM para corrigir o modify falha sempre: `hermes` não está no PATH do daemon (`[Errno 2] No such file or directory: 'hermes'`).
5. **Fix padrão perigoso do recovery** — "SL 5pts → 800pts" e validator inflando SL de BIT para **141.857pts** (≈1.418 nativos = R$ 280 de risco) → SL que não protege nada.
6. **20 EMERGENCY CLOSE** — quando PnL virava negativo, o sistema tentava fechar manualmente; vários falharam (`No changes`) e a posição só fechou quando o preço bateu no SL original.

**Conclusão AGI: não otimizar parâmetros com dados de hoje — o dia é inválido como amostra de estratégia** (execução corrompida por stop level + bugs de gerenciamento). O correto é: corrigir o código de gerenciamento, replicar na demo o comportamento da real (stop level simulado), e só então rodar estudo.

---

## 2. Matriz de operações do dia (16 reais + 6 GHOST reconciliados)

Legenda: `sl` = SL registrado em unidades do executor (× point = distância em preço). GHOST = fechou com preço 0, reconciliado depois pelo PnL real do broker. `*` = horário do reconcile (não do fechamento real).

| # | Ticket | Hora | Par | TF | Dir | Estratégia | Entry | SL(sl_pts) | Exit | PnL R$ | Saída |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 5870047320 | 09:21 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179310 | 535 | 09:26 @179260 | **−9,00** | SL_SERVIDOR |
| 2 | 5870119080 | 09:31 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179487.5 | 530 | 06:32* @179270 | **−87,00** | GHOST→BROKER_CLOSE |
| 3 | 5870121712 | 09:31 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179480 | 395 | 09:33 @0 | 0 | GHOST |
| 4 | 5870122533 | 09:32 | WSPU26 | M15 | SELL | BOLLINGER | 7804.75 | 1300 | 07:01* @7804 | **−13,75** | GHOST→BROKER_CLOSE |
| 5 | 5870202106 | 09:46 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179530 | 560 | 10:01 @179285 | **−47,00** | SL_SERVIDOR |
| 6 | 5870298499 | 10:02 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179255 | 445 | 10:04 @179170 | **−38,00** | SL_SERVIDOR |
| 7 | 5870313880 | 10:03 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179635 | 635 | 10:05 @0 | 0 | GHOST |
| 8 | 5870475688 | 10:16 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179550 | 655 | 10:17 @179490 | **−10,00** | SL_SERVIDOR |
| 9 | 5870655834 | 10:30 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179550 | 465 | 10:34 @179450 | **−19,00** | SL_SERVIDOR |
| 10 | 5871038542 | 11:01 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179665 | 480 | 11:27 @179510 | **−32,00** | SL_SERVIDOR |
| 11 | 5871423031 | 11:31 | WINQ26 | M15 | SELL | HTF_BIAS_LTF_ENTRY | 179110 | 800 | 11:39 @179405 | **−56,50** | SL_SERVIDOR |
| 12 | 5871432170 | 11:31 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 179135 | 570 | 11:32 @179270 | **−29,50** | SL_SERVIDOR |
| 13 | 5871464573 | 11:34 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179190 | 580 | 11:35 @0 | 0 | GHOST |
| 14 | 5871623377 | 11:48 | WINQ26 | M15 | BUY | HTF_BIAS_LTF_ENTRY | 179520 | 800 | 09:26* @179165 | **−40,00** | GHOST→BROKER_CLOSE |
| 15 | 5871748501 | 12:01 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179200 | 560 | 12:02 @0 | 0 | GHOST |
| 16 | 5871757040 | 12:01 | WSPU26 | H1 | BUY | EMA_PULLBACK | 7790.75 | 2400 | 12:24 @7779.5 | **−1,31** | SL_SERVIDOR |
| 17 | 5871900653 | 12:17 | BITQ26 | M15 | SELL | BOLLINGER | 333620 | **141857** | 12:40 @334220 | **−7,20** | SL_SERVIDOR |
| 18 | 5871993047 | 12:27 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 179155 | 580 | 12:28 @179180 | **−6,20** | SL_SERVIDOR |
| 19 | 5872026450 | 12:31 | WINQ26 | M30 | BUY | HTF_BIAS_LTF_ENTRY | 179185 | 550 | 12:33 @0 | 0 | GHOST |
| 20 | 5872034269 | 12:32 | WSPU26 | M15 | BUY | BOLLINGER | 7778.75 | 1500 | 12:56 @7764.75 | **−1,34** | SL_SERVIDOR |
| 21 | 5872055459 | 12:35 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 179150 | 555 | 12:36 @0 | 0 | GHOST |
| 22 | 5872309533 | 13:01 | WINQ26 | M30 | SELL | HTF_BIAS_LTF_ENTRY | 178955 | 560 | 13:01 @178960 | **−2,20** | SL_SERVIDOR |

**Soma dos trades registrados: −R$ 400,00. Saldo broker-truth: −R$ 552,88** (diferença = taxas B3/emolumentos/corretagem não itemizadas por trade + fills reais dos GHOST).

---

## 3. Como foi operado hoje (o mecanismo, passo a passo)

1. **Abertura** — o sinal dispara (22 sinais; 20 em WINQ26, dominado por HTF_BIAS_LTF_ENTRY em M15/M30). SL de entrada calculado por `_calc_sl` (2×ATR no WIN ≈ 400-800 pts) e enviado com a ordem. **Até aqui, OK.**
2. **~1 min depois** — TP1 tenta fechar 0.5 contrato → **`Invalid volume`** (B3: volume inteiro). Falha em silêncio, "mantém estado". **Nunca houve take parcial.**
3. **Mesmo ciclo** — o trailing avalia a posição: como `entry_price` no state em memória estava **0** (state reconstruído vazio às 09:21:12, 2s antes do 1º trade), `profit_pts = best − 0 = preço` → **"Lucro: 179375 pts (670x ATR)"** → ativa profit-lock/breakeven **imediatamente**.
4. **Breakeven/profit-lock** calcula SL a **5pts** do preço atual → envia `MODIFY` → **conta REAL rejeita com `INVALID_STOPS`** (SL dentro do stop level do broker; a demo aceitava).
5. **Recovery** tenta 2-3x → consulta LLM → **`hermes` não está no PATH do daemon** → "LLM abortou modify: ?" → cai no **fix padrão: "SL 5pts → 800pts"** (SL largo que não protege; no BIT o validator inflou para **141.857pts**).
6. **Loop a cada minuto** — TP1 falha + modify falha + LLM falha, enquanto o PnL flutua.
7. **PnL vira negativo** → `EMERGENCY CLOSE` (3 tentativas de fechar manualmente) → alguns falham com **`No changes`** → a posição só fecha quando o preço bate no SL **original** (ou num breakeven que por acaso passou) → **todas as 16 saídas = perda** (−2,20 a −87,00).

**Resultado líquido:** o sistema operou o dia inteiro **sem TP1, sem trailing funcional, sem breakeven funcional, sem emergency close confiável** — só SL de entrada (2×ATR) como única proteção real. Em 22 sinais, 16 viraram trades e TODOS perderam (o range de ~179.000-179.800 do WINQ26 matou compras no topo e vendas no fundo).

---

## 4. Causas raiz (priorizadas)

| # | Causa | Evidência | Severidade |
|---|---|---|---|
| C1 | **Stop level da conta REAL > SLs apertados do gerenciamento** — breakeven/trailing envia SL ~5pts do preço; real rejeita (demo aceitava) | `INVALID_STOPS` ×155, todos os modifies falharam | 🔴 CRÍTICA |
| C2 | **`entry_price=0` no state em memória → trailing ativa com lucro falso** (preço absoluto) | "Lucro: 179375 pts (670.6x ATR)" no 1º trade | 🔴 CRÍTICA |
| C3 | **TP1 fracionário inválido** (`tp1_pct=0.5` × volume 1.0 = 0.5 contrato) | `Invalid volume` ×98 | 🔴 CRÍTICA |
| C4 | **Recovery LLM quebrado** — `hermes` fora do PATH do daemon | `[Errno 2] No such file or directory: 'hermes'` ×106 | 🟠 ALTA |
| C5 | **Fix padrão/validator inflam SL** (5→800pts; BIT 50.000→141.857pts) | Logs `Fix padrão` + `SL ajustado pré-envio` | 🟠 ALTA |
| C6 | **Emergency close nem sempre fecha** (`No changes`) | EMERGENCY CLOSE ×20, 3 com `No changes` | 🟡 MÉDIA |
| C7 | **Exposição concentrada em WINQ26** (20/22 sinais) num dia de range — sem filtro de regime/volatilidade | Timeline de sinais | 🟡 MÉDIA |
| C8 | **AGI reativou WDO_M15 no meio do pregão** (v1174→v1175, 12:05) — mudança de config durante operação | Log do AGI 12:00 | 🟢 BAIXA (monitorar) |

---

## 5. Como deveria ter sido (correções a aplicar)

### 5.1 Código (correções obrigatórias antes de qualquer operação)

1. **Respeitar o stop level do broker em TODO modify** — nova função `_respect_stop_level(symbol, proposed_sl_price, price)`:
   - Consultar `trade_stops_level` do símbolo (ou manter tabela por contrato: WIN≈300pts, BIT≈500pts, WDO≈200pts, WSP≈200pts — **confirmar com a XP**);
   - Nunca enviar SL com distância < `stop_level + buffer(2 ticks)` do preço atual;
   - Se o breakeven/profit-lock cair dentro do stop level → **pular o modify** (manter SL anterior) em vez de mandar inválido.
2. **Corrigir o trailing** — garantir `entry_price` real da posição (broker-truth via `positions_get()`, nunca confiar em state em memória zerado); adicionar guard `if entry_price <= 0: skip trailing`.
3. **TP1 com volume inteiro** — se `volume * tp1_pct < 1.0`, **não tentar TP1** (ou arredondar para o múltiplo do `volume_step`); logar "TP1 skipped (volume fracionário)".
4. **PATH do daemon** — garantir `hermes` acessível no environment do autotrader (ex: `export PATH=$PATH:/home/bruno/.hermes/hermes-agent/venv/bin` no `start_autotrader.sh`) para o recovery LLM funcionar.
5. **Fix padrão do recovery NUNCA inflar SL** — remover "5pts → 800pts"; em vez disso: revalidar o SL com a regra do stop level e tentar novamente com distância válida; se ainda falhar → manter SL anterior e alertar.
6. **Validator v2: não sobrescrever o clamp do `_calc_sl`** — o ajuste "pré-envio" que inflou 50.000→141.857pts está invertendo a proteção (max_native do BIT é 500 nativos; 1.418 nativos = 2,8× o máximo).

### 5.2 Regras operacionais (decisões humanas)

- **Nunca operar conta real com bugs de gerenciamento conhecidos.** Hoje o sistema operou sem TP1/trailing/breakeven funcionais — deveria ter sido pausado às 09:24 (2º sinal) quando o padrão ficou evidente.
- **Stop do dia (kill switch humano):** definir perda máxima diária (ex: −10% do capital) com parada automática — hoje foi −36,9%.
- **Filtro de regime:** em dia de range (WINQ26 179.000-179.800), estratégias de momento (HTF_BIAS_LTF_ENTRY comprando topos) sangram. Considerar desabilitar por par quando ATR estiver baixo vs range do dia.

---

## 6. DEMO vs REAL — o que muda e como replicar fielmente amanhã

### 6.1 Diferenças identificadas

| Aspecto | Conta DEMO 52257579 | Conta REAL 2257579 | Impacto |
|---|---|---|---|
| **Stop level / freeze level** | Aceita SLs a poucos pts (nível ~0) | Rejeita SLs dentro do stop level (`INVALID_STOPS`) | 🔴 **É a diferença crítica do dia** — breakeven/trailing de 5pts funcionava na demo e quebrou na real |
| **Volume mínimo/step** | 1.0 (contrato B3) | 1.0 (igual) | Mesmo — TP1 de 0.5 é inválido nas duas |
| **Spread** | ~0-1 tick | spread real (maior em abertura/fechamento) | 🟡 pequeno |
| **Slippage no fill** | ideal/fixo | real (market orders escorregam) | 🟡 pequeno |
| **History de deals** | `history_deals_get()` = 0 (limitação demo) | retornou vazio também hoje (usou FALLBACK-BALANCE) | 🟡 mesmo caminho |
| **Margem/custo** | virtual | margem real + taxas B3 | 🟡 PnL líquido difere pelas taxas |

### 6.2 Como ajustar a DEMO para seguir fielmente a REAL (plano de amanhã)

1. **Simular o stop level da real na demo:** implementar a regra 5.1.1 no código (validação pré-envio do SL com `stop_level_simulado` por símbolo). Assim o que a demo rejeitar = o que a real rejeitaria → os modifies falham IGUAL na demo → comportamento fiel.
2. **Corrigir os bugs C2-C6 ANTES** de operar na demo (senão a demo replica o bug, não a realidade).
3. **Mesma config, mesmos parâmetros, mesmas estratégias** — `vt_config.json` é compartilhado; trocar apenas a credencial da conta (login demo via `start_mt5linux.sh 52257579 <senha> XPMT5-DEMO`).
4. **Replicar o modo de operação de hoje** (mesmo dispatch, mesmos sinais, mesmos SLs de entrada) para o AGI estudar com dados FIEIS à real — o objetivo é que a demo se comporte como a real se comportaria COM as correções aplicadas.
5. **Registrar manualmente o stop level real** (pergunta para a XP ou teste controlado na demo: enviar SL a 5pts — se aceitar, confirma que a demo não replica a real sem o simulador).

---

## 7. Plano de ação

| Ordem | Ação | Quando | Dono |
|---|---|---|---|
| 1 | Corrigir C2 (trailing `entry_price=0`) + C3 (TP1 volume inteiro) + C4 (PATH hermes) | HOJE à noite | Hermes |
| 2 | Implementar `_respect_stop_level` (C1) com stop level simulado por símbolo | HOJE à noite | Hermes |
| 3 | Corrigir C5 (fix padrão/validator não inflar SL) | HOJE à noite | Hermes |
| 4 | Testar na demo com o comportamento real simulado (smoke test) | Amanhã pre-flight | Hermes |
| 5 | Operar a demo com a MESMA config/modo de hoje (corrigido) | Amanhã | Autotrader |
| 6 | Rodar AGI sobre os dados da demo (forward-only) | Amanhã 17h | AGI |
| 7 | Confirmar stop level real com a XP (pergunta objetiva) | Até sexta | Bruno |

---

## 8. Perguntas abertas (investigar)

1. **Qual o stop level exato do WIN/BIT/WDO/WSP na conta XP real?** (causa C1 — precisa do número para calibrar o simulador)
2. O `entry_price=0` do state veio do "STATE-REBUILD" das 09:21:12? (confirmar o fluxo de inicialização do state após abertura de posição)
3. Por que o validator v2 ajustou BIT 50.000→141.857pts? (lógica do "SL ajustado pré-envio" — parece inverter o clamp)
4. Os GHOST (6 de 22) — por que o bot não registrou `exit_price` no fechamento? (reconcile funcionou, mas o registro primário falhou)
5. Taxas B3 do dia (diferença −400 vs −552,88) — itemizar para o AGI não tratar como perda de estratégia.

---

*Gerado por Hermes em 05/08/2026 ~16:30 BRT — forense read-only de vt_trades.db + /tmp/vt_autotrader.log + /tmp/vt_agi_v4_20260805_120001.log + vt_config.json (v1181).*
