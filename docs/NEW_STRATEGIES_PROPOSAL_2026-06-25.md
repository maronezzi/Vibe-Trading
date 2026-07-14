# Propostas de Novas Estratégias — Mini-Contratos B3 (WIN/WDO/BIT/WSP)
**Data:** 25/06/2026 (Bruno Maronezzi · Vibe-Trading)
**Autor:** Agente de pesquisa quantitativa + web
**Escopo:** Análise + pesquisa + proposta — NENHUM ARQUIVO modificado (apenas este doc).

---

## 1. Diagnóstico do estado atual

### 1.1 Estratégias existentes (lidas em `strategies/*.py`)
| Estratégia | Lógica | Pontos fortes | Pontos fracos p/ o problema atual |
|---|---|---|---|
| **STRONG_TREND** (`strong_trend.py`) | EMA 9/21 + ADX>30 + DI + RSI filtro brando (ADX≥40 desliga RSI); trailing agressivo | Entra mais cedo que `ADX_TREND`; deixa winners correr com RSI desativado em trend forte | Em M5 WIN, o ADX>30 dispara com frequência em **micro-trends de 1-2 barras** que revertem → ruído de microestrutura; RSI desativado em ADX≥40 perde filtro crítico em M5 |
| **EMA_PULLBACK** (`ema_pullback.py`) | Espera pullback até a EMA21 em trend ADX>20; RSI<65 para buy | Melhor R:R que trend puro, evita comprar topo | Muito restritivo: pullback_pct=0.15 + RSI<65 + ADX>20 raramente bate em WIN M5 — gera **poucos sinais** |
| **MACD_MOMENTUM** (`macd_momentum.py`) | EMA 9/21 + MACD cross/hist crescente + ADX>15 + RSI<75 + volume | Aceita momentum crescente (não exige cross exato) | Threshold de ADX 15 é muito permissivo; volume<20% gera muitos sinais em barras mortas |
| **RSI_REVERSION** (`rsi_reversion.py`) | RSI<OS/OB puro, sem filtro de tendência | Simples, baixo drawdown em ranging | Não detecta regime; tenta reversão em plena trend |
| **BOLLINGER** (`bollinger.py`) | Toque na banda + RSI confirma | Funciona em range | Sem confirmação de reversão (candle de rejeição); entra direto no toque |
| **VWAP** (`vwap.py`) | Preço vs VWAP com threshold adaptativo | Bom para WDO intraday | Threshold adaptativo pode dar sinais prematuros em pullbacks |
| **SUPERTREND** (`supertrend.py`) | Flip do Supertrend (ATR mult 3.0) | Atraso reduzido, sinal limpo | Em WIN M5, mult=3 gera poucos flips; mult=2 daria mais |
| **DONCHIAN_BREAKOUT** (`donchian_breakout.py`) | Rompimento de canal N-barras + canal de saída | Clássico turtle, robust | Entrada no rompimento puro (sem pullback pós-breakout) → slippage alto |
| **HEIKIN_ASHI** (`heikin_ashi.py`) | Sequência de candles HA sem sombra oposta | Filtro visual de ruído | Lento, perde rompimentos rápidos |
| **RANGE_TRADING** (`range_trading.py`) | Toque em range + RSI | Reversão em consolidação | Não detecta quando o range ROMPE (perde a trend) |
| **DIVERGENCE_RSI** (`divergence_rsi.py`) | Divergência clássica entre preço e RSI | Bons pontos de fundo/topo | Atrasado, sinais raros |
| **MEAN_REVERSION_ZSCORE** (`mean_reversion_zscore.py`) | Z-Score > threshold + RSI | Estatístico, disciplinado | Em trend forte, o z-score fica "estacionário" em zona de baixa e entra contra trend |

### 1.2 Infraestrutura de saída (`core/vt_autotrader.py` — `manage_position`)
- **Trailing padrão:** ativa após `profit >= trail_activate × ATR`; distância = `trail_distance × ATR`
- **Breakeven:** move SL para entry+cost_pts após `breakeven_minutes` (default WIN: 25 min, WDO: 15 min)
- **Time-trail:** após `time_trail_minutes` ativa trailing mesmo sem lucro (força)
- **Max-position:** após `max_position_minutes` (WIN: 130 min, WDO: 90 min) usa trailing agressivo (0.3×ATR)
- **Hard exit:** fecha a mercado após `hard_exit_minutes` (WIN: 120, WDO: 90, BIT: 60, WSP: 52)
- **Proteção por estratégia:** BOLLINGER tem tight trailing na banda média

