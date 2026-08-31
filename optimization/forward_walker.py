"""
forward_walker.py — Vibe-Trading AGI forward-only (mercado real, sem ordem)

REGRA DE OURO (Bruno 16/07): nunca treinar em trades passados. Otimizar no
mercado real simulando valores, sem enviar ordem ao broker.

O que faz:
  1. Conecta no MT5 (read-only — NÃO chama order_send em hipótese alguma)
  2. Loop contínuo: pra cada (symbol, tf) ativo, busca candles live via fetch_bars
  3. Replica o dispatch da estratégia do autotrader (mesma função check_entry)
  4. Quando check_entry retorna sinal → ABRE posição SIMULADA em memória,
     com SL/TP/trailing idênticos ao que o autotrader teria aplicado
  5. A cada candle novo, atualiza posições abertas (trailing, breakeven, TP1)
  6. Quando SL/TP/time-stop dispara → FECHA sim e grava em forward_sim_trades
  7. A cada N minutos imprime relatório forward (PnL, WR, PF, Sharpe, DD)
  8. Ao parar (Ctrl+C ou --duration-min), imprime relatório final consolidado

Diferenças do autotrader (intencionais):
  - entry_ticket = "SIM-{epoch_ms}" (não é ticket MT5 real)
  - magic_number = 555599 (reservado pra sim, não colide com 555501 do live)
  - Tabela própria forward_sim_trades (não toca `trades`)
  - Zero chamadas a mt5.order_send / mt5.positions_get / modify_sl

Modo --backfill (replay histórico, adicionado 16/08/2026): mesma semântica
(check_entry em candle fechado + gestão TP1/breakeven/trailing/hard/time +
gate aggregate_blackout do daemon — time_blocks/day_dir/events) sobre
candles HISTÓRICOS, gravando em tabela
ISOLADA forward_backfill_trades com run_id — o stage6 do AGI NÃO lê essa
tabela, então o sinal shadow do meio-dia continua vindo só do pregão ao
vivo. É validação contrafactual (padrão risk_calibrator), NÃO treinamento.
Rodar FORA do pregão (o script recusa dia útil 08-17h sem --force).

Uso:
  python3 optimization/forward_walker.py --duration-min 60
  python3 optimization/forward_walker.py --symbols WINQ26 --tfs M5
  python3 optimization/forward_walker.py --duration-min 30 --poll-secs 5

  # Backfill: 3 meses de replay do config atual (fim de semana/madrugada)
  python3 optimization/forward_walker.py --backfill --from 2026-05-01
  # A/B: mesmo período com run_id distinto pra comparar cenários depois
  python3 optimization/forward_walker.py --backfill --from 2026-06-01 --run-id baseline
  python3 optimization/forward_walker.py --backfill --from 2026-06-01 --run-id sem_bloco --ignore-time-blocks
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ─── path bootstrap (mesmo padrão do resto do repo) ────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "strategies"))

from core.vt_config_loader import load_config  # noqa: E402
from core import vt_autotrader as vat  # noqa: E402

CONFIG = load_config()
TRADES_DB = ROOT / "vt_trades.db"
SIM_TABLE = "forward_sim_trades"
SIM_MAGIC = 555599  # reservado pra forward sims
# Backfill (replay histórico): tabela ISOLADA com run_id. O stage6 do AGI
# (stage6_report.py:_shadow_today_summary) lê SOMENTE forward_sim_trades —
# backfill nunca contamina o sinal shadow do pregão atual.
BACKFILL_TABLE = "forward_backfill_trades"

# run_id do processo live (setado no main() antes do walker_loop). Cada walker
# live grava a própria partição — o gap-fill do cron deixa de ser indistinguível
# do PROD na mesma tabela (bug 31/08: 8 sinais WINZ26/M15 escritos 2x).
_LIVE_RUN_ID: str | None = None

# Telegram target — espelha scripts/check_symbols_active.py:155 e core/vt_autotrader.py:724
TELEGRAM_TARGET = os.environ.get(
    "VT_TELEGRAM_TARGET", "telegram:-1004284773048"
)

# ─── schema isolado (NÃO toca `trades`) ───────────────────────────────────────
SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {SIM_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ticket TEXT UNIQUE,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_sl REAL NOT NULL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_sl_price REAL,
    highest_price REAL,
    lowest_price REAL,
    gross_pnl_pts REAL DEFAULT 0,
    gross_pnl_brl REAL DEFAULT 0,
    fees_brl REAL DEFAULT 0,
    net_pnl_brl REAL DEFAULT 0,
    bars_held INTEGER DEFAULT 0,
    signal_detail TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_fwd_sym_tf ON {SIM_TABLE}(symbol, timeframe);
CREATE INDEX IF NOT EXISTS idx_fwd_entry ON {SIM_TABLE}(entry_time);
"""

# Wave 885 (2026-08-31): colunas novas do live (migração idempotente no
# ensure_schema, pois a tabela já existe no DB de produção):
#   - run_id: cada processo walker grava a própria partição (gap-fill do cron
#     rodava no MESMO pregão sem run_id → 2 walkers escreviam o mesmo sinal
#     2x em forward_sim_trades e o stage6 do AGI lia o shadow inflado).
#   - signal_bar_ts: epoch do candle fechado que gerou o sinal — identidade
#     estável entre walkers (entry_time virou relógio local, ver Fix entry_time)
#     e chave do skip-if-exists anti-dupla-escrita.
SIM_COLUMNS_NEW = ("run_id", "signal_bar_ts")


def ensure_schema() -> None:
    """Cria a tabela forward_sim_trades se não existir. Idempotente."""
    con = sqlite3.connect(str(TRADES_DB))
    try:
        con.executescript(SCHEMA_SQL)
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({SIM_TABLE})")}
        for _col in SIM_COLUMNS_NEW:
            if _col not in cols:
                con.execute(f"ALTER TABLE {SIM_TABLE} ADD COLUMN {_col} "
                            f"{'TEXT' if _col == 'run_id' else 'INTEGER'}")
        con.commit()
    finally:
        con.close()


