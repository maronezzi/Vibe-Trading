# Diagnóstico das estratégias vencedoras — 01/09/2026 (mineração de 111 trades)

> Fonte: `trades` (AGI4_WIN_121815 + DIVERGENCE_RSI, todas as contas), `signal_blocked_log`,
> `forward_sim_trades` (MFE/MAE via highest/lowest). Read-only. Este documento GERA
> hipóteses — **os gates decidem** (backtest 30d + walk-forward + backfill A/B do walker).
> Uma hipótese por variante no sandbox `strategies/_pending/`; nunca tocar na live direto.

## 1. Achados ranqueados por R$ (in-sample — validar fora da amostra)

| # | Achado | Evidência | Hipótese de mudança |
|---|---|---|---|
| 1 | **Zona de morte no RSI** | RSI<50: 55 trades **−119,77** · RSI≥75: 20 trades **−132,20** · banda 50–70: **+157** (65–70: média +34/trade) | Filtro de banda: só operar RSI 50–70; fora, sinal ignorado (não-monotônico — nem filtro direcional simples resolve) |
| 2 | **13h é o matadouro** | 13h: 8 trades **−199,90, WR 1/8** (12,5%!) · 10h: −143,65 · 11h+12h: **+171** | Veto de janela 13:00–14:00 (almoço B3: liquidez morta, momentum falso) |
| 3 | **Os primeiros 15 min decidem** | trades que morrem <15min: **−137** (WR 6/24) · que sobrevivem: **+513** somados | Time-stop assimétrico: perdedora flutuante aos 30min sai (hoje 90min); vencedora sem time-stop |
| 4 | **ATR é não-linear e próprio por símbolo** | WIN: Q1 baixa-vol **+259** (média +37) e Q4 alta-vol **+204**, mas Q2 médio **−487** · BIT: ATR irrelevante (leve pior em alta-vol) | Gate de regime por símbolo com percentil móvel (20d) — NÃO filtro linear; validar walk-forward (Q2 pode ser cluster de 1-2 dias) |
| 5 | **SELL sangra** | SELL: 62 trades **−169,56** (WR 40%) vs BUY +41,65 | Revisar o lado vendedor da DIVERGENCE_RSI (short em overextension falha em RSI≥75) |
| 6 | **Bloqueios nunca resolvidos** | `signal_blocked_log`: zero linhas `resolved=1` — bloqueamos sinais e **nunca medimos o contrafactual** | Infra: resolver cada bloqueio contra barras futuras (would-have-been) → base de conhecimento do que os gates ajudam ou atrapalham |
| 7 | **TP1 morto por volume 1** | volume 1 × 0,5 = fracionário (inválido B3) — 5 pulos só ontem | **Volume 2 no WIN** (dívida §14): 2×0,5=1 válido → reativa o TP1, metade do risco sai em 1R |

## 2. Ideias estruturais (além de filtros)

- **Regime gate diário**: AGI publica por raiz o percentil de ATR do dia vs 20d; o walker e
  o daemon consultam — "hoje é dia Q1 de WIN" vira contexto de decisão, não constante.
- **Spread gate**: pular entrada se spread > X% do ATR (o journal já captura spread por
  entrada desde ontem — BIT média 20pts = 1 tick inteiro).
- **Asymmetry check nos pares Δ**: o journal mede sim↔live por trade; divergência
  sistemática por hora/símbolo vira sinal de execução (alpha de execução, não de estratégia).
- **Cooldown pós-SL por regime**: perdas em cluster (hoje WIN 2 SL seguidos na mesma região
  de topo) — após SL, exigir preço sair da zona da entrada antes de rearmar o mesmo sinal.

## 3. Guardrails anti-overfitting

- Cada variante: UMA hipótese, nomeada (`AGI4_WIN_121815_v2_rsi_band` etc.), sandbox `_pending`.
- Validação obrigatória: backtest 30d → walk-forward 4 janelas → backfill A/B do walker
  (mesmo período, run_id distinto) → gates normais do Stage 5 (PF≥1,15, n≥20, dd≤2,5).
- Nenhuma variante entra na live por decisão do LLM — promoção é dos gates; LLM propõe.
- Semana de migração real (03/09+): sandbox roda em paralelo, zero interferência no pregão.

## 4. Caveats honestos

- Buckets pequenos (13h: n=8; 65–70: n=4) — o gate com n≥20 vai filtrar o que for ruído.
- Clusters sobrepostos: RSI<50 ≈ SELL ≈ 13h ≈ baixa-vol — podem ser o MESMO regime visto
  de 4 ângulos. As variantes precisam isolar uma variável por vez.
- In-sample: tudo acima olhou o próprio histórico que treina os gates — walk-forward e
  backfill A/B existem exatamente para punir o que não generaliza.

## 5. Variantes construídas (02/09, sandbox `_pending/`)

| Variante | Mudança única | Defaults (tunáveis) | Teste |
|---|---|---|---|
| `agi4_win_121815_v2_rsizones.py` | Zonas de RSI por lado: BUY em `[rsi_buy_min, rsi_buy_max]` (60–70), SELL veta `[35, 45]` (zona morta; flush <35 e fade neutro 45–50 seguem) | `rsi_buy_min=60`, `rsi_buy_max=70`, `rsi_sell_veto_lo=35`, `rsi_sell_veto_hi=45` | 9/9 asserts stub (incumbente intacta — RSI 55 still BUY nela) |
| `agi4_win_121815_v2_veto13h.py` | Veto de entrada na janela local 13→14h (`_local_hour` normaliza epoch-do-daemon e datetime-do-walker) | `veto_hour_start=13`, `veto_hour_end=14`, `veto_hours` extensível ("10,15") | 6/6 asserts stub (epoch + datetime walker) |

Rerefinamento que a leitura do código trouxe: a incumbente tem **teto de ADX mas não de
RSI** (BUY aceita RSI 80 = perseguir exaustão) e **piso de RSI ausente no SELL** (vende em
RSI 30). As zonas por lado ficaram mais cirúrgicas que a "banda 50–70" bruta do §1:
BUY 50–60 = −120 (momentum fraco) · BUY 60–70 = **+316** (n=4) · BUY 70–80 = −163 ·
SELL 35–45 = **−179** (WR 33%) · SELL <35 e 45–50 = positivos.

Próximo: entradas na esteira — backfill A/B do walker (mesmo período, run_id distinto) →
sweep/gates do Stage 5 → só então promoção. Live intocada.