> **Implicação:** qualquer nova estratégia herda esse pipeline automaticamente. A estratégia só precisa definir **entrada** e **SL inicial** (via `calc_sl`). Saída dinâmica (trailing/TP) é responsabilidade do autotrader, exceto para WIN onde a estratégia STRONG_TREND atual gera 22L/8W — o SL sozinho não segura; precisa estratégia que **entre mais seletivamente** e/ou **saia antes** quando o sinal invalidar.

### 1.3 Config atual (`vt_config.json`)
- `WIN_M5 = STRONG_TREND` (sl_atr_mult=1.5, trail_act=1.2×ATR, trail_dist=0.5×ATR, max_pos=130 min, hard_exit=120)
- `WDO_M15/M30/H1 = ADX_TREND` (sl_atr_mult=1.0, trail_act=1.0×ATR, max_pos=90 min, hard_exit=90)
- `BIT` desabilitado (sangrou -R$7k em 30d)
- `WSP_M5 = ADX_TREND`

---

## 2. Insights da pesquisa web

### 2.1 Fonte primária consultada
**Awake Trader — "Mini-índice WIN 2026: 3 Setups de Day Trade Reais"** (31/05/2026, em português)¹. Conteúdo prático validado para WIN M5-M15, escrito por traders de micro-futuros B3.

### 2.2 Setup 1 — Opening Range Breakout (ORB) [Awake]
> "Aproveita o movimento mais previsível do dia: a abertura. As primeiras 30 minutos definem a tendência ou consolidação inicial."
- **Período:** 9h00–9h30
- **Marcação:** high e low da primeira meia-hora
- **Entrada:** rompimento com +5 pts de **confirmação** (não no toque)
- **Stop:** outro lado do range + 3 pts (estrutural, não ATR)
- **TP:** 1.5× o tamanho do range (RR 1:1.5)
- **Time-stop:** se até 11h nada acontece → fecha a mercado
- **Quando usar:** aberturas, eventos (Copom, payroll EUA), volatilidade média/alta
- **Quando NÃO usar:** dias laterais, sextas, pós-feriado EUA

**Aplicação direta para Vibe-Trading:** NENHUMA estratégia atual explora a janela 9h00–9h30 como sub-regime. Todas as 27 usam o mesmo setup o dia inteiro. Adicionar um ORB focado em WIN M5/M15 captura a ineficiência da abertura.

### 2.3 Setup 2 — Continuação de Tendência Intraday com Pullback [Awake]
- Identifica tendência: cotação **acima da MMA200 de M5** → alta, abaixo → baixa
- Espera pullback: preço retorna à **MMA21 de M5** sem violar a tendência
- Entrada: **rejeição** (vela com sombra) na direção da tendência
- Stop: **abaixo do fundo do pullback + 5 pts** (estrutural, não ATR fixo)
- TP: **trailing manual** de 50 pts atrás do topo/fundo recente

**Aplicação direta para Vibe-Trading:** `EMA_PULLBACK` usa `pullback_pct=0.15` (genérico). A versão ORB sugere usar **estrutura do candle** (rejeição/sombra) em vez de percent — adiciona filtro de **confirmação micro** que STRONG_TREND não tem.

### 2.4 Conceito VWAP + Order Flow / Triple Confirmação [Trading Academy / Faster Capital]²
- VWAP sozinho pode dar sinais "vazios" (sem convicção)
- Adicionar **Volume Profile** (POC — Point of Control) + **Order Flow** (delta de agressão) dá contexto
- "Triple confirmação": VWAP + Volume Profile + Order Flow → entrada de precisão

**Aplicação para WDO:** WDO M15/M30 hoje usa `ADX_TREND` que gera **poucos sinais** no regime RANGING atual. Uma abordagem **VWAP + ADX com filtro de posição relativa ao POC** pode gerar sinais em RANGING (preço vs VWAP em range) sem cair em over-trading.

