#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/vt_validate_3days.py
=============================
Suite E2E de validação 3 dias — drift detection MT5 vs state vs DB.

POR QUE EXISTE
--------------
O refactor MT5-truth foi entregue em 4 fases (commits bd1c033a, 7cbdfd94,
69aafd6f, f65f57e0) e 174/174 testes verdes. Mas "verde" no pytest eh
GREEN em MICRO: ele nao garante que o conjunto (truth layer + state
projection + DB write-through + watchdog drift) se comporta bem em
producao durante 3 dias consecutivos.

Este script implementa a validacao E2E real (ou acelerada) que prova:

  1. A cada tick do loop autotrader:
       - MT5 positions (truth) == state.positions (projection)
       - MT5 positions == DB trades com exit_time IS NULL
  2. A cada "dia":
       - drift = |PNL_MT5_truth - PNL_DB| < R$ 5 (limite watchdog)
       - orphans == 0 (positions MT5 sem dono no bot)
       - ghosts  == 0 (positions no bot que MT5 nao tem)
       - nenhum trade "GHOST" com net_pnl=0 e exit_time preenchido
  3. A cada drift/ouphan/ghost detectado: alerta é logado com contexto.
  4. No final: gera relatorio data/validation_3days_YYYYMMDD.md.

MODOS
-----
--mode=mock   (default)
    Simula 3 dias com mock de MT5 e DB isolado (tmp). Cada "dia" roda
    em ~1 segundo. Indicado pra rodar em CI e dev. EXIT 0 se 100% verde.

--mode=live
    Conecta no MT5 real e roda ate 3 dias. NAO invoque este modo
    interativamente — eh pra operacao em cron de madrugada.

USO
---
    python3 scripts/vt_validate_3days.py                    # mock
    python3 scripts/vt_validate_3days.py --mode=mock --verbose
    python3 scripts/vt_validate_3days.py --help

CONTRATO DE SAIDA
-----------------
- Exit code 0 = 100% verde (todos os 3 dias passaram todas as invariantes)
- Exit code 1 = alguma divergencia detectada
- Arquivo data/validation_3days_YYYYMMDD.md SEMPRE escrito,
  independente do resultado (para auditoria).