# Mesmo schema do live + run_id (permite A/B: baseline vs cenário alternativo
# no mesmo período sem misturar amostras).
BACKFILL_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {BACKFILL_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ticket TEXT UNIQUE,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_sl REAL NOT NULL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_sl_price REAL,
    highest_price REAL,
    lowest_price REAL,
    gross_pnl_pts REAL DEFAULT 0,
    gross_pnl_brl REAL DEFAULT 0,
    fees_brl REAL DEFAULT 0,
    net_pnl_brl REAL DEFAULT 0,
    bars_held INTEGER DEFAULT 0,
    signal_detail TEXT,
    notes TEXT,
    run_id TEXT NOT NULL DEFAULT 'main',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_bf_run ON {BACKFILL_TABLE}(run_id);
CREATE INDEX IF NOT EXISTS idx_bf_entry ON {BACKFILL_TABLE}(entry_time);
"""


def ensure_backfill_schema() -> None:
    """Cria a tabela forward_backfill_trades se não existir. Idempotente."""
    con = sqlite3.connect(str(TRADES_DB))
    try:
        con.executescript(BACKFILL_SCHEMA_SQL)
        con.commit()
    finally:
        con.close()


# ─── position model (em memória, igual ao que o live faria) ────────────────────
@dataclass
class SimPosition:
    symbol: str
    timeframe: str
    strategy: str
    direction: str  # "BUY" | "SELL"
    volume: float
    entry_time: datetime
    entry_price: float
    # SL guardado em EXECUTOR UNITS (= distance_in_price / point_val).
    # Convenção do autotrader (manage_position linhas 2691-2692):
    #   sl_pts é SEMPRE POSITIVO no início (distância do entry).
    #   Após trailing/breakeven em lucro, vira NEGATIVO (profit-lock: SL acima do entry).
    #   cmd_modify: BUY SL = entry - sl_pts*point  → sl_pts<0 → SL acima entry ✓
    #               SELL SL = entry + sl_pts*point → sl_pts<0 → SL abaixo entry ✓
    initial_sl_pts: float
    current_sl_pts: float
    trail_activate_atr: float
    trail_distance_atr: float
    atr_at_entry: float
    be_after_minutes: float
    time_trail_after_minutes: float
    max_position_minutes: float
    hard_exit_minutes: float
    point_val: float = 1.0
    # TP1 — Wave N+2A (espelha autotrader manage_position:2517-2572)
    tp1_r: float = 1.0           # lucro em R*ATR pra disparar TP1
    tp1_pct: float = 0.5         # fração da posição original a fechar no TP1
    atr_trail_mult: float = 2.0  # trail apertado pós-TP1 (espelha vt_autotrader.py:2577)
    # state
    ticket: str = ""
    highest: float = 0.0
    lowest: float = 0.0
    current_atr: float = 0.0
    bars_held: int = 0
    trail_on: bool = False
    breakeven_applied: bool = False
    tp1_done: bool = False
    tp1_profit_brl: float = 0.0        # PnL acumulado do TP1 parcial (pra compor net_pnl_brl)
    tp1_volume_closed: float = 0.0     # volume já fechado no TP1
    remaining_volume: float = 0.0       # volume restante após TP1
    original_volume: float = 0.0        # volume original (imutável, base do TP1)
    # epoch do candle fechado que gerou o sinal — identidade do sinal p/ dedupe
    # (2 walkers no mesmo pregão) e init do last_bar_ts. 0 = legado/backfill sem ts.
    signal_bar_ts: int = 0
    last_bar_ts: int = 0  # epoch do último candle que processamos (pra detectar novo candle)
    notes: str = ""

    def __post_init__(self):
        self.ticket = f"SIM-{int(time.time() * 1000)}-{self.symbol}-{self.timeframe}"
        self.highest = self.entry_price
        self.lowest = self.entry_price
        self.last_bar_ts = self.signal_bar_ts or int(self.entry_time.timestamp())
        # TP1 state init (espelha autotrader manage_position:2523)
        self.original_volume = self.volume
        self.remaining_volume = self.volume
        self.tp1_done = False
        self.tp1_profit_brl = 0.0
        self.tp1_volume_closed = 0.0

    # ── preço absoluto do SL atual (pra check_exit + DB) ────────────────────
    # ESPELHA EXATAMENTE manage_position:2691-2692 do autotrader:
    #   BUY  SL = entry - sl_pts * point_val   (sl_pts pode ser NEGATIVO = profit lock)
    #   SELL SL = entry + sl_pts * point_val   (sl_pts pode ser NEGATIVO = profit lock)
    @property
    def current_sl_price(self) -> float:
        if self.direction == "BUY":
            return self.entry_price - self.current_sl_pts * self.point_val
        else:
            return self.entry_price + self.current_sl_pts * self.point_val

    @property
    def initial_sl_price(self) -> float:
        if self.direction == "BUY":
            return self.entry_price - abs(self.initial_sl_pts) * self.point_val
        else:
            return self.entry_price + abs(self.initial_sl_pts) * self.point_val

    @property
    def profit_pts(self) -> float:
        """Profit em pontos de preço (NÃO executor units)."""
        if self.direction == "BUY":
            return self.highest - self.entry_price
        else:
            return self.entry_price - self.lowest

    def update_extremes(self, bar: dict) -> None:
        h = bar["high"]
        low = bar["low"]
        if h > self.highest:
            self.highest = h
        if low < self.lowest:
            self.lowest = low

    def _set_sl_price(self, new_sl_price: float) -> bool:
        """Move current_sl_pts pra fazer SL = new_sl_price (se melhorar o lock).

        Convenção signed (espelhada de autotrader:2741-2745):
          sl_pts signed: positivo=abaixo entry (loss), negativo=acima entry (profit lock).
          BUY: novo SL acima do atual = melhor.
          SELL: novo SL abaixo do atual = melhor.
        """
        if self.direction == "BUY":
            if new_sl_price <= self.current_sl_price:
                return False
            new_sl_pts = (self.entry_price - new_sl_price) / self.point_val
            self.current_sl_pts = new_sl_pts  # signed: pode ficar negativo (profit lock)
            return True
        else:
            if new_sl_price >= self.current_sl_price:
                return False
            new_sl_pts = (new_sl_price - self.entry_price) / self.point_val
            self.current_sl_pts = new_sl_pts  # signed
            return True

    # ─── TP1 — fechamento parcial (espelha autotrader manage_position:2517-2572) ─
    def maybe_tp1(self, atr: float) -> bool:
        """Aplica TP1 parcial UMA vez se atingiu tp1_r * ATR de profit.

        Retorna True se TP1 foi aplicado neste bar.
        """
        if self.tp1_done or atr <= 0:
            return False
        if not (0 < self.tp1_pct < 1):
            return False
        profit_pts = self.profit_pts
        if profit_pts < self.tp1_r * atr:
            return False
        if self.remaining_volume <= 0:
            return False
        # Fração do volume original a fechar (mesma fórmula do autotrader:2527-2530)
        close_volume = self.original_volume * self.tp1_pct
        actual_close = min(close_volume, self.remaining_volume)
        if actual_close <= 0:
            self.tp1_done = True  # idempotente
            return False
        # PnL proporcional à fração fechada (mesma fórmula do autotrader:2545-2547)
        profit_pts_total = profit_pts
        if profit_pts_total > 0:
            tp1_pnl_pts = (actual_close / max(0.001, self.original_volume)) * profit_pts_total
        else:
            tp1_pnl_pts = 0.0
        multiplier = CONFIG.get("multiplier", 0.20)
        tp1_pnl_brl = tp1_pnl_pts * multiplier * actual_close
        self.tp1_profit_brl += tp1_pnl_brl
        self.tp1_volume_closed += actual_close
        self.remaining_volume = max(0.0, self.volume - self.tp1_volume_closed)
        self.tp1_done = True
        return True

    def apply_trailing(self, atr: float, held_minutes: float) -> None:
        """Replica EXATAMENTE manage_position (vt_autotrader.py:2628-2750).

        Ordem:
          1. TRAILING POR LUCRO — ativa ao atingir trail_activate * ATR (linha 2629)
          2. BREAKEVEN — após be_after_minutes sem trailing, move SL pra entry + custo (linha 2637-2661)
          3. TIME-BASED TRAILING — após time_trail_after_minutes, ativa trailing (linha 2664-2668)
          4. TRAILING STOP — aplica novo SL se trail_on (linha 2693-2714)
             - Após max_position_minutes, trail agressivo (0.3x ATR)
             - Pós-TP1, usa atr_trail_mult (mais apertado, linha 2577)
        """
        self.current_atr = atr
        if atr <= 0:
            return
        profit_pts = self.profit_pts

        # TP1 (Wave N+2A) — executa antes de trailing para que remaining_volume
        # já esteja correto. Espelha autotrader:2519-2572.
        self.maybe_tp1(atr)

        # ===== TRAILING POR LUCRO (autotrader:2629) =====
        if not self.trail_on and profit_pts >= self.trail_activate_atr * atr:
            self.trail_on = True

        # ===== BREAKEVEN (autotrader:2637-2661) =====
        # sl_pts signed: BE aperta SL pra perto do entry (cost_pts é positivo pequeno).
        if not self.trail_on and held_minutes >= self.be_after_minutes:
            cost_pts = 5 / self.point_val if self.point_val > 0 else 5
            # be_sl_pts é POSITIVO (SL abaixo do entry para BUY, acima do entry para SELL).
            # Em executor units: cost_pts = distância do entry. Se for MENOR que o sl_pts
            # atual (SL inicial largo), apertar pra cost_pts é MELHOR.
            if cost_pts < abs(self.current_sl_pts):
                if self.direction == "BUY":
                    be_price = self.entry_price + cost_pts * self.point_val
                else:
                    be_price = self.entry_price - cost_pts * self.point_val
                if self._set_sl_price(be_price):
                    self.breakeven_applied = True

        # ===== TIME-BASED TRAILING (autotrader:2664-2668) =====
        if not self.trail_on and held_minutes >= self.time_trail_after_minutes and profit_pts > 0:
            self.trail_on = True

        # ===== TRAILING STOP (autotrader:2693-2714) =====
        # trail_dist_cfg muda se TP1 já aconteceu (autotrader:2576-2577).
        if self.trail_on:
            if self.tp1_done:
                trail_dist_cfg = self.atr_trail_mult
            else:
                trail_dist_cfg = self.trail_distance_atr
            # Após max_position_minutes, trail agressivo 0.3x ATR (autotrader:2696-2697)
            if held_minutes >= self.max_position_minutes:
                trail_dist = 0.3 * atr
            else:
                trail_dist = trail_dist_cfg * atr
            if self.direction == "BUY":
                new_sl = self.highest - trail_dist
            else:
                new_sl = self.lowest + trail_dist
            self._set_sl_price(new_sl)

    def check_exit(self, bar: dict, held_minutes: float) -> tuple[bool, str, float]:
        """Retorna (should_exit, reason, exit_price).

        Ordem de prioridade (espelha autotrader manage_position):
          HARD_EXIT > SL > TIME_MAX_NEG
        """
        # HARD EXIT — após hard_exit_minutes, força exit a mercado (autotrader:2593).
        if held_minutes >= self.hard_exit_minutes:
            return True, "HARD_EXIT", bar["close"]

        # SL — usa low/high do candle; se tocou, sai no preço do SL.
        sl_price = self.current_sl_price
        if self.direction == "BUY":
            if bar["low"] <= sl_price:
                return True, "SL", sl_price
        else:
            if bar["high"] >= sl_price:
                return True, "SL", sl_price

        # TIME_MAX_NEG — após max_position_minutes e no prejuízo, fecha a mercado.
        if held_minutes >= self.max_position_minutes:
            last = bar["close"]
            if self.direction == "BUY" and last < self.entry_price:
                return True, "TIME_MAX_NEG", last
            if self.direction == "SELL" and last > self.entry_price:
                return True, "TIME_MAX_NEG", last

        return False, "", 0.0


# ─── broker-free helpers ──────────────────────────────────────────────────────
def is_broker_open(symbol: str) -> bool:
    """Símbolo aberto pra receber candles. Read-only via orchestrator (Wine).

    NOTA: `import mt5` aqui resolve pro pacote local (mt5/__init__.py) que
    NÃO expõe `symbol_info_tick` nem `symbol_info`. O path real é via
    `mt5.mt5_orchestrator.symbol_info` — que cruza Wine sem mandar ordem.
    """
    try:
        from mt5 import mt5_orchestrator as _orch  # type: ignore
        info = _orch.symbol_info(symbol)
        return info is not None and "error" not in info
    except Exception:
        return True  # fail-open: deixa o fetch_bars() decidir com base em bars vazias


def fetch_bars(symbol: str, tf: str, count: int = 100) -> list:
    """Wrapper read-only — mesma função que o autotrader usa."""
    return vat.fetch_bars(symbol, tf, count)


def get_strategy_func(strategy_name: str):
    """Lookup dinâmico igual ao autotrader."""
    return vat.get_strategy_func(strategy_name)


def get_params_for_pair(symbol_root: str, tf: str) -> dict:
    """Pega params do config — mesma fonte que o autotrader usa.

    O autotrader expõe `_get_params_for_tf(symbol_root, tf)` (NÃO
    `get_params_for_pair` — esse nome não existe em vt_autotrader).
    Aqui só encapsulamos pra dar uma API mais limpa.
    """
    return vat._get_params_for_tf(symbol_root, tf)


# Mapa de point value por símbolo raiz (espelha manage_position do autotrader).
# sl_pts e distâncias de trailing vêm em EXECUTOR UNITS onde
# distance_price = sl_pts * point_val. Sem essa conversão, WDO/BIT/DOL/IND/WSP
# ficam com SL/TP 100-1000x errados (1 pt_executor = 0.001..0.01 preço).
POINT_VAL_MAP = {
    "WIN": 1.0, "IND": 1.0,
    "WDO": 0.001, "DOL": 0.001,
    "BIT": 0.01, "WSP": 0.01,
}


def symbol_root_of(symbol: str) -> str:
    """Extrai a raiz (WIN/WDO/BIT/DOL/IND/WSP) a partir do contrato (WINQ26 etc)."""
    for root in POINT_VAL_MAP:
        if root in symbol:
            return root
    return "WIN"


def resolve_contract(symbol_or_root: str) -> str:
    """Resolve symbol_root -> contrato (ex: WIN -> WINQ26) via CONFIG.

    Se já é um contrato (não está em resolved_symbols e contém letra/sufixo),
    devolve como está. Caso contrário devolve symbol_or_root inalterado.
    """
    resolved = CONFIG.get("resolved_symbols", {}) or {}
    if symbol_or_root in resolved:
        return resolved[symbol_or_root]
    return symbol_or_root


def is_tf_disabled(symbol_root: str, tf: str) -> bool:
    """Checa vt_config.json:disabled_timeframes (lista de 'SYM_TF').

    Critico: 16/07 walker rodou WSP_M5/M15 e WDO_M5 mesmo desabilitados
    (Wave G 15/07), contaminando amostra forward. Walker NÃO É writer de
    config, então ele apenas pula silenciosamente.
    """
    disabled = CONFIG.get("disabled_timeframes", []) or []
    key = f"{symbol_root}_{tf}"
    return key in disabled


def is_symbol_disabled(symbol_root: str) -> bool:
    """Checa vt_config.json:disabled_symbols (lista de roots)."""
    disabled = CONFIG.get("disabled_symbols", []) or []
    return symbol_root in disabled or symbol_root.lower() in [s.lower() for s in disabled]


# ─── core walker ──────────────────────────────────────────────────────────────
@dataclass
class WalkerState:
    positions: dict[tuple[str, str], SimPosition] = field(default_factory=dict)
    # estado de "última entry" pra anti-re-entry: (symbol,tf) -> epoch do último signal visto
    last_signal_seen_at: dict[tuple[str, str], int] = field(default_factory=dict)
    # epoch do último bar fechado (bars[1]) visto por par — entry só dispara em candle NOVO
    last_closed_bar_ts: dict[tuple[str, str], int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.now)
    total_bars_processed: int = 0
    total_signals_seen: int = 0
    total_signals_executed: int = 0
    report_history: list[dict] = field(default_factory=list)
    last_report_t: datetime = field(default_factory=datetime.now)


def open_sim_position(state: WalkerState, symbol: str, tf: str, strategy: str,
                      direction: str, price: float, sl_pts: float,
                      entry_time: datetime, atr: float, params: dict,
                      signal_detail: dict, bar_ts: int = 0) -> SimPosition:
    root = symbol_root_of(symbol)
    pos = SimPosition(
        symbol=symbol,
        timeframe=tf,
        strategy=strategy,
        direction=direction,
        # volume: live config guarda no root level (CONFIG["win"]["volume"]) —
        # tenta params, depois root config, depois root global, depois 1.0.
        volume=(
            params.get("volume")
            or CONFIG.get(symbol, {}).get("volume")
            or CONFIG.get(root.lower(), {}).get("volume")
            or CONFIG.get("volume", 1.0)
        ),
        entry_time=entry_time,
        entry_price=price,
        initial_sl_pts=sl_pts,
        current_sl_pts=sl_pts,
        point_val=POINT_VAL_MAP.get(root, 1.0),
        trail_activate_atr=params.get("trail_activate", 1.0),
        trail_distance_atr=params.get("trail_distance", 0.4),
        atr_at_entry=atr,
        be_after_minutes=params.get("breakeven_minutes", 10),
        time_trail_after_minutes=params.get("time_trail_minutes", 20),
        max_position_minutes=params.get("max_position_minutes", 60),
        hard_exit_minutes=params.get("hard_exit_minutes", 45),
        # TP1 (Wave N+2A) — defaults espelham autotrader manage_position:2517-2518
        tp1_r=params.get("tp1_r", 1.0),
        tp1_pct=params.get("tp1_pct", 0.5),
        atr_trail_mult=params.get("atr_trail_mult", 2.0),
        # default=str: estratégias podem botar datetime/Decimal no info
        # (crash real no backfill 16/08 — json.dumps levanta TypeError).
        notes=json.dumps(signal_detail, ensure_ascii=False, default=str)[:500],
        signal_bar_ts=bar_ts,
    )
    state.positions[(symbol, tf)] = pos
    state.total_signals_executed += 1
    return pos


def close_sim_position(con: sqlite3.Connection, pos: SimPosition,
                       exit_price: float, exit_reason: str, exit_time: datetime,
                       bars_held: int, table: str = SIM_TABLE,
                       run_id: str | None = None) -> dict | None:
    """Calcula PnL (composição TP1 + restante) e grava no DB. Retorna dict com métricas.

    table/run_id: backfill grava em forward_backfill_trades com o run_id
    explícito do replay; live grava em forward_sim_trades com _LIVE_RUN_ID
    (Wave 885: antes o live gravava sem run_id — 2 walkers no mesmo pregão
    duplicavam sinais e o stage6 do AGI lia o shadow inflado).

    Wave 885: no live, skip-if-exists por (symbol, tf, strategy, direction,
    signal_bar_ts) — o 2º walker que processar o MESMO candle de sinal não
    re-escreve o trade. Retorna None quando pula o INSERT (dup detectada).
    """
    if table == SIM_TABLE and pos.signal_bar_ts:
        _dup = con.execute(
            f"""SELECT 1 FROM {table}
                WHERE symbol=? AND timeframe=? AND strategy=? AND direction=?
                  AND signal_bar_ts=? LIMIT 1""",
            (pos.symbol, pos.timeframe, pos.strategy, pos.direction,
             pos.signal_bar_ts),
        ).fetchone()
        if _dup:
            print(f"  [SKIP-DUP] {pos.symbol}/{pos.timeframe} {pos.direction} "
                  f"sinal_bar_ts={pos.signal_bar_ts} já registrado — INSERT ignorado")
            return None
    if run_id is None and table == SIM_TABLE:
        run_id = _LIVE_RUN_ID
    if pos.direction == "BUY":
        gross_pts_remaining = exit_price - pos.entry_price
    else:
        gross_pts_remaining = pos.entry_price - exit_price
    multiplier = CONFIG.get("multiplier", 0.20)  # WIN mini
    # Volume que sobra após TP1 parcial → paga esse trecho a exit_price.
    gross_brl_remaining = gross_pts_remaining * pos.remaining_volume * multiplier
    # TP1 parcial já foi contabilizado em pos.tp1_profit_brl (BRL).
    # Fees: cada contrato pago 2x (entrada + saída). TP1 = 1 op extra.
    fees_per_leg = 0.50
    fees_brl = pos.volume * fees_per_leg * 2  # full cycle do volume original
    if pos.tp1_volume_closed > 0:
        # +1 leg adicional por causa do TP1 parcial
        fees_brl += pos.tp1_volume_closed * fees_per_leg * 2
    net_brl = pos.tp1_profit_brl + gross_brl_remaining - fees_brl

    # run_id: live e backfill gravam a partição do processo. signal_bar_ts só
    # existe no schema live (backfill dedupliza por DELETE run_id prévio).
    ts_extra = ", signal_bar_ts" if table == SIM_TABLE else ""
    ts_val = ", ?" if table == SIM_TABLE else ""
    ts_param = [pos.signal_bar_ts] if table == SIM_TABLE else []
    cols_extra = f"{ts_extra}, run_id" if run_id is not None else ts_extra
    vals_extra = f"{ts_val}, ?" if run_id is not None else ts_val
    params_extra = ts_param + ([run_id] if run_id is not None else [])
    con.execute(
        f"""INSERT INTO {table}
            (entry_ticket, symbol, timeframe, strategy, direction, volume,
             entry_time, entry_price, entry_sl, exit_time, exit_price,
             exit_reason, exit_sl_price, highest_price, lowest_price,
             gross_pnl_pts, gross_pnl_brl, fees_brl, net_pnl_brl,
             bars_held, signal_detail{cols_extra})
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{vals_extra})""",
        (
            pos.ticket, pos.symbol, pos.timeframe, pos.strategy, pos.direction,
            pos.volume, pos.entry_time.isoformat(sep=" ", timespec="seconds"),
            pos.entry_price, pos.initial_sl_price,
            exit_time.isoformat(sep=" ", timespec="seconds"),
            exit_price, exit_reason, pos.current_sl_price, pos.highest, pos.lowest,
            # gross_pnl_pts = (exit - entry) considerando volume restante
            # (tp1_pts está embutido em pos.tp1_profit_brl já em BRL).
            gross_pts_remaining,
            pos.tp1_profit_brl + gross_brl_remaining,
            fees_brl, net_brl, bars_held, pos.notes,
        ) + tuple(params_extra),
    )
    return {
        "symbol": pos.symbol, "tf": pos.timeframe, "strategy": pos.strategy,
        "direction": pos.direction, "gross_pts": gross_pts_remaining,
        "net_brl": net_brl, "bars_held": bars_held, "reason": exit_reason,
        "tp1_brl": pos.tp1_profit_brl, "tp1_volume_closed": pos.tp1_volume_closed,
    }


def compute_forward_metrics(con: sqlite3.Connection) -> dict:
    """Métricas forward-only — só conta sims FECHADAS.

    Wave 31/08: base das métricas virou net_pnl_brl (antes era gross_pnl_pts —
    o [FINAL] reportava WR/PF/totais em pontos brutos, ignorando fees/TP1).
    Dedupe por identidade do sinal: (symbol, tf, strategy, direction,
    signal_bar_ts); linhas legadas sem signal_bar_ts caem na chave
    (entry_time minuto, entry_price) — cobre os dups do gap-fill de 31/08.
    """
    rows = list(con.execute(
        f"""SELECT symbol, timeframe, strategy, direction,
                   gross_pnl_pts, net_pnl_brl, exit_reason,
                   signal_bar_ts, entry_time, entry_price
            FROM {SIM_TABLE}
            WHERE exit_time IS NOT NULL
              AND date(created_at) = date('now', 'localtime')
            ORDER BY id"""
    ))
    if not rows:
        return {"n": 0}
    seen: set = set()
    pnls = []
    for r in rows:
        sym, tf, strat, direction, _gpts, net, _reason, bar_ts, entry_time, entry_price = r
        if bar_ts:
            sig_key = (sym, tf, strat, direction, "bar", bar_ts)
        else:
            sig_key = (sym, tf, strat, direction, "legacy",
                       str(entry_time)[:16], round(entry_price or 0.0, 6))
        if sig_key in seen:
            continue
        seen.add(sig_key)
        pnls.append(net or 0.0)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    n = len(pnls)
    wr = wins / n if n else 0
    gross_wins = sum(p for p in pnls if p > 0)
    gross_losses = abs(sum(p for p in pnls if p < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf") if gross_wins > 0 else 0
    total = sum(pnls)
    # Sharpe simplificado (média / std)
    if len(pnls) > 1:
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(var)
        sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0
    else:
        sharpe = 0
    # Max DD (corrido, em R$ líquidos)
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return {
        "n": n, "wins": wins, "losses": losses, "wr": wr, "pf": pf,
        "total_brl": total, "sharpe": sharpe, "max_dd_brl": max_dd,
    }


def print_report(state: WalkerState, con: sqlite3.Connection, label: str,
                 min_trades: int = 0) -> dict:
    """Imprime relatório e retorna dict com métricas + by_pair."""
    m = compute_forward_metrics(con)
    elapsed = (datetime.now() - state.started_at).total_seconds() / 60
    open_pos = len(state.positions)
    print(f"\n{'=' * 60}")
    print(f"[{label}] +{elapsed:.1f}min | signals exec={state.total_signals_executed} "
          f"| bars={state.total_bars_processed} | open={open_pos}")
    print(f"  n={m.get('n', 0)} wins={m.get('wins', 0)} losses={m.get('losses', 0)} "
          f"WR={m.get('wr', 0) * 100:.1f}% PF={m.get('pf', 0):.2f} "
          f"Sharpe={m.get('sharpe', 0):.2f}")
    print(f"  total_brl={m.get('total_brl', 0):+.2f} "
          f"max_dd_brl={m.get('max_dd_brl', 0):+.2f}")
    # por par com WR + recomendação
    by_pair: dict[str, dict] = {}
    for r in con.execute(
        f"""SELECT symbol, timeframe, strategy, COUNT(*),
                   SUM(net_pnl_brl), AVG(gross_pnl_pts),
                   SUM(CASE WHEN net_pnl_brl > 0 THEN 1 ELSE 0 END) AS wins
            FROM {SIM_TABLE} t
            WHERE t.exit_time IS NOT NULL
              AND date(t.created_at) = date('now', 'localtime')
              AND t.id = (
                  SELECT MIN(t2.id) FROM {SIM_TABLE} t2
                  WHERE t2.exit_time IS NOT NULL
                    AND date(t2.created_at) = date('now', 'localtime')
                    AND t2.symbol = t.symbol AND t2.timeframe = t.timeframe
                    AND t2.strategy = t.strategy AND t2.direction = t.direction
                    AND (
                        (t.signal_bar_ts IS NOT NULL
                         AND t2.signal_bar_ts = t.signal_bar_ts)
                        OR (t.signal_bar_ts IS NULL AND t2.signal_bar_ts IS NULL
                            AND substr(t2.entry_time, 1, 16) = substr(t.entry_time, 1, 16)
                            AND abs(t2.entry_price - t.entry_price) < 0.0000001)
                    )
              )
            GROUP BY symbol, timeframe"""
    ):
        sym, tf, strat, n, pnl, avg_pts, wins = r
        n = n or 0
        wins = wins or 0
        wr = wins / n if n else 0
        rec = recommend(pnl or 0, n, wr)
        by_pair[f"{sym}/{tf}"] = {
            "symbol": sym, "tf": tf, "strategy": strat, "n": n,
            "wins": wins, "wr": wr, "pnl": pnl or 0,
            "avg_pts": avg_pts or 0, "recommendation": rec,
        }
    if by_pair:
        print("  por par:")
        for k, v in sorted(by_pair.items(), key=lambda x: -x[1]["pnl"]):
            tag = "  " if v["n"] >= min_trades else " (?)"
            print(f"    {k:<14}{tag} n={v['n']:>3} WR={v['wr']*100:>4.0f}% "
                  f"PnL R$ {v['pnl']:>+8.2f} avg {v['avg_pts']:+.1f}pts "
                  f"→ {v['recommendation']}")
    print(f"{'=' * 60}")
    return {"metrics": m, "by_pair": by_pair, "elapsed_min": elapsed}


def recommend(pnl: float, n: int, wr: float) -> str:
    """Heurística KEEP / ADJUST / DISABLE baseada em PnL forward, WR e n.

    Mesma lógica do skill vibe-trading-agi-tuning-defensive (Pitfall 23):
    n<5 → ruído amostral → INCONCLUSIVE.
    """
    if n < 5:
        return "INCONCLUSIVE"
    if pnl > 0 and wr >= 0.30:
        return "KEEP"
    if pnl > 0 and wr >= 0.20:
        return "KEEP_TIGHT"   # edge fraco, monitorar
    if pnl <= -500:
        return "DISABLE"
    if pnl < 0 and wr < 0.25:
        return "DISABLE"
    return "ADJUST"


# ─── drift detection: walker vs live `trades` table ──────────────────────────
def check_drift(con: sqlite3.Connection, pairs: list[tuple[str, str]]) -> None:
    """Compara contagem de trades forward (última 1h) vs `trades` live (última 1h).

    Emite linha stderr `drift: walker_n=X, live_n=Y, ratio=Z` quando divergem
    significativamente (ratio < 0.3 ou > 3.0).
    """
    for sym, tf in pairs:
        try:
            walker_n = con.execute(
                f"""SELECT COUNT(*) FROM {SIM_TABLE}
                    WHERE symbol=? AND timeframe=?
                      AND exit_time IS NOT NULL
                      AND datetime(exit_time) >= datetime('now','-1 hours')""",
                (sym, tf),
            ).fetchone()[0]
            walker_pnl = con.execute(
                f"""SELECT COALESCE(SUM(net_pnl_brl),0) FROM {SIM_TABLE}
                    WHERE symbol=? AND timeframe=?
                      AND exit_time IS NOT NULL
                      AND datetime(exit_time) >= datetime('now','-1 hours')""",
                (sym, tf),
            ).fetchone()[0]
            live_n = con.execute(
                """SELECT COUNT(*) FROM trades
                   WHERE symbol=? AND timeframe=?
                     AND exit_time IS NOT NULL
                     AND datetime(exit_time) >= datetime('now','-1 hours')""",
                (sym, tf),
            ).fetchone()[0]
            live_pnl = con.execute(
                """SELECT COALESCE(SUM(net_pnl),0) FROM trades
                   WHERE symbol=? AND timeframe=?
                     AND exit_time IS NOT NULL
                     AND datetime(exit_time) >= datetime('now','-1 hours')""",
                (sym, tf),
            ).fetchone()[0]
        except sqlite3.OperationalError as e:
            # tabela trades pode não existir em testes isolados
            sys.stderr.write(f"[drift] {sym}/{tf} skip: {e}\n")
            continue
        # ratio de contagem; ambos zero = ratio 1.0 (sem drift)
        if walker_n == 0 and live_n == 0:
            continue
        if live_n == 0:
            ratio = float("inf") if walker_n > 0 else 1.0
        else:
            ratio = walker_n / live_n
        line = (
            f"drift: {sym}/{tf} walker_n={walker_n} live_n={live_n} "
            f"ratio={ratio:.2f} walker_pnl=R${walker_pnl:+.2f} live_pnl=R${live_pnl:+.2f}"
        )
        if ratio < 0.3 or ratio > 3.0:
            sys.stderr.write(f"[DRIFT] {line}\n")
        else:
            sys.stderr.write(f"drift: {line}\n")


# ─── telegram summary delivery ───────────────────────────────────────────────
def send_telegram_summary(con: sqlite3.Connection, state: WalkerState,
                          args, by_pair: dict) -> bool:
    """Envia summary consolidado via core.vt_hermes_helper.hermes_send.

    Retorna True se enviado OK.
    """
    try:
        from core.vt_hermes_helper import hermes_send
    except Exception as e:
        print(f"[TELEGRAM] helper indisponível: {e}")
        return False
    if not by_pair:
        print("[TELEGRAM] sem trades — pulando summary")
        return False
    # Top winner / worst loser
    sorted_pairs = sorted(by_pair.items(), key=lambda x: -x[1]["pnl"])
    top = sorted_pairs[0]
    worst = sorted_pairs[-1]
    total_pnl = sum(v["pnl"] for v in by_pair.values())
    total_n = sum(v["n"] for v in by_pair.values())
    # 7d live baseline
    try:
        live_7d = con.execute(
            """SELECT COALESCE(SUM(net_pnl),0), COUNT(*)
               FROM trades
               WHERE exit_time IS NOT NULL
                 AND datetime(exit_time) >= datetime('now','-7 days')"""
        ).fetchone()
        live_7d_pnl, live_7d_n = live_7d
    except sqlite3.OperationalError:
        live_7d_pnl, live_7d_n = 0.0, 0
    # mount msg
    pairs_lines = []
    for k, v in sorted_pairs:
        if v["n"] >= args.min_trades:
            pairs_lines.append(
                f"• {k:<14} n={v['n']:>3} WR={v['wr']*100:>4.0f}% "
                f"R${v['pnl']:>+8.2f} → {v['recommendation']}"
            )
        else:
            pairs_lines.append(
                f"• {k:<14} n={v['n']:>3} (skip, n<{args.min_trades})"
            )
    msg = (
        f"📊 *FORWARD WALKER — {datetime.now():%Y-%m-%d %H:%M}*\n"
        f"Duração: {state.started_at:%H:%M} → {datetime.now():%H:%M} "
        f"({(datetime.now()-state.started_at).total_seconds()/60:.0f}min)\n"
        f"Pares: {', '.join(args.symbols)} | TFs: {', '.join(args.tfs)}\n\n"
        f"*PnL forward:* R$ {total_pnl:+.2f} (n={total_n})\n"
        f"Top: {top[0]} R$ {top[1]['pnl']:+.2f}\n"
        f"Worst: {worst[0]} R$ {worst[1]['pnl']:+.2f}\n\n"
        f"Compared to last 7d live: live_pnl=R$ {live_7d_pnl:+.2f} "
        f"(n={live_7d_n}), forward_pnl=R$ {total_pnl:+.2f}, "
        f"delta=R$ {total_pnl - live_7d_pnl:+.2f}\n\n"
        f"*Por par:*\n" + "\n".join(pairs_lines)
    )
    print(f"[TELEGRAM] enviando summary ({len(msg)} chars) → {TELEGRAM_TARGET}")
    ok = hermes_send(TELEGRAM_TARGET, msg, timeout=20)
    if ok:
        print("[TELEGRAM] OK")
    else:
        print("[TELEGRAM] FALHOU (hermes_send retornou False)")
    return ok


def walker_loop(args, state: WalkerState) -> None:
    """Loop principal — uma iteração por poll."""
    # CRITICAL: inicializa strategy_utils (KeyError senão).
    if not vat._strategy_utils:
        vat._init_strategy_utils()

    # WAL mode + busy_timeout: compartilha o DB com o autotrader sem causar
    # "database is locked" (lock contention faliu inserções do edge_estimator
    # durante o smoke 90min — autotrader perdeu 100+ inserts em 15min).
    con = sqlite3.connect(str(TRADES_DB), timeout=30.0)
    active_pairs: list[tuple[str, str]] = []  # default vazio pro finally
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")  # 30s
        con.commit()
    except Exception as _e:
        print(f"[WARN] Não consegui ativar WAL: {_e} — seguindo com default")
    try:
        deadline = datetime.now() + timedelta(minutes=args.duration_min)
        report_interval = timedelta(minutes=args.report_every_min)
        drift_interval = timedelta(seconds=60)  # spec: a cada 60s
        last_drift_t = datetime.now() - drift_interval  # dispara no 1º poll

        # Coletar pares ativos (resolved → contract) pra drift
        for s in args.symbols:
            sym = resolve_contract(s)
            for tf in args.tfs:
                if is_tf_disabled(symbol_root_of(sym), tf):
                    continue
                active_pairs.append((sym, tf))
        print(f"[drift] monitorando {len(active_pairs)} pares contra `trades` (1h window)")

        while datetime.now() < deadline:
            try:
                for symbol_or_root in args.symbols:
                    # resolve WIN → WINQ26 (ou contrato já é contrato, devolve igual)
                    symbol = resolve_contract(symbol_or_root)
                    root = symbol_root_of(symbol)
                    # GATE: pula pares desabilitados na config live (não é writer,
                    # apenas respeita). 16/07 walker contaminou forward com WSP/WDO
                    # que estão pausados por decisão Wave G 15/07.
                    if is_symbol_disabled(root):
                        continue
                    for tf in args.tfs:
                        if is_tf_disabled(root, tf):
                            continue
                        # pula se já tem posição aberta nesse slot
                        if (symbol, tf) in state.positions:
                            # atualiza posição aberta com último bar
                            bars = fetch_bars(symbol, tf, args.bars_count)
                            if not bars or len(bars) < 2:
                                continue
                            pos = state.positions[(symbol, tf)]
                            current_bar = bars[0]
                            atr_now = vat.calculate_atr(bars, 14)
                            held_min = (datetime.now() - pos.entry_time).total_seconds() / 60
                            pos.update_extremes(current_bar)
                            pos.apply_trailing(atr_now, held_min)
                            should_exit, reason, exit_px = pos.check_exit(current_bar, held_min)
                            # só conta novo bar se timestamp mudou
                            new_bar_ts = current_bar.get("time", 0)
                            if new_bar_ts != pos.last_bar_ts:
                                pos.bars_held += 1
                                pos.last_bar_ts = new_bar_ts
                                state.total_bars_processed += 1
                            if should_exit:
                                res = close_sim_position(con, pos, exit_px, reason,
                                                          datetime.now(), pos.bars_held)
                                con.commit()  # commit por close (não a cada poll)
                                if res:
                                    print(f"  [CLOSE] {symbol} {tf} {pos.direction} "
                                          f"@{exit_px:.0f} reason={reason} "
                                          f"pts={res['gross_pts']:+.0f} R$ {res['net_brl']:+.2f}")
                                del state.positions[(symbol, tf)]
                            continue

                        # sem posição → checa entry (SÓ EM CANDLE NOVO + EM HORÁRIO)
                        # CRITICAL: bars[1] é o último candle FECHADO — entre polls ele
                        # só muda quando um candle novo FECHA. Re-rodar check_entry no
                        # mesmo bar produz o mesmo sinal → re-entry loop (bug achado no
                        # smoke de 90min: 386 trades inflados de 2 sinais reais).
                        # --force-trading-time (DEV/SMOKE): ignora pregão pra validar E2E.
                        if not args.force_trading_time and not vat.is_trading_time():
                            continue
                        bars = fetch_bars(symbol, tf, args.bars_count)
                        if not bars or len(bars) < args.bars_count:
                            continue
                        atr = vat.calculate_atr(bars, 14)
                        if atr == 0:
                            continue
                        last_close = bars[1]["close"]
                        last_bar_ts_unix = bars[1]["time"]
                        # GATE: só processa se bars[1] mudou desde o último poll
                        prev_ts = state.last_closed_bar_ts.get((symbol, tf), 0)
                        if last_bar_ts_unix == prev_ts:
                            continue  # mesmo candle, pula
                        state.last_closed_bar_ts[(symbol, tf)] = last_bar_ts_unix
                        last_bar_ts = datetime.fromtimestamp(last_bar_ts_unix)
                        # dispatch igual ao autotrader (passa ROOT, não contrato)
                        strategy_name = vat._get_strategy_for_tf(root, tf)
                        if not strategy_name:
                            continue
                        params = get_params_for_pair(root, tf)
                        strat_func = get_strategy_func(strategy_name)
                        if not strat_func:
                            continue
                        result = strat_func(
                            symbol, tf, last_close, atr,
                            bar_ts=last_bar_ts, bars=bars,
                            params=params, utils=vat._strategy_utils,
                        )
                        state.total_signals_seen += 1
                        if not result:
                            continue
                        # ANTI-RE-ENTRY COOLDOWN: se já vimos signal igual recente,
                        # não re-executa (proteção extra além do gate de candle novo).
                        sig_key = (symbol, tf, strategy_name, result["direction"])
                        last_seen = state.last_signal_seen_at.get(sig_key[:2], 0)
                        # cooldown = 1 candle em segundos (5min pra M5, 15min pra M15)
                        tf_secs = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                                   "H1": 3600, "H4": 14400}.get(tf, 300)
                        if last_seen and (last_bar_ts_unix - last_seen) < tf_secs:
                            continue
                        state.last_signal_seen_at[sig_key[:2]] = last_bar_ts_unix
                        # ABRE SIM (não toca broker)
                        direction = result["direction"]
                        sl_pts = result["sl_pts"]
                        # entry_sl_price (pra log) — calcula via point_val
                        pv = POINT_VAL_MAP.get(root, 1.0)
                        if direction == "BUY":
                            entry_sl_price = last_close - sl_pts * pv
                        else:
                            entry_sl_price = last_close + sl_pts * pv
                        # Wave 885 fix: entry_time vira relógio local do walker — o
                        # timestamp do candle chega ~3h atrasado do MT5 via Wine
                        # (mesma defasagem do tick().time) e envenenava o held_min
                        # (posição "nascia" com ~180min → hard_exit no 1º poll —
                        # os 36/36 HARD_EXIT do relatório 31/08). O ts do candle
                        # segue registrado em signal_bar_ts p/ dedupe/auditoria.
                        pos = open_sim_position(
                            state, symbol, tf, strategy_name, direction,
                            last_close, sl_pts, datetime.now(), atr, params,
                            result.get("info", {}), bar_ts=last_bar_ts_unix,
                        )
                        print(f"  [OPEN]  {symbol} {tf} {strategy_name} {direction} "
                              f"@{last_close:.0f} sl@{entry_sl_price:.0f} "
                              f"(atr={atr:.0f}, sl_pts={sl_pts:.0f}, pv={pv})")
                        # sem commit aqui — open não muda DB, só close persiste

                # relatório periódico + drift check (60s)
                now = datetime.now()
                if now - state.last_report_t >= report_interval:
                    print_report(state, con, f"REPORT @{now:%H:%M:%S}",
                                 min_trades=args.min_trades)
                    state.last_report_t = now
                if now - last_drift_t >= drift_interval:
                    check_drift(con, active_pairs)
                    last_drift_t = now

                time.sleep(args.poll_secs)

            except KeyboardInterrupt:
                print("\n[!] KeyboardInterrupt — fechando posições abertas…")
                for (sym, tf), pos in list(state.positions.items()):
                    bars = fetch_bars(sym, tf, 5)
                    if bars:
                        exit_px = bars[0]["close"]
                        close_sim_position(con, pos, exit_px, "WALKER_STOP",
                                           datetime.now(), pos.bars_held)
                con.commit()
                break
            except Exception as e:
                print(f"[ERROR loop] {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(args.poll_secs)

        # fim do tempo — fecha abertas
        print("\n[DEADLINE] Fechando posições abertas…")
        for (sym, tf), pos in list(state.positions.items()):
            bars = fetch_bars(sym, tf, 5)
            if bars:
                exit_px = bars[0]["close"]
                close_sim_position(con, pos, exit_px, "DEADLINE",
                                   datetime.now(), pos.bars_held)
        con.commit()

    finally:
        con.close()
        # relatório final
        con = sqlite3.connect(str(TRADES_DB))
        try:
            final = print_report(state, con,
                                 f"FINAL @{datetime.now():%H:%M:%S}",
                                 min_trades=args.min_trades)
            # drift final + telegram
            print("\n[drift] check final:")
            check_drift(con, active_pairs)
            if not args.no_telegram:
                send_telegram_summary(con, state, args, final.get("by_pair", {}))
            else:
                print("[TELEGRAM] --no-telegram set, summary não enviado")
        finally:
            con.close()


# ─── backfill: replay histórico (validação contrafactual) ────────────────────
# Bruno 16/08: o walker live acumula ~1 pregão de amostra por dia — histórico
# pequeno demais pra validar filtros de horário/gestão. O modo --backfill
# replica a MESMA semântica do walker sobre candles históricos do MT5:
#   - entry: check_entry no candle FECHADO i (sim_bars[1] = i, espelho do live)
#   - gestão: barra subsequente (i+1) faz o papel da barra "formando" do poll
#     (extremos, TP1, breakeven, trailing, SL/time/hard exits)
#   - EOD: daemon fecha tudo às 16:45; replay fecha na virada de data
#   - gate time_blocks: mesma função _is_blocked_time do daemon (contrafactual
#     A/B futuro: --ignore-time-blocks remove o gate)
# Fidelidade máxima com o live, zero ordens, tabela isolada com run_id.
TF_SECS_MAP = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
               "H1": 3600, "H4": 14400}


def backfill_pair(con: sqlite3.Connection, state: WalkerState, symbol: str,
                  tf: str, args, run_id: str,
                  date_from, date_to) -> int:
    """Replay bar-a-bar de um par. Retorna nº de trades fechados."""
    root = symbol_root_of(symbol)
    window = args.bars_count  # mesma janela de check_entry do live (default 100)
    bars = fetch_bars(symbol, tf, args.backfill_bars)
    if not bars or len(bars) < window + 2:
        print(f"  [BACKFILL] {symbol} {tf}: barras insuficientes "
              f"({len(bars or [])} < {window + 2}) — pulando")
        return 0
    # fetch vem newest-first; cronológico facilita o replay. Recorta --from/--to.
    chron = [b for b in reversed(bars)
             if date_from <= datetime.fromtimestamp(b["time"]).date() <= date_to]
    if len(chron) < window + 2:
        print(f"  [BACKFILL] {symbol} {tf}: só {len(chron)} barras em "
              f"[{date_from}..{date_to}] — pulando")
        return 0
    earliest = datetime.fromtimestamp(chron[0]["time"])
    if earliest.date() > date_from:
        print(f"  [BACKFILL] {symbol} {tf}: ATENÇÃO — histórico disponível "
              f"começa em {earliest:%Y-%m-%d}, não em {date_from} (limite do "
              f"broker/terminal para este TF)")
    last_dt = datetime.fromtimestamp(chron[-1]["time"])
    print(f"  [BACKFILL] {symbol} {tf}: {len(chron)} barras "
          f"({earliest:%Y-%m-%d} → {last_dt:%Y-%m-%d})")

    tf_secs = TF_SECS_MAP.get(tf, 300)
    key = (symbol, tf)
    n_closed = 0
    strategy_name = vat._get_strategy_for_tf(root, tf)
    if not strategy_name:
        print(f"  [BACKFILL] {symbol} {tf}: sem estratégia mapeada — pulando")
        return 0
    params = get_params_for_pair(root, tf)
    strat_func = get_strategy_func(strategy_name)
    if not strat_func:
        print(f"  [BACKFILL] {symbol} {tf}: estratégia {strategy_name} não carrega — pulando")
        return 0

    # i = índice do candle que ACABOU DE FECHAR; chron[i+1] faz o papel da
    # barra formando (mesma forma de sim_bars do poll live: [formando, fechada, ...])
    for i in range(window - 1, len(chron) - 1):
        forming = chron[i + 1]
        closed = chron[i]
        closed_dt = datetime.fromtimestamp(closed["time"])
        forming_dt = datetime.fromtimestamp(forming["time"])
        sim_bars = [forming] + chron[i - window + 2: i + 1]
        pos = state.positions.get(key)

        # EOD: daemon fecha tudo às 16:45 — no replay, fecha na virada de data
        # (posição nunca atravessa a noite, igual ao live)
        if pos is not None and i > 0:
            prev_dt = datetime.fromtimestamp(chron[i - 1]["time"])
            if closed_dt.date() != prev_dt.date():
                res = close_sim_position(
                    con, pos, chron[i - 1]["close"], "EOD",
                    prev_dt, pos.bars_held,
                    table=BACKFILL_TABLE, run_id=run_id,
                )
                con.commit()
                n_closed += 1
                print(f"  [CLOSE] {symbol} {tf} {pos.direction} "
                      f"@{chron[i - 1]['close']:.0f} reason=EOD "
                      f"pts={res['gross_pts']:+.0f} R$ {res['net_brl']:+.2f}")
                del state.positions[key]
                pos = None

        if pos is not None:
            # gestão com a barra subsequente (formando): espelho do poll live
            held_min = (forming_dt - pos.entry_time).total_seconds() / 60
            pos.update_extremes(forming)
            atr_now = vat.calculate_atr(sim_bars, 14)
            pos.apply_trailing(atr_now, held_min)
            new_ts = forming.get("time", 0)
            if new_ts != pos.last_bar_ts:
                pos.bars_held += 1
                pos.last_bar_ts = new_ts
                state.total_bars_processed += 1
            should_exit, reason, exit_px = pos.check_exit(forming, held_min)
            if should_exit:
                res = close_sim_position(
                    con, pos, exit_px, reason, forming_dt, pos.bars_held,
                    table=BACKFILL_TABLE, run_id=run_id,
                )
                con.commit()
                n_closed += 1
                print(f"  [CLOSE] {symbol} {tf} {pos.direction} @{exit_px:.0f} "
                      f"reason={reason} pts={res['gross_pts']:+.0f} "
                      f"R$ {res['net_brl']:+.2f}")
                del state.positions[key]
            continue  # live: poll com posição aberta não avalia entry

        # ── sem posição → avalia entry no candle fechado (igual ao live) ──
        atr = vat.calculate_atr(sim_bars, 14)
        if atr == 0:
            continue
        result = strat_func(
            symbol, tf, closed["close"], atr,
            bar_ts=closed_dt, bars=sim_bars,
            params=params, utils=vat._strategy_utils,
        )
        state.total_signals_seen += 1
        if not result:
            continue
        # Gate de blackout do daemon (Wave N+4A): MESMA função e MESMO ts
        # (bar_ts do candle fechado) que o live usa em check_and_trade —
        # cobre time_blocks + day_direction + events (feriado é implícito:
        # candle só existe em dia de pregão). Avaliado APÓS o sinal, como no
        # daemon. --ignore-time-blocks pula o gate (braço B de contrafactual).
        if not args.ignore_time_blocks:
            from core.vt_calendar import aggregate_blackout
            _blocked, _reason = aggregate_blackout(
                symbol, result["direction"],
                config=CONFIG, ts=closed_dt,
            )
            if _blocked:
                continue
        # ANTI-RE-ENTRY COOLDOWN — mesma regra do live (1 candle por TF)
        last_seen = state.last_signal_seen_at.get(key, 0)
        if last_seen and (closed["time"] - last_seen) < tf_secs:
            continue
        state.last_signal_seen_at[key] = closed["time"]
        direction = result["direction"]
        sl_pts = result["sl_pts"]
        pv = POINT_VAL_MAP.get(root, 1.0)
        open_sim_position(
            state, symbol, tf, strategy_name, direction,
            closed["close"], sl_pts, closed_dt, atr, params,
            result.get("info", {}), bar_ts=int(closed["time"]),
        )
        if direction == "BUY":
            entry_sl_price = closed["close"] - sl_pts * pv
        else:
            entry_sl_price = closed["close"] + sl_pts * pv
        print(f"  [OPEN]  {symbol} {tf} {strategy_name} {direction} "
              f"@{closed['close']:.0f} sl@{entry_sl_price:.0f} "
              f"(atr={atr:.0f}, sl_pts={sl_pts:.0f}, pv={pv})")

    # posição que sobreviveu ao fim dos dados (nunca atravessa o "agora")
    pos = state.positions.pop(key, None)
    if pos is not None:
        last = chron[-1]
        close_sim_position(
            con, pos, last["close"], "END_OF_DATA",
            datetime.fromtimestamp(last["time"]), pos.bars_held,
            table=BACKFILL_TABLE, run_id=run_id,
        )
        con.commit()
        n_closed += 1
    return n_closed


def backfill_report(con: sqlite3.Connection, run_id: str, min_trades: int = 5) -> dict:
    """Relatório do run de backfill: geral + por hora + por par + por estratégia.

    O corte POR HORA é a razão de existir do backfill: valida (ou refuta)
    hipóteses de filtro de sessão com meses de amostra em vez de 1 pregão.
    """
    rows = list(con.execute(
        f"""SELECT symbol, timeframe, strategy, direction, net_pnl_brl,
                   exit_reason, strftime('%H', entry_time)
            FROM {BACKFILL_TABLE}
            WHERE run_id = ? AND exit_time IS NOT NULL""",
        (run_id,),
    ))
    print(f"\n{'=' * 60}")
    print(f"[BACKFILL FINAL] run_id={run_id} | trades={len(rows)}")
    if not rows:
        print("  (sem trades no período)")
        print(f"{'=' * 60}")
        return {"n": 0}
    pnls = [r[4] for r in rows]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_wins = sum(p for p in pnls if p > 0)
    gross_losses = abs(sum(p for p in pnls if p < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf") if gross_wins > 0 else 0
    cum = peak = max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    print(f"  n={n} wins={wins} WR={wins * 100 / n:.1f}% PF={pf:.2f} "
          f"total=R$ {sum(pnls):+.2f} max_dd=R$ {max_dd:+.2f}")

    # ── por hora de entrada (validação de time_blocks) ──
    by_hour: dict[str, list] = {}
    for r in rows:
        by_hour.setdefault(r[6], []).append(r[4])
    if by_hour:
        print("  por hora de entrada (escala do ts da barra do broker — a MESMA do")
        print("  gate time_blocks do daemon; confira offset vs relógio local antes")
        print("  de configurar blocks — hoje 06h renderizado ≈ 09h BRT de abertura):")
        for h in sorted(by_hour):
            ps = by_hour[h]
            w = sum(1 for p in ps if p > 0)
            print(f"    {h}h  n={len(ps):>4}  WR={w * 100 / len(ps):>4.0f}%  "
                  f"R$ {sum(ps):>+9.2f}")

    # ── por par ──
    by_pair: dict[str, dict] = {}
    for r in rows:
        k = f"{r[0]}/{r[1]}"
        d = by_pair.setdefault(k, {"symbol": r[0], "tf": r[1], "strategy": r[2],
                                   "n": 0, "wins": 0, "pnl": 0.0})
        d["n"] += 1
        d["wins"] += 1 if r[4] > 0 else 0
        d["pnl"] += r[4]
    if by_pair:
        print("  por par:")
        for k, v in sorted(by_pair.items(), key=lambda x: -x[1]["pnl"]):
            wr = v["wins"] / v["n"] if v["n"] else 0
            tag = "  " if v["n"] >= min_trades else " (?)"
            print(f"    {k:<14}{tag} n={v['n']:>4} WR={wr * 100:>4.0f}% "
                  f"R$ {v['pnl']:>+9.2f} ({v['strategy']}) → "
                  f"{recommend(v['pnl'], v['n'], wr)}")

    # ── por motivo de saída (onde nascem perdas vs lucros) ──
    by_reason: dict[str, list] = {}
    for r in rows:
        by_reason.setdefault(r[5] or "?", []).append(r[4])
    if by_reason:
        print("  por motivo de saída:")
        for reason, ps in sorted(by_reason.items(), key=lambda x: -sum(x[1])):
            print(f"    {reason:<18} n={len(ps):>4}  R$ {sum(ps):>+9.2f}")
    print(f"{'=' * 60}")
    return {"n": n, "wr": wins / n, "pf": pf, "total": sum(pnls),
            "by_hour": {h: sum(ps) for h, ps in by_hour.items()},
            "by_pair": by_pair}


def run_backfill(args, state: WalkerState) -> None:
    """Orquestra o replay histórico. Sem Telegram, sem drift — é ferramenta
    de validação manual (fora do pregão), não um job operacional."""
    # Guarda de horário: em dia útil de pregão o pgrep do cron 09:01
    # (start_forward_walker.sh) confundiria este processo com o walker live
    # (não reinicia o live) e o Wine/executor ficaria disputado entre os dois.
    now = datetime.now()
    if now.weekday() < 5 and 8 <= now.hour < 17 and not args.force_backfill_hours:
        print("[BACKFILL] RECUSADO: dia útil dentro do pregão (08–17h).")
        print("           Rode fora do pregão (fim de semana/madrugada) ou")
        print("           use --force-backfill-hours se souber o que está fazendo.")
        sys.exit(2)

    # Cenário contrafactual (A/B): sobrepõe chaves top-level do CONFIG
    # IN-MEMORY (time_blocks, disabled_timeframes, disabled_symbols...).
    # O config live em disco NÃO é tocado — é a via do backfill_intel do AGI
    # validar candidatos antes de aplicar pelo writer autorizado.
    # NOTA: params de estratégia NÃO são afetados (vivem no CONFIG do
    # autotrader, lidos por vat._get_params_for_tf).
    override = getattr(args, "config_override", None)
    if override:
        global CONFIG
        CONFIG = dict(CONFIG)
        CONFIG.update(override)
        print(f"[BACKFILL] config-override IN-MEMORY aplicado: {sorted(override)}")

    if not vat._strategy_utils:
        vat._init_strategy_utils()

    date_from = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    date_to = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    run_id = args.run_id or f"bf_{args.from_date}_{args.to_date}"
    ensure_backfill_schema()

    con = sqlite3.connect(str(TRADES_DB), timeout=30.0)
    try:
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.commit()
        except Exception as _e:
            print(f"[WARN] Não consegui ativar WAL: {_e} — seguindo com default")
        # Re-run idempotente com o mesmo run_id: recomeça a amostra do zero
        con.execute(f"DELETE FROM {BACKFILL_TABLE} WHERE run_id = ?", (run_id,))
        con.commit()

        print(f"[BACKFILL] run_id={run_id} | período {date_from} → {date_to} | "
              f"símbolos: {args.symbols} | TFs: {args.tfs}")
        print(f"[BACKFILL] DB: {TRADES_DB} | Tabela isolada: {BACKFILL_TABLE} "
              f"(stage6 do AGI NÃO lê esta tabela)")
        print("[BACKFILL] Read-only no MT5 — ZERO ordens serão enviadas")
        print(f"[BACKFILL] ignore_time_blocks={args.ignore_time_blocks} | "
              f"PID: {os.getpid()} | Início: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print()

        total_closed = 0
        for symbol_or_root in args.symbols:
            symbol = resolve_contract(symbol_or_root)
            root = symbol_root_of(symbol)
            if is_symbol_disabled(root):
                continue
            for tf in args.tfs:
                if is_tf_disabled(root, tf):
                    continue
                total_closed += backfill_pair(
                    con, state, symbol, tf, args, run_id, date_from, date_to)
        print(f"\n[BACKFILL] concluído: {total_closed} trades fechados | "
              f"signals vistos={state.total_signals_seen} "
              f"exec={state.total_signals_executed}")
        backfill_report(con, run_id, min_trades=args.min_trades)
    finally:
        con.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration-min", type=int, default=60,
                   help="Duração total em minutos (default: 60)")
    p.add_argument("--poll-secs", type=int, default=15,
                   help="Intervalo entre polls em segundos (default: 15)")
    p.add_argument("--bars-count", type=int, default=100,
                   help="Bars fetchadas por iteração (default: 100)")
    p.add_argument("--report-every-min", type=int, default=10,
                   help="Relatório forward a cada N min (default: 10)")
    p.add_argument("--symbols", nargs="+",
                   default=CONFIG.get("active_symbols", CONFIG.get("symbols", ["WINQ26"])),
                   help="Símbolos ou raízes a monitorar (default: CONFIG.symbols)")
    p.add_argument("--tfs", nargs="+", default=["M5", "M15"],
                   help="Timeframes (default: M5 M15)")
    p.add_argument("--include-disabled", action="store_true",
                   help="Incluir pares desabilitados em vt_config.json:disabled_timeframes "
                        "(default: respeita config e pula)")
    p.add_argument("--no-telegram", action="store_true",
                   help="Não envia summary ao Telegram ao final (default: envia)")
    p.add_argument("--min-trades", type=int, default=5,
                   help="Só reporta/recomenda pares com n >= N (default: 5)")
    p.add_argument("--force-trading-time", action="store_true",
                   help="[DEV/SMOKE] Força is_trading_time()=True ignorando pregão. "
                        "USAR APENAS em testes — em produção o autotrader segue o calendário.")
    # ── modo backfill (replay histórico) ──
    p.add_argument("--backfill", action="store_true",
                   help="Modo replay histórico: mesma semântica do walker sobre candles "
                        "passados, gravando em forward_backfill_trades (isolada do AGI). "
                        "Recusa dia útil 08-17h sem --force-backfill-hours.")
    p.add_argument("--from", dest="from_date",
                   default=(datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),
                   help="Backfill: data inicial YYYY-MM-DD (default: 90 dias atrás)")
    p.add_argument("--to", dest="to_date",
                   default=datetime.now().strftime("%Y-%m-%d"),
                   help="Backfill: data final YYYY-MM-DD (default: hoje)")
    p.add_argument("--backfill-bars", type=int, default=6000,
                   help="Backfill: barras fetchadas por par (default: 6000 — ~6 meses "
                        "de M15 ou ~2 meses de M5, sujeito ao limite do terminal)")
    p.add_argument("--run-id", default=None,
                   help="Backfill: identificador do run (default: bf_{from}_{to}). "
                        "A/B: rode o mesmo período com run_ids distintos.")
    p.add_argument("--config-override", default=None,
                   help="Backfill: JSON (string ou @arquivo) com chaves top-level de config "
                        "sobrescritas IN-MEMORY pro run (ex: '{\"time_blocks\": {...}}'). "
                        "Config live em disco NÃO é tocado — é a via de cenários A/B.")
    p.add_argument("--ignore-time-blocks", action="store_true",
                   help="Backfill: ignora o gate de blackout do daemon (time_blocks + "
                        "day_direction + events; braço B de um contrafactual — o default "
                        "honra o config como o live)")
    p.add_argument("--force-backfill-hours", action="store_true",
                   help="[AVANÇADO] Permite backfill em dia útil de pregão (risco: o "
                        "pgrep do cron confunde com o walker live + disputa de Wine).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[forward_walker] Duração: {args.duration_min}min | Poll: {args.poll_secs}s | "
          f"Símbolos: {args.symbols} | TFs: {args.tfs}")
    print(f"[forward_walker] include_disabled={args.include_disabled} | "
          f"min_trades={args.min_trades} | no_telegram={args.no_telegram}")
    print(f"[forward_walker] DB: {TRADES_DB} | Tabela isolada: {SIM_TABLE}")
    print("[forward_walker] Read-only no MT5 — ZERO ordens serão enviadas")
    print(f"[forward_walker] PID: {os.getpid()} | Início: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print()

    # Handler SIGTERM: o repo para processos via `kill <pid>` (start_autotrader.sh,
    # self-heal, cron de EOD). Sem isto, SIGTERM mata o walker sem fechar as
    # posições SIM em memória (perde o estado do dia). Reaproveitamos o caminho
    # de cleanup do KeyboardInterrupt (walker_loop:920-929) levantando-o aqui —
    # o signal é entregue na main thread, onde o loop roda.
    def _on_sigterm(signum, _frame):
        print(f"\n[!] SIGTERM({signum}) recebido — encerrando graciosamente…")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)

    ensure_schema()
    state = WalkerState()

    # Se --include-disabled, finge que lista de disabled tá vazia (override runtime)
    if args.include_disabled:
        global CONFIG
        CONFIG = dict(CONFIG)
        CONFIG["disabled_timeframes"] = []
        CONFIG["disabled_symbols"] = []
        # reload CONFIG-aware helpers (eles usam o module-level CONFIG)
        print("[forward_walker] --include-disabled: ignorando disabled_timeframes/disabled_symbols")

    # Modo replay histórico: caminho separado do loop live (sem Telegram/drift)
    if args.backfill:
        if args.config_override:
            raw = args.config_override
            if raw.startswith("@"):
                args.config_override = Path(raw[1:]).read_text(encoding="utf-8")
            args.config_override = json.loads(args.config_override)
        run_backfill(args, state)
        return

    # Partição deste processo live (Wave 885): run_id por walker — o
    # gap-fill do cron deixa de escrever indistinguível do PROD na mesma tabela.
    global _LIVE_RUN_ID
    _LIVE_RUN_ID = f"live_{datetime.now():%Y%m%d_%H%M%S}_pid{os.getpid()}"

    walker_loop(args, state)


if __name__ == "__main__":
    main()