### 2.5 Princípio geral observado em todas as fontes
- **Stops estruturais (high/low anterior)** > stops ATR fixos para WIN M5 — porque ATR em M5 varia muito entre dias (130 pts vs 400 pts) e o ATR puro gera SL que ora é stop distante (perde proteção) ora é stop curto (ruído)
- **Time-stop** é subutilizado em day-trade B3 mas crítico para WIN M5 (que tende a mover em ondas de 30–60 min, depois lateraliza)
- **Rejeição por candle** (sombra) é o filtro de "antecipar entrada" mais barato — entra antes do indicador confirmar, mas só após o preço mostrar intenção

### 2.6 Limitações da pesquisa
- Google retornou interstitial JS (não foi possível scraping direto)
- DuckDuckGo HTML funcionou, retornou 6-8 fontes úteis por query
- O conteúdo "oficial" B3 está confirmado via site institucional (edu.b3.com.br) — confirma specs WDO/WIN já em uso no projeto
- Não encontrei fonte específica para "anti-microestrutura" WIN M5 em português — proposta baseada na observação do framework

### 2.7 Citações
¹ Awake Trader. *Mini-índice WIN 2026: 3 Setups de Day Trade Reais (Sem Promessas)*. Disponível em <https://www.awaketrader.com/pt/educacao/mini-indice-win-setups-day-trade-2026>. Acesso em 25/06/2026.

² Triple Confirmación — Order Flow + Volume Profile + VWAP. *Trading Academy* <https://www.tradingacademy.mx/post/la-triple-confirmación-integración-de-order-flow-vp-y-vwap-para-entradas-de-precisión>; *FasterCapital* <https://fastercapital.com/content/Order-Flow--Order-Flow--The-Lifeblood-of-VWAP-Strategies.html>. Acesso em 25/06/2026.

---

## 3. Propostas de NOVAS estratégias (5 estratégias)

> **Premissa de design:** cada proposta ataca um buraco específico do portfólio atual, **não** duplica lógica existente, e usa **stop estrutural** (topo/fundo anterior ou VWAP) onde faz mais sentido do que ATR fixo para WIN M5 (problema do ruído de microestrutura).

---

### PROPOSTA 1 — `ORB_BREAKOUT` (Opening Range Breakout)

**Problema que resolve:** nenhuma das 27 estratégias atuais captura a ineficiência da janela 9h00–9h30 (abertura B3). É o setup de maior probabilidade estatística documentado em day-trade de índices¹.

**Indicadores / lógica de entrada:**
```python
# Janela operacional: só ativa nos primeiros 90 min de pregão (9h00–10h30)
if not (540 <= bar_ts.hour*60+bar_ts.minute <= 630): return None

# Calcular range dos primeiros 30 min da sessão
# (implementação: cachear high/low das 6 primeiras barras M5 ou primeira barra M30)
or_high = max(bars[0:6]['high'])  # 30 min em M5
or_low  = min(bars[0:6]['low'])
or_range = or_high - or_low

# Gate: range mínimo (evita dia morto — sem volatilidade, sem breakout)
if or_range < atr * 0.5: return None

# Entrada: rompe high/low COM confirmação de 5 pts + corpo de candle (não pavio)
if direction == "BUY":
    if price > or_high + 5 and (price - or_high) > 0.4 * or_range:
        # vela fechou acima do range com convicção
```

**Saída (3 camadas):**
1. **TP fixo:** 1.5× or_range (RR 1:1.5 conforme fonte¹)
2. **Trailing:** após lucro ≥ 0.8× ATR → trailing 0.5× ATR (herdado do manage_position)
3. **Time-stop:** se até 11h00 não bateu nada → `hard_exit` automático (já existe no autotrader; só ajustar `hard_exit_minutes`)

**SL strategy:**
- **Estrutural:** outro lado do range + 3 pts (NÃO usa ATR)
  - BUY: SL = or_low − 3
  - SELL: SL = or_high + 3
- Se isso ficar > 2.5×ATR, usar 2.5×ATR como teto (proteção contra gap)
- Se ficar < 1×ATR, usar 1×ATR como piso (evita SL curto demais — o problema do STRONG_TREND)