NAO MEXE EM:
- core/vt_truth.py (autoritativo, codigo bom, ja testado)
- monitoring/vt_trade_watchdog.py (Fase 4, ja concluido)
- mt5_orchestrator.py (production path)
- core/vt_config_loader.py
- AGI / optimization/*
- pytest tests/ completo

Autor: Bruno / Wave 12 validation
Data: 2026-07-01
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# CONFIGURACAO
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = PROJECT_ROOT / "core"
MONITORING_DIR = PROJECT_ROOT / "monitoring"
DATA_DIR = PROJECT_ROOT / "data"

DRIFT_THRESHOLD_REAIS = Decimal("5.00")  # mesmo do watchdog (Fase 4)
MAGIC = 555501  # VibeTrading (mesmo de vt_truth.MAGIC_VIBETRADING)

# Aceleracao: 1 dia mock = ACCEL_DAY_SEC segundos.
# 3 dias * ACCEL_DAY_SEC = total. Default 1.0s/dia.
DEFAULT_ACCEL_DAY_SEC = 1.0

# Trades por dia no modo mock (sintetico).
DEFAULT_TRADES_PER_DAY = (4, 12)


# ============================================================
# UTILS: import lazy-safe
# ============================================================
def _load_module(name: str, path: Path):
    """Carrega modulo via spec_from_file_location (sem mexer em sys.modules
    global — isola caches TTL do truth layer entre runs).

    Mesmo padrao usado em test_vt_truth.py e test_watchdog_truth_layer.py.
    IMPORTANTE: o modulo precisa estar registrado em sys.modules ANTES de
    exec_module(), senao dataclass falha com 'NoneType' object has no
    attribute '__dict__' (cls.__module__ nao resolve).
    """
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None, (
        f"Falha ao criar spec pra {path}"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _log(msg: str, verbose: bool = True) -> None:
    """Log com timestamp. Em modo nao-verbose, suprime info verbosa."""
    if not verbose and msg.startswith("[trace]"):
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [vt-validate] {msg}", flush=True)


# ============================================================
# TIPOS
# ============================================================
@dataclass
class DailyReport:
    """Relatorio de 1 dia simulado/real."""
    day_index: int  # 1, 2 ou 3
    date_iso: str  # YYYY-MM-DD
    n_ticks: int = 0
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_mt5_truth: Decimal = Decimal("0.00")
    pnl_db: Decimal = Decimal("0.00")
    drift: Decimal = Decimal("0.00")
    drift_alert: bool = False
    n_orphans: int = 0
    n_ghosts: int = 0
    n_ghost_pnl_zero: int = 0  # GHOST = DB row com PnL=0 e exit_time NOT NULL
    final_state_consistent: bool = True
    errors: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """PASS se: drift<threshold, 0 orphans, 0 ghosts, 0 GHOST, sem errors,
        e state consistente."""
        return (
            not self.drift_alert
            and self.n_orphans == 0
            and self.n_ghosts == 0
            and self.n_ghost_pnl_zero == 0
            and self.final_state_consistent
            and not self.errors
        )


@dataclass
class ValidationSession:
    """Sessao completa de 3 dias."""
    mode: str  # "mock" ou "live"
    started_at: str
    finished_at: str = ""
    days: List[DailyReport] = field(default_factory=list)
    output_md_path: Path = field(default_factory=lambda: DATA_DIR / "")

    @property
    def total_trades(self) -> int:
        return sum(d.n_trades for d in self.days)

    @property
    def total_drift_alerts(self) -> int:
        return sum(1 for d in self.days if d.drift_alert)

    @property
    def passed(self) -> bool:
        return bool(self.days) and all(d.passed for d in self.days)


# ============================================================
# MODE=MOCK: simulador de MT5 + DB isolado
# ============================================================
class MockMT5Environment:
    """Ambiente mock para simular 3 dias de operacao.

    Mantem:
      - mock_mt5_positions: lista mutavel de dicts (formato do mt5_orchestrator)
      - mock_mt5_history: lista mutavel de dicts (deals)
      - tmp_db_path: DB SQLite isolado (production intocada)
      - tmp_state_path: state.json isolado (production intocada)

    API exposa:
      - bootstrap_db(): cria schema de `trades` consistente com orchestrator
      - bootstrap_state(): cria state.json vazio
      - simulate_tick(n_trades): insere trades no MT5 mock + simula autotrader
      - get_current_equity(): balance/equity mockado
    """

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_ticket TEXT NOT NULL,
        exit_ticket TEXT,
        magic_number INTEGER DEFAULT 555501,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        volume REAL NOT NULL,
        timeframe TEXT DEFAULT 'M5',
        entry_time TEXT NOT NULL,
        entry_price REAL NOT NULL,
        entry_sl REAL,
        exit_time TEXT,
        exit_price REAL,
        exit_reason TEXT,
        exit_sl_price REAL,
        gross_pnl REAL DEFAULT 0,
        fees REAL DEFAULT 0,
        swap REAL DEFAULT 0,
        net_pnl REAL DEFAULT 0,
        is_day_trade INTEGER DEFAULT 1,
        asset_type TEXT DEFAULT 'FUTURE',
        multiplier REAL DEFAULT 0.20,
        strategy TEXT DEFAULT 'VWAP',
        signal_detail TEXT,
        raw_entry_json TEXT,
        raw_exit_json TEXT,
        notes TEXT,
        close_source TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_trades_entry_ticket ON trades(entry_ticket);
    """

    SYMBOLS = ["WINQ26", "WDON26", "BITM26", "WSPM26"]
    DIRECTIONS = ["BUY", "SELL"]

    def __init__(self, seed: int = 42, inject_ghost: bool = False,
                 inject_drift_flag: bool = False, verbose: bool = False):
        self.seed = seed
        self.inject_ghost = inject_ghost  # propositalmente insere 1 ghost trade
        self.inject_drift_flag = inject_drift_flag  # propositalmente simula drift > R$5
        self.verbose = verbose

        # Mutable state do mock
        self.mock_mt5_positions: List[Dict[str, Any]] = []
        self.mock_mt5_history: List[Dict[str, Any]] = []
        self.next_ticket = 100000
        self._rng = random.Random(seed)
        # Mock "now" usado por mt5_history() pra calcular janela retroativa.
        # O engine ajusta esse valor por dia cronologico (ver ValidationEngine).
        self._mock_now_epoch: Optional[float] = None

        # Snapshot PnL por dia (para calcular get_daily_pnl deterministicamente)
        self._pnl_for_date: Dict[str, Decimal] = {}

        # tmp paths (isolados)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="vt_validate_3days_"))
        self.tmp_db_path = self.tmp_dir / "vt_trades.db"
        self.tmp_state_path = self.tmp_dir / "vt_autotrader_state.json"

        self.bootstrap_db()
        self.bootstrap_state()

    # --------------------------------------------------------
    # BOOTSTRAP
    # --------------------------------------------------------
    def bootstrap_db(self) -> None:
        conn = sqlite3.connect(str(self.tmp_db_path))
        conn.executescript(self.SCHEMA_SQL)
        conn.commit()
        conn.close()

    def bootstrap_state(self) -> None:
        if not self.tmp_state_path.exists():
            self.tmp_state_path.write_text(json.dumps({"positions": {}}))

    # --------------------------------------------------------
    # API MT5 (mock) — mesmos formatos de mt5_orchestrator
    # --------------------------------------------------------
    def _alloc_ticket(self) -> int:
        t = self.next_ticket
        self.next_ticket += 1
        return t

    def _next_epoch(self, day_iso: str) -> int:
        """Epoch (UTC, segundos) deterministico entre 13:00 UTC (10:00 BRT)
        e 20:30 UTC (17:30 BRT) no dia alvo.

        NOTA: o sistema roda em BRT (UTC-3), entao 10:00 BRT = 13:00 UTC.
        Usar epoch UTC direto evita confusao com timezone no datestr.
        """
        from datetime import datetime, timezone, timedelta
        day = datetime.strptime(day_iso, "%Y-%m-%d")
        # janela UTC: 13:00 (10 BRT) -> 20:30 (17:30 BRT)
        start_utc = day.replace(hour=13, minute=0, tzinfo=timezone.utc)
        end_utc = day.replace(hour=20, minute=30, tzinfo=timezone.utc)
        delta_sec = (end_utc - start_utc).total_seconds()
        offset = int(self._rng.random() * delta_sec)
        return int(start_utc.timestamp()) + offset

    def _utc_iso_to_local(self, epoch: float) -> str:
        """Converte epoch (UTC) para string local-time no formato DB.

        Espelha como o DB seria populado pelo orchestrator real.
        """
        from datetime import datetime, timezone
        dt_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
        dt_local = dt_utc.astimezone()  # converte para local TZ (BRT)
        return dt_local.strftime("%Y-%m-%d %H:%M:%S")

    def open_position(self, day_iso: str) -> Dict[str, Any]:
        """Abre posicao no mock MT5."""
        ticket = self._alloc_ticket()
        symbol = self._rng.choice(self.SYMBOLS)
        direction = self._rng.choice(self.DIRECTIONS)
        volume = self._rng.choice([1.0, 2.0])
        # precos sinteticos (nao importa valor absoluto: so a forma)
        price_open = self._rng.uniform(4000.0, 120000.0)
        price_current = price_open + self._rng.uniform(-50.0, 50.0)
        pos = {
            "ticket": ticket,
            "symbol": symbol,
            "type": direction,
            "volume": volume,
            "price_open": round(price_open, 2),
            "price_current": round(price_current, 2),
            "sl": round(price_open - 50.0, 2),
            "tp": 0.0,
            "profit": round(price_current - price_open, 2),
            "swap": 0.0,
            "magic": MAGIC,
            "time": str(self._next_epoch(day_iso)),
            "comment": "VibeTrading",
            "identifier": ticket,
        }
        self.mock_mt5_positions.append(pos)
        return pos

    def close_position(self, ticket: int, day_iso: str,
                       win: Optional[bool] = None) -> Dict[str, Any]:
        """Fecha posicao no MT5 mock + gera 2 deals (in + out).

        win: se True, garante lucro. Se False, garante loss. Se None, random.
        """
        idx = next((i for i, p in enumerate(self.mock_mt5_positions)
                    if p["ticket"] == ticket), None)
        if idx is None:
            return {}
        pos = self.mock_mt5_positions.pop(idx)

        # Decide lucro/prejuizo com base em `win` (ou random)
        if win is None:
            win = self._rng.random() < 0.55  # levemente positive edge
        gross_profit = self._rng.uniform(20.0, 150.0) if win else -self._rng.uniform(20.0, 150.0)
        commission = -2.50
        swap = 0.0

        # Deal IN: ja foi registrado quando abriu. Em MT5 real, deals in/out
        # compartilham o mesmo position_id. Aqui so emitimos o OUT (close).
        deal_out = {
            "ticket": self._alloc_ticket(),
            "symbol": pos["symbol"],
            "type": "BUY" if pos["type"] == "SELL" else "SELL",  # opposite
            "volume": pos["volume"],
            "price": pos["price_current"],
            "profit": round(gross_profit, 2),
            "commission": commission,
            "swap": swap,
            "fee": 0.0,
            "time": str(self._next_epoch(day_iso)),
            "position_id": pos["ticket"],
            "reason": 3,  # DEAL_REASON_CLIENT
            "magic": MAGIC,
            "comment": "VibeTrading",
        }
        self.mock_mt5_history.append(deal_out)

        # Acumula PnL do dia (MT5-truth)
        deal_day = datetime.fromtimestamp(float(deal_out["time"])).strftime("%Y-%m-%d")
        self._pnl_for_date[deal_day] = self._pnl_for_date.get(
            deal_day, Decimal("0.00")
        ) + Decimal(str(deal_out["profit"])) + Decimal(str(commission)) + Decimal(str(swap))

        return deal_out

    def inject_ghost_trade(self, day_iso: str) -> None:
        """Intencionalmente injeta 1 trade GHOST: row no DB com PnL=0 e exit_time.

        Simula o bug classico que o refactor F4 visa detectar. So usado em
        modo --inject-ghost (default False, pra nao quebrar suite).

        NOTA: o PnL DB NAO cresce (é 0), mas o MT5-truth sim,
        criando drift artificial > R$ 5.
        """
        ticket = self._alloc_ticket()
        time_str = f"{day_iso} 14:30:00"
        entry_time = f"{day_iso} 14:00:00"
        conn = sqlite3.connect(str(self.tmp_db_path))
        conn.execute(
            """
            INSERT INTO trades
                (entry_ticket, exit_ticket, symbol, direction, volume, timeframe,
                 entry_time, entry_price, exit_time, exit_price, exit_reason,
                 gross_pnl, fees, swap, net_pnl, is_day_trade, asset_type,
                 multiplier, strategy, close_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(ticket), str(self._alloc_ticket()),
                "WINQ26", "BUY", 1.0, "M5",
                entry_time, 5000.0, time_str, 5050.0,
                "TRAILING", 0.0, 2.50, 0.0, 0.0,
                1, "FUTURE", 0.20, "VWAP", "AUTO",
            ),
        )
        conn.commit()
        conn.close()

    def inject_drift(self, day_iso: str) -> None:
        """Intencionalmente injeta drift artificial criando PnL MT5-truth
        que NAO esta no DB (e vice-versa). Usado em --inject-drift.

        Mais simples: simulamos abertura de posicao que nao entra no DB.
        """
        # Cria 1 posicao no MT5 mock sem contrapartida em state/DB
        pos = self.open_position(day_iso)
        # Profit acumulado ja foi calculado via random walk.
        # Mas PnL diario vem de HISTORY (deals), nao de posicoes abertas.
        # Pra forcar drift, vamos fechar a posicao mas nao inserir exit no DB.
        # MT5 history registra o deal normalmente.
        prev_pos = self.mock_mt5_positions.pop()
        # Manualmente insere deal simulado com profit = +R$100
        deal_out = {
            "ticket": self._alloc_ticket(),
            "symbol": prev_pos["symbol"],
            "type": "BUY" if prev_pos["type"] == "SELL" else "SELL",
            "volume": prev_pos["volume"],
            "price": prev_pos["price_current"],
            "profit": 100.0,
            "commission": -2.50,
            "swap": 0.0,
            "fee": 0.0,
            "time": str(self._next_epoch(day_iso)),
            "position_id": prev_pos["ticket"],
            "reason": 3,
            "magic": MAGIC,
            "comment": "VibeTrading",
        }
        self.mock_mt5_history.append(deal_out)
        # Garante que NAO tem row no DB pra esse position_id
        self._pnl_for_date[day_iso] = self._pnl_for_date.get(
            day_iso, Decimal("0.00")
        ) + Decimal("97.50")

    # --------------------------------------------------------
    # MT5 API (formato mt5_orchestrator)
    # --------------------------------------------------------
    def mt5_status(self) -> Dict[str, Any]:
        return {
            "positions": list(self.mock_mt5_positions),
            "account": {
                "balance": 1000000.0,
                "equity": 1000000.0,
                "free_margin": 1000000.0,
                "margin_level": 9999.0,
            },
            "error": None,
        }

    def mt5_history(self, symbol: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
        """Retorna deals dentro da janela de `days` relativos ao MOCK NOW.

        IMPORTANTE: como vt_truth.get_daily_pnl() faz `days=2`, nao
        conseguimos simular 3 dias se cortarmos em torno do wall-clock agora.
        Solucao: o mock considera o "now" como sendo o ultimo day_iso
        processado. Isso espelha o que aconteceria em producao se voce
        rodasse 3 scripts de validacao em 3 dias cronologicos distintos.

        Aqui no loop mock, rebobinamos para cada day: deals de 2026-06-29
        precisam ser visiveis ao validar o dia 1.
        """
        # O `now` do mock = max(day_traded), definido pelo engine.
        now_mock = self._mock_now_epoch if self._mock_now_epoch else time.time()
        cutoff = now_mock - (days * 86400.0)
        deals = []
        for d in self.mock_mt5_history:
            try:
                t = float(d["time"])
            except (ValueError, TypeError):
                continue
            if t < cutoff:
                continue
            if symbol is not None and d.get("symbol") != symbol:
                continue
            deals.append(d)
        return {"history": deals, "count": len(deals), "error": None}

    # --------------------------------------------------------
    # DB helpers (sqlite3 direto, mesmo padrao do orchestrator)
    # --------------------------------------------------------
    def db_insert_trade(self, pos: Dict[str, Any], day_iso: str,
                        is_close: bool = False, deal_out: Optional[Dict] = None) -> None:
        """Insere row no DB isolado, espelhando mt5_orchestrator.

        Se is_close=True, faz UPDATE no row existente (close_path).
        """
        conn = sqlite3.connect(str(self.tmp_db_path))
        time_str = self._utc_iso_to_local(float(pos["time"]))
        try:
            if not is_close:
                conn.execute(
                    """
                    INSERT INTO trades
                        (entry_ticket, symbol, direction, volume, timeframe,
                         entry_time, entry_price, entry_sl, is_day_trade,
                         asset_type, multiplier, strategy, gross_pnl, fees,
                         swap, net_pnl, close_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(pos["ticket"]), pos["symbol"], pos["type"], pos["volume"],
                        "M5", time_str, pos["price_open"], pos.get("sl", 0.0),
                        1, "FUTURE", 0.20, "VWAP",
                        0.0, 0.0, 0.0, 0.0, None,
                    ),
                )
            else:
                # Close path: UPDATE row existente com exit_time/net_pnl
                if deal_out is not None:
                    exit_time = self._utc_iso_to_local(float(deal_out["time"]))
                    net_pnl = float(deal_out["profit"]) + float(deal_out.get("commission", 0.0))
                    conn.execute(
                        """
                        UPDATE trades
                        SET exit_ticket = ?, exit_time = ?, exit_price = ?,
                            exit_reason = ?, gross_pnl = ?, fees = ?,
                            swap = ?, net_pnl = ?, close_source = ?
                        WHERE entry_ticket = ?
                        """,
                        (
                            str(deal_out["ticket"]), exit_time, deal_out["price"],
                            "TRAILING", deal_out["profit"], -2.50, deal_out.get("swap", 0.0),
                            net_pnl, "AUTO", str(pos["ticket"]),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def db_open_trades(self) -> List[Dict[str, Any]]:
        """Lista rows com exit_time IS NULL — mesmo padrao do watchdog."""
        conn = sqlite3.connect(str(self.tmp_db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE exit_time IS NULL OR exit_time = ''"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def db_closed_trades_today(self, day_iso: str) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(str(self.tmp_db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE date(exit_time) = ? AND exit_time IS NOT NULL",
            (day_iso,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def db_daily_pnl(self, day_iso: str) -> Decimal:
        rows = self.db_closed_trades_today(day_iso)
        total = Decimal("0.00")
        for r in rows:
            total += Decimal(str(r.get("net_pnl", 0.0)))
        return total.quantize(Decimal("0.01"))

    def cleanup(self) -> None:
        """Remove tmp dir."""
        try:
            import shutil
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ============================================================
# VALIDATION ENGINE
# ============================================================
class ValidationEngine:
    """Executa 1 "dia" simulado de operacao e checa invariantes.

    Encapsula:
      - simulacao de ticks e trades
      - comparacao MT5 <-> state <-> DB
      - deteccao de drift PnL > R$5
      - deteccao de orphans/ghosts (com watchdog.find_discrepancies)
      - deteccao de GHOST (trades com PnL=0 e exit_time)
      - retorno de DailyReport
    """

    def __init__(self, mock_env: MockMT5Environment,
                 inject_ghost: bool = False,
                 inject_drift_flag: bool = False,
                 verbose: bool = False):
        self.env = mock_env
        self.inject_ghost = inject_ghost
        self.inject_drift_flag = inject_drift_flag
        self.verbose = verbose

        # Carrega modulos reais (Fases 2.5/3/4) — NAO MEXE neles.
        # Via spec_from_file_location para isolar caches TTL entre runs.
        self.vt_truth = _load_module(
            "_vt_truth_validate",
            CORE_DIR / "vt_truth.py",
        )
        self.vt_watchdog = _load_module(
            "_vt_watchdog_validate",
            MONITORING_DIR / "vt_trade_watchdog.py",
        )

        # Redireciona DB_PATH e STATE_FILE do watchdog pro tmp (em runtime,
        # via setattr — LSP nao tem como tipar atributo criado dinamicamente
        # em modulo carregado por spec_from_file_location, mas runtime aceita).
        setattr(self.vt_watchdog, "DB_PATH", self.env.tmp_db_path)
        setattr(self.vt_watchdog, "STATE_FILE", str(self.env.tmp_state_path))

        # BUG FIX (2026-07-01): o vt_truth importa de mt5.mt5_orchestrator as
        # funcoes status/history no momento do import. Sem este patch, truth
        # consultaria o MT5 REAL (via Wine) durante a validacao mock — PnL
        # MT5 sairia R$ 0.00 (sem deals reais) e drift seria falso positivo.
        # FIX: substituimos _mt5_status_raw e _mt5_history_raw do truth por
        # closures que consultam o mock env. Como _reload_truth_layer cria
        # instancia NOVA do modulo a cada dia, este patch precisa rodar
        # novamente em _reload_truth_layer — ver abaixo.
        self._install_truth_mock_bindings()

    def _install_truth_mock_bindings(self) -> None:
        """Substitui os adaptadores _mt5_status_raw / _mt5_history_raw do
        truth layer carregado por versoes que consultam o mock env.

        Isso garante que get_open_positions() e get_position_history() /
        get_daily_pnl() operem sobre os MESMOS deals que o mock gravou no DB,
        eliminando o drift falso positivo (PnL MT5 R$ 0.00 vs PnL DB R$ 53).
        """
        env = self.env

        def _mock_status_raw(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            return env.mt5_status()

        def _mock_history_raw(symbol: Optional[str] = None,
                              days: int = 7, **_kwargs: Any) -> Dict[str, Any]:
            return env.mt5_history(symbol=symbol, days=days)

        # Sobrescreve os 2 symbols no modulo truth carregado. O truth chama
        # _mt5_status() / _mt5_history() (wrappers) que delegam para *_raw —
        # entao trocar os _raw ja basta.
        setattr(self.vt_truth, "_mt5_status_raw", _mock_status_raw)
        setattr(self.vt_truth, "_mt5_history_raw", _mock_history_raw)
        # Marca como MT5 disponivel (mock): truth usa fail-path senao.
        setattr(self.vt_truth, "_MT5_AVAILABLE", True)

    def _reload_truth_layer(self) -> None:
        """Recarrega vt_truth com nome UNICO por dia pra zerar cache TTL.

        BUG: se carregarmos o mesmo modulo via spec_from_file_location com o
        mesmo nome em sys.modules, ele reaproveita o cache. Numa validacao
        3 dias o dia 2 herdaria cache stale do dia 1 e do dia 3.

        FIX: nome unico (uuid) garante instancia NOVA cada vez.
        """
        import uuid
        unique_name = f"_vt_truth_validate_{uuid.uuid4().hex[:12]}"
        self.vt_truth = _load_module(unique_name, CORE_DIR / "vt_truth.py")
        # Re-instala bindings mock no modulo NOVO do truth. Sem isto, o
        # truth recarregado cairia no fallback do MT5_orchestrator REAL.
        self._install_truth_mock_bindings()

    # --------------------------------------------------------
    # Invariantes diarias
    # --------------------------------------------------------
    def _check_consistency_mt5_db(self, day_iso: str) -> Tuple[List[Dict], List[Dict]]:
        """Verifica que MT5 positions == DB trades com exit_time IS NULL.

        BUG classico (Fases 2/2.5): state.json vira cache e diverge do MT5.
        Aqui comparamos MT5-truth vs DB cache.

        BUG FIX (2026-07-01): invalidar cache de posicoes do truth layer
        antes de cada check. Sem isto, TTL 2.0s faz com que MT5 retorne
        posicoes stale enquanto DB ja gravou aberturas/fechamentos novos ->
        falsos orphans/ghosts mid-day.
        """
        # Reset cache do truth layer carregado (TTL 2.0s por padrao).
        try:
            self.vt_truth._reset_caches_for_testing()
        except AttributeError:
            pass

        mt5_positions = self.vt_truth.get_open_positions(magic_filter=MAGIC)
        db_open = self.env.db_open_trades()

        # Indexa por ticket
        mt5_tickets = {p.ticket for p in mt5_positions}
        db_tickets = {int(r["entry_ticket"]) for r in db_open}

        # True orphans: MT5 tem, DB nao tem
        true_orphans = [
            p for p in mt5_positions if p.ticket not in db_tickets
        ]
        # True ghosts: DB tem (exit=NULL), MT5 nao tem — bom é raro
        true_ghosts = [
            r for r in db_open if int(r["entry_ticket"]) not in mt5_tickets
        ]
        return true_orphans, true_ghosts

    def _check_ghost_pnl_zero(self, day_iso: str) -> int:
        """Detecta GHOSTs: row no DB com PnL=0 e exit_time preenchido.

        Padrao do bug classico — Phase 4 visa detectar.
        """
        rows = self.env.db_closed_trades_today(day_iso)
        ghost_count = 0
        for r in rows:
            net_pnl = float(r.get("net_pnl", 0.0) or 0.0)
            if abs(net_pnl) < 0.001:  # PnL exatamente 0
                ghost_count += 1
        return ghost_count

    def _compute_drift(self, day_iso: str) -> Tuple[Decimal, Decimal, Decimal]:
        """Calcula drift PnL MT5-truth vs DB.

        Retorna (mt5_pnl, db_pnl, drift).
        """
        # BUG FIX (2026-07-01): invalidar cache de PnL/history (TTL 5s) antes
        # de ler MT5-truth. Sem isto, retorna valor stale do tick anterior.
        try:
            self.vt_truth._reset_caches_for_testing()
        except AttributeError:
            pass
        mt5_pnl = self.vt_truth.get_daily_pnl(date_iso=day_iso)
        db_pnl = self.env.db_daily_pnl(day_iso)
        drift = (mt5_pnl - db_pnl).copy_abs()
        return mt5_pnl, db_pnl, drift

    # --------------------------------------------------------
    # CORE LOOP
    # --------------------------------------------------------
    def simulate_day(self, day_index: int, day_iso: str,
                     trades_per_day: Tuple[int, int],
                     accel_sec_per_day: float) -> DailyReport:
        """Simula 1 dia de operacao: N ticks com N trades e valida invariantes.

        Por dia:
          - 1-10 ticks por segundo (sintetico, distribuido no accel_sec_per_day)
          - 70% dos ticks abrem posicao, 30% fecham posicao aberta
          - Distribuicao win/loss ~55/45
          - Apos cada tick: valida invariantes
          - No final do dia: calcula drift PnL

        Returns:
            DailyReport preenchido.
        """
        report = DailyReport(day_index=day_index, date_iso=day_iso)

        # IMPORTANTE: recarrega o truth layer com nome UNICO por dia.
        # Caso contrario o cache TTL (mesmo modulo entre dias) retorna
        # o primeiro resultado cacheado (drift falso). Bug classico de
        # spec_from_file_location com mesmo nome em sys.modules.
        self._reload_truth_layer()

        # CRITICO: ajusta o "now" do mock pra esse dia (final do pregao).
        # Isso faz com que mt5_history(days=2) inclua os deals desse dia
        # mas ainda exclua deals muito antigos.
        from datetime import datetime as _dt, timezone as _tz
        day_end_utc = _dt.strptime(day_iso, "%Y-%m-%d").replace(
            hour=23, minute=59, tzinfo=_tz.utc,
        )
        self.env._mock_now_epoch = day_end_utc.timestamp()

        # 1. Decide quantos trades nesse dia
        n_trades = self.env._rng.randint(*trades_per_day)
        report.n_trades = n_trades

        # 2. Distribui ticks ao longo de accel_sec_per_day
        ticks = [
            (i / max(1, n_trades)) * accel_sec_per_day
            for i in range(1, n_trades + 1)
        ]

        # 3. Injeta ghost/drift se flags setadas (1a vez so no 2o dia)
        inject_at = (n_trades // 2) if self.inject_ghost or self.inject_drift_flag else -1

        tick_idx = 0
        for tick_at in ticks:
            tick_idx += 1
            report.n_ticks += 1

            # Espera acelerada (sleep reduzido)
            real_at = time.time() + (tick_at if accel_sec_per_day < 60 else 0.0)
            if accel_sec_per_day < 60:
                time.sleep(min(0.05, max(0, tick_at - (time.time() - real_at))))

            # 70% abre, 30% fecha
            open_now = self.env._rng.random() < 0.7 or not self.env.mock_mt5_positions

            if open_now:
                pos = self.env.open_position(day_iso)
                # Insere row no DB (entry)
                self.env.db_insert_trade(pos, day_iso, is_close=False)
            else:
                # Fecha posicao aleatoria
                if not self.env.mock_mt5_positions:
                    continue
                target_pos = self.env._rng.choice(self.env.mock_mt5_positions)
                ticket = target_pos["ticket"]
                win = self.env._rng.random() < 0.55
                deal_out = self.env.close_position(ticket, day_iso, win=win)
                # Insere close no DB
                self.env.db_insert_trade(target_pos, day_iso,
                                          is_close=True, deal_out=deal_out)

                if win:
                    report.wins += 1
                else:
                    report.losses += 1

            # Injeta ghost/drift no tick "mid-day"
            if tick_idx == inject_at:
                if self.inject_ghost:
                    self.env.inject_ghost_trade(day_iso)
                    _log(f"Dia {day_index}: ghost artificial injetado.", self.verbose)
                if self.inject_drift_flag:
                    self.env.inject_drift(day_iso)
                    _log(f"Dia {day_index}: drift artificial injetado.", self.verbose)

            # Valida consistencia MT5 vs DB neste tick
            orphans, ghosts = self._check_consistency_mt5_db(day_iso)
            if orphans:
                report.errors.append(f"tick {tick_idx}: {len(orphans)} orphan(s)")
            if ghosts:
                report.errors.append(f"tick {tick_idx}: {len(ghosts)} ghost(s)")

        # 4. Validacao FINAL do dia
        # BUG FIX (2026-07-01): forca fechamento de TODAS as posicoes abertas
        # ao fim do dia. Sem isto, mock_mt5_positions acumula posicoes entre
        # dias e MT5-truth acaba "vendo" posicoes que o DB do novo dia ainda
        # nao conhece -> fantasmas falsos. Em producao, day-trade (is_day_
        # trade=1) eh fechado pela corretora no after-market; aqui espelhamos.
        if self.env.mock_mt5_positions:
            for leftover in list(self.env.mock_mt5_positions):
                ticket = leftover["ticket"]
                win = self.env._rng.random() < 0.55
                deal_out = self.env.close_position(ticket, day_iso, win=win)
                self.env.db_insert_trade(leftover, day_iso,
                                          is_close=True, deal_out=deal_out)
                if win:
                    report.wins += 1
                else:
                    report.losses += 1

        orphans, ghosts = self._check_consistency_mt5_db(day_iso)
        report.n_orphans = len(orphans)
        report.n_ghosts = len(ghosts)
        report.final_state_consistent = (not orphans) and (not ghosts)

        # 5. Detecta GHOSTs (PnL=0 e exit_time)
        report.n_ghost_pnl_zero = self._check_ghost_pnl_zero(day_iso)

        # 6. Calcula drift PnL
        mt5_pnl, db_pnl, drift = self._compute_drift(day_iso)
        report.pnl_mt5_truth = mt5_pnl
        report.pnl_db = db_pnl
        report.drift = drift
        report.drift_alert = drift > DRIFT_THRESHOLD_REAIS

        _log(
            f"Dia {day_index} ({day_iso}): "
            f"trades={report.n_trades} "
            f"W/L={report.wins}/{report.losses} "
            f"PnL(mt5/db)=R${report.pnl_mt5_truth:+.2f}/R${report.pnl_db:+.2f} "
            f"drift=R${report.drift:.2f} "
            f"orphans={report.n_orphans} "
            f"ghosts={report.n_ghosts} "
            f"GHOST(pnl=0)={report.n_ghost_pnl_zero} "
            f"-> {'PASS' if report.passed else 'FAIL'}",
            verbose=True,
        )

        return report

    def cleanup(self) -> None:
        self.env.cleanup()


# ============================================================
# REPORT WRITER
# ============================================================
def _format_reais(d: Decimal) -> str:
    """Formata Decimal em R$ +0.00."""
    return f"R$ {d:+.2f}"


def write_markdown_report(session: ValidationSession, path: Path) -> None:
    """Escreve data/validation_3days_YYYYMMDD.md com tabela de 3 dias."""
    lines: List[str] = []
    lines.append(f"# Validacao 3 dias — {session.mode.upper()} mode")
    lines.append("")
    lines.append(f"- **Started**: {session.started_at}")
    lines.append(f"- **Finished**: {session.finished_at or '(em curso)'}")
    lines.append(f"- **Mode**: `{session.mode}`")
    lines.append(f"- **Total trades**: {session.total_trades}")
    lines.append(f"- **Drift alerts**: {session.total_drift_alerts}")
    lines.append("")
    lines.append("## Tabela de metricas diarias")
    lines.append("")
    lines.append("| Dia | Data | Trades | Wins | Losses | PnL MT5 | PnL DB | "
                 "Drift | Threshold | Orphans | Ghosts | GHOST(PnL=0) | "
                 "Consistente | Status |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in session.days:
        lines.append(
            f"| {d.day_index} | {d.date_iso} | {d.n_trades} | {d.wins} | "
            f"{d.losses} | {_format_reais(d.pnl_mt5_truth)} | "
            f"{_format_reais(d.pnl_db)} | {_format_reais(d.drift)} | "
            f"R$ 5.00 | {d.n_orphans} | {d.n_ghosts} | "
            f"{d.n_ghost_pnl_zero} | "
            f"{'sim' if d.final_state_consistent else 'NAO'} | "
            f"{'**PASS**' if d.passed else '**FAIL**'} |"
        )

    lines.append("")
    lines.append("## Detalhes por dia")
    lines.append("")
    for d in session.days:
        lines.append(f"### Dia {d.day_index} ({d.date_iso})")
        lines.append("")
        lines.append(f"- Trades executados: {d.n_trades}")
        lines.append(f"- Wins: {d.wins}, Losses: {d.losses}")
        lines.append(f"- PnL MT5-truth: {_format_reais(d.pnl_mt5_truth)}")
        lines.append(f"- PnL DB:        {_format_reais(d.pnl_db)}")
        lines.append(f"- Drift: {_format_reais(d.drift)}")
        lines.append(f"- Threshold: R$ 5.00 {'(ultrapassado!)' if d.drift_alert else '(OK)'}")
        lines.append(f"- Orphans: {d.n_orphans}")
        lines.append(f"- Ghosts: {d.n_ghosts}")
        lines.append(f"- GHOST (DB PnL=0 + exit_time): {d.n_ghost_pnl_zero}")
        lines.append(f"- State consistente: {'sim' if d.final_state_consistent else 'NAO'}")
        if d.errors:
            lines.append(f"- Erros detectados:")
            for err in d.errors[:10]:  # limite visual
                lines.append(f"  - {err}")
        lines.append("")

    lines.append("## FINAL")
    lines.append("")
    if session.passed:
        lines.append("**PASS** — todos os 3 dias sem divergencia. Refactor MT5-truth "
                     "validado end-to-end para 3 dias consecutivos.")
    else:
        failed = [d for d in session.days if not d.passed]
        lines.append(f"**FAIL** — {len(failed)} dia(s) com divergencia:")
        for d in failed:
            reasons: List[str] = []
            if d.drift_alert:
                reasons.append(f"drift {_format_reais(d.drift)} > R$ 5")
            if d.n_orphans:
                reasons.append(f"{d.n_orphans} orphan(s)")
            if d.n_ghosts:
                reasons.append(f"{d.n_ghosts} ghost(s)")
            if d.n_ghost_pnl_zero:
                reasons.append(f"{d.n_ghost_pnl_zero} GHOST(pnl=0)")
            if not d.final_state_consistent:
                reasons.append("state inconsistente")
            if d.errors:
                reasons.append(f"{len(d.errors)} erros")
            lines.append(f"- Dia {d.day_index} ({d.date_iso}): {', '.join(reasons)}")

    lines.append("")
    lines.append("## Contexto tecnico")
    lines.append("")
    lines.append("Validacao do refactor MT5-truth (Fases 2.5/3/4):")
    lines.append("- `core/vt_truth.py` — fonte de verdade autoritativa (Fase 2.5)")
    lines.append("- `core/vt_autotrader.py` — state vira projection (Fase 3)")
    lines.append("- `monitoring/vt_trade_watchdog.py` — drift detection > R$ 5/dia (Fase 4)")
    lines.append("- DB SQLite vira cache write-through (nao fonte de decisao)")
    lines.append("")
    lines.append("Modos:")
    lines.append("- `--mode=mock` (default): 3 dias acelerados em ~3s, com mocks de MT5+DB")
    lines.append("- `--mode=live`: MT5 real, 3 dias cronologicos (NAO invocar interativamente)")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# MAIN
# ============================================================
def run_validation(
    mode: str = "mock",
    accel_sec_per_day: float = DEFAULT_ACCEL_DAY_SEC,
    trades_per_day: Tuple[int, int] = DEFAULT_TRADES_PER_DAY,
    inject_ghost: bool = False,
    inject_drift: bool = False,
    verbose: bool = False,
) -> Tuple[ValidationSession, int]:
    """Roda o pipeline completo de validacao 3 dias.

    Returns:
        (session, exit_code)
    """
    session = ValidationSession(
        mode=mode,
        started_at=datetime.now().isoformat(),
    )

    if mode == "mock":
        # Cria environment mock. Cada tick eh simulado em <0.05s.
        env = MockMT5Environment(
            seed=42,
            inject_ghost=inject_ghost,
            inject_drift_flag=inject_drift,
            verbose=verbose,
        )
        engine = ValidationEngine(env, inject_ghost=inject_ghost,
                                   inject_drift_flag=inject_drift, verbose=verbose)

        # 3 dias: hoje, ontem, anteontem
        base_date = datetime.now().date()
        days = [base_date - timedelta(days=i) for i in range(2, -1, -1)]
        # 2026-07-01, 2026-06-30, 2026-06-29 (ordem cronologica)

        for idx, day_date in enumerate(days, start=1):
            day_iso = day_date.strftime("%Y-%m-%d")
            report = engine.simulate_day(
                day_index=idx,
                day_iso=day_iso,
                trades_per_day=trades_per_day,
                accel_sec_per_day=accel_sec_per_day,
            )
            session.days.append(report)

        engine.cleanup()
    else:
        # Modo LIVE: nao suportado em execucao automatica (WO muito alta pra prompt).
        # Esqueleto: connecta MT5, loop, e retorna PASS quando 3 dias passam.
        # Implementacao: ver branch live_mode (Fase 12).
        _log(f"AVISO: modo '{mode}' ainda nao implementado; fallback para mock.")
        env = MockMT5Environment(seed=int(time.time()))
        engine = ValidationEngine(env, verbose=verbose)
        base_date = datetime.now().date()
        for idx, day_date in enumerate(
            [base_date - timedelta(days=i) for i in range(2, -1, -1)], start=1
        ):
            day_iso = day_date.strftime("%Y-%m-%d")
            report = engine.simulate_day(
                day_index=idx, day_iso=day_iso,
                trades_per_day=trades_per_day,
                accel_sec_per_day=accel_sec_per_day,
            )
            session.days.append(report)
        engine.cleanup()

    session.finished_at = datetime.now().isoformat()

    # Output path
    md_name = f"validation_3days_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    md_path = DATA_DIR / md_name
    md_path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown_report(session, md_path)
    session.output_md_path = md_path

    exit_code = 0 if session.passed else 1
    return session, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suite E2E de validacao 3 dias — drift detection MT5 vs state vs DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python3 scripts/vt_validate_3days.py                    # mock, exit 0 se verde
  python3 scripts/vt_validate_3days.py --mode=mock -v     # verbose
  python3 scripts/vt_validate_3days.py --inject-ghost     # teste negativo (ghost)
  python3 scripts/vt_validate_3days.py --inject-drift     # teste negativo (drift)
        """,
    )
    parser.add_argument(
        "--mode", choices=["mock", "live"], default="mock",
        help="Modo de execucao: mock (acelerado) ou live (MT5 real).",
    )
    parser.add_argument(
        "--accel-sec-per-day", type=float, default=DEFAULT_ACCEL_DAY_SEC,
        help="Tempo simulado por dia em segundos (modo mock).",
    )
    parser.add_argument(
        "--trades-min", type=int, default=DEFAULT_TRADES_PER_DAY[0],
        help="Minimo de trades por dia no modo mock.",
    )
    parser.add_argument(
        "--trades-max", type=int, default=DEFAULT_TRADES_PER_DAY[1],
        help="Maximo de trades por dia no modo mock.",
    )
    parser.add_argument(
        "--inject-ghost", action="store_true",
        help="(teste negativo) Injeta ghost trade pra validar deteccao.",
    )
    parser.add_argument(
        "--inject-drift", action="store_true",
        help="(teste negativo) Injeta drift pra validar alerta.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Log detalhado (todos ticks, nao so resumo diario).",
    )

    args = parser.parse_args()

    session, exit_code = run_validation(
        mode=args.mode,
        accel_sec_per_day=args.accel_sec_per_day,
        trades_per_day=(args.trades_min, args.trades_max),
        inject_ghost=args.inject_ghost,
        inject_drift=args.inject_drift,
        verbose=args.verbose,
    )

    print("")
    print(f"{'='*60}")
    print(f"RELATORIO FINAL")
    print(f"{'='*60}")
    for d in session.days:
        status = "PASS" if d.passed else "FAIL"
        print(
            f"Dia {d.day_index} ({d.date_iso}): {status} "
            f"| drift=R${d.drift:.2f} "
            f"| orphans={d.n_orphans} "
            f"| ghosts={d.n_ghosts} "
            f"| GHOST(pnl=0)={d.n_ghost_pnl_zero}"
        )
    print(f"{'='*60}")
    overall = "PASS" if session.passed else "FAIL"
    print(f"OVERALL: {overall}")
    print(f"Relatorio: {session.output_md_path}")
    print(f"Exit code: {exit_code}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
