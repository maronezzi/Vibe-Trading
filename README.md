# Vibe-Trading 🚀

Sistema autônomo de auto-trading para mini-contratos BM&F (WDO/Win) operando via MetaTrader 5.
Desenvolvido por [Bruno Maronezzi](https://github.com/maronezzi).

Arquitetura de **plugins de estratégia dinâmica** com hot-reload, gestão anti-drawdown,
e otimização via AGI (Inteligência Artificial Generalista).

## Visão Geral

```
┌─────────────────────────────────────────────────┐
│                  Hermes Agent                    │
│         (Orquestrador + Crontab)                 │
├─────────────────────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Autotrader│  │ Analyst  │  │ AGI 17h / 12h│  │
│  │  (9h-16h) │  │ (watch)  │  │ (otimização)  │  │
│  └─────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│        │              │                 │          │
│  ┌─────▼──────────────▼─────────────────▼───────┐ │
│  │          vt_config.json (hot reload)         │ │
│  └──────────────────────┬────────────────────────┘ │
│                         │                          │
│  ┌──────────────────────▼────────────────────────┐ │
│  │     Strategy Loader (strategies/*.py)         │ │
│  │  VWAP │ EMA_PULLBACK │ STRONG_TREND │ BOLL   │ │
│  └──────────────────────┬────────────────────────┘ │
│                         │                          │
│  ┌──────────────────────▼────────────────────────┐ │
│  │         MT5 Orchestrator (Linux)               │ │
│  │    ┌──────────────────────────────┐            │ │
│  │    │  MT5 Executor (Wine/Python)  │            │ │
│  │    │    MetaTrader 5 Terminal     │            │ │
│  │    └──────────────────────────────┘            │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

## Stack Técnica

| Componente | Tecnologia |
|---|---|
| **Orquestrador** | Hermes Agent (cron jobs) |
| **Autotrader** | Python 3.11 |
| **MT5 Bridge** | MetaTrader 5 → Wine → Python cross-call |
| **Estratégias** | Plugins Python dinâmicos |
| **Config** | JSON centralizado + hot-reload por mtime |
| **Trade Log** | SQLite (vt_trades.db) |
| **Backtesting** | Python puro (pandas + dados MT5) |
| **Notificações** | Telegram via Hermes CLI |
| **Otimização** | AGI (LLM) rodando em crontab |

## Ativos Operados

- **WDO$** — Mini Dólar (contratos futuros de USD/BRL)
- **WIN$** — Mini Índice Bovespa (contratos futuros de Ibovespa)

## Configuração Central — `vt_config.json`

Toda a configuração do sistema está em um único arquivo JSON.
O autotrader faz **hot-reload automático** (detecta mudanças por mtime).

```json
{
  "_version": 14,
  "_updated_by": "agi_17h",
  "symbols": ["WDO", "WIN"],
  "timeframes": ["M5", "M15"],
  "timeframes_by_symbol": {
    "WDO": ["M5", "M15"],
    "WIN": ["M5", "M15"]
  },
  "volume": 1,
  "start_hour": 9, "start_minute": 5,
  "close_hour": 16, "close_minute": 45,
  "check_interval": 30,
  "bars_count": 30,
  "magic": 555501,

  "strategy": {
    "WDO": "EMA_PULLBACK",
    "WIN": "EMA_PULLBACK"
  },

  "resolved_symbols": {
    "WDO": "WDON26",
    "WIN": "WINM26"
  },

  "wdo": {
    "ema_fast": 9, "ema_slow": 21,
    "sl_atr_mult": 1.0,
    "trail_activate": 1.5, "trail_distance": 0.5,
    "breakeven_minutes": 15,
    "time_trail_minutes": 30,
    "max_position_minutes": 120,
    "cooldown_seconds": 300,
    "max_daily_trades": 8
  },

  "win": {
    "ema_fast": 9, "ema_slow": 21,
    "adx_period": 14, "adx_threshold": 30,
    "sl_atr_mult": 1.5,
    "breakeven_minutes": 20,
    "time_trail_minutes": 45,
    "max_position_minutes": 120,
    "cooldown_seconds": 600,
    "max_daily_trades": 10
  }
}
```

Parâmetros por seção:

**Gerais:**
- `symbols` / `timeframes` — ativos e tempos gráficos (global)
- `timeframes_by_symbol` — override de timeframes por ativo (prioridade sobre `timeframes`)
- `volume` — contratos por operação
- `start_hour/min` / `close_hour/min` — horário de mercado
- `check_interval` — segundos entre verificações
- `resolved_symbols` — símbolos MT5 resolvidos (fixados pelo Symbol Resolver às 8h55)

**Por ativo (wdo/win):**
- `sl_atr_mult` — Stop Loss como múltiplo do ATR
- `trail_activate` — lucro mínimo em ATR para ativar trailing
- `trail_distance` — distância do trailing em ATR
- `breakeven_minutes` — tempo para mover SL ao ponto de entrada
- `time_trail_minutes` — tempo para ativar trailing sem lucro suficiente
- `max_position_minutes` — tempo máximo de posição (trailing agressivo)
- `cooldown_seconds` — intervalo mínimo entre trades
- `max_daily_trades` — limite diário de operações

## Scripts Principais

### Autotrader — `vt_autotrader.py` (1.344 linhas)

Daemon principal que opera durante o horário de mercado.

**Fluxo de execução:**
1. Carrega config (`vt_config.json`) e estratégias (plugins)
2. Loop a cada 30s durante horário de mercado (9:05–16:45)
3. A cada iteração:
   - Hot-reload da config e das estratégias (mtime check)
   - Busca dados de ticks via MT5
   - Para cada ativo/timeframe:
     - Verifica sinal de entrada via strategy plugin
     - Se sinal → executa ordem via orchestrator
   - Para cada posição aberta:
     - Atualiza SL via trailing stop
     - Aplica proteções anti-drawdown
   - Detecta posições órfãs (MT5 vs estado local)
4. Às 16:45 fecha todas posições e encerra

**Comandos:**
```bash
python vt_autotrader.py              # Modo daemon (loop contínuo)
python vt_autotrader.py --once      # Uma única verificação
python vt_autotrader.py --close     # Fecha tudo e encerra
python vt_autotrader.py --status    # Status atual
```

**Proteções anti-drawdown implementadas:**
1. **Breakeven Timer** — Após X minutos sem trailing, move SL para o ponto de entrada
2. **Time-Based Trailing** — Após Y minutos, ativa trailing mesmo sem atingir o lucro mínimo
3. **Max Position Time** — Após 2h, trailing agressivo (0.3x ATR)

### MT5 Orchestrator — `mt5_orchestrator.py` (183 linhas)

Camada Linux que envia ordens ao MT5 via subprocess Wine.

```python
from mt5_orchestrator import status, buy, sell, close, close_all, tick, resolve_symbol

status()                          # Estado da conta e posições
buy('WIN$ WDO$', volume=1, sl_pts=200)
sell('WDON26', volume=1, sl_pts=50)
close('WIN$')                      # Fecha posição do símbolo
close_all()                        # Fecha tudo
tick('WDO$')                       # Preço atual
resolve_symbol('WDO')              # Resolve símbolo (WDON26, WDOQ26, etc)
modify_sl('WDON26', new_sl=5190.0) # Move stop loss
```

### MT5 Executor — `mt5_executor.py` (615 linhas)

Roda **dentro do Wine** (Python Windows). Interface direta com a API MetaTrader5.

```bash
wine python.exe mt5_executor.py status
wine python.exe mt5_executor.py buy WIN$ 1 200
wine python.exe mt5_executor.py sell WDON26 1 50
wine python.exe mt5_executor.py close_all
wine python.exe mt5_executor.py tick WDO$
wine python.exe mt5_executor.py modify_sl WDON26 5190.0
```

### MT5 Fetch — `mt5_fetch.py` (105 linhas)

Coleta dados do MT5 em formato CSV (para backtest e análise).

```bash
wine python.exe mt5_fetch.py rates WDO$ M5 500  # OHLCV
wine python.exe mt5_fetch.py ticks WIN$ 20       # Ticks
wine python.exe mt5_fetch.py info                 # Info do terminal
```

### Config Loader — `vt_config_loader.py` (109 linhas)

Hot-reload da configuração. Detecta mudanças via mtime do arquivo.

```python
from vt_config_loader import load_config, save_params, save_full_config

CONFIG = load_config()           # Carrega (com cache + mtime)
CONFIG = load_config(force=True) # Força reload

save_params("wdo", params, updated_by="agi_17h")        # Salva params de um ativo
save_full_config(config, updated_by="optimizer")           # Salva config inteira
```

### Strategy Loader — `vt_strategy_loader.py` (130 linhas)

Carrega estratégias dinamicamente da pasta `strategies/`.

```python
from vt_strategy_loader import load_strategies, get_strategy_func, reload_strategies

strategies = load_strategies()           # Carrega todos plugins
func = get_strategy_func("VWAP")         # Retorna check_entry da estratégia
reload_strategies()                      # Recarrega se mtime mudou
```

### Trade Log — `vt_trade_log.py` (540 linhas)

Registro de operações em SQLite. Gera dados para Imposto de Renda.

```python
from vt_trade_log import init_db, log_entry, log_exit, get_daily_summary

init_db()                              # Cria tabelas se não existem
log_entry(symbol, direction, price, sl_pts, strategy, info)
log_exit(trade_id, exit_price, pnl, reason)
get_daily_summary(date)                # Resumo do dia
```

### Analyst — `vt_analyst.py` (479 linhas)

Detecta eventos de mercado em tempo real (zero tokens na coleta, LLM sob demanda).

**Eventos detectados:**
- Volume spike (> 2x média)
- Volatilidade spike (ATR > 2x média)
- Drawdown em posição aberta
- Trades consecutivos perdendo
- Reversão forte contra posição
- VWAP crossover
- Breakout de máximas/mínimas

### Daily Report — `vt_daily_report.py` (267 linhas)

Relatório diário automático (Python puro, zero LLM). Executa às 16:50.

### Copilot — `vt_copilot.py` (641 linhas)

Health check autônomo. Reconcilia posições órfãs, ajustes automáticos.

## Sistema de Estratégias (Plugins)

Cada estratégia é um arquivo Python em `strategies/` com interface padrão:

```python
STRATEGY_NAME = "NOME_DA_ESTRATEGIA"

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Args:
        symbol: Símbolo (ex: "WDON26")
        tf: Timeframe ("M5", "M15")
        price: Preço atual
        atr: ATR(14) calculado
        bar_ts: Timestamp da barra
        bars: Lista de barras OHLCV recentes
        params: Dict de parâmetros do config (wdo/win)
        utils: Dict de funções utilitárias (calc_ema, calc_rsi, etc)

    Returns:
        None → sem sinal
        {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}} → sinal
    """
    ...
```

### Estratégias disponíveis

| Estratégia | Arquivo | Tipo | Uso |
|---|---|---|---|
| **VWAP** | `strategies/vwap.py` | Trend-continuation | WDO (mercado trending) |
| **EMA_PULLBACK** | `strategies/ema_pullback.py` | Trend-following + pullback | WIN (pullback na tendência) |
| **STRONG_TREND** | `strategies/strong_trend.py` | Trend-following agressivo | WIN (ADX forte, ignora RSI) |
| **BOLLINGER** | `strategies/bollinger.py` | Reversão à média | WIN (mercado choppy) |
| **EMA_CROSSOVER** | `strategies/ema_crossover.py` | Crossover de médias | Genérico |
| **ADX_TREND** | `strategies/adx_trend.py` | Trend-following com ADX | Genérico |
| **MACD_MOMENTUM** | `strategies/macd_momentum.py` | Momentum MACD | Genérico |
| **WIN_REVERSION** | `strategies/win_reversion.py` | Reversão específica WIN | WIN |

### Criando uma nova estratégia

1. Criar arquivo `strategies/minha_estrategia.py`:

```python
STRATEGY_NAME = "MINHA_ESTRATEGIA"

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calc_ema = utils["calc_ema"]
    calc_rsi = utils["calc_rsi"]

    # Lógica aqui...
    # return {"direction": "BUY", "sl_pts": 200, "info": {"reason": "sinal forte"}}
    return None
```

2. Atualizar config: `"strategy": {"WDO": "MINHA_ESTRATEGIA"}`
3. Autotrader detecta automaticamente em ~30s (hot-reload)

## Backtesting

### Backtest Principal — `backtest/backtest_agi_v11.py`

Backtest que replica **exatamente** a lógica do autotrader, incluindo:
- Mesmas estratégias (lê do `vt_config.json`)
- Mesmas proteções anti-drawdown (breakeven, time-trail, max position)
- Mesmas regras de horário, cooldown, limite diário
- Comparação com config antiga (baseline)

```bash
PYTHONPATH=./agent ./agent/venv/bin/python backtest/backtest_agi_v11.py
```

**Output:**
- Trades detalhados do dia
- PnL por ativo/timeframe
- Comparativo NEW vs OLD config
- Métricas: WR, Profit Factor, Sharpe

### Outros backtests

| Script | Uso |
|---|---|
| `backtest/backtest_autotrader_v6.py` | Versão anterior (só VWAP) |
| `backtest/backtest_multi_strategy.py` | Compara múltiplas estratégias |

## Crontab — Fluxo Diário

| Horário | Job | Descrição |
|---|---|---|
| **08:55** | Symbol Resolver | Resolve WDO→WDON26/WDOQ26, WIN→WINM26/WINQ26 |
| **09:00** | Autotrader | Inicia daemon (pkill anterior + startup MT5) |
| **11:00, 13:00** | Inteligência Trader | Análise LLM do mercado, ajusta parâmetros |
| **12:00** | Otimização Meio-Dia | Ajusta parâmetros sem mudar estratégia |
| **16:50** | Relatório Diário | Fecha posições, gera relatório, envia Telegram |
| **17:10** | AGI Otimizador | Otimização completa (pode trocar estratégias) |

## Gestão de Risco

### Camadas de proteção

1. **SL obrigatório** — Toda entrada tem stop loss (baseado em ATR)
2. **Trailing stop** — Ativa após X ATR de lucro, acompanha o preço
3. **Breakeven Timer** — Move SL para entrada após 15-20min sem trailing
4. **Time-Based Trailing** — Ativa trailing após 30-45min mesmo sem lucro mínimo
5. **Max Position Time** — Trailing agressivo (0.3x ATR) após 2h
6. **Cooldown** — Intervalo mínimo entre trades (300s WDO, 600s WIN)
7. **Max Daily Trades** — Limite diário de operações (8 WDO, 10 WIN)
8. **EOD Close** — Fecha tudo às 16:45 automaticamente

### Fluxo do SL

```
Entrada → SL = entry ± (ATR × sl_atr_mult)
        → Se trailing ativado: SL segue melhor preço
        → Se breakeven: SL vai pra entry
        → Se time-trail: ativa trailing por tempo
        → Se max_position: trailing agressivo
```

## Instalação

### Pré-requisitos

- Python 3.11
- Wine (para rodar MT5 + Python Windows)
- MetaTrader 5 instalado no Wine
- Python 3.11 instalado no Wine (`C:\Python311`)
- MetaTrader5 Python package no Wine

### Setup

```bash
# 1. Clonar
git clone <repo>
cd Vibe-Trading

# 2. Instalar MT5 no Wine
./scripts/install_mt5.sh

# 3. Instalar Python no Wine (MT5 API)
# (manual — precisa do Python Windows com MetaTrader5 package)

# 4. Configurar
cp vt_config.json.example vt_config.json
# Editar vt_config.json com seus parâmetros

# 5. Testar conexão MT5
wine ~/.wine/drive_c/Python311/python.exe mt5_executor.py status

# 6. Rodar autotrader
PYTHONPATH=./agent ./agent/venv/bin/python vt_autotrader.py
```

### Shell helpers

```bash
./scripts/vt.sh buy WIN$ 1 200          # Compra rápida
./scripts/vt.sh sell WDON26 1 50        # Venda rápida
./scripts/vt_start.sh                   # Startup completo (MT5 + autotrader)
./scripts/start_autotrader.sh           # Só o autotrader
```

## Estrutura de Diretórios

```
Vibe-Trading/
├── vt_autotrader.py          # Daemon principal
├── vt_config.json            # Configuração central (hot-reload)
├── vt_config_loader.py       # Loader com hot-reload por mtime
├── vt_strategy_loader.py      # Carrega plugins de estratégia
├── vt_trade_log.py           # SQLite trade log + relatório IR
├── vt_analyst.py             # Detecção de eventos em tempo real
├── vt_daily_report.py       # Relatório diário automático
├── vt_copilot.py             # Health check + reconciliação
├── vt_tax_report.py          # Relatório fiscal (IR)
├── vt_resolve_symbols.py     # Resolve símbolos MT5
├── mt5_orchestrator.py       # Interface Linux → MT5 (Wine bridge)
├── mt5_executor.py           # Executor Windows (roda dentro Wine)
├── mt5_fetch.py              # Coleta dados do MT5
├── mt5_resolve.py            # Resolve símbolos (Wine side)
├── strategies/               # Plugins de estratégia
│   ├── vwap.py              #   VWAP trend-continuation
│   ├── ema_pullback.py      #   EMA pullback trend-following
│   ├── strong_trend.py       #   Trend-following agressivo
│   ├── bollinger.py         #   Reversão à média
│   ├── ema_crossover.py     #   EMA crossover
│   ├── adx_trend.py         #   ADX trend-following
│   ├── macd_momentum.py     #   MACD momentum
│   └── win_reversion.py     #   Reversão específica WIN
├── backtest/                 # Backtests ativos
│   ├── backtest_agi_v11.py  #   Backtest principal (lê config)
│   ├── backtest_autotrader_v6.py # Versão anterior
│   └── backtest_multi_strategy.py # Compara estratégias
├── scripts/                  # Shell helpers
│   ├── install_mt5.sh       #   Instalação MT5
│   ├── vt_start.sh          #   Startup completo
│   ├── start_autotrader.sh   #   Launcher autotrader
│   ├── start_mt5linux.sh    #   Inicia MT5
│   ├── vt.sh                #   Wrapper CLI
│   └── vt-resolve.sh        #   Resolve símbolos
├── archive/                  # Scripts obsoletos (histórico)
│   ├── backtests/           #   Backtests antigos (v1-v7)
│   └── utils/               #   Scripts temporários/teste
├── data/                     # Dados CSV (OHLCV M5/M15)
├── agent/                    # Hermes Agent (orquestação)
├── prediction/               # Modelos preditivos (ML)
├── wiki/                     # Documentação wiki
├── docs/                     # Documentação técnica
├── tools/                    # CI e utilitários
└── vt_trades.db              # SQLite trade database
```

## Changelog

Veja [CHANGELOG.md](CHANGELOG.md) para histórico completo.

## Licença

MIT