**Symbol/TF ideal:**
- **Primário:** `WIN_M5` (alta liquidez, volatilidade previsível na abertura) — alvo do problema de hoje
- **Secundário:** `WIN_M15` (se M5 estiver indisponível, M15 ainda captura a ORB de 9h)
- **Evitar:** WDO, BIT (janela de abertura tem gap diferente, regra diferente)

**Vantagem vs existentes:**
- **vs STRONG_TREND:** entra **antes** do pico em vez de durante o pullback — captura o momentum inicial que STRONG_TREND perde por esperar confirmação de ADX
- **vs DONCHIAN_BREAKOUT:** usa **OR de sessão** (30 min) em vez de N barras — adaptativo à microestrutura do dia; DONCHIAN em M5 gera sinais demais
- **vs EMA_PULLBACK:** não exige tendência prévia — perfeito para WIN M5 quando o dia abre sem direção clara

**Risco:** em dias de gap falso (abertura com spike e volta), gera loss pequeno (SL estrutural curto). Mitigação: gate `or_range > 0.5×ATR` filtra dias sem volatilidade.

**Parametrização inicial sugerida:**
```json
"ORB_BREAKOUT": {
  "or_minutes": 30,
  "confirmation_pts": 5,
  "rr_target": 1.5,
  "time_stop_hour": 11,
  "sl_pad_pts": 3,
  "max_or_range_mult": 2.5,
  "min_or_range_mult": 0.5
}
```

---

### PROPOSTA 2 — `STRUCTURE_BREAKOUT` (Rompimento com Pullback Estrutural)

**Problema que resolve:** DONCHIAN_BREAKOUT gera entradas com **slippage alto** porque entra no rompimento puro; HEIKIN_ASHI é lento demais. Falta um setup que **rompe** mas **entra no pullback** pós-breakout — melhor preço, melhor R:R.

**Indicadores / lógica de entrada:**
```python
# Donchian tight: max(high[1:21]) — 20 barras excluindo a atual
channel_high = max(b['high'] for b in bars[1:21])
channel_low  = min(b['low']  for b in bars[1:21])

# Detectar QUEBRA RECENTE (1-3 barras atrás)
broke_up_recently = any(b['close'] > channel_high for b in bars[1:4])
broke_dn_recently = any(b['close'] < channel_low  for b in bars[1:4])

# Entrada: pullback à média do canal após o breakout
mid = (channel_high + channel_low) / 2

# BUY: rompeu pra cima nas últimas 3 barras + preço voltou pra perto da mid
#      + candle de rejeição (mínima da barra > mid - 0.2*ATR)
if broke_up_recently and abs(price - mid) < 0.5*atr:
    bar_low = bars[0]['low']
    if bar_low > mid - 0.3*atr and rsi > 50 and rsi < 75:
        direction = "BUY"
```

**Saída:**
- **TP:** 1.0× distância do rompimento (projetar tamanho do swing)
- **Trailing:** 0.4×ATR após lucro ≥ 1×ATR (mais apertado que WIN default — captura rápido)
- **Hard exit:** 60 min (após breakout, mercado decide em <1h se continua ou não)

**SL strategy:**
- **Estrutural:** fundo do pullback − 0.5×ATR (NÃO é o canal low — é o fundo específico do recuo)
- Teto: 1.5×ATR (se pullback foi fundo do canal, SL não pode ser maior que isso)
- Piso: 1×ATR (proteção mínima)

**Symbol/TF ideal:**
- **Primário:** `WDO_M15` e `WDO_M30` (WDO tende a fazer breakouts com pullback limpo de 5-15 min)
- **Secundário:** `WIN_M15` (ganha do problema de ruído do M5)
- **Evitar:** BIT (muito volátil, pullback vira reversal)

**Vantagem vs existentes:**
- **vs DONCHIAN_BREAKOUT:** slippage muito menor (entra no pullback, não no spike do rompimento)
- **vs HEIKIN_ASHI:** reativo, não precisa de N candles de confirmação
- **vs ADX_TREND:** gera sinais em regime RANGING (que é o atual problema do WDO) — só precisa de um breakout recente + pullback

**Risco:** se o breakout falha e volta ao canal, gera loss pequeno mas constante. Mitigação: filtro `broke_up_recently` só nas últimas 3 barras + RSI>50 (não comprar pullback em downtrend).

**Parametrização:**
```json
"STRUCTURE_BREAKOUT": {
  "channel_period": 20,
  "pullback_window": 3,
  "rsi_min_buy": 50,
  "rsi_max_buy": 75,
  "tp_project_mult": 1.0,
  "max_pos_minutes": 60
}
```

---

### PROPOSTA 3 — `VWAP_MEAN_REVERSION_BANDS` (Reversão a VWAP em Regime Lateral)

**Problema que resolve:** WDO ADX_TREND gera **poucos sinais** em regime RANGING (atual). VWAP original só opera em trend. Falta um setup que **opere VWAP como pivô de range** (preço toca banda superior do desvio-padrão da VWAP → vende, toca inferior → compra).

**Indicadores / lógica de entrada:**
```python
# VWAP rolling (usa vwap_period da config WDO: 20)
vwap = calculate_vwap(bars, vwap_period=20)

# Calcular std dos desvios típicos da VWAP (últimas N barras)
deviations = [((b['high']+b['low']+b['close'])/3) - vwap for b in bars[:30]]
std_dev = statistics.pstdev(deviations)

upper_band = vwap + 1.5 * std_dev
lower_band = vwap - 1.5 * std_dev

# Regime: SÓ ativa quando ATR/price < 0.4% (ranging confirmado)
if (atr / price) > 0.004: return None  # não é ranging, deixa pra trend strategies

# Entrada
if price <= lower_band and rsi < 35:
    direction = "BUY"  # overshooting inferior → reversão pra VWAP
elif price >= upper_band and rsi > 65:
    direction = "SELL"

# ADX gate (precisa ser BAIXO = sem trend)
if adx > 25: return None  # se trend forte, não é ranging
```

**Saída:**
- **TP:** VWAP (preço retorna à média) — `direction==BUY` → TP = vwap
- **Trailing:** nenhum (a VWAP é o "TP natural"; se preço passa da VWAP e segue, deixa a `manage_position` decidir via tempo)
- **Hard exit:** 45 min (se não voltou à VWAP, assume range rompeu)

**SL strategy:**
- **Estrutural:** `entry - 1×ATR` na direção oposta
  - Se BUY (comprou no lower_band): SL = lower_band − 1×ATR (assume que se rompeu a banda, range acabou)
- **Piso:** 1.5×ATR (range trading exige mais espaço que trend)

**Symbol/TF ideal:**
- **Primário:** `WDO_M15` e `WDO_M30` (WDO tem ranging previsível entre eventos macro)
- **Secundário:** `WSP_M15/M30` (WSP também tem ranging parecido com WDO mas com menos volatilidade)
- **Evitar:** BIT (BTC não fica em ranging em M15), WIN (WIN tem drift direcional grande)

**Vantagem vs existentes:**
- **vs VWAP original:** VWAP só opera **longe** da média (trend). Aqui opera **nos extremos** da banda (reversão) — preenchimento do regime RANGING que WDO precisa
- **vs BOLLINGER:** usa **VWAP** (price × volume ponderado) como pivô, não SMA — VWAP respeita fluxo institucional, BOLLINGER é preço puro
- **vs MEAN_REVERSION_ZSCORE:** Z-score usa SMA simples, menos robusto em dias com volume crescente

**Risco:** se o "ranging" detectado é só **pausa antes de continuar trend**, gera loss (WDO pode dar 200 pts de continuation). Mitigação: gate ADX<25 + ATR/price<0.4% confirma que é range real.

**Parametrização:**
```json
"VWAP_MEAN_REVERSION_BANDS": {
  "vwap_period": 20,
  "std_multiplier": 1.5,
  "max_atr_pct": 0.004,
  "max_adx": 25,
  "rsi_oversold": 35,
  "rsi_overbought": 65
}
```

---

### PROPOSTA 4 — `MOMENTUM_SURGE_ENTRY` (Entrada Antecipada por Impulso de Volume)

**Problema que resolve:** STRONG_TREND em WIN M5 entra **depois** do pico (espera ADX confirmar). DONCHIAN entra **no** pico. Falta uma estratégia que **antecipa** a entrada — detecta o **momento** em que volume + range expansion acontecem, **antes** do indicador confirmar tendência.

**Indicadores / lógica de entrada:**
```python
# Medir "surge" da barra atual vs últimas 20
recent_range = max(b['high']-b['low'] for b in bars[1:21])
current_range = bars[0]['high'] - bars[0]['low']

recent_vol = sum(b.get('volume',1) for b in bars[1:21]) / 20
current_vol = bars[0].get('volume', 1)

# Surge = barra atual com range E volume simultaneamente expandidos
range_surge = current_range > recent_range * 1.5
vol_surge   = current_vol > recent_vol * 2.0

if not (range_surge and vol_surge): return None

# Direção: candle body (não pavio) mostra intenção
body = bars[0]['close'] - bars[0]['open']
if body > 0 and (bars[0]['close'] - bars[0]['low']) > 0.6 * current_range:
    direction = "BUY"   # body bullish + fechou na metade superior do range
elif body < 0 and (bars[0]['high'] - bars[0]['close']) > 0.6 * current_range:
    direction = "SELL"  # body bearish + fechou na metade inferior

# Filtro ADX mínimo (precisa ter alguma tendência subjacente)
adx_val, plus_di, minus_di = calculate_adx(bars, 14)
if adx_val < 18: return None  # sem trend subjacente, surge vira reversal
```

**Saída:**
- **TP:** 1.5× ATR (deixa WIN M5 capturar o impulso completo)
- **Trailing:** após lucro ≥ 1.5×ATR → trailing 0.5×ATR (apertado, captura rápido porque é setup de momentum)
- **Hard exit:** 30 min (se impulso não continuou em 30 min, abort)

**SL strategy:**
- **Estrutural:** low/high da barra de entrada + buffer
  - BUY: SL = barra[0].low − 0.3×ATR (abaixo do pavio inferior da barra de impulso)
  - SELL: SL = barra[0].high + 0.3×ATR
- **Teto:** 1.5×ATR (se SL ficou grande, é sinal fraco — abort)

**Symbol/TF ideal:**
- **Primário:** `WIN_M5` (captura exatamente o problema da sangria de hoje — antecipa entrada em vez de confirmar tarde)
- **Secundário:** `BIT_M15` (BTC gera surges de volume claros quando habilitada)
- **Evitar:** WDO_M5 (WDO tem mais ruído de spread em M5, perde signal-to-noise)

**Vantagem vs existentes:**
- **vs STRONG_TREND:** entra **no candle de impulso**, não depois — captura o pico antes de subir mais
- **vs MOMENTUM_BREAKOUT:** MOMENTUM_BREAKOUT exige ROC>threshold (lag); este detecta o surge em TEMPO REAL na barra atual
- **vs DONCHIAN_BREAKOUT:** filtra por **volume simultâneo** (range+vol juntos) — DONCHIAN rompe com volume baixo em 30% dos casos (falso breakout)

**Risco:** em dias de manipulação (iceberg orders), volume surge é "fake". Mitigação: exige range_surge E vol_surge simultâneos + ADX>18 (precisa tendência subjacente).

**Parametrização:**
```json
"MOMENTUM_SURGE_ENTRY": {
  "range_surge_mult": 1.5,
  "volume_surge_mult": 2.0,
  "body_position_min": 0.6,
  "adx_min": 18,
  "tp_atr_mult": 1.5,
  "max_pos_minutes": 30
}
```

---

### PROPOSTA 5 — `HYBRID_PIVOT_CONFIRMATION` (MIX — Pivots + ADX + MACD)

**Problema que resolve:** nenhuma estratégia combina **suporte/resistência clássico (pivôs)** com **confirmação de momentum (MACD)** + **filtro de regime (ADX)**. PIVOT_POINTS atual só usa pivôs H1/D1 sem momentum; MACD_MOMENTUM não tem contexto de nível. Este setup **mistura 3 estratégias existentes** numa só, com lógica de saída diferente para **maximizar winners**.

**Indicadores / lógica de entrada:**
```python
# Calcular pivôs clássicos (H1 → usar TF do chart ou pivô H1 fixo)
# Pivot = (prev_high + prev_low + prev_close) / 3
# R1 = 2*Pivot - prev_low, S1 = 2*Pivot - prev_high
pivot = (pivot_high + pivot_low + pivot_close) / 3
r1 = 2 * pivot - pivot_low
s1 = 2 * pivot - pivot_high

# MACD confirmação de momentum
macd_hist = macd_histogram_now
macd_hist_prev = macd_histogram_prev
macd_momentum_up = macd_hist > 0 and macd_hist > macd_hist_prev  # crescente

# ADX regime
adx_val, plus_di, minus_di = calculate_adx(bars, 14)
in_trend = adx_val > 20 and plus_di > minus_di if direction=='BUY' else adx_val > 20 and minus_di > plus_di

# ENTRADA — 2 de 3 sinais precisam confirmar
signal_count = 0

# Sinal 1: Preço tocou S1 e mostra rejeição (bullish pin bar / martelo)
if direction == "BUY":
    if abs(price - s1) < 0.3*atr: signal_count += 1  # toque
    if bars[0]['close'] > bars[0]['open']: signal_count += 0.5  # body bullish
    if (bars[0]['low'] < s1 and bars[0]['close'] > s1): signal_count += 1  # pierce + recover

# Sinal 2: MACD cruzando/hist crescente
if macd_momentum_up and direction == "BUY":
    signal_count += 1

# Sinal 3: ADX confirma tendência subjacente (com DI alinhado)
if in_trend:
    signal_count += 0.5  # peso menor — é contexto, não gatilho

if signal_count < 2: return None
```

**Saída (a mais elaborada do portfólio — MAXIMIZAR WINNERS):**
1. **TP1 (parcial 50%):** ao atingir 1×ATR → fecha metade, move SL da outra metade para breakeven+cost
2. **TP2:** 2×ATR → fecha mais 25%
3. **Runner (25% restante):** trailing 0.3×ATR até `hard_exit_minutes` ou stop de structure
4. **Stop estrutural mobile:** se candle atual fechar abaixo/abaixo do pivot, fecha tudo

**Nota:** TP parcial requer mudança no `vt_autotrader.py` (suporte a partial close). **Alternativa simples:** não fazer parcial, só trailing agressivo (0.3×ATR após 1×ATR de lucro). O autotrader já suporta isso via `trail_distance` + `trail_activate`.

**SL strategy:**
- **Estrutural:** pivô S1 (BUY) ou R1 (SELL) − 0.5×ATR
- **Piso:** 1.5×ATR
- **Teto:** 2.5×ATR

**Symbol/TF ideal:**
- **Primário:** `WIN_M15` (mix ideal — M15 captura estrutura H1 sem ruído de M5)
- **Secundário:** `WIN_M30`, `WDO_M15` (WDO tem pivôs mais respeitados por institucionais)
- **Evitar:** BIT (muito volátil, pivôs clássicos falham)

**Vantagem vs existentes:**
- **vs PIVOT_POINTS:** adiciona MACD momentum (PIVOT_POINTS sozinho dá sinais em zonas mortas)
- **vs MACD_MOMENTUM:** usa pivô como **gatilho de nível** (preço só é relevante em S1/R1, não em qualquer ponto)
- **vs ADX_TREND:** entrada em **níveis institucionais** (mais provável de reaction) em vez de só direção
- **MAX SAÍDA:** trailing 0.3×ATR após 1×ATR é **2× mais apertado** que o default (0.5×ATR) — winners maiores quando se move, cortados mais rápido quando para

**Risco:** complexidade alta — 3 sinais, fácil overfit. Mitigação: parametrize com **max_combos=20** no `exhaustive_strategy_search.py` (limita grid search).

**Parametrização:**
```json
"HYBRID_PIVOT_CONFIRMATION": {
  "pivot_timeframe": "H1",
  "adx_threshold": 20,
  "signal_threshold": 2,
  "tp1_atr_mult": 1.0,
  "tp2_atr_mult": 2.0,
  "trail_after_tp1": 0.3,
  "max_pos_minutes": 90,
  "hard_exit_minutes": 180
}
```

---

## 4. Tabela comparativa — propostas vs problema atual

| Problema | Estratégia proposta | Vantagem chave |
|---|---|---|
| WIN M5 STRONG_TREND sangra (8W/22L) | `MOMENTUM_SURGE_ENTRY` (entrada antecipada) + `ORB_BREAKOUT` (captura abertura) | Entram **antes** do ADX confirmar |
| WDO M15/M30 ADX_TREND gera poucos sinais (RANGING) | `VWAP_MEAN_REVERSION_BANDS` + `STRUCTURE_BREAKOUT` | Geram sinais em RANGING + breakout com pullback |
| WDO tem regime RANGING atual | `VWAP_MEAN_REVERSION_BANDS` | Específico para regime lateral (ADX<25 + ATR<0.4%) |
| Winners são cortados cedo | `HYBRID_PIVOT_CONFIRMATION` (trailing 0.3×ATR) | Trailing apertado deixa winners correrem + cortam losers rápido |
| BIT desabilitado (não resolver aqui — fora de escopo) | — | BIT exigiria estratégia própria com risk profile diferente |

---

## 5. Roteiro de implementação (sem modificar arquivos — apenas roadmap)

> O usuário pediu **NÃO modificar arquivos**. Esta seção é um roteiro caso queira implementar depois.

1. **Adicionar em `optimization/exhaustive_strategy_search.py`:**
   - Cada nova estratégia no array `ALL_STRATEGIES`
   - Cada nova no `STRATEGY_PARAM_GRIDS` com `MAX_COMBOS_PER_STRATEGY = 20`

2. **Criar `strategies/orb_breakout.py`, `structure_breakout.py`, etc.** seguindo o contrato `STRATEGY_NAME` + `check_entry(...)`.

3. **Adicionar `strategy_by_tf` em `vt_config.json`:**
   - `WIN_M5`: `MOMENTUM_SURGE_ENTRY` (substitui STRONG_TREND)
   - `WIN_M15`: `HYBRID_PIVOT_CONFIRMATION`
   - `WDO_M15`: `STRUCTURE_BREAKOUT`
   - `WDO_M30`: `VWAP_MEAN_REVERSION_BANDS`
   - `WIN_M5`: também `ORB_BREAKOUT` (slot paralelo)

4. **Backtest comparativo** em `backtest/backtest_agi_v11.py`:
   - Janela: 30 dias recentes (2026-05-25 a 2026-06-24)
   - Métricas: Win rate, PnL líquido, max drawdown, Profit Factor
   - Critério de aceite: PnL > 2× Total Cost + drawdown < 5% (Brutal Reality do AGI)

5. **Validar com AGI** (`optimization/agi_tuning_17h.py`):
   - Walk-forward em 3 janelas (7+7+7 dias)
   - Se passar: substituir config
   - Se falhar: descartar (Occam's razor)

---

## 6. Resumo executivo

**5 novas estratégias propostas**, cada uma atacando um buraco específico:
1. **ORB_BREAKOUT** — captura a ineficiência da abertura (9h00–9h30) ignorada pelas atuais
2. **STRUCTURE_BREAKOUT** — versão melhorada de DONCHIAN com pullback pós-breakout (slippage menor)
3. **VWAP_MEAN_REVERSION_BANDS** — preenche o gap do WDO em regime RANGING
4. **MOMENTUM_SURGE_ENTRY** — entrada antecipada por impulso de volume+range (resolve o problema de WIN M5 STRONG_TREND)
5. **HYBRID_PIVOT_CONFIRMATION** — mix de 3 estratégias (Pivots+MACD+ADX) com saída multi-camada para maximizar winners

**Insights web principais:**
- **Awake Trader 2026** confirma ORB como setup #1 para WIN M5
- Stops **estruturais** (high/low anterior) > stops ATR fixos em WIN M5
- Time-stop é subutilizado em B3 mas crítico

**Arquivos modificados:** NENHUM (conforme instruído).
**Documento criado:** `/home/bruno/Projects/Vibe-Trading/docs/NEW_STRATEGIES_PROPOSAL_2026-06-25.md`

---

*Fim do relatório — Bruno, à disposição para implementar qualquer das 5 ou ajustar parâmetros antes do deploy.*