#!/usr/bin/env python3
"""
Vibe-Trading Autotrader — Daemon autônomo com estratégias plugin.

Estratégias definidas em vt_config.json (atualmente EMA_PULLBACK para WDO e WIN).
Novas estratégias: adicione em strategies/ e referencie no config.

Funcionalidades:
- Estratégias por símbolo (configurável)
- SL obrigatório (ATR × multiplicador)
- Trailing stop (ativa X×ATR, distância Y×ATR)
- Breakeven automático após X minutos
- Time-trail após Y minutos
- Validação pós-envio com LLM (validator)
- Fecha tudo às 16:45
- Log completo no SQLite
- Notificações Telegram

Uso:
    python vt_autotrader.py              # Roda durante horário de mercado
    python vt_autotrader.py --once      # Uma única verificação
    python vt_autotrader.py --close     # Fecha tudo e encerra
    python vt_autotrader.py --status    # Status atual
"""

import sys
import os
import time
import json
import signal
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mt5"))  # para imports 'from mt5_orchestrator' em mt5_error_recovery

from core.vt_trade_log import init_db, log_entry, log_exit, import_mt5_history, get_daily_summary, get_events_daily_summary, sync_fees_from_mt5
from mt5.mt5_orchestrator import status, tick, history, _run_wine, EXECUTOR_WIN, close_all
from mt5.mt5_error_recovery import safe_buy, safe_sell, safe_close
from core.vt_emergency import safe_modify_sl_with_emergency_close
from core.vt_config_loader import load_config, load_effective_config
from core.vt_strategy_loader import load_strategies, get_strategy_func, reload_strategies
from core.vt_order_validator_v2 import validate_order, validate_pre_send
from core.vt_calendar import is_trading_day, resolve_all_symbols, get_contract_expiry, _parse_contract_code, is_rollover_contract
from core.vt_block_notify import (  # noqa: E402,F401  (Wave N+block_notify 2026-07-20)
    notify_block_activated,
    CAT_HALT_TRADING,
    CAT_HALT_NEW_TRADES,
    CAT_DISABLED_SYMBOLS,
    CAT_DISABLED_TF,
    CAT_AGGREGATE_BLACKOUT,
    CAT_MAX_DAILY_LOSS,
)
# Wave 1C.3 (Bruno 08/07): snapshot do saldo MT5 no startup para o helper
# do copilot (FALLBACK-BALANCE em monitoring/vt_copilot.py) ter baseline
# confiavel do dia. Helper eh idempotente e sanity-checked.
from core.vt_starting_balance import (
    get_today_starting_balance as _get_starting_balance,
    set_today_starting_balance as _set_starting_balance,
)

# GUARD DE CONTA (Bruno 05/08): ÚNICAS contas em que o autotrader pode operar.
# A conta real 2257579 (XPMT5-PRD) foi REMOVIDA da operação — NÃO adicioná-la
# de volta sem autorização explícita do Bruno. Para restaurar: relogar a conta
# no MT5 (senha no portal XP) e incluir o login nesta tupla.
ALLOWED_ACCOUNT_LOGINS = (52257579,)
# Fase 2.5 (architecture_proposal_2026_07_01.md, secao 3.2): centraliza acesso
# MT5 + truth layer. validate_order_pre_send() daqui e re-export da fonte
# autoritativa em core.vt_truth (toda chamada MT5 sensivel passa por la).
# Mantemos validate_order_pre_send local como thin wrapper que delega, para
# nao quebrar callers externos (tests, scripts) que importam daqui.
from core import vt_truth as _truth
# Bruno 30/06 (defesa drift DB↔MT5): reconciliação proativa via MT5 history.
# Import lazy para não crashar autotrader se módulo tem bug — mas módulo é
# razoavelmente isolado (sem dependências pesadas).
try:
    from core.vt_history_reconcile import reconcile_db_with_mt5_history, reconcile_pending_excluded as _reconcile_pending_excluded
    _HISTORY_RECONCILE_AVAILABLE = True
except Exception as _e_imp:
    print(f"[INIT] ⚠️  vt_history_reconcile indisponível: {_e_imp}", flush=True)
    _HISTORY_RECONCILE_AVAILABLE = False

# ===== PAUSE FILE (Bruno 2026-07-20) =====
# API manual para travar novas entradas sem derrubar o daemon:
#   touch data/autotrader.paused  → bloqueia novas entradas
#   rm    data/autotrader.paused  → retoma operação normal
# Semântica: posições já abertas continuam sendo gerenciadas (trailing/SL/
# breakeven). Idêntico ao que halt_new_trades deveria fazer, mas via arquivo
# (auditoria fácil, reage em ≤ check_interval s, sem reiniciar o daemon).
PAUSE_FILE = Path(__file__).parent.parent / "data" / "autotrader.paused"
# Cache do último estado observado para detectar transição e notificar uma
# única vez via Telegram (evita spam a cada tick). None = ainda não avaliado.
_last_pause_state: Optional[bool] = None


def _is_paused() -> bool:
    """True se data/autotrader.paused existe. Idempotente, não levanta."""
    try:
        return PAUSE_FILE.exists()
    except OSError:
        return False


# ===== CONFIGURAÇÃO =====
# Config carregada do vt_config.json com hot reload
# Para alterar parâmetros: edite vt_config.json ou use save_params/save_full_config
CONFIG = load_effective_config()

# Funções utilitárias passadas para as estratégias plugins
_strategy_utils = {}

# Bruno 30/06 (defesa #3 — reconciliação periódica): counter compartilhado
# entre iterações do run_daemon. Mutável (list) porque counter += 1 em loop
# sem precisar redeclarar global.
_iter_counter = [0]
# Wave 1110.B (Bruno 30/07): fast-check mode — quando trailing profit lock
# está ativo (PnL >= 50% do target), o loop acelera de check_interval (30s)
# para trailing_fast_interval (5s) pra capturar o pico com precisão.
_trailing_fast_mode = [False]


def _init_strategy_utils():
    """Inicializa o dict de utils para as estratégias (chamado no startup)."""
    global _strategy_utils
    _strategy_utils = {
        "calculate_vwap": calculate_vwap,
        "calculate_ema": calculate_ema,
        "calculate_rsi": calculate_rsi,
        "calculate_adx": calculate_adx,
        "calculate_bollinger": calculate_bollinger,
        "calculate_atr": calculate_atr,
        "get_market_regime": get_market_regime,
        "calc_sl": _calc_sl,
    }


# Wave 8.6.2 (2026-06-26): sincroniza state.daily_pnl com DB.
# Bug detectado: após restart, state em /tmp ficava off-by-one
# com PnL real (não somava trades pré-restart). Causava notificações
# Telegram com PnL errado.
def _sync_daily_pnl_with_db(state):
    """Recalcula state.daily_pnl, trade_count, wins, losses a partir do DB.

    Chamado:
    - Na inicialização do autotrader (depois de carregar state do disco)
    - Pode ser chamado manualmente para forçar sync

    Idempotente: se state já estiver em sync, não muda nada.
    """
    import sqlite3
    from datetime import datetime as _dt

    db_path = "/home/bruno/Projects/Vibe-Trading/vt_trades.db"
    if not os.path.exists(db_path):
        return
    try:
        # Bug fix Bruno 2026-06-30: timeout=30 + busy_timeout evita "database is locked"
        # que crashava o autotrader quando pytest/scripts tocavam o DB em paralelo.
        db = sqlite3.connect(db_path, timeout=30.0)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        today = _dt.now().strftime("%Y-%m-%d")
        row = db.execute("""
            SELECT COALESCE(SUM(net_pnl), 0),
                   COUNT(*),
                   COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0)
            FROM trades WHERE date(entry_time) = ?
            AND exit_time IS NOT NULL
            AND (strategy IS NULL OR strategy NOT LIKE '%[EXCLUDED]%')
        """, (today,)).fetchone()
        db.close()
        if row is None:
            return
        db_pnl = row[0] or 0
        db_n = row[1] or 0
        db_wins = row[2] or 0
        # Se difere do state (off-by-one), resincroniza
        # Tolerância: R$ 0.01 (floats)
        if (abs(state.daily_pnl - db_pnl) > 0.01 or
            state.trade_count != db_n or
            state.wins != db_wins):
            print(f"[STATE] Resync com DB: daily_pnl R${state.daily_pnl:+.2f}→R${db_pnl:+.2f}, "
                  f"n={state.trade_count}→{db_n}, wins={state.wins}→{db_wins}",
                  flush=True)
            state.daily_pnl = db_pnl
            state.trade_count = db_n
            state.wins = db_wins
            state.losses = max(0, db_n - db_wins)
    except Exception as e:
        print(f"[STATE] Erro ao sincronizar com DB: {e}", flush=True)


class SessionState:
    def __init__(self):
        # FIX 3 (Wave 14.3 — 2026-07-14, Bruno): posições são slots
        # independentes por (symbol, tf). Múltiplos TFs do mesmo symbol
        # podem coexistir (WINQ26_M5 + WINQ26_M15 + WINQ26_M30 + ...).
        # Chave sempre no formato `f"{symbol}_{tf}"`. Nenhum código deve
        # bloquear entrada em um TF por haver posição aberta em outro TF
        # do mesmo symbol — isso era o bug DEFESA2-DRIFT, corrigido em
        # _defenses_ok() (L2016+). Ver também manage_position() e
        # reconcile_positions_with_mt5() que iteram por slot, não por symbol.
        self.positions = {}
        self.last_signals = {}
        self.daily_pnl = 0
        # Wave 1C.3: baseline do dia, gravado em /tmp/vt_intraday_starting_balance.json
        # no startup (record_starting_balance). Usado pelo FALLBACK-BALANCE
        # do copilot quando MT5 history esta vazio. Antes do fix, o copilot
        # tinha `base_balance = 1002230.57` HARDCODED e dava drift acumulado
        # (R$403,83 entre 02/07 e 08/07).
        self.starting_balance = None
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.started_at = None
        self.closed = False
        self.notified_close = False
        self.last_trade_time = {}
        self.daily_trade_count = 0
        self.current_day = None
        self.daily_trade_by_symbol = {}  # {symbol: count}
        self.consecutive_losses = {}      # per-symbol tracking: {symbol: count}
        self.max_consecutive_losses = 999  # DESATIVADO (demo mode) — era 3
        self.halt_until = {}              # per-symbol: {symbol: datetime} — halt until this time
        self.resolved_symbols = {}        # cache: {"WDO": "WDON26", "WIN": "WINM26"}
        self.resolved_day = ""            # dia do cache (reseta a cada dia)
        # Wave Per-TF (Bruno 2026-07-07): cooldown cross-TF quando symbol tem
        # 2+ pos perdendo. Chave: "{symbol_root}" → datetime de desbloqueio.
        # Bloqueia novas entradas em OUTROS TFs do mesmo symbol ate o tempo
        # expirar (default 30min). Por symbol_root (WIN/WDO/BIT/WSP), nao por
        # contracto resolved (WDON26), porque a regra é por ativo, não por letra.
        self.cross_tf_cooldown = {}       # {symbol_root: datetime}

        # Wave N+4B (2026-07-08): per-(symbol, direction) tracking para
        # cooldown de perda consecutiva. Chave: f"{symbol_root}_{direction}" → datetime.
        # Bloqueia sinais anti-mesma-causa por 30min após 2 losses.
        self.last_loss_direction_per_symbol = {}  # {(symbol, direction): datetime}
        self.consecutive_loss_direction_count = {}  # {(symbol, direction): int}

        # Wave N+1 (2026-07-08): rastreia ts da última vez que cada (symbol, tf,
        # strategy) retornou signal (truthy). Usado pela heurística de
        # "setup latente vs sem setup" — se estratégia retornou None agora mas
        # tinha retornado signal há <LATENT_LOOKBACK_MINUTES, é filter-reject
        # e deve ser logado em core/vt_signal_journal.log_blocked_signal.
        self.recent_signal_ts = {}        # {(symbol, tf, strategy): datetime}

        # FASE 3 (2026-07-01): SessionState NAO le mais /tmp/vt_autotrader_state.json
        # no startup. State vira projecao em memoria, reconstruida do MT5
        # via rebuild_state_from_mt5() (chamado no startup, ver linha 429).
        # Mantemos so o _sync_daily_pnl_with_db (le do DB SQLite, nao do state
        # file) para nao zerar state.daily_pnl apos restart.
        _sync_daily_pnl_with_db(self)

    # FASE 3: STATE_FILE descontinuado. NUNCA eh lido/escrito em disco.
    # Mantido como atributo de classe apenas para evitar AttributeError em
    # testes legacy (test_state_mirror_blocked.py usa setattr pra redirecionar).
    # A Fase 3 ELIMINOU o conceito de state persistente: rebuild_state_from_mt5()
    # eh a unica fonte de verdade para state.positions.
    STATE_FILE = "/tmp/vt_autotrader_state.json"  # noqa: F841 — legado, nao usado

    @staticmethod
    def _json_default(obj):
        """Serializa datetime/date pra JSON."""
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return str(obj)

    def _serialize_positions(self):
        """Serializa positions tratando datetimes."""
        out = {}
        for k, v in self.positions.items():
            pos = {}
            for pk, pv in v.items():
                if isinstance(pv, datetime):
                    pos[pk] = pv.isoformat()
                else:
                    pos[pk] = pv
            out[k] = pos
        return out

    def to_dict(self):
        return {
            "positions": self._serialize_positions(),
            "last_signals": {k: {**v, "ts": v["ts"].isoformat() if isinstance(v.get("ts"), datetime) else None}
                             for k, v in self.last_signals.items()},
            "last_trade_time": {k: v.isoformat() if isinstance(v, datetime) else str(v)
                                for k, v in self.last_trade_time.items()},
            "daily_pnl": self.daily_pnl,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "started_at": str(self.started_at) if self.started_at else None,
            "closed": self.closed,
            "daily_trade_count": self.daily_trade_count,
            "current_day": str(self.current_day) if self.current_day else None,
            "daily_trade_by_symbol": self.daily_trade_by_symbol,
            "consecutive_losses": self.consecutive_losses,
            "halt_until": {k: v.isoformat() if isinstance(v, datetime) else str(v)
                           for k, v in self.halt_until.items()},
        }

    # Wave 1C.3 (Bruno 08/07): snapshot do saldo MT5 no startup. Chamada
    # APENAS UMA VEZ antes do primeiro ciclo do run_daemon() (ver bloco
    # startup abaixo). Grava em /tmp/vt_intraday_starting_balance.json via
    # core.vt_starting_balance — le helper idempotente com sanity check.
    #
    # Fixa o bug do FALLBACK-BALANCE do copilot: antes o hardcoded 1002230.57
    # causava drift acumulado (R$403,83 entre 02/07 e 08/07) quando o saldo
    # real tinha saido daquele snapshot EOD de 01/07.
    def record_starting_balance(self):
        """Grava snapshot do saldo MT5 do dia no helper.

        Comportamento:
        - Se ja existe snapshot HOJE, NO-OP (idempotente: caller pode chamar
          em restart mid-day sem estragar o valor original).
        - Se MT5 status() falhar, NO-OP + log (nao bloqueia startup).
        - Se balance <= 0 ou > 10M, NO-OP + log (sanity check protege contra
          retorno lixoso do MT5).
        """
        try:
            # Idempotencia: se ja tem snapshot de hoje, nao sobrescreve.
            existing = _get_starting_balance()
            if existing is not None:
                print(
                    f"[STARTING-BALANCE] snapshot ja existe para hoje "
                    f"(R$ {existing:,.2f}); record_starting_balance NO-OP",
                    flush=True,
                )
                self.starting_balance = existing
                return existing

            s = status()
            if not isinstance(s, dict):
                print(
                    "[STARTING-BALANCE] status() retornou tipo invalido "
                    f"({type(s).__name__}); record_starting_balance NO-OP",
                    flush=True,
                )
                return None

            balance = float(s.get("account", {}).get("balance", 0.0) or 0.0)
            if not (0 < balance < 10_000_000):
                print(
                    f"[STARTING-BALANCE] balance fora da sanity "
                    f"(R$ {balance:,.2f}); record_starting_balance NO-OP",
                    flush=True,
                )
                return None

            wrote = _set_starting_balance(balance, source="autotrader_startup")
            if wrote:
                self.starting_balance = balance
                print(
                    f"[STARTING-BALANCE] baseline gravado no startup: "
                    f"R$ {balance:,.2f}",
                    flush=True,
                )
            else:
                # Ja gravado entre o check acima e o set (race com outro
                # processo); re-le helper pra alinhar state.
                self.starting_balance = _get_starting_balance()
            return self.starting_balance

        except Exception as _e:
            # Falha do helper NAO bloqueia startup — copilot cai no
            # fallback hardcoded se este snapshot nao existir.
            print(
                f"[STARTING-BALANCE] erro nao-tratado (continua): "
                f"{type(_e).__name__}: {_e}",
                flush=True,
            )
            return None


    #
    # Mantido apenas o helper _fetch_mt5_truth_symbols (consulta status()
    # filtrada por magic=555501 + comment=VibeTrading). Era usado pelo save()
    # antigo (Fase 2) que gravava apos filtrar; o save() foi descontinuado
    # na Fase 3. O helper fica disponivel para reuso futuro (ex.: filtros
    # de watchdog / preview de expose positions).
    #
    # _filter_positions_via_mt5_truth foi REMOVIDO na Fase 3 (sem callers).
    _mt5_truth_symbols_cache = None
    _mt5_truth_symbols_ts = 0.0
    _MT5_TRUTH_TTL_SEC = 5.0

    @classmethod
    def _fetch_mt5_truth_symbols(cls) -> Optional[set]:
        """Consulta MT5 status() e retorna set de symbols abertos (magic 555501).

        Retorna None se MT5 falhou (caller deve FAIL-SAFE e não filtrar).
        Cache TTL = _MT5_TRUTH_TTL_SEC para evitar spam Wine.
        """
        now = time.time()
        if cls._mt5_truth_symbols_cache is not None and (now - cls._mt5_truth_symbols_ts) < cls._MT5_TRUTH_TTL_SEC:
            return cls._mt5_truth_symbols_cache

        try:
            s = status()
        except Exception as e:
            # FAIL-SAFE: log + retorna None (caller não filtra)
            print(f"[STATE-MIRROR] status() falhou ({type(e).__name__}: {e}) — FAIL-SAFE: não filtra state", flush=True)
            cls._mt5_truth_symbols_cache = None
            cls._mt5_truth_symbols_ts = now
            return None

        if not isinstance(s, dict):
            print(f"[STATE-MIRROR] status() retornou tipo inválido ({type(s).__name__}) — FAIL-SAFE: não filtra state", flush=True)
            cls._mt5_truth_symbols_cache = None
            cls._mt5_truth_symbols_ts = now
            return None

        truth_symbols = set()
        for p in s.get("positions") or []:
            if not isinstance(p, dict):
                continue
            try:
                magic_int = int(p.get("magic", 0) or 0)
            except (TypeError, ValueError):
                continue
            comment = (p.get("comment") or "").strip()
            symbol = (p.get("symbol") or "").strip()
            if magic_int == 555501 and comment == "VibeTrading" and symbol:
                truth_symbols.add(symbol)

        cls._mt5_truth_symbols_cache = truth_symbols
        cls._mt5_truth_symbols_ts = now
        return truth_symbols

    def save(self):
        """STATE FILE DESCONTINUADO (FASE 3, 2026-07-01).

        Comportamento historico (FASE 1/2): persistia state em disco
        via atomic write (json.dump + os.fsync + rename). Orfas
        propagavam entre restarts — state stale reconstruido do cache,
        NAO do MT5 (bug latente: data/architecture_audit_2026_07_01.md
        secao 4.3, drift state↔MT5).

        Comportamento atual: NO-OP. Loga WARN para nao silenciar
        call sites existentes (mantidos no codigo por seguranca).
        NAO escreve em /tmp/vt_autotrader_state.json. Toda decisao
        passa por _truth.get_open_positions() (cache 2s) no proximo tick.

        Migre qualquer call site que dependia de persistir valores
        via state.save() para usar _truth diretamente. Exemplos:
          - halt_until, consecutive_losses: lidos/escritos no DB.
          - daily_pnl, trade_count, wins, losses: _sync_daily_pnl_with_db().
          - positions: rebuild_state_from_mt5() no proximo tick.
        """
        print(
            "[STATE] WARN: save() descontinuado na Fase 3. State vira "
            "projecao em memoria. Use _truth.get_open_positions() como "
            "fonte de verdade. NAO escreve em /tmp/vt_autotrader_state.json.",
            flush=True,
        )

    # === INICIO BLOCO FASE 3 — REMOVIDO ====================================
    # save() antigo persistia em disco via atomic write. Foi ELIMINADO na
    # Fase 3 (state nao eh mais cache autoritativo). O bloco abaixo eh mantido
    # comentado apenas como referencia historica para o README/CHANGELOG do
    # refactor. Removido em definitivo em commits futuros.
    #
    # def _save_legacy_discontinued(self):
    #     """ANTIGO — Phase 1/2. Persistia state com filtro MT5-truth.
    #
    #     O codigo antigo escrevia em STATE_FILE apos filtrar
    #     state.positions via _filter_positions_via_mt5_truth(). Orfas
    #     eram removidas antes de gravar. FAIL-SAFE: se MT5 falhasse,
    #     gravava sem filtro.
    #     """
    #     import json as _json, os
    #     try:
    #         self._filter_positions_via_mt5_truth()
    #     except Exception as _e_filt:
    #         print(f"[STATE-MIRROR] filtro explodiu ({type(_e_filt).__name__}: {_e_filt})", flush=True)
    #     tmp = self.STATE_FILE + ".tmp"
    #     try:
    #         with open(tmp, "w") as f:
    #             _json.dump(self.to_dict(), f, indent=2, default=self._json_default)
    #             f.flush()
    #             os.fsync(f.fileno())
    #         os.rename(tmp, self.STATE_FILE)
    #     except Exception as e:
    #         print(f"[STATE] Erro ao salvar: {e}", flush=True)
    #         try:
    #             os.unlink(tmp)
    #         except OSError:
    #             pass
    # === FIM BLOCO FASE 3 — REMOVIDO =======================================

    # FASE 3: load() descontinuado. NUNCA le disco.
    # Mantido como no-op para nao quebrar test_state_daily_pnl_sync_with_db.py
    # (legacy), que faz SessionState() + state.load() no fluxo de import.
    # A unica fonte de verdade para popular state agora eh rebuild_state_from_mt5().
    def load(self):
        """STATE FILE DESCONTINUADO (FASE 3, 2026-07-01). No-op.

        Comportamento historico: lia /tmp/vt_autotrader_state.json (se existia
        e era do mesmo dia) e populava self.* com os valores persistidos.

        Comportamento atual: retorna imediatamente. State vazio + 1 tick =
        state reconstruido do MT5 via rebuild_state_from_mt5().

        O restart do autotrader NAO herda mais halt_until, consecutive_losses,
        daily_trade_count — todos esses valores passam pelo DB ou sao derivados
        de _truth (PnL) / rebuild_state_from_mt5() (positions). Os call sites
        foram migrados; load() existe apenas para compat.
        """
        return

    # FASE 3 (2026-07-01): API publica nova. Recen construido do MT5.
    # Substitui o ciclo save/load que era feito em /tmp/vt_autotrader_state.json.
    #
    # FLUXO:
    #   1. SessionState() constroi state VAZIO (positions={}, halt_until={}).
    #   2. rebuild_state_from_mt5() eh chamado IMEDIATAMENTE para popular
    #      state.positions a partir de core.vt_truth.get_open_positions().
    #   3. No proximo tick, manage_position() ja encontra state.positions
    #      sincronizado com MT5 (zero orphans por definicao).
    #
    # VANTAGENS vs save/load:
    #   - Restart mid-day: state consistente com broker (truth MT5, nao
    #     cache stale que ficou dias na /tmp).
    #   - Sem race entre save() e modify_sl() (que era a causa raiz dos
    #     orphans persistentes — state.save() no momento errado).
    #   - Sem file I/O (json.dump + os.fsync + rename) — ~5ms economizados
    #     por save() chamado 10x/tick.
    def rebuild_state_from_mt5(self):
        """Reconstrói state.positions consultando MT5 (truth autoritativo).

        Limpa self.positions e popula com 1 entry por Position MT5 aberta
        (magic=555501). Para cada position, monta dict compatível com o
        formato historico de state.positions usado por manage_position():

            state.positions[f"{symbol}_M5"] = {
                "direction": "BUY" | "SELL",
                "entry_price": float,
                "entry_ticket": str(ticket),
                "entry_time": datetime.fromisoformat(open_time),
                "volume": float,
                "tf": "M5",
                "from_mt5_rebuild": True,  # flag de origem (debug only)
            }

        Args:
            (nenhum — usa _truth.get_open_positions())

        Returns:
            int: numero de positions reconstruidas (0 se MT5 indisponivel).

        Comportamento:
            - FAIL-SAFE: se MT5 indisponivel, NAO levanta. Retorna 0 e
              loga WARN. O proximo tick pode reconstruir novamente.
            - Idempotente: rodar 2x seguidas com mesmo MT5 = mesmo state.
            - tf default = "M5": o MT5 nao expoe timeframe na Position
              (vem do deal, nao da posicao aberta). Mantemos M5 como
              padrao porque o autotrader opera majoritariamente em M5.
              Se algum call site precisa de outro TF, refatoramos depois.
            - NUNCA toca halt_until, consecutive_losses, daily_pnl (esses
              passam pelo DB / sao derivados em _sync_daily_pnl_with_db).
        """
        # _truth ja trata falhas do MT5 silenciosamente e devolve list vazia.
        # Import lazy para evitar import circular no startup.
        from core import vt_truth as _truth_layer
        positions_mt5 = _truth_layer.get_open_positions(magic_filter=_truth_layer.MAGIC_VIBETRADING)

        # Limpa e repopula positions. NAO faz merge: state deve ser
        # EXATAMENTE o que o MT5 diz. Nenhum orfao persiste por
        # definicao (vs Fase 1/2 onde load() podia reintroduzir keys).
        self.positions = {}

        for p in positions_mt5:
            # _truth.Position eh frozen dataclass. Campos ja normalizados.
            # monta dict compativel com o que manage_position espera.
            # IMPORTANTE: manage_position() faz pos["atr"], pos["sl_pts"],
            # pos["best_price"], pos["trail_on"], pos["bar_count"],
            # pos["trade_log_id"] hard-access. ANTES do fix 2026-07-14
            # (Wave 14.2) o rebuild deixava esses campos ausentes → KeyError
            # a cada tick → autotrader travado em loop de erro. Defaults
            # abaixo são fail-safe: trajam a posição como "recém-construída"
            # (trail desligado, sem bar progress, sem link com DB).
            try:
                state_key = f"{p.symbol}_M5"
                # Wave 880.B1 fix (Bruno 2026-08-05): o XPMT5-PRD pode retornar
                # price_open=0 (documentado em ~L2304). Antes isto gravava
                # entry_price=0.0 no state, e manage_position calculava
                # profit_pts = best - 0 = preço absoluto, armeando toda a gestão
                # falsa (trailing/TP1/profit-lock). Agora, se price_open é 0/None,
                # usa price_current como fallback razoável (a posição existe com
                # algum preço; current é melhor estimativa que 0) e loga WARN.
                # manage_position também tem um guard defensivo (L2687) que pula
                # a gestão se entry_price ainda vier <= 0.
                _entry_price = float(p.price_open or 0.0)
                if _entry_price <= 0:
                    _fallback = float(getattr(p, "price_current", 0.0) or 0.0)
                    if _fallback > 0:
                        print(
                            f"[STATE-REBUILD] {p.symbol} price_open=0 — usando "
                            f"price_current={_fallback} como entry_price (fallback)",
                            flush=True,
                        )
                        _entry_price = _fallback
                _current = float(p.price_current or _entry_price)
                _direction = "BUY" if p.direction in ("BUY", 0, "0") else "SELL"
                # best_price: do lado a favor (BUY=max, SELL=min)
                _best = _current if _direction == "BUY" else _current
                if _direction == "BUY":
                    _best = max(_entry_price, _current)
                else:
                    _best = min(_entry_price, _current)
                # bar_count: estimado pelo tempo de abertura. manage_position
                # usa bar_count para calcular pos_minutes = bar_count *
                # check_interval/60. Estimar pela idade da posição.
                _check_interval = 30  # default — autotrader real usa
                _entry_ts = _parse_mt5_time(p.open_time).timestamp() if p.open_time else None
                if _entry_ts and _entry_ts > 0:
                    _age_min = max(0, (datetime.now().timestamp() - _entry_ts) / 60)
                    _bar_count = max(1, int(_age_min / (_check_interval / 60)))
                else:
                    _bar_count = 1
                # atr: tenta buscar via bars; senão usa fallback razoável.
                # WINQ26 M5: ATR típico ~150-300. Se não conseguir calcular,
                # não bloqueia — manage_position tem guards (atr > 0 checks).
                _atr = 0
                _sl_pts = 0
                try:
                    _bars = fetch_bars(p.symbol, "M5", CONFIG.get("bars_count", 100))
                    if _bars and len(_bars) >= 20:
                        _atr = calculate_atr(_bars, 14) or 0
                except Exception:
                    pass
                # trade_log_id: tenta achar no DB
                _trade_log_id = None
                try:
                    import sqlite3 as _sq
                    _c = _sq.connect("vt_trades.db", timeout=5)
                    _c.row_factory = _sq.Row
                    _r = _c.execute(
                        "SELECT id FROM trades WHERE entry_ticket = ? "
                        "AND exit_time IS NULL",
                        (str(p.ticket),),
                    ).fetchone()
                    if _r:
                        _trade_log_id = _r["id"]
                    _c.close()
                except Exception:
                    pass
                self.positions[state_key] = {
                    "direction": _direction,
                    "entry_price": _entry_price,
                    "entry_ticket": str(p.ticket),
                    "entry_time": _parse_mt5_time(p.open_time),
                    "volume": float(p.volume or 0.0),
                    "tf": "M5",
                    "from_mt5_rebuild": True,  # flag: veio do rebuild (nao de _execute_entry)
                    # Wave 14.2: campos que manage_position() faz hard-access
                    "atr": _atr,
                    "sl_pts": _sl_pts,
                    "best_price": _best,
                    "trail_on": False,
                    "bar_count": _bar_count,
                    "trade_log_id": _trade_log_id,
                }
            except Exception as _e_rebuild:
                # FAIL-SAFE: pula pos mal-formada (mesmo padrao do _truth)
                print(
                    f"[STATE-REBUILD] pulou pos malformada "
                    f"(ticket={getattr(p, 'ticket', '?')}, "
                    f"symbol={getattr(p, 'symbol', '?')}, "
                    f"err={type(_e_rebuild).__name__}: {_e_rebuild})",
                    flush=True,
                )
                continue

        n = len(self.positions)
        if n > 0:
            print(
                f"[STATE-REBUILD] reconstruidas {n} positions do MT5: "
                f"{list(self.positions.keys())}",
                flush=True,
            )
        else:
            print(
                "[STATE-REBUILD] MT5 sem posicoes abertas (state reconstruido vazio)",
                flush=True,
            )
        return n

# ==== FASE 3 helper: parse de tempo MT5 ====
# _truth.Position.open_time vem como str epoch ("1719840300") ou ISO
# ("2026-07-01 14:30:00"). _sync_daily_pnl_with_db usa logica similar,
# mas aqui eh local pq o topo do arquivo ja usa varios wrappers ad-hoc.
def _parse_mt5_time(time_str):
    """Converte open_time do MT5 (epoch ou ISO) pra datetime.

    Retorna datetime.now() se parsing falhar (fail-safe — estado valido,
    mesmo que com timestamp impreciso).
    """
    from datetime import datetime as _dt
    if not time_str:
        return _dt.now()
    # Epoch numerico
    try:
        epoch = float(time_str)
        if epoch > 1e9:
            return _dt.fromtimestamp(epoch)
    except (ValueError, TypeError, OSError):
        pass
    # ISO
    try:
        s = str(time_str).replace("T", " ")[:19]
        return _dt.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    return _dt.now()


state = SessionState()
# FASE 3: substitui state.load() (que lia /tmp/vt_autotrader_state.json)
# por state.rebuild_state_from_mt5() (consulta MT5 truth).
# Pos-processamento: _sync_daily_pnl_with_db() ja eh chamado dentro do __init__
# da SessionState para popular state.daily_pnl com base no DB (nao state file).
state.rebuild_state_from_mt5()  # Fase 3: state reconstruido do MT5, NAO do disco

# Wave 1C.3 — fixa baseline do dia para FALLBACK-BALANCE no copilot.
# Chamada UMA VEZ no startup (module-level, executada quando o autotrader
# eh importado). Idempotente: se ja existe snapshot de HOJE (restart mid-day),
# NA-OP e reaproveita. Se MT5 indisponivel, loga e segue sem bloquear.
state.record_starting_balance()

log_file = Path("/tmp/vt_autotrader.log")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


# Bug fix (30/06): target original "telegram:-1004284773048" cai no guard
# anti-loop do Hermes quando o autotrader roda sob o cron (mesmo target do
# deliver do job e9895f2d2176). O guard suprime silenciosamente o envio.
#
# Testes: "telegram:-1004284773048" → bloqueado (Skipped)
#         "telegram:-1004284773048:1" → "sent" (passa pelo guard)
#
# Solução: adicionar thread ID ":1" ao final. O ":1" indica a thread raiz do
# grupo (topico principal), mantendo o destino visual correto.
TELEGRAM_TARGET = "telegram:-1004284773048:1"


# ============================================================
# Hard-kill list (PERMANENTLY_DISABLED)
# Decisão Bruno 2026-06-30: estes símbolos NUNCA podem ser operados,
# independente do que o config, AGI optimizer, vt_copilot ou hot-reload
# tentem fazer. Defesa em profundidade contra "Wave 9-style" reativações
# (commit 7930b4ac reativou IND baseado em backtest otimista de 1 dia).
#
# Para adicionar outro símbolo permanente: basta incluir no set.
# ============================================================
PERMANENTLY_DISABLED = {"IND"}  # índice cheio, sem edge, risco alto


def is_permanently_disabled(symbol: str) -> bool:
    """True se o símbolo (ou seu root) está na hard-kill list."""
    if not symbol:
        return False
    sym = symbol.upper()
    for root in PERMANENTLY_DISABLED:
        if root in sym:
            return True
    return False


def notify_telegram(msg: str):
    try:
        from core.vt_hermes_helper import hermes_send
        ok = hermes_send(TELEGRAM_TARGET, msg)
        if not ok:
            log("[NOTIFY FAIL] hermes_send retornou False")
    except Exception as e:
        log(f"[NOTIFY FAIL] {e}")


def fetch_bars(symbol: str, tf: str = "M5", count: int = 30) -> list:
    result = _run_wine(EXECUTOR_WIN, "bars", symbol, tf, str(count))
    if "bars" in result:
        return result["bars"]
    return []


def calculate_vwap(bars: list, period: int = 20) -> float:
    if not bars or len(bars) < period:
        return 0
    data = bars[:period]
    sum_pv = 0
    sum_v = 0
    for b in data:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        vol = max(b["volume"], 1)
        sum_pv += typical * vol
        sum_v += vol
    return sum_pv / sum_v if sum_v > 0 else 0


def calculate_atr(bars: list, period: int = 14) -> float:
    if not bars or len(bars) < period + 1:
        return 0
    data = bars[:period + 1]
    tr_sum = 0
    for i in range(period):
        h = data[i]["high"]
        l = data[i]["low"]
        c_prev = data[i + 1]["close"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_sum += tr
    return tr_sum / period


def calculate_ema(bars: list, period: int) -> float:
    if not bars or len(bars) < period:
        return 0
    # bars are newest-first; reverse to process chronologically
    chronological = list(reversed(bars))
    seed = sum(b["close"] for b in chronological[:period]) / period
    ema = seed
    multiplier = 2 / (period + 1)
    for b in chronological[period:]:
        ema = b["close"] * multiplier + ema * (1 - multiplier)
    return ema


def calculate_rsi(bars: list, period: int = 14) -> float:
    if not bars or len(bars) < period + 1:
        return 50
    gains = []
    losses = []
    for i in range(min(period, len(bars) - 1)):
        diff = bars[i]["close"] - bars[i + 1]["close"]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    _n = max(len(gains), 1)
    avg_gain = sum(gains) / _n if gains else 0
    avg_loss = sum(losses) / _n if losses else 0.001
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return 100 - (100 / (1 + rs))


def calculate_bollinger(bars: list, period: int = 20, num_std: float = 2.0):
    """Retorna (upper, middle, lower) das Bollinger Bands."""
    if not bars or len(bars) < period:
        return 0, 0, 0
    closes = [b["close"] for b in bars[:period]]
    mid = sum(closes) / period
    variance = sum((c - mid) ** 2 for c in closes) / period
    std = variance ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def calculate_adx(bars: list, period: int = 14):
    """Average Directional Index — mede força da tendência."""
    if not bars or len(bars) < period * 2:
        return 0, 0, 0
    # CRITICAL: bars do MT5 são newest-first; inverter para ordem cronológica
    # Sem isso, +DI/-DI ficam invertidos (tendência de alta parece queda)
    chron_bars = list(reversed(bars[:period * 2]))
    highs = [b["high"] for b in chron_bars]
    lows = [b["low"] for b in chron_bars]
    closes = [b["close"] for b in chron_bars]
    plus_dm = []
    minus_dm = []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    tr_list = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
    if len(tr_list) < period:
        return 0, 0, 0
    atr_val = sum(tr_list[:period]) / period
    plus_dm_smooth = sum(plus_dm[:period]) / period
    minus_dm_smooth = sum(minus_dm[:period]) / period
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm[i]) / period
    if atr_val == 0:
        return 0, 0, 0
    plus_di = 100 * plus_dm_smooth / atr_val
    minus_di = 100 * minus_dm_smooth / atr_val
    di_sum = plus_di + minus_di
    dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
    return dx, plus_di, minus_di


def get_market_regime(bars: list, params: dict = None) -> str:
    if params is None:
        params = CONFIG.get("win", {})
    ema_slow_val = params.get("ema_slow", 21)
    if not bars or len(bars) < ema_slow_val + 5:
        return "CHOPPY"
    ema_f = calculate_ema(bars, params.get("ema_fast", 9))
    ema_s = calculate_ema(bars, ema_slow_val)
    current_price = bars[0]["close"]
    if ema_f == 0 or ema_s == 0 or current_price == 0:
        return "CHOPPY"
    spread = abs(ema_f - ema_s) / current_price
    if spread < params.get("trend_min_spread", 0.001):
        return "CHOPPY"
    elif ema_f > ema_s:
        return "TREND_UP"
    else:
        return "TREND_DOWN"


def is_trading_time() -> bool:
    now = datetime.now()
    # Verifica dia útil + feriados B3
    ok, motivo = is_trading_day(now.date())
    if not ok:
        return False
    h, m = now.hour, now.minute
    start = CONFIG["start_hour"] * 60 + CONFIG["start_minute"]
    end = CONFIG["close_hour"] * 60 + CONFIG["close_minute"]
    current = h * 60 + m
    return start <= current < end


def is_close_time() -> bool:
    now = datetime.now()
    return (now.hour == CONFIG["close_hour"] and now.minute >= CONFIG["close_minute"])


def get_truth_from_mt5(timeout: int = 8) -> dict:
    """Snapshot live do MT5 — fonte absoluta de verdade.

    Lê balance, equity, margin_free, n_positions, pnl_flutuante, positions_open.
    Falha graciosamente: retorna ok=False + error, sem levantar exceção.
    """
    truth = {
        "balance": 0.0, "equity": 0.0, "margin_free": 0.0,
        "n_positions": 0, "pnl_flutuante": 0.0,
        "positions_open": [], "ts": datetime.now().isoformat(),
        "ok": False, "error": None,
    }
    try:
        s = status()  # já importado de mt5.mt5_orchestrator (linha 38)
        acc = s.get("account", {}) or {}
        positions = s.get("positions", []) or []
        truth.update({
            "balance": float(acc.get("balance", 0) or 0),
            "equity": float(acc.get("equity", 0) or 0),
            "margin_free": float(acc.get("free_margin", 0) or 0),
            "n_positions": len(positions),
            "pnl_flutuante": round(sum(float(p.get("profit", 0) or 0) for p in positions), 2),
            "positions_open": positions,
            "account_login": int(acc.get("login", 0) or 0),
            "ok": True,
        })
    except Exception as e:
        truth["error"] = str(e)[:200]
    return truth


def _get_strategy(symbol_root: str) -> str:
    """Retorna a estratégia para o símbolo: VWAP ou BOLLINGER."""
    return CONFIG["strategy"].get(symbol_root, "VWAP")


def _get_strategy_for_tf(symbol_root: str, tf: str) -> str:
    """Retorna a estratégia para o símbolo+TF.
    Prioridade: strategy_by_tf["SYMBOL_TF"] > strategy[symbol] > VWAP
    """
    key = f"{symbol_root}_{tf}"
    by_tf = CONFIG.get("strategy_by_tf", {})
    if key in by_tf:
        return by_tf[key]
    return CONFIG["strategy"].get(symbol_root, "VWAP")


def _get_params_for_tf(symbol_root: str, tf: str) -> dict:
    """Retorna parâmetros para o símbolo+TF.
    Prioridade: params_by_tf["symbol_tf"] > params[symbol] > {}

    Mantém o merge base ∪ params_by_tf porque os exit/risk params
    (breakeven_minutes, max_position_minutes, hard_exit_minutes, trail_*, ...)
    vivem na base do símbolo (ex.: CONFIG["win"]) e NÃO em params_by_tf — eles
    são lidos por manage_position(). Retornar só params_by_tf quebraria as saídas.

    SANITIZAÇÃO: a base carrega chaves legadas `strategy` (ex.: "BOLLINGER") e
    `buy_enabled` (ex.: false) que NÃO devem chegar aos plugins:
      - a estratégia real vem de strategy_by_tf (variável `strategy` em
        check_and_trade → get_strategy_func), NÃO de params['strategy'];
      - buy_enabled não é lido em nenhum lugar do path live (core/+strategies/).
    Removemos as duas chaves do resultado para que nenhum plugin (presente ou
    futuro) leia params['strategy'] errado nem seja bloqueado por buy_enabled.
    """
    key = f"{symbol_root}_{tf}"
    by_tf = CONFIG.get("params_by_tf", {})
    base = CONFIG.get(symbol_root.lower(), {})
    # Tentar match case-insensitive
    if key in by_tf:
        merged = {**base, **by_tf[key]}
    else:
        key_lower = key.lower()
        if key_lower in by_tf:
            merged = {**base, **by_tf[key_lower]}
        else:
            merged = dict(base)  # cópia: nunca mutar CONFIG
    # Chaves legadas que conflitam com strategy_by_tf — remover sempre
    merged.pop("strategy", None)
    merged.pop("buy_enabled", None)
    return merged


def _get_params(symbol_root: str) -> dict:
    """Retorna os parâmetros otimizados para o símbolo."""
    return CONFIG.get(symbol_root.lower(), {})


def _reset_daily_counter():
    """Reseta contador diário se mudou o dia."""
    today = datetime.now().date()
    if state.current_day != today:
        state.current_day = today
        state.daily_trade_count = 0
        state.daily_trade_by_symbol = {}
        state.last_trade_time = {}
        state.consecutive_losses = {}
        log(f"[DAILY] Contador diário resetado para {today}")
        state.save()  # persistir reset diário


def _is_safe_time_window() -> bool:
    """Evita operar nos primeiros/últimos minutos da sessão.
    - warmup_minutes: primeiros X min após abertura (mercado definindo direção)
    - winddown_minutes: últimos X min antes do fechamento (risco de gap/ilha)
    Retorna True se está em janela segura.
    """
    now = datetime.now()
    current = now.hour * 60 + now.minute
    start = CONFIG["start_hour"] * 60 + CONFIG["start_minute"]
    end = CONFIG["close_hour"] * 60 + CONFIG["close_minute"]
    warmup = CONFIG.get("warmup_minutes", 15)
    winddown = CONFIG.get("winddown_minutes", 15)
    # Primeiros warmup minutos após abertura
    if start <= current <= start + warmup:
        return False
    # Últimos winddown minutos antes do fechamento
    if end - winddown <= current <= end:
        return False
    return True


# Wave 3.1 (2026-06-26): bloqueio de combinações (weekday, direction) perdedoras.
# Lista 100% CONFIG-DRIVEN via CONFIG["blocked_day_directions"] (vt_config.json).
# NÃO há mais DEFAULT hardcoded — a chave sempre existe no config (mesmo que []).
#
# Refactor Bruno 2026-06-30: "NÃO pode ser hardcoded. Tem que ser CONFIGURÁVEL via
# vt_config.json E o AGI deve poder ajustar DIARIAMENTE baseado em dados reais."
#
# Fail-open garantido: se CONFIG está corrompido/ausente/sem a chave,
# retorna False (NÃO bloqueia). Zero DEFAULT_BLOCKED_DAY_DIRECTIONS hardcoded.


# Wave 8.4 (2026-06-26): time_blocks - bloqueia combinações
# (symbol, hour_range) baseado em evidência DB.
# Achado do sub-agente DB (RELATORIO_OPORTUNIDADES_LUCRATIVAS.md):
#   - BITM26 STRONG_TREND 09h-11h: -R$3.234 em 12 trades
#   - WINQ26 VWAP (todos TFs): 52 SL_SERVIDOR, 23.1% WR
#   - Total: corta ~R$2.8k/mês
def _is_blocked_time(symbol: str, hour: int, tf: str = "M5") -> bool:
    """Retorna True se (symbol, hour) está em time_blocks.

    Wave 8.4 (2026-06-26): corta loss direto via bloqueio horário.

    Schema time_blocks (em CONFIG['time_blocks']):
      {
        "BITM26": [{"start": 9, "end": 11, "reason": "..."}],
        "WINQ26": [{"start": 0, "end": 24, "strategy": "VWAP"}],
      }

    Fail-open: se CONFIG não tem time_blocks, retorna False.
    """
    try:
        time_blocks = CONFIG.get("time_blocks", {}) or {}
    except Exception:
        return False
    if not time_blocks:
        return False
    # Extrai root do symbol (ex: 'WINQ26' → 'WIN', 'WIN$26' → 'WIN')
    symbol_root = symbol[:3] if len(symbol) >= 3 else symbol

    # BUG FIX 2026-06-26: a lógica antiga comparava sb (ex: 'WINQ26') com
    # contract (ex: 'WINQ26') — match sempre True. Resultado: WINQ26 VWAP
    # block (9h-17h) BLOQUEAVA WIN INTEIRO, não só WIN VWAP.
    # FIX: itera pelos time_blocks, compara por ROOT (sb[:3]).
    for sb, blocks_list in time_blocks.items():
        if not isinstance(blocks_list, list):
            continue
        sb_root = sb[:3] if len(sb) >= 3 else sb
        if symbol_root != sb_root and not symbol.startswith(sb_root):
            continue
        for block in blocks_list:
            if not isinstance(block, dict):
                continue
            start = block.get("start", 0)
            end = block.get("end", 24)
            # FIX: se block tem 'strategy', só bloqueia se a estratégia ativa
            # do par é a bloqueada. Se não tem, bloqueia TUDO do symbol.
            block_strategy = block.get("strategy")
            if block_strategy is not None:
                active_strategy = CONFIG.get("strategy_by_tf", {}).get(
                    f"{symbol_root}_{tf}", "?"
                )
                if active_strategy != block_strategy:
                    continue
            if start <= hour <= end:
                return True
    return False


def _is_blocked_day_direction(direction: str) -> bool:
    """Retorna True se (weekday_atual, direction) está em CONFIG["blocked_day_directions"].

    Wave 3.1 (2026-06-26): padrão claro de "dia da semana ruim pra direção X"
    na análise DB 30d. Filtro simples, retorno alto, baixo risco de regressão.

    Refactor Bruno 2026-06-30: agora 100% CONFIG-DRIVEN. A chave
    CONFIG["blocked_day_directions"] SEMPRE existe em vt_config.json (mesmo
    como [] vazia). AGI ajusta diariamente esta lista via
    optimization/agi_tuning_17h.py::_update_blocked_day_directions().

    Schema esperado em CONFIG["blocked_day_directions"]:
        [(0, "BUY"), (1, "SELL"), (2, "BUY"), ...]
        onde 0=Seg, 1=Ter, 2=Qua, 3=Qui, 4=Sex, 5=Sáb, 6=Dom

    Fail-open total: se CONFIG está corrompido/ausente/sem a chave
    /não-iterável, retorna False (NÃO bloqueia, prefere falsos negativos).
    Log explícito quando bloqueia para auditoria.

    IMPORTANTE: NÃO há fallback hardcoded. Se a chave sumir do config,
    o autotrader PERMITE tudo (fail-open) — o AGI tem 24h pra re-popular.
    """
    # ── Fail-open total: qualquer exceção → retorna False ──
    try:
        blocked_raw = CONFIG.get("blocked_day_directions", None)
    except Exception:
        log("⚠️ _is_blocked_day_direction: CONFIG.get falhou — fail-open")
        return False

    # Sem chave / chave vazia / None → fail-open
    if not blocked_raw:
        return False

    # Defesa contra config corrompido (não-list, não-tuple de tuples)
    if not isinstance(blocked_raw, (list, tuple)):
        log(
            f"⚠️ _is_blocked_day_direction: blocked_day_directions não é list/tuple "
            f"(tipo={type(blocked_raw).__name__}) — fail-open"
        )
        return False

    # Normaliza para set de tuples (weekday:int, direction:str)
    try:
        blocked = set()
        for entry in blocked_raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                wd, dr = entry
                blocked.add((int(wd), str(dr).upper()))
    except Exception as e:
        log(
            f"⚠️ _is_blocked_day_direction: erro ao normalizar "
            f"blocked_day_directions={blocked_raw!r} ({e}) — fail-open"
        )
        return False

    try:
        now = datetime.now()
        weekday = now.weekday()
    except Exception:
        return False

    is_blocked = (weekday, direction.upper()) in blocked
    if is_blocked:
        log(
            f"🚫 day-direction BLOCK: weekday={weekday} ({now.strftime('%A')}) "
            f"direction={direction.upper()} (origem: CONFIG['blocked_day_directions'], "
            f"{len(blocked)} combinações ativas)"
        )
    return is_blocked



def _check_cooldown(symbol: str, params: dict, tf: str = "", direction: str = "") -> bool:
    """Retorna True se pode operar (cooldown ok).

    Cooldown por (symbol, tf, direction) para evitar reversões rápidas.

    Ordem de resolução do cooldown_seconds (mais específica → menos):
      1. params do caller (vindo de _get_params_for_tf: por symbol+TF)
      2. CONFIG[symbol] (base do ativo)
      3. CONFIG["win"] (fallback final)
      4. 300s (default hardcoded)
    """
    now = datetime.now()

    # Resolve cooldown_seconds em ordem de prioridade (FIX: respeita params_by_tf)
    cd = None
    if params and isinstance(params, dict):
        cd = params.get("cooldown_seconds")
    if cd is None:
        _root = symbol[:3].lower() if len(symbol) >= 3 else symbol.lower()
        _sym_params = CONFIG.get(_root, {})
        cd = _sym_params.get("cooldown_seconds") if isinstance(_sym_params, dict) else None
    if cd is None:
        cd = CONFIG.get("win", {}).get("cooldown_seconds") if isinstance(CONFIG.get("win"), dict) else None
    if cd is None or not isinstance(cd, (int, float)) or cd <= 0:
        cd = 300  # default final

    if tf and direction:
        key = f"{symbol}_{tf}_{direction}"
        last_time = state.last_trade_time.get(key)
        if last_time:
            elapsed = (now - last_time).total_seconds()
            if elapsed < cd:
                return False
    # Também checa cooldown por symbol (proteção geral)
    last_time_sym = state.last_trade_time.get(symbol)
    if last_time_sym:
        elapsed = (now - last_time_sym).total_seconds()
        if elapsed < cd * 0.6:  # symbol-level cooldown pode ser 60% do per-direction
            return False
    return True


def _symbol_root(symbol: str) -> str:
    """Extrai o root do contrato: 'WDON26' → 'WDO', 'DOLM26' → 'DOL'."""
    for r in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
        if r in symbol:
            return r
    return "WIN"


def _resolve_volume(symbol: str, tf: str) -> float:
    """
    Volume (qtd contratos) por (symbol, tf) — hierarquia documentada e única:

        CONFIG["volume_by_tf"][f"{symbol_root}_{tf}"]   (mais específico)
          ↓ (se ausente nesse TF)
        CONFIG["volume_by_symbol"][symbol_root]          (nível do ativo)
          ↓ (se ausente no ativo)
        CONFIG["volume"]                                 (raiz do config)
          ↓ (se ausente/corrompido)
        1                                                (safety default)

    Wave Per-TF (Bruno 2026-07-07): libera que cada TF tenha volume proprio
    (ex.: WDO_M5=2 contratos, WDO_M15=1, WDO_M30=1, WDO_H1=1 — estratégia
    agressiva no curto + conservadora no longo). Fica CONFIG-driven para o
    AGI poder tunar granularmente.

    Args:
        symbol: contrato MT5 resolvido (ex: "WDON26").
        tf: timeframe ("M5", "M15", "M30", "H1").

    Returns:
        float >= 1.0 (número de contratos). NUNCA retorna 0 — se config
        explicitamente quiser zerar um par, isso deve ser feito via
        disabled_timeframes (não aqui).

    Fail-safe: se config corrompido (string, None, negativo), usa fallback
    conservador (1.0) e loga WARN.
    """
    # Extrai root (WIN/WDO/BIT/WSP/etc.) do symbol resolvido
    _root = ""
    for r in ["WIN", "WDO", "BIT", "DOL", "IND", "WSP"]:
        if r in symbol:
            _root = r
            break

    # 1. volume_by_tf (mais específico)
    vol_by_tf = CONFIG.get("volume_by_tf") or {}
    if isinstance(vol_by_tf, dict):
        tf_key = f"{_root}_{tf}"
        try:
            v = vol_by_tf.get(tf_key)
            if isinstance(v, (int, float)) and v >= 1.0:
                return float(v)
            elif v is not None:
                log(f"[VOL] {tf_key}: valor invalido {v!r} em volume_by_tf — usando fallback")
        except Exception as e:
            log(f"[VOL] {tf_key}: erro ao ler volume_by_tf ({type(e).__name__}: {e}) — fallback")

    # 2. volume_by_symbol (nivel do ativo)
    vol_by_sym = CONFIG.get("volume_by_symbol") or {}
    if isinstance(vol_by_sym, dict):
        try:
            v = vol_by_sym.get(_root)
            if isinstance(v, (int, float)) and v >= 1.0:
                return float(v)
        except Exception:
            pass

    # 3. volume (raiz)
    try:
        v = CONFIG.get("volume")
        if isinstance(v, (int, float)) and v >= 1.0:
            return float(v)
    except Exception:
        pass

    # 4. Safety default
    return 1.0


def _resolve_max_daily_trades(params: dict, symbol_root: str) -> int:
    """
    Limite diário de trades POR SÍMBOLO — hierarquia documentada e única:

        params_by_tf[SYMBOL_TF].max_daily_trades    (mais específico)
          ↓ (se ausente nesse TF)
        CONFIG[symbol_root].max_daily_trades         (nível do ativo)
          ↓ (se ausente no ativo)
        CONFIG["max_daily_trades"]                   (raiz do config)
          ↓ (se ausente)
        15                                            (safety default)

    `params` chega já mesclado (base ∪ params_by_tf) via _get_params_for_tf,
    então o nível TF e o nível do ativo já estão resolvidos nele; este helper
    apenas completa a cadeia com a raiz + default quando ambos faltam. Isto
    resolve a redundância: antes o fallback era sempre 15 (ignorando o limite
    do ativo e da raiz), agora a hierarquia é explícita e respeitada.

    NOTA — esclarece confusão comum sobre o config (ver _doc_max_daily_trades):
    - risk_management.daily_limits.max_daily_trades_by_symbol NÃO é consultado
      aqui. Essa chave só é lida no log de startup (~L1907); como não existe no
      config, sempre cai no fallback CONFIG[symbol].max_daily_trades.
    - O teto GLOBAL (min(global_max_daily_trades, 50)) é aplicado À PARTE em
      _check_max_trades via _global_max_daily_trades(), não por esta função.
      Mesmo com global_max_daily_trades=999 no config, o total real é 50/dia.
    - Exemplo WDO hoje: WDO_M5→2, WDO_M15→4, WDO_M30→4, WDO_H1→2 (todos em
      params_by_tf), então wdo.max_daily_trades=4 e max_daily_trades=999 da
      raiz nunca são alcançados para WDO.
    """
    if params.get("max_daily_trades") is not None:
        return int(params["max_daily_trades"])
    sym = CONFIG.get(symbol_root.lower(), {})
    if sym.get("max_daily_trades") is not None:
        return int(sym["max_daily_trades"])
    if CONFIG.get("max_daily_trades") is not None:
        return int(CONFIG["max_daily_trades"])
    return 15


def _global_max_daily_trades() -> int:
    """Teto global de trades/dia. Respeita global_max_daily_trades do config,
    mas nunca ultrapassa o backstop de segurança 50."""
    return min(int(CONFIG.get("global_max_daily_trades", 50)), 50)


def _check_max_trades(params: dict, symbol: str = "") -> bool:
    """Retorna True se pode operar (limite não atingido). Conta por símbolo.

    Hierarquia do limite por símbolo: ver _resolve_max_daily_trades()
    (params_by_tf > ativo > raiz > default). Teto global: ver
    _global_max_daily_trades() (global_max_daily_trades, backstop 50).
    """
    # ── KILL SWITCH: Max daily loss ──
    # Wave N+1 B5 fix (Bruno 2026-07-17): usar broker-truth (MT5 deals) em vez
    # de state.daily_pnl que vem do DB SQLite envenenado por ORPHANs. Confia
    # no que o MT5 diz, não no que o DB pensa.
    max_daily_loss = CONFIG.get("max_daily_loss", -500)
    pnl_broker_truth = float(_truth.get_daily_pnl())
    if pnl_broker_truth <= max_daily_loss:
        log(f"🛑 KILL SWITCH: PnL diário R$ {pnl_broker_truth:.2f} ≤ limite R$ {max_daily_loss:.2f} — TRAVADO (broker-truth)")
        # Wave N+block_notify: notifica 1x/dia (cooldown 1440min). Re-fires
        # automaticamente no próximo pregão. Bot NÃO abre novas posições
        # enquanto PnL ≤ max_daily_loss.
        notify_block_activated(
            CAT_MAX_DAILY_LOSS,
            reason=(f"PnL broker-truth R$ {pnl_broker_truth:.2f} ≤ limite R$ {max_daily_loss:.2f} — "
                    f"KILL SWITCH travado, sem novas entradas até o próximo dia"),
            severity="critical", cooldown_min=1440,
        )
        return False

    # ── KILL SWITCH: disabled_ativos (AGI pode desativar ativos que perdem) ──
    disabled = CONFIG.get("disabled_symbols", [])
    if symbol in disabled:
        return False

    # Teto global (segurança)
    if state.daily_trade_count >= _global_max_daily_trades():
        return False
    # Limite por símbolo (hierarquia tf > ativo > raiz > default)
    sym_count = state.daily_trade_by_symbol.get(symbol, 0)
    max_per_sym = _resolve_max_daily_trades(params, _symbol_root(symbol))
    if sym_count >= max_per_sym:
        return False
    return True


def _check_consecutive_losses(symbol: str) -> bool:
    """Retorna True se pode operar (sem sequência de derrotas)."""
    # Check halt_until first
    halt_time = state.halt_until.get(symbol)
    if halt_time and datetime.now() < halt_time:
        remaining = (halt_time - datetime.now()).total_seconds() / 60
        log(f"[BLOQUEADO] {symbol} — HALT ativo, {remaining:.0f}min restantes")
        return False

    # Se 3+ perdas consecutivas no símbolo, pausar
    sym_losses = state.consecutive_losses.get(symbol, 0)
    if sym_losses >= state.max_consecutive_losses:
        from datetime import timedelta
        state.halt_until[symbol] = datetime.now() + timedelta(hours=1)
        log(f"[HALT] {symbol}: {sym_losses} perdas consecutivas! Pausado 1h")
        return False
    if sym_losses > 0:
        log(f"[DEBUG] {symbol} — {sym_losses}/{state.max_consecutive_losses} perdas consecutivas")
    return True


# ─── Wave N+4B / N+5A — wire-in do módulo position_manager ──────────
# Refator 3.1 (2026-07-08): helpers de loss-cooldown e day-trade flatten
# migrados para core/vt_position_manager.py. Wrappers abaixo mantêm a
# assinatura antiga (state e CONFIG capturados por closure) para preservar
# a chamada interna nos callsites existentes.
from core.vt_position_manager import (  # noqa: E402,F401
    check_loss_cooldown_active as _pm_check_cooldown,
    bump_loss_cooldown as _pm_bump_cooldown,
    reset_loss_cooldown as _pm_reset_cooldown,
    day_trade_flatten_window as _pm_dt_flatten,
)
from core.vt_position_manager import _symbol_root as _symbol_root_for_day_trade  # noqa: E402,F401


def _is_loss_cooldown_active(symbol, direction):
    """Wrapper que fecha sobre autotrader.state + CONFIG para preservar
    assinatura antiga nos callsites."""
    return _pm_check_cooldown(symbol, direction, state=state, config=CONFIG)


def _bump_loss_cooldown_counter(symbol, direction):
    _pm_bump_cooldown(symbol, direction, state=state)


def _reset_loss_cooldown_counter(symbol, direction):
    _pm_reset_cooldown(symbol, direction, state=state)


def _is_day_trade_flatten_window(symbol, tf, pos_minutes,
                                 buffer_minutes=15, now=None):
    return _pm_dt_flatten(
        symbol, tf, pos_minutes,
        config=CONFIG, buffer_minutes=buffer_minutes, now=now,
    )


# Wave Per-TF (Bruno 2026-07-07): cross-TF cooldown defensivo.
#
# Modelo per-TF libera até 4 posições simultâneas no mesmo symbol (M5/M15/M30/H1).
# Se 2+ TFs estão perdendo ao mesmo tempo, o sinal macro é adverso → novos TFs
# entram em cooldown 30min para evitar martelar contra a tendência.
#
# Parametros (config-driven, defaults razoáveis):
#   threshold (default 2): mínimo de posições perdendo do mesmo symbol_root
#                          para acionar o cooldown.
#   cooldown_min (default 30): minutos de bloqueio após acionamento.
#
# Comportamento:
#   - Conta posições abertas no MT5 com magic+symbol_root match e profit<0.
#   - Se losing_count >= threshold E cooldown NÃO ativo: ATIVA cooldown, retorna False.
#   - Se losing_count >= threshold E cooldown ATIVO: retorna False (continua bloqueado).
#   - Se losing_count < threshold: limpa cooldown (se houver) e retorna True.
#   - FAIL-SAFE: MT5 indisponível → retorna True (não bloqueia por defeito de leitura).
#
# Reset: state.cross_tf_cooldown eh limpo automaticamente no proximo tick em que
# losing_count < threshold (liberação automática). Não persiste em disco (Fase 3
# eliminou save/load); restart mid-cooldown reseta a proteção.
def _check_cross_tf_cooldown(symbol_root: str, threshold: int = 2, cooldown_min: int = 30) -> bool:
    """Bloqueia novos TFs do symbol_root se >= threshold posições perdendo."""
    from datetime import timedelta

    # Conta posições perdendo do symbol_root via truth layer (cache 2s)
    losing_count = 0
    try:
        _open = _truth.get_open_positions(magic_filter=_truth.MAGIC_VIBETRADING)
        for p in _open:
            try:
                if _symbol_root(p.symbol) == symbol_root and float(p.profit or 0) < 0:
                    losing_count += 1
            except Exception:
                continue
    except Exception as e:
        log(f"[CROSS-TF] {symbol_root}: leitura MT5 falhou ({type(e).__name__}: {e}) — FAIL-SAFE: permite")
        return True

    cooldown_key = symbol_root
    cooldown_until = state.cross_tf_cooldown.get(cooldown_key)
    now = datetime.now()

    if losing_count < threshold:
        # Limpa cooldown se estiver setado (situação melhorou)
        if cooldown_until is not None:
            log(f"[CROSS-TF] {symbol_root}: {losing_count} pos perdendo (< {threshold}), cooldown removido")
            state.cross_tf_cooldown.pop(cooldown_key, None)
        return True

    # losing_count >= threshold
    if cooldown_until is None or now >= cooldown_until:
        # Ativa cooldown agora
        state.cross_tf_cooldown[cooldown_key] = now + timedelta(minutes=cooldown_min)
        log(f"[CROSS-TF COOLDOWN] {symbol_root}: {losing_count} pos perdendo — cooldown {cooldown_min}min ATIVADO")
        return False

    # Cooldown ativo
    remaining = (cooldown_until - now).total_seconds() / 60
    log(f"[CROSS-TF BLOQUEADO] {symbol_root}: {losing_count} pos perdendo, cooldown ativo {remaining:.0f}min restantes")
    return False


def _maybe_log_blocked_signal(state, symbol: str, tf: str, strategy: str, bar_ts) -> bool:
    """Wave N+1 (2026-07-08): heurística "setup latente vs sem setup".

    Se estratégia retornou None AGORA mas a MESMA estratégia no MESMO (symbol,
    tf) retornou signal há <LATENT_LOOKBACK_MINUTES minutos, é provável que
    houve filter-reject (volatility, MTF low score, day-dir blocked, etc.).
    Nesse caso, loga em signal_blocked_log para alimentar N+3B (edge decay) e
    N+5B (loser replay).

    Returns True se logou, False se foi "sem setup" genuíno.

    Sem raise — falhas de I/O nunca interrompem o tick loop. O log fica em
    fila no signal_journal (batch flush), consistente com o resto do módulo.
    """
    try:
        from core import vt_signal_journal
    except ImportError:
        return False
    try:
        last_signal_ts = state.recent_signal_ts.get((symbol, tf, strategy))
        if not last_signal_ts:
            return False
        # last_signal_ts pode ser datetime (do state) — compara com datetime.now()
        from datetime import datetime as _dt
        now = _dt.now()
        if isinstance(last_signal_ts, str):
            try:
                last_signal_ts = _dt.fromisoformat(last_signal_ts)
            except ValueError:
                return False
        delta_min = (now - last_signal_ts).total_seconds() / 60.0
        if delta_min > vt_signal_journal.LATENT_LOOKBACK_MINUTES:
            return False
        # Heurística acionada → loga contrafactual.
        vt_signal_journal.log_blocked_signal(
            symbol=symbol,
            tf=tf,
            strategy=strategy,
            direction=None,         # filtro barrou ANTES de decidir direcao
            block_reason="STRATEGY_RETURNED_NONE_AFTER_SIGNAL",
            sl_pts=None,
            atr_pts=None,
            regime=None,
        )
        return True
    except Exception as exc:  # defense-in-depth: nunca quebra tick loop
        log(f"[N+1] signal_journal falhou silenciosamente: {exc!r}")
        return False


def check_and_trade():
    from monitoring.vt_analyst import fetch_snapshot, save_snapshot, detect_anomalies, log_anomaly, notify as analyst_notify

    _reset_daily_counter()  # ← sempre resetar no início do ciclo

    # Bruno 30/06: contador de bloqueios pra enviar resumo agregado a cada hora via Telegram.
    # Usa arquivo /tmp pra compartilhar estado entre chamadas (sem globals complicados).
    _block_counter_file = "/tmp/vt_block_counter.json"
    try:
        with open(_block_counter_file) as _f:
            _bc = json.load(_f)
    except (FileNotFoundError, json.JSONDecodeError):
        _bc = {"day_dir": 0, "time": 0, "last_report": datetime.now().isoformat(), "date": datetime.now().strftime("%Y-%m-%d")}
    # Reset diário
    if _bc.get("date") != datetime.now().strftime("%Y-%m-%d"):
        _bc = {"day_dir": 0, "time": 0, "last_report": datetime.now().isoformat(), "date": datetime.now().strftime("%Y-%m-%d")}

    # ── KILL SWITCH centralizado (vt_config.json) ──
    if CONFIG.get("halt_trading", False):
        log("🛑 halt_trading=true no config — PARADO")
        # Wave N+block_notify: notifica 1x/dia (cooldown 1440min) — re-fires
        # automaticamente no próximo pregão se ainda travado.
        notify_block_activated(
            CAT_HALT_TRADING, reason="halt_trading=true no vt_config.json — bot TOTALMENTE parado",
            severity="critical", cooldown_min=1440,
        )
        return
    if CONFIG.get("halt_new_trades", False):
        # Permite gerenciar posições abertas mas não abre novas
        if not state.positions:
            log("ℹ️ halt_new_trades=true e sem posições — aguardando")
            # Wave N+block_notify: notifica 1x/dia — sem novas entradas.
            notify_block_activated(
                CAT_HALT_NEW_TRADES,
                reason="halt_new_trades=true e sem posições abertas — sem novas entradas",
                severity="warning", cooldown_min=1440,
            )
            return

    # Profit Lock (Wave 880.H — Bruno 2026-07-20): se travou hoje, não abre
    # novas até o dia seguinte. Posições abertas podem ainda ser gerenciadas
    # pelo manage_position() abaixo — o lock só bloqueia novas entradas.
    # Quando o lock arma, ele JÁ FECHA tudo (close_all_and_report), então
    # normalmente aqui não há posições a gerenciar. Mas em race conditions
    # (posição aberta entre o tick de arm e o tick seguinte), ainda deixa
    # gerenciar.
    try:
        from core.vt_profit_lock import is_locked as _pl_is_locked
        _pl_locked, _pl_state = _pl_is_locked()
        if _pl_locked:
            log(f"🔒 PROFIT LOCK ativo desde {(_pl_state.get('armed_at','?')[:16])} "
                f"(target R$ {_pl_state.get('target',0):.2f}, "
                f"PnL no arm R$ {_pl_state.get('armed_pnl',0):.2f}, "
                f"closed_n={_pl_state.get('closed_n',0)}) — novas entradas bloqueadas")
            return
    except Exception as _e_pl:
        log(f"[PROFIT-LOCK] gate falhou (não-crash): {_e_pl}")

    # Trailing Profit Lock gate (Wave 1111 — Bruno 2026-08-11): se o trailing
    # engajou hoje (PnL >= 50% do target), NÃO abre novas entradas — o ratchet
    # protege o lucro acumulado e uma entrada nova é vetor de risco que pode
    # derrubar o PnL abaixo do floor (BREACH fecha TUDO). Posições abertas
    # seguem gerenciadas por manage_position() abaixo (mesma semântica do
    # profit lock full: só bloqueia ENTRADAS).
    try:
        from core.vt_trailing_profit_lock import is_active as _tpl_is_active
        if _tpl_is_active():
            from core.vt_trailing_profit_lock import get_trailing_state as _tpl_state
            _tpl_st = _tpl_state()
            log(f"🔒 TRAILING PROFIT LOCK ativo desde {(_tpl_st.get('activated_at','?')[:16])} "
                f"(pico R$ {_tpl_st.get('peak',0):.2f}, floor R$ {_tpl_st.get('floor',0):.2f}) "
                f"— novas entradas bloqueadas (proteção de lucro)")
            return
    except Exception as _e_tpl_gate:
        log(f"[TRAILING-PL] gate falhou (não-crash): {_e_tpl_gate}")

    # Safety: avoid first/last 15 min of session
    if not _is_safe_time_window():
        return

    # ── Filtrar símbolos desabilitados (kill switch por símbolo) ──
    disabled_symbols = CONFIG.get("disabled_symbols", [])
    active_symbols = [s for s in CONFIG["symbols"] if s not in disabled_symbols]
    if disabled_symbols:
        log(f"🚫 Símbolos desabilitados: {disabled_symbols} (ativos: {active_symbols})")
        # Wave N+block_notify: notifica 1x/hora. dedup key inclui os
        # símbolos afetados na reason para que mudar a lista dispare nova msg
        # após o cooldown (mudança visível pro operador).
        notify_block_activated(
            CAT_DISABLED_SYMBOLS,
            reason=f"disabled_symbols={disabled_symbols} (ativos: {active_symbols})",
            severity="warning", cooldown_min=3600,
        )

    for symbol_root in active_symbols:
        # Wave Per-TF (Bruno 2026-07-07): cooldown cross-TF. Se symbol_root já
        # tem 2+ posições perdendo, bloqueia novos TFs do mesmo symbol por 30min.
        # Avalia 1x por symbol_root antes de iterar TFs (barato: 1 truth call).
        if not _check_cross_tf_cooldown(symbol_root):
            continue

        # Se o config tem symbol resolvido (ex: "WDO": "WDON26"), usa direto
        # Caso contrário, resolve e cacheia no state
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Verificar se config tem símbolos resolvidos
        resolved_map = CONFIG.get("resolved_symbols", {})
        if resolved_map.get(symbol_root):
            symbol = resolved_map[symbol_root]
        else:
            log(f"[ERROR] Símbolo {symbol_root} não encontrado em resolved_symbols. Verifique vt_config.json.")
            continue

        if not symbol:
            log(f"[WARN] Não resolveu símbolo {symbol_root}")
            continue

        # Fail-closed: rejeita contratos com sufixo de rollover (N99/N00/...).
        # Esses contratos NÃO têm liquidez real e geram fills fantasma
        # (-R$256/30d histórico, ver análise DB 2026-06-25). Ver doc em
        # core.vt_calendar.is_rollover_contract.
        if is_rollover_contract(symbol):
            log(f"[ROLLOVER] Rejeitado {symbol_root} → {symbol} (sufixo rollover automático)")
            continue

        # Coletar snapshot + anomalias
        snap = fetch_snapshot(symbol, CONFIG["timeframes"][0])
        if "error" not in snap:
            save_snapshot(snap)
            anomalies = detect_anomalies(snap)
            for a in anomalies:
                log_anomaly(symbol, a["type"], a)
                analyst_notify(a["type"], symbol, a["msg"], a.get("tf", ""))

        # Strategy/params per TF (with fallback to symbol-level)
        _default_strategy = _get_strategy(symbol_root)
        _default_params = _get_params(symbol_root)
        # Timeframes por símbolo (override do global)
        timeframes = CONFIG.get("timeframes_by_symbol", {}).get(symbol_root, CONFIG["timeframes"])

        for tf in timeframes:
            # ── KILL SWITCH: TF desativado pelo AGI ──
            # Se TF está desativado MAS existe posição aberta, ainda gerenciar
            # (não pode abandonar trade em curso só porque o AGI desativou a estratégia).
            # Se TF está desativado E sem posição aberta, pula (não abre nova).
            disabled_tfs = CONFIG.get("disabled_timeframes", [])
            tf_disabled = f"{symbol_root}_{tf}" in disabled_tfs
            if tf_disabled:
                # Verificar se há posição aberta (precisa gerenciar mesmo com TF off)
                existing_pos = state.positions.get(f"{symbol}_{tf}")
                if not existing_pos:
                    # Wave N+block_notify: 1x/hora por (symbol_root, tf).
                    notify_block_activated(
                        CAT_DISABLED_TF,
                        symbol=symbol_root, tf=tf,
                        reason=f"TF {symbol_root}_{tf} em disabled_timeframes — sem nova entrada",
                        severity="warning", cooldown_min=3600,
                    )
                    continue  # sem posição → pula
                log(f"[ORPHAN_RECOVERY] {symbol} {tf}: TF desativado mas posição aberta, gerenciando")
                # Continua pra gerenciar — fall through

            strategy = _get_strategy_for_tf(symbol_root, tf)
            params = _get_params_for_tf(symbol_root, tf)
            bars = fetch_bars(symbol, tf, CONFIG["bars_count"])
            if not bars or len(bars) < CONFIG["bars_count"]:
                continue

            atr = calculate_atr(bars, params.get("atr_period", 14))
            if atr == 0:
                continue

            last_close = bars[1]["close"]
            last_bar_ts = bars[1].get("time")

            pos = state.positions.get(f"{symbol}_{tf}")
            if pos:
                manage_position(symbol, tf, pos, atr, strategy, params)
            else:
                # Pause file (Bruno 2026-07-20): não abre novas entradas,
                # mas posições abertas em outros (symbol, tf) seguem sendo
                # gerenciadas acima. API: touch/rm data/autotrader.paused.
                # Corrige bug latente do halt_new_trades (que só bloqueava
                # quando not state.positions — ver forense 20/07).
                if _is_paused():
                    continue
                # ===== SAFETY CHECKS (cooldown, max, consecutive losses) =====
                # Cooldown precisa de tf e direction — mas ainda não sabemos a direction
                # do sinal. Pré-checa por symbol-level apenas aqui; por direction
                # é re-checado dentro da strategy_func antes de executar.
                if not _check_cooldown(symbol, params, tf=tf):
                    continue
                if not _check_max_trades(params, symbol):
                    log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
                    continue
                if not _check_consecutive_losses(symbol):
                    continue

                # Dispatch dinâmico de estratégia
                strategy_func = get_strategy_func(strategy)
                if strategy_func:
                    result = strategy_func(symbol, tf, last_close, atr,
                                           bar_ts=last_bar_ts, bars=bars,
                                           params=params, utils=_strategy_utils)
                    # Wave N+1 (2026-07-08): heurística "setup latente vs
                    # sem setup". Estratégia retornou None — pode ser filter-
                    # reject (mesma estratégia gerou signal recentemente) ou
                    # ausencia genuína de setup. Logga contrafactual só no
                    # primeiro caso (alimenta Wave N+3B + N+5B).
                    if not result:
                        _maybe_log_blocked_signal(
                            state, symbol, tf, strategy, last_bar_ts
                        )
                    if result:
                        # Wave N+1 (2026-07-08): registra ts do signal para
                        # heurística de setup-latente usar depois.
                        state.recent_signal_ts[(symbol, tf, strategy)] = datetime.now()
                        info = result.get("info", {})
                        # Pitfall #2 fix: pop TODOS os campos que conflitam com
                        # _execute_entry params (strategy, atr, sl_pts, direction, price, symbol, tf, bar_ts).
                        # Sem isso, spread **info causa "got multiple values for argument X".
                        for k in ("strategy", "atr", "sl_pts", "direction", "price", "symbol", "tf", "bar_ts"):
                            info.pop(k, None)
                        # Wave N+4A (2026-07-08): consolida os 2 gates antigos.
                        # Antes: _is_blocked_day_direction + _is_blocked_time.
                        # Agora: calendar.aggregate_blackout() une trading_day +
                        # day_direction + time_blocks + events/news num gate único.
                        from core.vt_calendar import aggregate_blackout
                        _blocked, _reason = aggregate_blackout(
                            symbol, result["direction"],
                            config=CONFIG,
                            ts=last_bar_ts or datetime.now(),
                        )
                        if _blocked:
                            log(f"[BLACKOUT] {symbol} {result['direction']} → {_reason}")
                            # Mantém o counter pra relatório do copilot
                            # (compatibilidade com a estrutura anterior).
                            if _reason.startswith("day_dir"):
                                _bc["day_dir"] += 1
                            elif _reason.startswith("time_block"):
                                _bc["time"] += 1
                            # Wave N+block_notify: dedup 60min por (symbol, tf).
                            # O counter horario continua existindo no copilot
                            # report — este notify é apenas o "primeiro hit"
                            # visivel em tempo real.
                            notify_block_activated(
                                CAT_AGGREGATE_BLACKOUT,
                                symbol=symbol, tf=tf,
                                reason=f"{result['direction']} bloqueado: {_reason}",
                                severity="warning", cooldown_min=60,
                            )
                            continue
                        # Wave N+4B (2026-07-08): cooldown por loss consecutiva
                        # per-(symbol, direction). Corta cauda de "revenge-trade".
                        if _is_loss_cooldown_active(symbol, result["direction"]):
                            log(f"[LOSS_COOLDOWN] {symbol} {result['direction']} bloqueado")
                            continue
                        # DEFESAS: plugins não chamam _defenses_ok — validar aqui
                        if not _defenses_ok(symbol, tf, result["direction"], last_bar_ts):
                            continue
                        _execute_entry(symbol, tf, result["direction"],
                                       last_close, result["sl_pts"], atr,
                                       last_bar_ts, strategy=strategy,
                                       **info)
                else:
                    log(f"[ERRO] Estratégia '{strategy}' não encontrada")

    # Bruno 30/06: salvar contadores e notificar resumo agregado a cada hora
    try:
        _bc["date"] = datetime.now().strftime("%Y-%m-%d")
        with open(_block_counter_file, "w") as _f:
            json.dump(_bc, _f)
        # Notificar via Telegram a cada 60min
        _now = datetime.now()
        if (_now - datetime.fromisoformat(_bc.get("last_report", _now.isoformat()))).total_seconds() >= 3600:
            if _bc["day_dir"] > 0 or _bc["time"] > 0:
                notify_telegram(
                    f"📊 *Resumo de Bloqueios*\n"
                    f"Período: {_bc.get('last_report', '?')[:16]} → {_now.strftime('%H:%M')}\n"
                    f"🚫 Day-direction blocks: {_bc['day_dir']}\n"
                    f"⏰ Time blocks: {_bc['time']}\n"
                    f"Total filtrado: {_bc['day_dir'] + _bc['time']}\n"
                )
                _bc["day_dir"] = 0
                _bc["time"] = 0
            _bc["last_report"] = _now.isoformat()
            with open(_block_counter_file, "w") as _f:
                json.dump(_bc, _f)
    except Exception as _be:
        log(f"[BLOCK-COUNTER ERR] {_be}")


def check_entry_vwap(symbol: str, tf: str, price: float,
                     atr: float, bar_ts=None, bars=None, params=None):
    """Entrada via VWAP (para WDO — mercado trending)."""
    if params is None:
        params = CONFIG.get("win", {})
    _reset_daily_counter()

    if not _check_cooldown(symbol, params, tf=tf):
        return

    if not _check_max_trades(params, symbol):
        log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
        return

    if not _check_consecutive_losses(symbol):
        return

    # Market regime
    regime = "UNKNOWN"
    ema_slow_val_cfg = params.get("ema_slow", 21)
    if bars and len(bars) >= ema_slow_val_cfg + 5:
        regime = get_market_regime(bars, params)
        if regime == "CHOPPY":
            return  # silencioso — WDO em choppy não opera

    # VWAP
    vwap = calculate_vwap(bars, params.get("vwap_period", 20))
    if vwap == 0:
        return

    # Trend direction
    ema_fast = ema_slow_val = 0
    if bars and len(bars) >= ema_slow_val_cfg + 5:
        ema_fast = calculate_ema(bars, params.get("ema_fast", 9))
        ema_slow_val = calculate_ema(bars, ema_slow_val_cfg)

    # Threshold adaptativo
    atr_pct = (atr / price) if price > 0 else 0
    if atr_pct < 0.0015:
        buy_mult = 1.0005
        sell_mult = 0.9995
    elif atr_pct < 0.003:
        buy_mult = 1.0015
        sell_mult = 0.9985
    else:
        buy_mult = params.get("vwap_buy_threshold", 1.003)
        sell_mult = params.get("vwap_sell_threshold", 0.997)

    buy_thresh = vwap * buy_mult
    sell_thresh = vwap * sell_mult

    direction = None
    if price > buy_thresh:
        direction = "BUY"
    elif price < sell_thresh:
        direction = "SELL"

    if not direction:
        return

    # Trend filter
    if ema_fast > 0 and ema_slow_val > 0:
        if direction == "BUY" and ema_fast < ema_slow_val:
            return
        if direction == "SELL" and ema_fast > ema_slow_val:
            return

    # RSI filter
    rsi = 50
    rsi_period = params.get("rsi_period", 14)
    if bars and len(bars) >= rsi_period + 2:
        rsi = calculate_rsi(bars, rsi_period)
        if direction == "BUY" and rsi > params.get("rsi_overbought", 70):
            return
        if direction == "SELL" and rsi < params.get("rsi_oversold", 30):
            return

    # Defesas
    if not _defenses_ok(symbol, tf, direction, bar_ts):
        return

    # SL
    sl_pts = _calc_sl(symbol, atr, params)

    # Executar
    _execute_entry(symbol, tf, direction, price, sl_pts, atr, bar_ts,
                   strategy="VWAP", vwap=vwap, rsi=rsi, regime=regime,
                   ema_fast=ema_fast, ema_slow=ema_slow_val,
                   buy_thresh=buy_thresh, sell_thresh=sell_thresh)


def check_entry_bollinger(symbol: str, tf: str, price: float,
                          atr: float, bar_ts=None, bars=None, params=None):
    """Entrada via Bollinger Bands (para WIN — mercado choppy, reversão à média)."""
    if params is None:
        params = CONFIG.get("win", {})
    _reset_daily_counter()

    if not _check_cooldown(symbol, params, tf=tf):
        return

    if not _check_max_trades(params, symbol):
        log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
        return

    if not _check_consecutive_losses(symbol):
        return

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, params.get("bb_period", 20), params.get("bb_std", 2.0))
    if bb_upper == 0 or bb_lower == 0:
        return

    # RSI
    rsi = 50
    rsi_period = params.get("rsi_period", 14)
    if bars and len(bars) >= rsi_period + 2:
        rsi = calculate_rsi(bars, rsi_period)

    # Sinal: reversão à média
    direction = None
    high = bars[1]["high"]
    low = bars[1]["low"]

    rsi_buy = params.get("rsi_buy", 30)
    rsi_sell = params.get("rsi_sell", 75)
    if low <= bb_lower and rsi < rsi_buy:
        direction = "BUY"
    elif high >= bb_upper and rsi > rsi_sell:
        direction = "SELL"

    if not direction:
        return

    # Volume filter: só entra se volume > média (confirmação)
    if bars and len(bars) >= 20:
        avg_vol = sum(b.get("volume", 1) for b in bars[:20]) / 20
        current_vol = bars[1].get("volume", 1)
        if current_vol < avg_vol * 0.8:  # volume precisa ser >= 80% da média
            return

    # Trend filter (NOVO): só compra em uptrend, vende em downtrend
    # Mean reversion funciona melhor na direção da tendência
    if params.get("trend_filter", False) and bars and len(bars) >= 26:
        ema_f = calculate_ema(bars, params.get("ema_fast", 9))
        ema_s = calculate_ema(bars, params.get("ema_slow", 21))
        if ema_f > 0 and ema_s > 0:
            if direction == "BUY" and ema_f < ema_s:
                return  # mercado em downtrend, não comprar
            if direction == "SELL" and ema_f > ema_s:
                return  # mercado em uptrend, não vender

    # Defesas
    if not _defenses_ok(symbol, tf, direction, bar_ts):
        return

    # SL
    sl_pts = _calc_sl(symbol, atr, params)

    # Executar
    _execute_entry(symbol, tf, direction, price, sl_pts, atr, bar_ts,
                   strategy="BOLLINGER",
                   bb_upper=bb_upper, bb_mid=bb_mid, bb_lower=bb_lower,
                   rsi=rsi)


def check_entry_ema_crossover(symbol: str, tf: str, price: float,
                               atr: float, bar_ts=None, bars=None, params=None):
    """Entrada via EMA Crossover + ADX (para WIN — trend-following)."""
    if params is None:
        params = CONFIG.get("win", {})
    _reset_daily_counter()

    if not _check_cooldown(symbol, params, tf=tf):
        return

    if not _check_max_trades(params, symbol):
        log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
        return

    if not _check_consecutive_losses(symbol):
        return

    ema_fast_period = params.get("ema_fast", 12)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return

    if adx_val < adx_threshold:
        return

    prev_fast = calculate_ema(bars[1:], ema_fast_period) if len(bars) > ema_fast_period else ema_fast_val
    prev_slow = calculate_ema(bars[1:], ema_slow_period) if len(bars) > ema_slow_period else ema_slow_val

    direction = None
    if prev_fast <= prev_slow and ema_fast_val > ema_slow_val:
        direction = "BUY"
    elif prev_fast >= prev_slow and ema_fast_val < ema_slow_val:
        direction = "SELL"

    if not direction:
        return

    if direction == "BUY" and rsi > rsi_ob:
        return
    if direction == "SELL" and rsi < rsi_os:
        return

    if direction == "BUY" and plus_di < minus_di:
        return
    if direction == "SELL" and minus_di < plus_di:
        return

    if not _defenses_ok(symbol, tf, direction, bar_ts):
        return

    sl_pts = _calc_sl(symbol, atr, params)

    _execute_entry(symbol, tf, direction, price, sl_pts, atr, bar_ts,
                   strategy="EMA_CROSSOVER",
                   ema_fast=ema_fast_val, ema_slow=ema_slow_val,
                   adx=adx_val, plus_di=plus_di, minus_di=minus_di,
                   rsi=rsi)


def _calc_sl(symbol: str, atr: float, params: dict = None) -> int:
    """Calcula SL em unidades do executor (sl_pts * point = distância em preço).

    ATR vem em "pontos nativos" do preço (ex: DOL ATR≈4.5 pts).
    point_mult converte pra unidades do executor (sl_pts * mt5_point = dist).

    min_native é o SL MÍNIMO em pontos nativos (antes do point_mult):
    - WIN/IND: 150 pts (1.0 → sl_pts direto)
    - WDO/DOL: 3 pts  (point=0.001 → sl_pts * 1000)
    - BIT:     30 pts  (point=0.01  → sl_pts * 100)
    - WSP:      5 pts  (point=0.01  → sl_pts * 100)

    MAX_NATIVE é o SL MÁXIMO (proteção contra ATR inflado ou sl_atr_mult muito alto):
    - BIT:    500 pts nativos (com mult 0.5 = R$ 250 de risco máximo)
    - IND:    600 pts nativos (com mult 1.0 = R$ 600 de risco)
    - WDO/DOL: 80 pts nativos (com mult 10/50 = R$ 800/4000 de risco)
    - WIN:    800 pts nativos (com mult 0.2 = R$ 160 de risco)
    - WSP:   300 pts nativos (com mult 2.5 = R$ 750 de risco)
    """
    _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
            "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
            "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"

    if params is None:
        params = CONFIG.get(_root.lower(), CONFIG.get("win", {}))

    # Specs: min/max_native em pontos do preço, point_mult = 1/mt5_point
    # max_native calibrado para limitar loss máximo POR TRADE em ~R$150-250
    _specs = {
        "WIN": {"min_native": 150, "max_native": 800,  "point_mult": 1},      # R$160 max loss
        "WDO": {"min_native": 3,   "max_native": 12,   "point_mult": 1000},    # R$120 max loss
        "BIT": {"min_native": 30,  "max_native": 500,  "point_mult": 100},     # R$500 max (ATR grande)
        "DOL": {"min_native": 3,   "max_native": 200,  "point_mult": 1000},    # R$200 max loss
        "IND": {"min_native": 150, "max_native": 350,  "point_mult": 1},       # R$350 max loss
        "WSP": {"min_native": 5,   "max_native": 200,  "point_mult": 100},     # R$200 max loss
    }
    spec = _specs.get(_root, {"min_native": 100, "max_native": 500, "point_mult": 1})

    # SL em pontos nativos (= distância em preço)
    sl_native = int(atr * params.get("sl_atr_mult", 1.5))
    # Aplicar limites min/max (CRÍTICO: max protege contra losses catastróficos)
    sl_native = max(spec["min_native"], min(sl_native, spec["max_native"]))

    # Converter pra unidades do executor
    sl_pts = sl_native * spec["point_mult"]

    # Arredondar pra múltiplo de 5
    return ((sl_pts + 4) // 5) * 5


# Wave 880.B3 (Bruno 2026-08-05): cache curto de volume_step por símbolo.
# Evita chamar info() (Wine RPC) a cada TP1/TP2. TTL 60s é suficiente porque
# volume_step é estático por contrato (só muda em troca de vencimento).
_VOLUME_STEP_CACHE: dict = {}  # {symbol: (step, expires_at)}


def _get_volume_step(symbol: str) -> float:
    """Retorna o volume_step do contrato (múltiplo mínimo da B3).

    Consulta info(symbol) via orchestrator; fallback 1.0 (B3 índices = 1 contrato).
    Usado pelo TP1/TP2 pra garantir volume inteiro (Invalid volume ×98 no dia
    05/08: original=1.0 × tp1_pct=0.5 = 0.5 contrato, rejeitado pela B3).
    """
    import time as _t
    now = _t.time()
    cached = _VOLUME_STEP_CACHE.get(symbol)
    if cached and cached[1] > now:
        return cached[0]
    step = 1.0  # fallback conservador (B3 WIN/WDO/BIT = 1 contrato)
    try:
        from mt5.mt5_orchestrator import info as _mt5_info
        _info = _mt5_info(symbol)
        if _info and "error" not in _info:
            vs = _info.get("volume_step")
            if vs and float(vs) > 0:
                step = float(vs)
    except Exception:
        pass
    _VOLUME_STEP_CACHE[symbol] = (step, now + 60.0)
    return step


def _normalize_partial_volume(close_volume: float, volume_step: float) -> float:
    """Arredonda close_volume para o múltiplo de volume_step mais próximo.

    Retorna o volume normalizado, ou 0.0 se for menor que 1 step (fracionário
    inválido). Chamador deve tratar 0.0 como "skip TP parcial".
    """
    if volume_step <= 0:
        volume_step = 1.0
    rounded = round(close_volume / volume_step) * volume_step
    # Arredondar casas decimais pra evitar float dust (ex: 0.9999999)
    rounded = round(rounded, 6)
    return rounded if rounded >= volume_step else 0.0


# Wave 880.B2 fix (Bruno 2026-08-05): cache curto de trade_stops_level por
# símbolo. Evita chamar info() (Wine RPC) a cada modify de SL. TTL 30s — o
# stop_level é estático por contrato, mas TTL curto tolera troca de sessão.
_STOP_LEVEL_CACHE: dict = {}  # {symbol: (stops_level, expires_at)}


def _get_stops_level(symbol: str) -> float:
    """Retorna o trade_stops_level do símbolo em pontos nativos (preço).

    Consulta info(symbol) via orchestrator; fallback 0.0 (não sabe → não
    bloqueia, mas o caller decide). Cache 30s.

    Wave 880.B2-PARIDADE (Bruno 2026-08-05): quando o broker retorna 0 (conta
    DEMO, que aceita SLs a poucos pts), mas há um override
    config["simulated_stop_level"] ativo, usa o valor simulado. Isto faz a DEMO
    rejeitar os mesmos SLs que a REAL rejeitaria → paridade demo-real. As
    estimativas (WIN~300, WDO~200, BIT~500, WSP~200 pts nativos) vêm da
    literatura/XP e devem ser confirmadas com a corretora (Q1 do doc matriz).
    Sem override, comportamento original (stops=0 na demo = não bloqueia).
    """
    import time as _t
    now = _t.time()
    cached = _STOP_LEVEL_CACHE.get(symbol)
    if cached and cached[1] > now:
        return cached[0]
    stops = 0.0
    try:
        from mt5.mt5_orchestrator import info as _mt5_info
        _info = _mt5_info(symbol)
        if _info and "error" not in _info:
            stops = float(_info.get("trade_stops_level", 0) or 0)
    except Exception:
        pass
    # Paridade demo-real: se broker retornou 0 (DEMO) e há override, usa-o.
    if stops <= 0:
        _sim = CONFIG.get("simulated_stop_level") if isinstance(CONFIG, dict) else None
        if isinstance(_sim, dict):
            for _prefix, _val in _sim.items():
                if _prefix in symbol and _val > 0:
                    stops = float(_val)
                    break
    _STOP_LEVEL_CACHE[symbol] = (stops, now + 30.0)
    return stops


def _within_stop_level(symbol: str, sl_pts: int, entry_price: float,
                       direction: str, point_val: float,
                       buffer_ticks: float = 2.0) -> bool:
    """Verifica se um SL proposto está DENTRO do stop level do broker (inválido).

    O stop_level (stops_level) do broker define a distância MÍNIMA entre o SL e
    o preço atual. SLs mais próximos que isso são rejeitados ("Invalid stops").

    Wave 880.B2 fix (Bruno 2026-08-05): no dia 05/08, breakeven/profit-lock/
    trailing enviavam SLs a ~5pts do preço e a conta REAL rejeitava ×255
    (stop_level real; a DEMO aceitava por ter stop_level ≈ 0). Esta função é o
    gate PRÉ-ENVIO: se o SL proposto está dentro do stop_level (+buffer), o
    caller deve SKIPAR o modify e manter o SL anterior (que é válido).

    sl_pts é SIGNED (positivo=abaixo entry/loss, negativo=acima entry/profit
    lock), mesma convenção do cmd_modify. point_val é o valor do point em R$.

    Retorna True se o SL está DENTRO do stop_level (modify seria rejeitado).
    """
    if not entry_price or entry_price <= 0 or not sl_pts:
        return False  # não dá pra avaliar; deixa passar (caller pode ter outro guard)
    stops_level = _get_stops_level(symbol)
    if stops_level <= 0:
        return False  # broker não reporta stop_level (DEMO) → não bloqueia
    # Distância mínima exigida em unidades de PREÇO.
    # Wave 880.E fix (Bruno 06/08): stops_level vem em PONTOS NATIVOS
    # (ex.: WIN=300 pontos → 300.0 de preço; WSP=200 pontos → 2.00 de preço,
    # porque WSP point=0.01). ANTES este código comparava stops_level cru
    # (pontos) contra sl_distance_price (preço), inflando o stop level em
    # 1/point_val (WSP/WDO 100-1000x, BIT 100x) → TODO trailing/breakeven/
    # profit-lock do WSP/WDO/BIT era silenciosamente pulado. WIN (point=1.0)
    # não era afetado. Converter pontos → preço via point_val.
    min_distance_price = (stops_level + buffer_ticks) * point_val
    # Distância do SL ao entry em preço: |sl_pts| * point_val
    sl_distance_price = abs(sl_pts) * point_val
    return sl_distance_price < min_distance_price


def _defenses_ok(symbol: str, tf: str, direction: str, bar_ts) -> bool:
    """Verifica defesas anti-duplicação.

    Wave Per-TF (Bruno 2026-07-07): slot eh (symbol, tf) — Defesa 2 deixa de
    ser "qualquer pos no MT5 com mesma direction" (que bloqueava cross-TF) e
    vira "drift detector": se MT5 tem pos no symbol mas state NAO tem no slot
    (symbol, tf), eh orfao — bloqueia pra evitar duplicar.
    """
    # Defesa 1: posição no state (slot per-TF: f"{symbol}_{tf}")
    state_key = f"{symbol}_{tf}"
    if state.positions.get(state_key):
        return False

    # Defesa 2 (Wave 14.3 — 2026-07-14): drift MT5↔state REAL via OrderTracker.
    # Antes bloqueava errado: iterava truth.get_open_positions() (que NÃO tem
    # campo TF) e, para QUALQUER pos aberta com mesmo symbol, considerava o
    # slot per-TF state["{symbol}_{tf}"] vazio como "órfão" → bloqueava até
    # WINQ26_M5 coexistir com WINQ26_M15 (multi-TF impossível). Agora orfão
    # = ticket que MT5 tem aberto MAS o OrderTracker nao conhece
    # (server-side open nao registrado pelo bot). Reconcilia depois.
    #
    # Camada defensiva extra (Wave 14.3, Bruno 2026-07-14): se QUALQUER slot
    # state.positions[f"{symbol}_*"] gerencia um ticket presente em MT5, NÃO
    # bloquear — está sendo gerenciado em outro TF do mesmo symbol (per-TF
    # slot é independente por design).
    try:
        _open_pos = _truth.get_open_positions()
        _mt5_tickets = {str(int(p.ticket)) for p in _open_pos if getattr(p, 'ticket', None)}
        # 1) Coletar tickets conhecidos pelo state (qualquer TF)
        _state_known_tickets = set()
        for _v in state.positions.values():
            _t = _v.get("entry_ticket")
            if _t is not None:
                try:
                    _state_known_tickets.add(str(int(_t)))
                except (ValueError, TypeError):
                    _state_known_tickets.add(str(_t))
        # 2) Coletar tickets conhecidos pelo OrderTracker
        try:
            from core.vt_order_tracker import OrderTracker
            _tracker = OrderTracker()
            _ot_known = set(_tracker._active.keys()) if hasattr(_tracker, '_active') else set()
            _ot_known_str = {str(int(t)) for t in _ot_known}
        except Exception:
            _ot_known_str = set()
        _known_tickets = _state_known_tickets | _ot_known_str
        # 3) Só é orfão real se MT5 tem ticket MAS NEM state NEM tracker conhecem
        _orphan_tickets = _mt5_tickets - _known_tickets
        if _orphan_tickets:
            log(
                f"[DEFESA2-DRIFT] {len(_orphan_tickets)} orfao(s) real(is) no MT5 "
                f"={_orphan_tickets} — bloqueando {symbol} {tf} {direction}"
            )
            return False
    except Exception:
        pass

    # Defesa 3: sinal idêntico na mesma barra
    sig_key = f"{symbol}_{tf}_{direction}"
    last = state.last_signals.get(sig_key)
    if last and last.get("bar_ts") is not None and last.get("bar_ts") == bar_ts:
        return False

    # Defesa 4: cooldown por (symbol, tf, direction) — evita reversão rápida
    dir_key = f"{symbol}_{tf}_{direction}"
    last_dir_time = state.last_trade_time.get(dir_key)
    if last_dir_time:
        _root = symbol[:3] if len(symbol) >= 3 else symbol
        _params = CONFIG.get(_root.lower(), CONFIG.get("win", {}))
        cd = _params.get("cooldown_seconds", 300)
        if (datetime.now() - last_dir_time).total_seconds() < cd:
            return False

    return True


# Phase 1 PLUS (2026-07-01, Bruno): guard anti-duplicação.
#
# BUG HOJE: depois que modify_sl falha 3x e emergency_close fecha a posição
# no MT5 (PnL +0,00), o next tick re-evalua a estratégia e gera um NOVO sinal
# idêntico (mesmo symbol, mesma direction, mesmo magic). O bot cria um novo
# ticket imediatamente, abrindo uma posição DUPLICADA que ninguém pediu.
#
# Forense: data/architecture_audit_2026_07_01.md secao 4.2 mapeou os write
# paths sem validate. Proposta: data/architecture_proposal_2026_07_01.md L350.
#
# FIX: validate_order_pre_send() consulta MT5.status() ANTES de qualquer
# BUY/SELL. Se já existe posição aberta com mesmo magic+symbol (independente
# de direction — porque se tem BUY aberta e a estratégia diz SELL, primeiro
# fecha a BUY antes de abrir SELL nova; ver manage_position), bloqueia com
# log [BLOCKED-DUPLICATE] e retorna False.
#
# FAIL-SAFE: status() exception => permite (assume broker offline / leitura
# falha e não bloqueia trading por defeito de leitura). É o oposto de "tudo
# bloqueado quando MT5 não responde", que seria pior (lockup total).
VT_BOT_MAGIC = 555501  # magic do bot, ver mt5/mt5_executor.py L231/L341


def validate_order_pre_send(symbol: str, tf: str = "", direction: str = "", magic: int = VT_BOT_MAGIC) -> bool:
    """Consulta state.positions (per-TF) antes de enviar BUY/SELL. Bloqueia duplicacao.

    Wave Per-TF (Bruno 2026-07-07): cada (symbol, tf) eh slot independente.
    Multiplos TFs podem coexistir no mesmo symbol. Verifica slot
    state.positions[f"{symbol}_{tf}"] em vez de MT5 status magic+symbol.

    DELEGADO para core.vt_truth.validate_order_pre_send (Fase 2.5 — truth layer
    autoritativo). Mantido como thin wrapper aqui para nao quebrar callers
    existentes (tests, scripts) que importam de core.vt_autotrader.

    Args:
        symbol: contrato MT5 (ex: "WDON26").
        tf: timeframe ("M5", "M15", "M30", "H1"). Obrigatorio para semantica
            per-TF; se vazio, truth layer usa fallback legado (magic+symbol).
        direction: "BUY"/"SELL" (log apenas).
        magic: magic number do bot. Default 555501.
    """
    return _truth.validate_order_pre_send(symbol=symbol, tf=tf, direction=direction, magic=magic)


def _execute_entry(symbol: str, tf: str, direction: str, price: float,
                   sl_pts: int, atr: float, bar_ts, strategy: str = "VWAP", **kwargs):
    """Executa entrada e registra tudo."""
    # GUARD DUPLO (defesa em profundidade): mesmo que check_and_trade falhe
    # por refactor, a hard-kill list bloqueia aqui. Bruno 2026-06-30.
    if is_permanently_disabled(symbol):
        log(f"🚫 [_execute_entry] HARD-KILL: {symbol} recusado (PERMANENTLY_DISABLED={PERMANENTLY_DISABLED})")
        return {"status": "BLOCKED", "reason": "PERMANENTLY_DISABLED", "symbol": symbol}

    # Phase 1 PLUS (Bruno 2026-07-01): guard anti-duplicacao. Consulta MT5
    # ANTES de enviar BUY/SELL. Se ja tem pos aberta com mesmo magic+symbol,
    # bloqueia (ver validate_order_pre_send).
    if not validate_order_pre_send(symbol, tf=tf, direction=direction):
        return {"status": "BLOCKED", "reason": "BLOCKED-DUPLICATE", "symbol": symbol}

    # ===== GATE PRÉ-ENVIO (validator v2) =====
    # Apenas corrige SL quando necessário (sem bloqueio de ordens).
    _pre_order = {
        "symbol": symbol, "direction": direction, "tf": tf,
        "timeframe": tf, "entry_price": price, "sl_pts": sl_pts,
        "atr": atr, "strategy": strategy,
    }
    _pre_result = validate_pre_send(_pre_order)
    if _pre_result.get("adjusted_sl"):
        _old_sl = sl_pts
        sl_pts = _pre_result["adjusted_sl"]
        log(f"[VALIDATOR] SL ajustado pré-envio: {symbol} {direction} {tf} | {_old_sl}pts → {sl_pts}pts")
        notify_telegram(
            f"🤖 [VALIDATOR] SL ajustado (pré-envio)\n"
            f"{symbol} {direction} {tf} | SL: {_old_sl}pts → {sl_pts}pts"
        )

    # Log
    detail_parts = [f"{strategy}"]
    if strategy == "VWAP":
        detail_parts.append(f"VWAP={kwargs.get('vwap', 0):.2f}")
    detail_parts.append(f"ATR={atr:.0f}")
    detail_parts.append(f"RSI={kwargs.get('rsi', 50):.1f}")
    if strategy == "VWAP":
        detail_parts.append(f"Regime={kwargs.get('regime', 'UNKNOWN')}")
    elif strategy == "BOLLINGER":
        detail_parts.append(f"BB=[{kwargs.get('bb_lower', 0):.0f}|{kwargs.get('bb_mid', 0):.0f}|{kwargs.get('bb_upper', 0):.0f}]")
    log(f"[SINAL] {symbol} {tf}: {direction} @ {price:.2f} | {' | '.join(detail_parts)}")

    # Volume (Wave Per-TF, Bruno 2026-07-07): prioridade volume_by_tf >
    # volume_by_symbol > volume. Cada (symbol, tf) pode ter volume proprio
    # via CONFIG["volume_by_tf"]["WDO_M5"] etc.
    # Wave N+2B (2026-07-08): sizing extraído para core/vt_sizing.resolve_volume
    # com suporte a vol-scaled (mode="vol_scaled" + sizing.atr_baseline).
    # bars_count não está em scope aqui; warmup via vt_pre_flight snapshot.
    from core.vt_sizing import resolve_volume as _resolve_volume_new
    _vol = _resolve_volume_new(
        symbol, tf,
        config=CONFIG,
        current_atr=atr,
        bars_count=None,
    )
    if direction == "BUY":
        result = safe_buy(symbol, _vol, sl_pts=sl_pts, strategy=strategy)
    else:
        result = safe_sell(symbol, _vol, sl_pts=sl_pts, strategy=strategy)

    if result.get("status") == "FILLED":
        ticket = result.get("ticket", "?")
        # Wave N+6A (2026-08-05): servidor XPMT5-PRD retorna price=0.0 no
        # order_send. Executor agora faz fallback broker-truth (positions_get),
        # mas defesa em profundidade: se ainda vier 0/None, usa o preço do
        # sinal (tick) — nunca deixar entry_price=0 (quebra trailing/TP1/DB).
        exec_price = result.get("price") or price

        # ===== Fase 3 — Lei 4: valida ticket confirmado pelo MT5 =====
        # Antes o código aceitava ticket="?" como válido. Agora exigimos int > 0.
        # Se result veio BLOCKED/NOT_CONFIRMED do orchestrator (Fase 3.3), status
        # != FILLED já caiu no else abaixo. Mas ticket="?" ainda pode escapar de
        # FILLED legítimo com campo ausente — defendemos aqui também.
        try:
            _ticket_int = int(ticket) if ticket not in ("?", None, "") else 0
        except (ValueError, TypeError):
            _ticket_int = 0
        if _ticket_int <= 0:
            print(f"[LEI4] {symbol} {direction} FILLED mas ticket inválido "
                  f"(ticket={ticket}). Recusando abrir posição — não confiar sem confirmação.")
            return {"status": "BLOCKED", "reason": "INVALID_TICKET",
                    "detail": f"ticket={ticket}", "symbol": symbol}

        # ===== Fase 3.1 — Registra no OrderTracker (rastreio ininterrupto) =====
        try:
            from core.vt_order_tracker import OrderTracker
            _tracker = OrderTracker()  # autoload do /tmp/vt_order_tracker.json
            _tracker.register_order(
                ticket=_ticket_int, symbol=symbol, direction=direction,
                volume=_vol, entry_price=exec_price, sl_pts=sl_pts,
                tp_pts=None, reason=f"{tf}_{strategy}", strategy=strategy,
            )
        except Exception as _tracker_err:
            # Tracker é observabilidade — NUNCA derruba o path de ordens.
            # A verdade final continua sendo o MT5 (reconcile_positions_with_mt5).
            print(f"[TRACKER] aviso: falha ao registrar ticket={_ticket_int}: "
                  f"{_tracker_err} (não bloqueia ordem)")

        # Wave 1111 (Bruno 2026-08-11): notifica abertura de posição no Telegram.
        # Antes a abertura só logava [SINAL]/[TRACKER] — Bruno não via a ordem
        # abrir (ex: WDOU26 abriu 2x em 11/08 sem nenhuma notificação).
        try:
            _open_emoji = "🟢" if direction == "BUY" else "🔴"
            _notify_msg = (
                f"{_open_emoji} *{direction} {symbol}* {tf}\n"
                f"💵 Preço: {exec_price:.2f}\n"
                f"🛡️ SL: {sl_pts}pts\n"
                f"🎫 Ticket: {_ticket_int} | Vol: {_vol}\n"
                f"📊 {strategy}"
            )
            notify_telegram(_notify_msg)
        except Exception as _notify_err:
            log(f"[NOTIFY] falha notificando abertura: {_notify_err}")

        # ===== VALIDAÇÃO PÓS-ENVIO =====
        try:
            order_data = {
                "symbol": symbol,
                "direction": direction,
                "tf": tf,
                "timeframe": tf,
                "entry_price": exec_price,
                "sl_pts": sl_pts,
                "atr": atr,
                "strategy": strategy,
                "volume": _vol,
                "ticket": ticket,
            }
            use_llm = CONFIG.get("validate_with_llm", False)
            validation = validate_order(order_data, use_llm=use_llm)

            # Se LLM sugeriu correção de SL (SEMPRE aplicar)
            if validation.get("suggested_action") and validation["suggested_action"].get("type") == "MODIFY_SL":
                action = validation["suggested_action"]
                new_sl = action["suggested_sl"]
                reason = action.get("reason", "")
                risco = action.get("risco", "")

                # Notificar Bruno no Telegram
                _tf_msg = f"{symbol} {direction} @ {exec_price} | SL: {sl_pts}pts → {new_sl}pts"
                _rag = f"\n⚠️ Risco: {risco}" if risco else ""
                notify_telegram(f"🤖 [VALIDATOR] SL sugerido:\n{_tf_msg}\n📝 {reason}{_rag}")

                # Bounds check: garantir SL dentro dos limites seguros
                _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                        "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                        "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
                _limits = {"WDO": {"min": 3000, "max": 300000}, "WIN": {"min": 200, "max": 3000},
                           "BIT": {"min": 3000, "max": 2000000}, "DOL": {"min": 3000, "max": 300000},
                           "IND": {"min": 200, "max": 3000}, "WSP": {"min": 500, "max": 30000}
                          }.get(_root, {"min": 200, "max": 50000})
                if isinstance(new_sl, (int, float)) and _limits["min"] <= new_sl <= _limits["max"]:
                    # Pitfall fix: re-aplicar max_native DEPOIS da correção do validator.
                    # Sem isso, validator pode amplificar SL além do risco máximo por trade.
                    # NOTA: max_native cobre SL de ~1.2x ATR. Para BIT com ATR=1624, 1.2x ATR = 1948
                    # nativos = 194800 pts executores. max_native=2000 cobre isso com folga.
                    _specs = {
                        "WIN": {"max_native": 800,  "point_mult": 1},
                        "WDO": {"max_native": 12,   "point_mult": 1000},
                        "BIT": {"max_native": 2000, "point_mult": 100},
                        "DOL": {"max_native": 200,  "point_mult": 1000},
                        "IND": {"max_native": 350,  "point_mult": 1},
                        "WSP": {"max_native": 200,  "point_mult": 100},
                    }
                    _spec = _specs.get(_root, {"max_native": 500, "point_mult": 1})
                    _sl_native = new_sl / _spec["point_mult"]
                    _max_exec = _spec["max_native"] * _spec["point_mult"]
                    if new_sl > _max_exec:
                        log(f"[VALIDATOR] SL {int(new_sl)}pts ({_sl_native:.0f} nativos) excede max_native {_spec['max_native']}pts → clampado para {_max_exec}pts")
                        new_sl = _max_exec
                    log(f"[VALIDATOR] Corrigindo SL: {sl_pts}pts → {int(new_sl)}pts ({reason})")
                    fix_result = safe_modify_sl_with_emergency_close(symbol, ticket, int(new_sl), exec_price, direction)
                    if fix_result.get("status") == "ok":
                        sl_pts = int(new_sl)
                        log(f"[VALIDATOR] SL corrigido com sucesso para {sl_pts}pts")
                        notify_telegram(f"✅ SL aplicado: {symbol} ticket={ticket} → {sl_pts}pts")
                    else:
                        log(f"[VALIDATOR] Falha ao corrigir SL: {fix_result}")
                        notify_telegram(f"❌ Falha ao aplicar SL: {fix_result}")
                else:
                    log(f"[VALIDATOR] LLM sugeriu SL fora dos limites ({new_sl}pts [{_limits['min']}-{_limits['max']}]), ignorado")
            elif validation.get("llm_analysis"):
                # LLM analisou e não sugeriu mudança — notificar Telegram + log completo
                _llm_raw = validation['llm_analysis']
                _resumo_short = _llm_raw[:200]
                _sl_sug_str = "?"
                try:
                    _s = _llm_raw.find('{')
                    _e = _llm_raw.rfind('}') + 1
                    if _s >= 0 and _e > _s:
                        _parsed_llm = json.loads(_llm_raw[_s:_e])
                        _resumo_short = _parsed_llm.get('resumo', _resumo_short)
                        _sl_sug_str = _parsed_llm.get('sl_sugerido', '?')
                except Exception:
                    pass
                # Converter sl_pts (executor) para pontos reais (legível)
                _pm_map = {"WIN": 1, "WDO": 1000, "BIT": 100, "DOL": 1000, "IND": 1, "WSP": 100}
                _vm_root = next((k for k in _pm_map if k in symbol), "")
                _pm = _pm_map.get(_vm_root, 1)
                _sl_real = sl_pts / _pm if _pm else sl_pts
                log(f"[VALIDATOR] LLM OK (SL mantido {sl_pts}pts = {_sl_real:.0f}pts reais, sugerido {_sl_sug_str}): {_resumo_short[:300]}")
                # Removido [:200] — _resumo_short agora vai completo.
                # O limite de 4096 chars do Telegram é tratado em vt_hermes_helper
                # via _split_long_message, que divide em múltiplos chunks com
                # prefixo [N/M] quando necessário (commit 0a20ab25).
                notify_telegram(
                    f"✅ [VALIDATOR] {symbol} {direction} {tf} | "
                    f"SL mantido em {_sl_real:.0f}pts\n📝 {_resumo_short}"
                )
            elif not validation.get("llm_analysis") and validation.get("alerts"):
                # LLM falhou mas há alertas locais — aplicar correção local
                for alert in validation["alerts"]:
                    if "suggestion" in alert:
                        # Extrair valor sugerido da sugestão
                        import re
                        match = re.search(r'(\d+)pts', alert["suggestion"])
                        if match:
                            suggested_pts = int(match.group(1))
                            if suggested_pts != sl_pts:
                                log(f"[VALIDATOR] LLM falhou, aplicando correção local: {sl_pts}pts → {suggested_pts}pts")
                                # Notificar Bruno ANTES de aplicar (audit trail)
                                _alert_msg = alert.get("detail", alert.get("message", "alerta sem descrição"))
                                _alert_sev = alert.get("severity", "?")
                                _alert_type = alert.get("type", "?")
                                notify_telegram(
                                    f"🤖 [VALIDATOR] ⚠️ LLM falhou, aplicando alerta local\n"
                                    f"{symbol} {direction} {tf} | "
                                    f"SL: {sl_pts}pts → {suggested_pts}pts\n"
                                    f"📋 Alerta [{_alert_sev}/{_alert_type}]: {_alert_msg}"
                                )
                                fix_result = safe_modify_sl_with_emergency_close(symbol, ticket, suggested_pts, exec_price, direction)
                                if fix_result.get("status") == "ok":
                                    sl_pts = suggested_pts
                                    log(f"[VALIDATOR] SL corrigido localmente para {sl_pts}pts")
                                    notify_telegram(f"✅ SL aplicado (local): {symbol} ticket={ticket} → {sl_pts}pts")
                                break
            # LLM falhou/timeout e sem alertas locais — logar para diagnóstico
            if not validation.get("llm_analysis") and not validation.get("alerts"):
                log(f"[VALIDATOR] Sem análise LLM (timeout/falha) | {symbol} {direction} {tf}")
        except Exception as e:
            log(f"[VALIDATOR] Erro na validação (não bloqueante): {e}")

        state_key = f"{symbol}_{tf}"
        sig_key = f"{symbol}_{tf}_{direction}"

        # Trava anti-duplicação
        state.last_signals[sig_key] = {
            "ts": datetime.now(),
            "close": price,
            "bar_ts": bar_ts,
            "ticket": ticket,
            "direction": direction,
        }

        # Signal detail pro banco
        signal_detail = {
            "strategy": strategy,
            "atr": round(atr, 2),
            "rsi": round(kwargs.get("rsi", 50), 1),
            "sl_pts": sl_pts,
        }
        if strategy == "VWAP":
            signal_detail.update({
                "vwap": round(kwargs.get("vwap", 0), 2),
                "regime": kwargs.get("regime", "UNKNOWN"),
                "ema_fast": round(kwargs.get("ema_fast", 0), 0),
                "ema_slow": round(kwargs.get("ema_slow", 0), 0),
                "threshold_buy": round(kwargs.get("buy_thresh", 0), 2),
                "threshold_sell": round(kwargs.get("sell_thresh", 0), 2),
            })
        elif strategy == "BOLLINGER":
            signal_detail.update({
                "bb_upper": round(kwargs.get("bb_upper", 0), 2),
                "bb_mid": round(kwargs.get("bb_mid", 0), 2),
                "bb_lower": round(kwargs.get("bb_lower", 0), 2),
            })
        elif strategy == "EMA_CROSSOVER":
            signal_detail.update({
                "ema_fast": round(kwargs.get("ema_fast", 0), 2),
                "ema_slow": round(kwargs.get("ema_slow", 0), 2),
                "adx": round(kwargs.get("adx", 0), 1),
                "plus_di": round(kwargs.get("plus_di", 0), 1),
                "minus_di": round(kwargs.get("minus_di", 0), 1),
            })

        # Registrar no banco
        # entry_sl: calcular preço real do SL baseado no symbol
        _point_map = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
        _root_pv = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                   "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                   "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
        point_val = _point_map.get(_root_pv, 1.0)
        entry_sl_price = exec_price - sl_pts * point_val if direction == "BUY" else exec_price + sl_pts * point_val
        trade_id = log_entry(
            symbol=symbol, direction=direction,
            volume=_vol,
            entry_price=exec_price,
            entry_sl=entry_sl_price,
            entry_ticket=ticket,
            timeframe=tf,
            strategy=strategy,
            signal_detail=signal_detail,
            raw_json=result,
        )

        # Estado
        state.positions[state_key] = {
            "direction": direction,
            "entry_price": exec_price,
            "entry_ticket": ticket,
            "sl_pts": sl_pts,
            "atr": atr,
            "trail_on": False,
            "best_price": exec_price,
            "bar_count": 0,
            "trade_log_id": trade_id,
            "strategy": strategy,
            "bb_mid": kwargs.get("bb_mid", 0),
            "entry_time": datetime.now(),
            "volume": _vol,
            "tf": tf,
            # Wave N+2A (2026-07-08): TP1 partial close state. original_volume
            # congelado na abertura; remaining_volume = original * (1 - tp1_pct)
            # após TP1 fechar fração. tp1_done=True dispara trail em modo
            # atr_trail_mult (mais apertado que trail_distance).
            "original_volume": _vol,
            "remaining_volume": _vol,
            "tp1_done": False,
            # Wave 880.B4 (2026-07-19): TP2 ladder — segundo parcial em tp2_r*ATR.
            "tp2_done": False,
        }

        # Cooldown (por symbol, tf, direction — evita reversões rápidas)
        # Wave 14.3 (Bruno 2026-07-14): NÃO escrever em `state.last_trade_time[symbol]`
        # puro — isso violava o modelo per-TF, fazendo cooldown de M5 vazar pra
        # M15/M30. Antes da fix, um trade em WINQ26_M5 bloqueava WINQ26_M15
        # por cd=300s. Agora só chaves per-TF.
        # Wave 14.3.1: removida chave _symbol_root (root 3 letras) — ninguém
        # lia (ver `_check_cooldown` L1183 lê `symbol` completo). Era dead
        # write que poluía o state. Apenas `f"{symbol}_{tf}_{direction}"`
        # é consultada em `_defenses_ok:2076`.
        now = datetime.now()
        state.last_trade_time[f"{symbol}_{tf}"] = now
        state.last_trade_time[f"{symbol}_{tf}_{direction}"] = now
        state.daily_trade_count += 1
        state.daily_trade_by_symbol[symbol] = state.daily_trade_by_symbol.get(symbol, 0) + 1
        state.save()  # persistir estado

        # Notificação de abertura — informações completas do servidor
        sl_label = exec_price - sl_pts * point_val if direction == "BUY" else exec_price + sl_pts * point_val
        strategy_label_map = {"VWAP": "VWAP", "BOLLINGER": "Bollinger", "EMA_CROSSOVER": "EMA Cross",
                              "EMA_PULLBACK": "EMA Pullback", "RSI_REVERSION": "RSI Reversion",
                              "MACD_MOMENTUM": "MACD Momentum", "SMART_EMA": "Smart EMA"}
        strategy_label = strategy_label_map.get(strategy, strategy)
        _ts = datetime.now().strftime("%H:%M:%S")
        _atr_mult = sl_pts / (atr * point_val) if atr > 0 and point_val > 0 else 0
        notify_telegram(
            f"📊 *{direction} {symbol} {tf}* ({strategy_label})\n"
            f"• Entrada: {exec_price:.2f} | SL: {sl_label:.2f}\n"
            f"• ATR: {atr:.0f} | RSI: {kwargs.get('rsi', 50):.1f} | SL: {_atr_mult:.1f}x ATR\n"
            f"• Volume: {_vol} contrato(s) | Ticket: {ticket}\n"
            f"• Trade {state.daily_trade_count}/dia | {_ts}"
        )
    else:
        reason = result.get("comment", result.get("error", "desconhecido"))
        log(f"[REJEITADO] {symbol} {tf} {direction}: {reason}")


def _lookup_exit_event_from_db(symbol, direction, entry_ticket,
                               db_path="vt_trades.db", retries=2,
                               retry_sleep=0.4):
    """Busca o deal de SAÍDA real (broker-truth) em mt5_trade_events.

    Fonte: EA TradeLogger → CSV → watcher → SQLite. Mais confiável que o
    history Wine (_truth.get_position_history), que falha em fechamentos
    server-side e deixa current_price stale (== entry → alerta Entrada==Saída).

    Matching determinístico: entry_ticket (order ticket da abertura) →
    position_ticket (via deal IN) → deal OUT da direção OPOSTA nessa posição.
    O EA imprime tickets com %d (int32 signed), então valores >= 2^31 viram
    negativos — converte o entry_ticket antes de casar. Dedup por deal_ticket
    (um deal pode existir 2x: capturado ao vivo + backfill).

    Retorna dict {price, profit, commission, swap, ticket, time} ou None
    (sem deal / DB indisponível). Nunca levanta. Retry curto dá tempo ao
    watcher (~1s) de ingerir o deal recém-escrito pelo EA.

    Auto-contida (só sqlite3/time) para permitir teste via extração AST sem
    importar o autotrader (que constrói estado global e contacta o MT5).
    """
    import sqlite3
    import time as _time
    try:
        et = int(entry_ticket)
    except (TypeError, ValueError):
        return None
    et_i32 = et - (1 << 32) if et >= (1 << 31) else et
    want_type = "SELL" if direction == "BUY" else "BUY"

    def _query():
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            # 1) position_ticket a partir do deal de entrada (IN) desta ordem
            row = conn.execute(
                "SELECT position_ticket FROM mt5_trade_events "
                "WHERE trans_type='DEAL_ADD' AND deal_entry='IN' AND order_ticket=? "
                "ORDER BY id DESC LIMIT 1", (et_i32,)).fetchone()
            if not row or row[0] in (None, 0):
                return None
            pos_tk = row[0]
            # 2) deal de saída (OUT, direção oposta) dessa posição, dedup por ticket
            r = conn.execute(
                "SELECT deal_price, deal_profit, deal_commission, deal_swap, "
                "       deal_ticket, event_time "
                "FROM mt5_trade_events WHERE id IN ("
                "  SELECT MAX(id) FROM mt5_trade_events "
                "  WHERE trans_type='DEAL_ADD' AND deal_entry='OUT' "
                "    AND position_ticket=? AND deal_type=? "
                "  GROUP BY deal_ticket) "
                "ORDER BY event_time DESC LIMIT 1", (pos_tk, want_type)).fetchone()
            if not r:
                return None
            return {
                "price": float(r[0] or 0),
                "profit": float(r[1] or 0),
                "commission": float(r[2] or 0),
                "swap": float(r[3] or 0),
                "ticket": r[4],
                "time": r[5],
            }
        finally:
            conn.close()

    for attempt in range(retries + 1):
        try:
            res = _query()
        except Exception:
            res = None
        if res is not None:
            return res
        if attempt < retries:
            _time.sleep(retry_sleep)
    return None


def manage_position(symbol: str, tf: str, pos: dict, current_atr: float, strategy: str = "VWAP", params: dict = None):
    """Gerencia trailing stop e verifica saídas.

    Proteções anti-drawdown:
    1. Breakeven: após breakeven_minutes sem trailing, move SL pra entry + custo
    2. Time trailing: após time_trail_minutes, aperta trailing mesmo sem trail_activate
    3. Max position: após max_position_minutes, trailing agressivo (0.3x ATR)
    """
    if params is None:
        _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
        params = CONFIG.get(_root.lower(), CONFIG.get("win", {}))
    key = f"{symbol}_{tf}"
    direction = pos["direction"]
    entry_price = pos["entry_price"]
    # Wave 880.B1 fix (Bruno 2026-08-05): guard contra entry_price inválido.
    # O state rebuild (L582) e recover_open_positions (L3510/L3529) podem gravar
    # entry_price=0.0 quando o XPMT5-PRD retorna price_open=0. Com entry=0,
    # profit_pts = best - 0 = preço absoluto (ex: 179375 pts = "670x ATR"),
    # armeando trailing/TP1/profit-lock/BREAKEVEN imediatamente — o vetor do
    # colapso do dia 05/08 (todas as gestões disparavam falsas). Se entry_price
    # é 0/None, PULA toda a gestão (TP1/trailing/BE/profit-lock) até o
    # broker-truth estar disponível. A posição continua protegida pelo SL de
    # entrada (já no MT5); apenas não tenta apertá-lo com base em lucro falso.
    if not entry_price or entry_price <= 0:
        log(
            f"[GESTÃO-SKIP] {symbol} {tf} entry_price={entry_price} (inválido) — "
            f"pulando TP1/trailing/BE/profit-lock até broker-truth disponível"
        )
        return
    atr = pos["atr"]
    sl_pts = pos["sl_pts"]
    best = pos["best_price"]
    trail_on = pos["trail_on"]
    bar_count = pos["bar_count"]
    trade_log_id = pos["trade_log_id"]
    # Point value per symbol
    _point_map = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
    _root_pv = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
               "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
               "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
    point_val = _point_map.get(_root_pv, 1.0)

    tick_data = tick(symbol)
    if not tick_data or tick_data.get("bid", 0) == 0:
        return

    current_price = tick_data["bid"] if direction == "BUY" else tick_data["ask"]

    # Atualizar melhor preço
    if direction == "BUY":
        best = max(best, tick_data["bid"])
    else:
        best = min(best, tick_data["ask"]) if best > 0 else tick_data["ask"]

    pos["best_price"] = best
    pos["bar_count"] = bar_count + 1

    # Lucro em pontos (Wave N+2A: TP1 abaixo precisa disto)
    if direction == "BUY":
        profit_pts = best - entry_price
    else:
        profit_pts = entry_price - best

    # ════════════════════════════════════════════════════════════════════
    # Wave N+2A (2026-07-08): TP1 — fechamento parcial em R*ATR de profit.
    # Dispara UMA vez por posição, em profit >= tp1_r * atr. Fecha fração
    # tp1_pct da posição original; resto segue sob trailing (que passa a usar
    # atr_trail_mult após TP1, mais apertado).
    # ════════════════════════════════════════════════════════════════════
    tp1_r = params.get("tp1_r", 1.0)
    tp1_pct = params.get("tp1_pct", 0.5)
    if (
        not pos.get("tp1_done", False)
        and atr > 0
        and profit_pts >= tp1_r * atr
        and pos.get("remaining_volume", pos["volume"]) > 0
        and 0 < tp1_pct < 1
    ):
        # tp1_pct: fração da posição ORIGINAL a fechar.
        original = pos.get("original_volume", pos["volume"])
        close_volume = original * tp1_pct
        # Não fechar MAIS do que o que está aberto.
        actual_close = min(close_volume, pos.get("remaining_volume", pos["volume"]))
        # Wave 880.B3 fix (Bruno 2026-08-05): normalizar pro volume_step da B3.
        # Antes, original=1.0 × tp1_pct=0.5 = 0.5 contrato → "Invalid volume"
        # ×98 (B3 exige múltiplo de volume_step=1.0). Agora arredonda pro step;
        # se fracionário (< 1 step), skip TP1 idempotente (não tenta de novo).
        _vs = _get_volume_step(symbol)
        actual_close = _normalize_partial_volume(actual_close, _vs)
        if actual_close <= 0:
            pos["tp1_done"] = True  # idempotente — volume fracionário, sem TP1 possível
            log(
                f"[TP1-SKIP] {symbol} {direction} volume fracionário "
                f"(original={original} × tp1_pct={tp1_pct} = {close_volume:.2f} "
                f"< step {_vs}) — sem TP parcial possível neste sizing"
            )
        else:
            try:
                from mt5.mt5_error_recovery import safe_partial_close
                tp1_result = safe_partial_close(
                    symbol, pos["entry_ticket"], actual_close,
                )
                if tp1_result.get("status") in ("ok", "already_closed"):
                    new_remaining = pos["volume"] - actual_close
                    if tp1_result.get("status") == "already_closed":
                        new_remaining = 0.0
                    pos["remaining_volume"] = new_remaining
                    pos["tp1_done"] = True
                    tp1_profit_pts = actual_close * (
                        profit_pts / max(0.001, original)
                    )
                    log(
                        f"[TP1] {symbol} {direction} fechou {actual_close:.2f} "
                        f"de {original:.2f} @ profit {profit_pts:.1f}pts "
                        f"(>= {tp1_r}*ATR={tp1_r*atr:.1f}) "
                        f"→ resta {new_remaining:.2f}"
                    )
                    # Telegram alert (não-spam). Se notify_telegram falhar
                    # (Telegram offline), não bloqueia trading.
                    try:
                        notify_telegram(
                            f"🎯 *TP1* {symbol} {tf}\n"
                            f"• Fechou {actual_close:.2f} contrato(s) "
                            f"(de {original:.2f}, {tp1_pct*100:.0f}%)\n"
                            f"• Trail restante @ atr_trail_mult={params.get('atr_trail_mult', 2.0)}"
                        )
                    except Exception:
                        pass
                else:
                    log(
                        f"[TP1] partial_close falhou ticket={pos['entry_ticket']}: "
                        f"{tp1_result.get('error', '?')} — mantém estado"
                    )
                    # NÃO seta tp1_done — próxima barra tenta de novo.
            except Exception as exc:
                log(f"[TP1] erro inesperado: {exc!r} — mantém estado")

    # ════════════════════════════════════════════════════════════════════
    # Wave 880.B4 (2026-07-19): TP2 — segundo fechamento parcial em R*ATR.
    # Dispara UMA vez por posição APÓS TP1, em profit >= tp2_r * atr.
    # Fecha fração tp2_pct do que resta (remaining_volume). O restante
    # segue sob trailing (que já está em atr_trail_mult tighter pós-TP1).
    # Default tp2_r=2.0, tp2_pct=0.5 — alinhado com backtest_v944.py.
    # ════════════════════════════════════════════════════════════════════
    tp2_r = params.get("tp2_r", 2.0)
    tp2_pct = params.get("tp2_pct", 0.5)
    if (
        not pos.get("tp2_done", False)
        and pos.get("tp1_done", False)
        and atr > 0
        and profit_pts >= tp2_r * atr
        and pos.get("remaining_volume", pos["volume"]) > 0
        and 0 < tp2_pct < 1
    ):
        # tp2_pct: fração do REMAINING (não do original) a fechar.
        current_remaining = pos.get("remaining_volume", pos["volume"])
        close_volume = current_remaining * tp2_pct
        actual_close = min(close_volume, current_remaining)
        # Wave 880.B3 fix: normalizar pro volume_step (mesmo bug do TP1).
        _vs = _get_volume_step(symbol)
        actual_close = _normalize_partial_volume(actual_close, _vs)
        if actual_close <= 0:
            pos["tp2_done"] = True  # idempotente — volume fracionário
            log(
                f"[TP2-SKIP] {symbol} {direction} volume fracionário "
                f"(remaining={current_remaining} × tp2_pct={tp2_pct} = "
                f"{close_volume:.2f} < step {_vs}) — sem TP2 parcial possível"
            )
        else:
            try:
                from mt5.mt5_error_recovery import safe_partial_close
                tp2_result = safe_partial_close(
                    symbol, pos["entry_ticket"], actual_close,
                )
                if tp2_result.get("status") in ("ok", "already_closed"):
                    new_remaining = current_remaining - actual_close
                    if tp2_result.get("status") == "already_closed":
                        new_remaining = 0.0
                    pos["remaining_volume"] = new_remaining
                    pos["tp2_done"] = True
                    log(
                        f"[TP2] {symbol} {direction} fechou {actual_close:.2f} "
                        f"de {current_remaining:.2f} @ profit {profit_pts:.1f}pts "
                        f"(>= {tp2_r}*ATR={tp2_r*atr:.1f}) "
                        f"→ resta {new_remaining:.2f}"
                    )
                    try:
                        notify_telegram(
                            f"🎯 *TP2* {symbol} {tf}\n"
                            f"• Fechou {actual_close:.2f} contrato(s) "
                            f"(de {current_remaining:.2f}, {tp2_pct*100:.0f}% do restante)\n"
                            f"• Restante segue sob trailing apertado"
                        )
                    except Exception:
                        pass
                else:
                    log(
                        f"[TP2] partial_close falhou ticket={pos['entry_ticket']}: "
                        f"{tp2_result.get('error', '?')} — mantém estado"
                    )
            except Exception as exc:
                log(f"[TP2] erro inesperado: {exc!r} — mantém estado")

    # Tempo de posição em minutos (check_interval = 30s por padrão)
    check_interval = CONFIG.get("check_interval", 30)
    pos_minutes = bar_count * check_interval / 60

    # Parâmetros de proteção temporal
    # Wave 880.B3: breakeven default 10→20 — antes era agressivo demais e
    # whipsawava vencedoras lentas. Backtest usa 0 (desligado); 20 é meio-termo.
    breakeven_min = params.get("breakeven_minutes", 20)
    time_trail_min = params.get("time_trail_minutes", 20)
    max_pos_min = params.get("max_position_minutes", 60)
    trail_act = params.get("trail_activate", 1.0)
    # Wave 880.B1: BUG CRÍTICO — antes este `trail_dist_cfg = trail_distance`
    # vinha DEPOIS do bloco "if tp1_done: trail_dist_cfg = atr_trail_mult"
    # acima, sobrescrevendo silenciosamente o tighter trail pós-TP1. Agora
    # o default é trail_distance, e SE tp1_done E atr_trail_mult estiver
    # explicitamente setado no config, aperta (comportamento documentado).
    trail_dist_cfg = params.get("trail_distance", 0.4)
    if pos.get("tp1_done"):
        _tp1_trail_mult = params.get("atr_trail_mult", None)
        if _tp1_trail_mult is not None:
            trail_dist_cfg = _tp1_trail_mult  # tighter trail pós-TP1
            log(f"[TP1_TRAIL] {symbol} trail_dist_cfg → atr_trail_mult={_tp1_trail_mult} (pós-TP1)")
    hard_exit_min = params.get("hard_exit_minutes", 45)  # FORÇA exit a mercado após X min

    # ===== FORCED EXIT — fecha posição a mercado após hard_exit_min =====
    # Previne desastres como #66 (WDO -R$566 em 375min) e #104 (BIT -R$901 em 104min)
    # Wave 880.B2: hard_exit agora CONDICIONAL a PnL — alinhado com
    # backtest_v944.py:429. Antes o live fechava TUDO aos 45min, matando
    # vencedoras no auge. Agora só força saída a mercado se a posição NÃO
    # estiver em lucro (profit_pts <= 0). Vencedoras seguem sob trailing/EOD.
    # Justificativa: vencedora com 2×ATR de lucro aos 44min não deve ser
    # sacrificada; perdedora/flutuante perto de zero sim (proteção anti-desastre).
    if pos_minutes >= hard_exit_min and profit_pts <= 0:
        log(f"[HARD_EXIT] {symbol} {direction} — {pos_minutes:.0f}min >= {hard_exit_min}min E profit {profit_pts:.0f}pts<=0. Fechando a mercado.")
        try:
            close_result = safe_close(symbol)
            if close_result and close_result.get("status") == "ok":
                # PnL será calculado no próximo ciclo quando servidor fechar
                _volume = pos.get("volume", "?")
                _ticket = pos.get("entry_ticket", "?")
                _ts = datetime.now().strftime("%H:%M:%S")
                _entry_price = pos.get("entry_price", 0)
                # Estimar PnL rápido do hard exit
                _point_map = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
                _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                        "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                        "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
                _pv = _point_map.get(_root, 1.0)
                if direction == "BUY":
                    _pnl_est = (current_price - _entry_price) * _pv
                else:
                    _pnl_est = (_entry_price - current_price) * _pv
                _pnl_emoji = "🟢" if _pnl_est > 0 else "🔴" if _pnl_est < 0 else "⚪"
                notify_telegram(
                    f"⏱️ *HARD EXIT* {symbol} {tf}\n"
                    f"• {direction} | {_pnl_emoji} R$ {_pnl_est:+.2f} (est.)\n"
                    f"• Entrada: {_entry_price:.2f} → Saída: {current_price:.2f}\n"
                    f"• Volume: {_volume} contrato(s) | Ticket: {_ticket}\n"
                    f"• Motivo: Tempo máximo ({pos_minutes:.0f}min >= {hard_exit_min}min)\n"
                    f"• PnL Dia: R$ {state.daily_pnl:+.2f} | {_ts}"
                )
                return  # posição será detectada como fechada no próximo ciclo
            else:
                log(f"[HARD_EXIT] Falha ao fechar {symbol}: {close_result}")
        except Exception as e:
            log(f"[HARD_EXIT] Erro: {e}")

    # ===== TRAILING POR LUCRO (original) =====
    if not trail_on and atr > 0 and profit_pts >= trail_act * atr:
        trail_on = True
        pos["trail_on"] = True
        log(f"[TRAIL] Ativado trailing {symbol} | Lucro: {profit_pts:.0f} pts ({profit_pts/atr:.1f}x ATR)")

    # ════════════════════════════════════════════════════════════════════
    # Wave 880.A1 (2026-07-19): PROFIT-LOCK por R — port do
    # backtest_v944.py:396-399. Quando o lucro atinge profit_lock_r × risco
    # inicial (1R = distância absoluta do SL em pontos), move SL pra
    # entry + 1 tick (zero-loss lock). Default 0.0 = desligado (BIT_M5).
    # be_applied é compartilhado com o BREAKEVEN abaixo — mutuamente
    # exclusivos (quem disparar primeiro sela o SL). Igual ao backtest.
    # ════════════════════════════════════════════════════════════════════
    be_applied = False
    profit_lock_r = params.get("profit_lock_r", 0.0)
    if (
        not trail_on
        and profit_lock_r > 0
        and atr > 0
        and abs(sl_pts) > 0
        # Wave 880.J (2026-07-20): não re-tentar PROFIT_LOCK no mesmo ciclo se
        # já foi tentado (sucesso OU falha). Antes, falha deixava pos["sl_pts"]
        # inalterado e o gate re-disparava a cada 30s, gerando storm de
        # INVALID_STOPS (6 modifies em 6 min hoje). Ver diagnóstico (D).
        and not pos.get("profit_lock_attempted")
    ):
        _one_r_pts = abs(sl_pts)  # captura antes de possivelmente mutar
        if profit_pts >= profit_lock_r * _one_r_pts:
            # Wave 880.J (2026-07-20): lock_pts deve respeitar trade_stops_level
            # do broker. Antes era -max(1, int(1/point_val)) = -1 p/ WIN,
            # resultando em SL a 1pt do entry — sempre rejeitado pelo MT5
            # ("Invalid stops"). Agora usa distância segura (stops_level + 10%).
            try:
                from mt5.mt5_orchestrator import info as _mt5_info
                _info_data = _mt5_info(symbol)
                _stops_level = (_info_data.get("trade_stops_level", 0)
                                if _info_data and "error" not in _info_data else 0)
                # stops_level em unidades nativas; converter pra pts (executor units).
                _min_lock_pts = max(int(_stops_level / point_val) if point_val > 0 else 1, 1)
                # +10% de margem (arredondado p/ cima) p/ evitar rejeição por tick.
                _min_lock_pts = int(_min_lock_pts * 1.1) + 1
            except Exception:
                # Fallback conservador: 50pts (valor histórico do broker p/ WIN).
                _min_lock_pts = 50
            # sl_pts NEGATIVO sinaliza profit-lock (cmd_modify é sign-aware):
            # BUY SL = entry - lock_pts*point_val → lock_pts negativo = SL acima.
            lock_pts = -_min_lock_pts
            # Wave 880.B2 fix: gate PRÉ-ENVIO contra stop_level do broker. Mesmo
            # com _min_lock_pts derivado do stops_level, a leitura pode voltar
            # degradada (stops_level=0) e o modify seria rejeitado (Invalid stops
            # ×255 no dia 05/08). Se está dentro do stop_level, SKIP (mantém SL
            # anterior válido) em vez de mandar modify que vai falhar.
            if _within_stop_level(symbol, lock_pts, entry_price, direction, point_val):
                pos["profit_lock_attempted"] = True
                log(
                    f"[PROFIT_LOCK-SKIP] {symbol} {direction} | lock_pts={lock_pts} "
                    f"dentro do stop_level do broker — modify pulado, SL anterior mantido"
                )
            else:
                # Marca ANTES do modify para não re-disparar em caso de falha.
                pos["profit_lock_attempted"] = True
                result = safe_modify_sl_with_emergency_close(
                    symbol, pos["entry_ticket"], lock_pts, entry_price, direction
                )
                if result.get("status") == "ok":
                    pos["sl_pts"] = lock_pts
                    sl_pts = lock_pts  # refresh local: trailing/BREAKEVEN não afrouxam
                    be_applied = True
                    log(
                        f"[PROFIT_LOCK] {symbol} {direction} | profit {profit_pts:.0f}pts "
                        f">= {profit_lock_r}×{_one_r_pts}pts (1R) | SL → entry+{_min_lock_pts}pts (lock)"
                    )
                else:
                    log(
                        f"[PROFIT_LOCK] {symbol} {direction} | profit {profit_pts:.0f}pts "
                        f"| falhou modify (lock_pts={lock_pts}): {result.get('error', '?')} — "
                        f"não retenta até próxima posição"
                    )

    # ===== PROTEÇÃO 1: BREAKEVEN =====
    # Após X minutos sem trailing, move SL pra entry + custo mínimo
    # sl_pts é ALWAYS POSITIVE (distância). cmd_modify converte pra preço.
    # (be_applied já inicializado no bloco PROFIT_LOCK acima — mutuamente exclusivos.)
    if not trail_on and not be_applied and pos_minutes >= breakeven_min and atr > 0:
        cost_pts = int(5 / point_val)  # custo aprox (comissão + slippage) em pontos
        # Wave 880.B2 fix: gate PRÉ-ENVIO contra stop_level do broker. O
        # breakeven usa cost_pts (ex: 5pts p/ WIN), que fica DENTRO do stop_level
        # real e era rejeitado ("Invalid stops" ×255 no dia 05/08). Se dentro do
        # stop_level, SKIP (mantém SL de entrada, mais largo, que é válido).
        _be_sl_pts_candidate = cost_pts
        if _within_stop_level(symbol, _be_sl_pts_candidate, entry_price, direction, point_val):
            log(
                f"[BREAKEVEN-SKIP] {symbol} {direction} após {pos_minutes:.0f}min | "
                f"be_sl_pts={_be_sl_pts_candidate} dentro do stop_level do broker "
                f"— modify pulado, SL anterior mantido"
            )
        elif direction == "BUY":
            # BUY: breakeven = SL no entry + custo (SL = entry + custo*point)
            be_sl_pts = cost_pts  # positivo → SL = entry - cost_pts*point_val (abaixo de entry mas perto)
            if be_sl_pts < abs(sl_pts):  # menor distância = SL mais apertado = melhor
                result = safe_modify_sl_with_emergency_close(symbol, pos["entry_ticket"], be_sl_pts, entry_price, direction)
                if result.get("status") == "ok":
                    pos["sl_pts"] = be_sl_pts
                    sl_pts = be_sl_pts  # CRITICAL: refresh local para trailing não afrouxar
                    be_price = entry_price + cost_pts * point_val
                    log(f"[BREAKEVEN] {symbol} BUY após {pos_minutes:.0f}min | SL → {be_price:.2f} ({be_sl_pts}pts)")
                    be_applied = True
        else:
            # SELL: breakeven = SL no entry - custo (SL = entry + cost_pts*point_val)
            be_sl_pts = cost_pts
            if be_sl_pts < abs(sl_pts):
                result = safe_modify_sl_with_emergency_close(symbol, pos["entry_ticket"], be_sl_pts, entry_price, direction)
                if result.get("status") == "ok":
                    pos["sl_pts"] = be_sl_pts
                    sl_pts = be_sl_pts  # CRITICAL: refresh local para trailing não afrouxar
                    be_price = entry_price - cost_pts * point_val
                    log(f"[BREAKEVEN] {symbol} SELL após {pos_minutes:.0f}min | SL → {be_price:.2f} ({be_sl_pts}pts)")
                    be_applied = True

    # ===== PROTEÇÃO 2: TIME-BASED TRAILING =====
    # Após Y minutos, ativa trailing mesmo sem atingir trail_activate
    if not trail_on and pos_minutes >= time_trail_min and profit_pts > 0:
        trail_on = True
        pos["trail_on"] = True
        log(f"[TIME_TRAIL] Ativado por tempo {symbol} após {pos_minutes:.0f}min | Lucro: {profit_pts:.0f}pts")

    # ===== Wave N+5A (2026-07-08): DAY-TRADE FLATTEN =====
    # Para day-trade (intent=True), força flatten quando faltam <buffer
    # minutos pro EOD (CLOSE_TIME). Default buffer=15min para evitar
    # slippage caótico no último minuto.
    if _is_day_trade_flatten_window(symbol, tf, pos_minutes):
        log(f"[DAY_TRADE_FLATTEN] {symbol} {direction} — perto do EOD, fechando a mercado")
        try:
            _dd_close = safe_close(symbol)
            if _dd_close and _dd_close.get("status") == "ok":
                notify_telegram(
                    f"🕒 *DAY-TRADE FLATTEN* {symbol} {tf}\n"
                    f"• {direction} | Posição: {pos_minutes:.0f}min\n"
                    f"• Motivo: buffer pre-EOD (day-trade intent)\n"
                    f"• PnL dia: R$ {state.daily_pnl:+.2f}"
                )
                return  # posição será detectada como fechada no próximo ciclo
        except Exception as exc:
            log(f"[DAY_TRADE_FLATTEN] Falha: {exc!r}")

    # ===== TRAILING STOP =====
    # Calcula novo SL mas NÃO aplica no state até MT5 confirmar.
    # Convenção: sl_pts é ALWAYS POSITIVO (distância em executor units).
    # cmd_modify: BUY sl = entry - sl_pts*point, SELL sl = entry + sl_pts*point
    new_sl_pts = None  # candidato (só aplica se MT5 confirmar)
    if trail_on and atr > 0:
        # Proteção 3: após max_position_minutes, trailing mais agressivo
        if pos_minutes >= max_pos_min:
            trail_dist = 0.3 * atr  # agressivo
        else:
            trail_dist = trail_dist_cfg * atr

        if direction == "BUY":
            new_sl_price = best - trail_dist
            old_sl_price = entry_price - abs(sl_pts) * point_val
            if new_sl_price > old_sl_price and new_sl_price > 0:
                # sl_pts SIGNED: positivo=abaixo entry (loss), negativo=acima entry (profit lock)
                # cmd_modify: BUY SL = entry - sl_pts*point → sl_pts negativo = SL acima entry ✓
                new_sl_pts = int((entry_price - new_sl_price) / point_val)
        else:
            new_sl_price = best + trail_dist
            old_sl_price = entry_price + abs(sl_pts) * point_val
            if new_sl_price < old_sl_price and new_sl_price > 0:
                # SELL: sl_pts signed. cmd_modify: SELL SL = entry + sl_pts*point
                # sl_pts negativo = SL abaixo entry (profit lock) ✓
                new_sl_pts = int((new_sl_price - entry_price) / point_val)

    # ===== BOLLINGER: Tight trailing na banda oposta =====
    if strategy == "BOLLINGER":
        bb_mid = pos.get("bb_mid", 0)
        if bb_mid > 0:
            if direction == "BUY" and current_price >= bb_mid and profit_pts > 0:
                tight_dist = 0.3 * atr
                tight_sl_price = best - tight_dist
                old_sl_price = entry_price - abs(sl_pts) * point_val
                if tight_sl_price > old_sl_price and tight_sl_price > 0:
                    tight_pts = int((entry_price - tight_sl_price) / point_val)
                    if tight_pts != 0 and (new_sl_pts is None or tight_pts < new_sl_pts):
                        new_sl_pts = tight_pts
            elif direction == "SELL" and current_price <= bb_mid and profit_pts > 0:
                tight_dist = 0.3 * atr
                tight_sl_price = best + tight_dist
                old_sl_price = entry_price + abs(sl_pts) * point_val
                if tight_sl_price < old_sl_price and tight_sl_price > 0:
                    tight_pts = int((tight_sl_price - entry_price) / point_val)
                    if tight_pts != 0 and (new_sl_pts is None or tight_pts < new_sl_pts):
                        new_sl_pts = tight_pts

    # Enviar modify SL pro MT5 — só atualiza state se MT5 confirmar
    # sl_pts pode ser NEGATIVO (profit-lock). cmd_modify já suporta:
    #   BUY: SL = entry - pts*point (pts<0 → SL acima entry ✓)
    #   SELL: SL = entry + pts*point (pts<0 → SL abaixo entry ✓)
    # Wave 880.B2 fix: gate PRÉ-ENVIO contra stop_level do broker. O trailing
    # gera new_sl_pts apertados (próximos do best_price) que ficam dentro do
    # stop_level real → "Invalid stops" ×255 no dia 05/08. Se dentro do
    # stop_level, SKIP o modify (mantém SL anterior válido, que é mais largo).
    if (new_sl_pts is not None and new_sl_pts != 0 and new_sl_pts != sl_pts
            and not _within_stop_level(symbol, new_sl_pts, entry_price, direction, point_val)):
        try:
            result = safe_modify_sl_with_emergency_close(symbol, pos["entry_ticket"], new_sl_pts, entry_price, direction)
            if result.get("status") == "ok":
                pos["sl_pts"] = new_sl_pts
                log(f"[TRAIL] SL atualizado no MT5: {symbol} ticket={pos['entry_ticket']} → SL={new_sl_pts} pts")
            else:
                log(f"[TRAIL] Falha modify SL: {result.get('error', '?')} (mantido {abs(sl_pts)}pts)")
        except Exception as e:
            log(f"[TRAIL] Erro modify SL: {e}")

    # Verificar se posição ainda existe no MT5 (Fase 2.5 — via truth layer)
    _open_pos = _truth.get_open_positions()
    mt5_tickets = [str(p.ticket) for p in _open_pos]

    if str(pos["entry_ticket"]) not in mt5_tickets:
        log(f"[FECHADO PELO SERVIDOR] {symbol} | Ticket {pos['entry_ticket']}")

        # Bruno 2026-06-30: pegar PnL REAL do MT5 (broker-truth) ao invés de calcular
        # localmente. Se histórico disponível, usar profit do deal out. Fallback para
        # cálculo local só se histórico indisponível. Defesa contra DB lock que perce PnL.
        # Fase 2.5: via truth layer (cache 2s) ao inves de chamar orchestrator direto.
        profit = None
        _profit_source = "fallback local"  # Wave 880.G: rastreia origem p/ note honesta
        # FIX 2026-07-26 (P0 bug dados — Qwen Code + Hermes): o loop original
        # dava break no PRIMEIRO deal com position_id match — que é o deal de
        # ENTRADA (mesma direção, profit=0, price=entry_price). O deal de SAÍDA
        # (direção oposta, profit real) vinha depois e era ignorado. Resultado:
        # current_price = entry_price → exit_price = entry_price → PnL = -fees.
        # Fix: selecionar o deal de direção OPOSTA (= fechamento), como o
        # reconcile já fazia corretamente em L4091 (_want_type).
        try:
            _entry_ticket = str(pos.get("entry_ticket") or "").strip()
            _deals = _truth.get_position_history(
                symbol=symbol, days=1,
                position=_entry_ticket if _entry_ticket else None,
            )
            _exit_deal = None
            for d in _deals:
                if str(d.position_id) != str(pos["entry_ticket"]):
                    continue
                # Deal de saída tem direção OPOSTA à posição
                _want_dir = "SELL" if direction == "BUY" else "BUY"
                if d.direction == _want_dir:
                    _exit_deal = d  # mantém o último (sem break)
            if _exit_deal is not None:
                profit = float(_exit_deal.profit) + float(_exit_deal.commission) + float(_exit_deal.swap)
                if _exit_deal.price:
                    current_price = float(_exit_deal.price)
                _profit_source = "broker-truth via MT5 history (exit deal)"
                log(f"[FECHADO PELO SERVIDOR] MT5 history: profit=R$ {profit:.2f} price={_exit_deal.price}")
        except (OSError, ValueError, KeyError) as _he:
            # Wave 880.I: except estreito — TypeError/AttributeError propagam.
            log(f"[HISTORY FAIL] usando PnL local: {_he}")

        # Wave 880.J: broker-truth via EA events (mt5_trade_events) — mais
        # confiável que o history Wine, que falha em fechamentos server-side e
        # deixa current_price stale (== entry → alerta com Entrada==Saída). Se
        # achar o deal de saída real, usa preço e PnL do broker (corrige alerta,
        # exit_price no DB e daily_pnl de forma consistente). Só roda se o
        # history não resolveu (profit ainda None) — fallback seguro.
        if profit is None:
            _evt = _lookup_exit_event_from_db(symbol, direction, pos.get("entry_ticket"))
            if _evt is not None:
                profit = _evt["profit"] + _evt["commission"] + _evt["swap"]
                if _evt["price"]:
                    current_price = _evt["price"]
                _profit_source = "broker-truth via EA events (mt5_trade_events)"
                log(f"[FECHADO PELO SERVIDOR] EA events: profit=R$ {profit:.2f} "
                    f"price={_evt['price']} deal={_evt['ticket']}")

        # Fallback: cálculo local se history falhou
        # Wave 880.G (Bruno 2026-07-20): usar get_multiplier() (R$/ponto) ao
        # invés de point_val (preço/ponto). Antes inflava notes 5x para WIN
        # (point_val=1.0 era tratado como R$/pt, mas o real é 0.20 — mini).
        # point_val segue correto nos demais usos (conversão preço↔ponto p/
        # SL/breakeven/trailing); só aqui ele era o multiplicador errado.
        if profit is None:
            from core.vt_trade_log import get_multiplier
            _brl_per_pt = get_multiplier(symbol)
            if direction == "BUY":
                profit = (current_price - entry_price) * _brl_per_pt
            else:
                profit = (entry_price - current_price) * _brl_per_pt
            log(f"[FECHADO PELO SERVIDOR] PnL local fallback: R$ {profit:.2f} (mult={_brl_per_pt})")

        # Calcular preço teórico do SL que foi enviado pro broker — habilita
        # diagnóstico de slippage real (exit_price - exit_sl_price). Sem isso,
        # 100% dos SL_SERVIDOR ficam sem exit_sl_price gravado no DB
        # (bug identificado 2026-06-26, 276/309 trades afetados).
        if direction == "BUY":
            exit_sl_price = entry_price - abs(pos.get("sl_pts", 0)) * point_val
        else:
            exit_sl_price = entry_price + abs(pos.get("sl_pts", 0)) * point_val

        # FIX 2026-07-26 (P0 bug dados): se o history do MT5 falhou e o
        # current_price do tick é igual ao entry_price (tick stale / mercado
        # parado / Wine bug), o exit_price gravado fica == entry (dist=0) e o
        # PnL calculado pelo log_exit vira só a comissão (-R$1,20). O fill
        # real do SL é desconhecido, mas o melhor proxy é o exit_sl_price
        # (preço teórico do SL enviado ao broker). Usa ele como fallback
        # quando current_price não é confiável.
        _exit_price_for_db = current_price
        if _profit_source == "fallback local" and abs(current_price - entry_price) < point_val:
            # Tick retornou preço ≈ entry — não é o fill real do SL.
            _exit_price_for_db = exit_sl_price
            log(f"[SL_SERVIDOR] tick stale (price≈entry), usando exit_sl_price={exit_sl_price:.0f} como exit_price")

        exit_result = log_exit(
            trade_log_id,
            exit_price=_exit_price_for_db,
            exit_reason="SL_SERVIDOR",
            exit_ticket="server",
            exit_sl_price=exit_sl_price,
            swap=0,
            notes=f"FECHADO PELO SERVIDOR | PnL real: R${profit:.2f} ({_profit_source})",
            close_source="MT5_SERVER_SL",
        )
        pnl = 0  # default para quando exit_result falha
        if exit_result:
            pnl = exit_result.get("net_pnl", 0)
            state.daily_pnl += pnl
            state.trade_count += 1
            if pnl > 0:
                state.wins += 1
                state.consecutive_losses[symbol] = 0  # reset streak per symbol
                state.halt_until.pop(symbol, None)  # clear halt on win
                # Wave N+4B (2026-07-08): reset per-(sym,dir) cooldown
                # streak on WIN.
                _reset_loss_cooldown_counter(symbol, direction)
            else:
                state.losses += 1
                state.consecutive_losses[symbol] = state.consecutive_losses.get(symbol, 0) + 1
                # Wave N+4B: incrementa per-(sym,dir) cooldown counter.
                _bump_loss_cooldown_counter(symbol, direction)
                if state.consecutive_losses[symbol] >= state.max_consecutive_losses:
                    from datetime import timedelta
                    state.halt_until[symbol] = datetime.now() + timedelta(hours=1)
                    log(f"[HALT] {symbol}: {state.consecutive_losses[symbol]} perdas consecutivas! Pausado até {state.halt_until[symbol].strftime('%H:%M')}")
                    notify_telegram(
                        f"🛑 *HALT TRADING*\n"
                        f"{symbol}: {state.consecutive_losses.get(symbol, 0)} perdas consecutivas\n"
                        f"PnL diário: R$ {state.daily_pnl:+.2f}\n"
                        f"Aguardando reset (próximo dia)"
                    )

        log(f"[FECHADO] {symbol} {tf} — PnL estimado R\\${pnl:+.2f}, notificando Telegram...")
        # Notificação de fechamento — informações completas do servidor
        _ts = datetime.now().strftime("%H:%M:%S")
        _volume = pos.get("volume", "?")
        _ticket = pos.get("entry_ticket", "?")
        _entry_time = pos.get("entry_time")
        _duracao = ""
        if _entry_time:
            try:
                if isinstance(_entry_time, str):
                    _entry_time = datetime.fromisoformat(_entry_time)
                _duracao_min = (datetime.now() - _entry_time).total_seconds() / 60
                _duracao = f" | Duração: {_duracao_min:.0f}min"
            except Exception:
                pass
        _pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        notify_telegram(
            f"⚡ *Fechou {symbol} {tf}*\n"
            f"• {direction} | {_pnl_emoji} R$ {pnl:+.2f}\n"
            f"• Entrada: {entry_price:.2f} → Saída: {_exit_price_for_db:.2f}\n"
            f"• Volume: {_volume} contrato(s) | Ticket: {_ticket}\n"
            f"• Motivo: SL atingido no servidor{_duracao}\n"
            f"• PnL Dia: R$ {state.daily_pnl:+.2f} | {_ts}"
        )

        del state.positions[key]
        state.save()  # persistir após fechamento
        return


def close_all_and_report(close_source: str = "EOD_CLOSE", exit_reason: str = "EOD_16:45",
                         notes: str = "Fechamento obrigatório de intraday"):
    """Fecha todas posições e gera relatório diário.

    Wave 880.H (Bruno 2026-07-20): adicionados parâmetros close_source/exit_reason/notes
    para reaproveitar no Profit Lock (close_source='PROFIT_LOCK'). Defaults preservam
    o comportamento original do EOD 16:45.
    """
    log(f"=== FECHANDO TUDO ({close_source}) ===")

    _closed_count = 0  # Wave 880.H: contador para Profit Lock reportar.
    for key, pos in list(state.positions.items()):
        parts = key.rsplit("_", 1)
        symbol = parts[0]
        tf = parts[1] if len(parts) > 1 else "M5"

        result = safe_close(symbol)
        log(f"Fechei {symbol}: {result}")

        tick_data = tick(symbol)
        exit_price = tick_data.get("bid", pos["entry_price"]) if tick_data else pos["entry_price"]

        exit_result = log_exit(
            pos["trade_log_id"],
            exit_price=exit_price,
            exit_reason=exit_reason,
            exit_ticket=close_source.lower(),
            notes=notes,
            close_source=close_source,
        )
        if exit_result:
            pnl = exit_result.get("net_pnl", 0)
            state.daily_pnl += pnl
            state.trade_count += 1
            _closed_count += 1  # Wave 880.H
            if pnl > 0:
                state.wins += 1
                state.consecutive_losses[symbol] = 0
            else:
                state.losses += 1
                state.consecutive_losses[symbol] = state.consecutive_losses.get(symbol, 0) + 1

    time.sleep(2)

    # ── Sweep broker-truth (Wave 10/08, Bruno 2026-08-10) ──────────────
    # O loop acima fecha apenas posições que o bot CONHECE (state.positions).
    # Se uma posição existe no MT5 mas o bot perdeu o tracking (ex: state
    # reconstruído vazio — incidente do 1º dia REAL 05/08, C2 do lesson
    # learning), ela ficaria aberta após o EOD. close_all() do orchestrator
    # enumera TODAS as posições via positions_get() e fecha — independente
    # do state. Rede de segurança: qualquer coisa que sobrou, fecha.
    try:
        _sweep = close_all()
        _sweep_closed = 0
        if isinstance(_sweep, dict):
            _sweep_closed = int(_sweep.get("closed", 0) or 0)
        if _sweep_closed > 0:
            log(f"[EOD-SWEEP] close_all() fechou {_sweep_closed} posição(ões) "
                f"remanescente(s) não rastreadas pelo state (ghost/orphan)")
        else:
            log(f"[EOD-SWEEP] close_all() OK — nenhuma posição remanescente no MT5")
    except Exception as _e_sweep:
        log(f"[EOD-SWEEP] close_all() falhou (não-crash): {_e_sweep}")

    # ── Double-check broker-truth (Wave 10/08, Bruno 2026-08-10) ────────
    # "Manda o comando, aguarda um pouco, analisa e manda de novo."
    # Não confiar em UMA única chamada de close: o MT5 pode ter rejeitado
    # silenciosamente ou a posição pode ter sido reaberta no intervalo.
    # Padrão: fechar → aguardar → status() (analisar) → se sobrou, fechar de novo.
    _max_double_check = 3
    for _dc in range(_max_double_check):
        try:
            _dc_st = status()
            _dc_positions = _dc_st.get("positions", []) or []
        except Exception as _e_dc:
            log(f"[EOD-DOUBLE-CHECK] status() falhou (tentativa {_dc+1}): {_e_dc}")
            break
        if not _dc_positions:
            log("[EOD-DOUBLE-CHECK] MT5 flat confirmado (0 posições abertas)")
            break
        _n_open = len(_dc_positions)
        _sym_list = ", ".join(
            str(p.get("symbol", "?")) for p in _dc_positions[:5]
        )
        log(f"[EOD-DOUBLE-CHECK] ⚠️ {_n_open} posição(ões) AINDA aberta(s) "
            f"({_sym_list}) — tentativa {_dc+1}/{_max_double_check}, fechando de novo")
        try:
            _dc_close = close_all()
            _dc_closed = int(_dc_close.get("closed", 0) or 0) if isinstance(_dc_close, dict) else 0
            log(f"[EOD-DOUBLE-CHECK] close_all() #2 fechou {_dc_closed} posição(ões)")
        except Exception as _e_dc2:
            log(f"[EOD-DOUBLE-CHECK] close_all() #2 falhou (não-crash): {_e_dc2}")
        time.sleep(5)  # aguardar o MT5 processar antes de re-analisar

    # Importar deals reais do MT5 e sincronizar taxas.
    # Wave 880.I (2026-07-20): iterar por ticket via history(position=) — o
    # caminho bulk history() (sem args) retorna [] no Wine MT5 (bug documentado).
    # state.positions ainda contém os entry_ticket neste ponto (clear é depois).
    try:
        _all_deals = []
        _tickets_seen = set()
        for _pos in state.positions.values():
            _tk = str(_pos.get("entry_ticket") or "").strip()
            if not _tk or _tk in _tickets_seen:
                continue
            _tickets_seen.add(_tk)
            try:
                _hist = history(position=_tk)
                if isinstance(_hist, dict):
                    _all_deals.extend(_hist.get("history") or [])
            except Exception as _e_h:
                log(f"[WARN] history(position={_tk}) falhou: {_e_h}")
        if _all_deals:
            n_imported = import_mt5_history(_all_deals)
            log(f"MT5 history: {n_imported} deals importados ({len(_tickets_seen)} tickets)")
            # Sync fees/swap reais do MT5 para os trades do dia
            n_synced = sync_fees_from_mt5()
            log(f"Fees sync: {n_synced} trades atualizados com taxas reais")
        else:
            log("MT5 history: 0 deals (nenhum ticket retornou deals — pode ser Wine MT5 bug)")
    except Exception as e:
        log(f"[WARN] import_mt5_history falhou: {e}")

    # Limpar positions do state para não gerar fantasmas no watchdog
    state.positions.clear()
    state.closed = True
    state.save()

    today = datetime.now().strftime("%d/%m/%Y")
    # Fonte primária: EA events (broker-truth, dedup por deal_ticket).
    # Fallback: tabela trades do DB (pode ter fantasmas/duplicatas).
    # Bruno 27/07: relatório deve usar EA como fonte principal.
    ev_summary = get_events_daily_summary()
    if ev_summary is not None:
        n_trades_db = ev_summary["total_trades"]
        net_pnl_db = ev_summary["net_pnl"]
        best = ev_summary["best_trade"]
        worst = ev_summary["worst_trade"]
        wr = ev_summary["win_rate"]
        _report_source = "EA"
        _ev_wins = ev_summary["wins"]
        _ev_losses = ev_summary["losses"]
        _ev_by_symbol = ev_summary.get("by_symbol", {})
    else:
        db_summary = {}
        try:
            db_summary = get_daily_summary()
            n_trades_db = db_summary["total_trades"]
            net_pnl_db = db_summary["net_pnl"]
            best = db_summary["best_trade"]
            worst = db_summary["worst_trade"]
            wr = db_summary["win_rate"]
        except Exception:
            n_trades_db = state.trade_count
            net_pnl_db = state.daily_pnl
            best = worst = 0
            wr = 0
        _report_source = "DB"
        _ev_wins = db_summary.get("wins", state.wins)
        _ev_losses = db_summary.get("losses", state.losses)
        _ev_by_symbol = {}

    try:
        mt5_status = status()
        acc = mt5_status.get("account", {})
        balance = acc.get("balance", 0)
        equity = acc.get("equity", 0)
        margin_free = acc.get("free_margin", 0)
    except Exception:
        balance = equity = margin_free = 0

    pnl_emoji = "🟢" if net_pnl_db >= 0 else "🔴"
    src_label = "EA broker-truth" if _report_source == "EA" else "DB (fallback)"
    msg = (
        f"📊 *RELATÓRIO DIÁRIO Vibe-Trading*\n"
        f"📅 {today}\n"
        f"{'─' * 25}\n\n"
        f"🤖 *Estado da Conta*\n"
        f"• Saldo: R$ {balance:,.2f}\n"
        f"• Equity: R$ {equity:,.2f}\n"
        f"• Margem livre: R$ {margin_free:,.2f}\n\n"
    )

    msg += (
        f"📈 *Operações do Dia* _(fonte: {src_label})_\n"
        f"• Trades: {n_trades_db}\n"
        f"• Acertos: {_ev_wins} ({wr:.0f}%)\n"
        f"• Erros: {_ev_losses}\n"
    )

    if n_trades_db > 0:
        msg += (
            f"• Melhor: R$ {best:+.2f}\n"
            f"• Pior: R$ {worst:+.2f}\n"
        )

    msg += f"\n{pnl_emoji} *PnL Líquido: R$ {net_pnl_db:+.2f}*\n"

    # Breakdown por símbolo (EA events)
    if _ev_by_symbol:
        msg += "\n📊 *Por Símbolo*\n"
        for sym, data in _ev_by_symbol.items():
            sym_wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            icon = "🟢" if data["pnl"] > 0 else "🔴" if data["pnl"] < 0 else "⚪"
            msg += f"  {icon} {sym}: {data['trades']}t | WR {sym_wr:.0f}% | R$ {data['pnl']:+.2f}\n"

    try:
        # Fase 2.5: via truth layer (cache 2s) ao inves de status() direto.
        # get_truth_from_mt5() ja usa status() internamente, mas truth layer
        # adiciona contrato tipado e centralizacao.
        _open_now = _truth.get_open_positions()
        mt5_positions = [
            {
                "ticket": p.ticket, "symbol": p.symbol, "type": p.direction,
                "volume": p.volume, "price_open": p.price_open, "profit": p.profit,
            }
            for p in _open_now
        ]
    except Exception:
        mt5_positions = []

    if mt5_positions:
        msg += f"\n📂 *Posições Abertas* ({len(mt5_positions)})\n"
        for p in mt5_positions:
            direction = p.get("type", "?")
            pnl_pos = p.get("profit", 0)
            emoji_pos = "🟢" if pnl_pos >= 0 else "🔴"
            msg += (
                f"  {emoji_pos} {p['symbol']} {direction} "
                f"@ {p['price_open']:,.0f} "
                f"SL={p['sl']:,.0f} "
                f"PnL=R$ {pnl_pos:+.2f}\n"
            )
    else:
        msg += "\n✅ Nenhuma posição aberta.\n"

    notify_telegram(msg)
    log(f"Relatório: {n_trades_db} trades, PnL R$ {net_pnl_db:+.2f}")
    return _closed_count  # Wave 880.H: número de posições efetivamente fechadas.


def run_once():
    init_db()
    _init_strategy_utils()
    load_strategies()
    log("Verificação única...")
    check_and_trade()
    log("Verificação concluída")


def recover_open_positions():
    try:
        mt5_status = status()
    except Exception as e:
        log(f"[RECOVER] Erro ao conectar MT5: {e}")
        return

    mt5_positions = mt5_status.get("positions", [])
    if not mt5_positions:
        log("[RECOVER] Nenhuma posição aberta no MT5")
        return

    log(f"[RECOVER] {len(mt5_positions)} posições abertas no MT5, verificando...")

    import sqlite3
    conn = sqlite3.connect("vt_trades.db", timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    open_in_db = {r["entry_ticket"]: r for r in conn.execute(
        "SELECT * FROM trades WHERE exit_time IS NULL "
        "AND (strategy IS NULL OR strategy NOT LIKE '%[EXCLUDED]%')"
    ).fetchall()}
    conn.close()

    recovered = 0
    for p in mt5_positions:
        symbol = p["symbol"]
        symbol_root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                         "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                         "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else None
        if symbol_root not in CONFIG["symbols"]:
            continue

        ticket = str(p.get("ticket", ""))
        comment = p.get("comment", "")
        if comment != "VibeTrading":
            continue

        already_managed = any(str(v.get("entry_ticket")) == ticket for v in state.positions.values())
        if already_managed:
            continue

        db_trade = open_in_db.get(ticket) or (open_in_db.get(int(ticket)) if str(ticket).isdigit() else None)
        strategy = _get_strategy(symbol_root)
        params = _get_params(symbol_root)

        if db_trade:
            tf = db_trade["timeframe"] or "M5"
            direction = db_trade["direction"]
            entry_price = db_trade["entry_price"]
            atr = 0
            sl_pts = 0
            try:
                sig = json.loads(db_trade["signal_detail"]) if db_trade["signal_detail"] else {}
                atr = sig.get("atr", 0) or 0
                sl_pts = sig.get("sl_pts", 0) or 0
                if sig.get("strategy"):
                    strategy = sig["strategy"]
            except Exception:
                pass

            if not atr or not sl_pts:
                bars = fetch_bars(symbol, tf, CONFIG["bars_count"])
                atr_calc = calculate_atr(bars, params.get("atr_period", 14)) if bars else 0
                atr = atr_calc or 200
                sl_pts = _calc_sl(symbol, atr)
        else:
            direction = "BUY" if p["type"] in (0, "BUY") else "SELL"
            entry_price = p["price_open"]
            tf = "M5"
            bars = fetch_bars(symbol, tf, CONFIG["bars_count"])
            atr = calculate_atr(bars, params.get("atr_period", 14)) if bars else 200
            sl_pts = _calc_sl(symbol, atr, params)

        # Wave 880.B1 fix (Bruno 2026-08-05): entry_price pode vir 0/None do DB
        # ou do MT5 (XPMT5-PRD price_open=0). Fallback p/ price_current; se ainda
        # <= 0, manage_position tem guard defensivo (L2687) que pula a gestão.
        _cur_for_fb = p.get("price_current", 0.0) or 0.0
        if not entry_price or float(entry_price) <= 0:
            entry_price = float(_cur_for_fb) if _cur_for_fb else entry_price

        current_price = p.get("price_current", entry_price)
        if direction == "BUY":
            profit_pts = current_price - entry_price if entry_price else 0
            best = max(entry_price, p.get("high_price", current_price)) if entry_price else current_price
        else:
            profit_pts = entry_price - current_price if entry_price else 0
            best = min(entry_price, p.get("low_price", current_price)) if entry_price else current_price

        trail_on = atr > 0 and profit_pts >= params.get("trail_activate", 1.5) * atr

        # BB mid pra estratégia Bollinger
        bb_mid = 0
        if strategy == "BOLLINGER" and bars:
            _, bb_mid, _ = calculate_bollinger(bars, params.get("bb_period", 20), params.get("bb_std", 2.0))

        # Estimar bar_count real a partir do tempo de abertura da posição
        # (999 causava max_pos_min imediato → fechava posições recuperadas injustamente)
        check_interval = CONFIG.get("check_interval", 30)
        _entry_ts = None
        if db_trade and db_trade["entry_time"]:
            try:
                _dt = datetime.strptime(db_trade["entry_time"], "%Y-%m-%d %H:%M:%S")
                _entry_ts = _dt.timestamp()
            except Exception:
                pass
        if _entry_ts is None and p.get("time"):
            _entry_ts = float(p["time"])
        if _entry_ts:
            _age_min = max(0, (datetime.now().timestamp() - _entry_ts) / 60)
            _est_bar_count = int(_age_min / (check_interval / 60))
        else:
            _est_bar_count = 1  # fallback conservador

        state.positions[f"{symbol}_{tf}"] = {
            "direction": direction,
            "entry_price": entry_price,
            "entry_ticket": ticket,
            "sl_pts": sl_pts,
            "atr": atr,
            "trail_on": trail_on,
            "best_price": best,
            "bar_count": _est_bar_count,
            "trade_log_id": db_trade["id"] if db_trade else None,
            "recovered": True,
            "strategy": strategy,
            "bb_mid": bb_mid,
        }

        sig_key = f"{symbol}_{tf}_{direction}"
        state.last_signals[sig_key] = {
            "ts": datetime.now(),
            "close": entry_price,
            "bar_ts": None,
            "ticket": int(ticket) if str(ticket).isdigit() else ticket,
            "direction": direction,
        }
        recovered += 1
        log(f"[RECOVER] ✅ {direction} {symbol} {tf} @ {entry_price:.2f} SL={sl_pts} trail={'on' if trail_on else 'off'} [{strategy}]")

    if recovered:
        notify_telegram(
            f"🔄 *Recuperadas {recovered} posição(ões)*\n"
            f"O bot está gerenciando trailing/SL normalmente"
        )


def _resolve_orphan_closes():
    """
    Bruno 2026-07-01 — Terceira defesa da trindade anti-orphan.

    PROBLEMA REAL (trades #2069 #2073 #2074 #2075 — 01/07/2026):
        O autotrader abriu 4-5 trades que viraram GHOST no DB com PnL=0.
        O bot fechou essas posições via SL_SERVIDOR (MT5 fechou sozinho)
        mas o PnL nunca chegou no DB porque:
          (a) reconcile_positions_with_mt5 (commit ce026460) detectou drift
              ANTES de close()/_persist_close_to_db rodar → marcou GHOST
              com PnL=0.
          (b) O bot nunca chamou close() para esses tickets (MT5 fechou
              sozinho via server-side SL), então _persist_close_to_db
              (commit dc447fd6) nunca rodou.
        Resultado: trades com PnL grande ficaram de FORA do intraday report
        (que exclui GHOST).

    SOLUÇÃO — close resolution pass a cada tick:
        1. Lista trades no DB com exit_time IS NULL AND entry_ticket NOT NULL.
        2. Verifica tickets abertos no MT5 (via status()).
        3. Para cada ticket NÃO em MT5 (servidor fechou sozinho):
           a) Pega deal mais recente desse position_id via history().
           b) Se exit_time IS NULL → UPDATE completo com PnL REAL do broker:
              exit_time=now, exit_price=deal.price, gross_pnl=deal.profit,
              net_pnl=broker_net (profit+commission+swap), close_reason=
              'SERVER_CLOSE_RESOLVED'.
           c) Se exit_time IS NOT NULL (já reconciliado por reconcile_positions_
              with_mt5 ou vt_history_reconcile): só atualiza PnL SE o novo
              valor for diferente (re-reconcile preserva exit_reason original).

    IDEMPOTÊNCIA:
        - close_source LIKE 'ORPHAN_CLOSE_RESOLVED_%' (já foi resolvido
          por esta função) → skip.
        - strategy LIKE '%[HISTORY_RECONCILE_%' já tratado por vt_history_
          reconcile; esta função PRESERVA exit_reason/exit_time do reconcile.
        - exit_time IS NULL garante UPDATE completo, exit_time IS NOT NULL
          garante UPDATE cirúrgico (só PnL).

    SEGURANÇA:
        - Failure-safe: try/except em todas as chamadas externas
          (status/history/DB). NUNCA crasha o bot.
        - Custo: 1 status() + N history() por tick (~30s). Aceitável.
        - Não conflita com _persist_close_to_db de close() manual: aquele
          sempre vê exit_time IS NULL quando bot chamou close() por último
          e preenche corretamente. Esta função cobre o caso onde bot NÃO
          chamou close() (SL_SERVIDOR).

    NÃO FAZ:
        - NÃO sobrescreve exit_reason se já estiver setado (preserva
          GHOST/SL_SERVIDOR/TP_SERVIDOR/HISTORY_RECONCILE* já gravados).
        - NÃO toca trades com entry_ticket IS NULL (orfaos sem ticket).
        - NÃO insere novos trades — só atualiza existentes.
    """
    stats = {"checked": 0, "resolved": 0, "skipped_legit": 0,
             "skipped_no_history": 0, "errors": 0, "updated_pnl": 0}
    conn = None

    try:
        # ─── 1. Ler DB: trades abertos com ticket ───
        try:
            conn = sqlite3.connect("vt_trades.db", timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
        except Exception as _e_db:
            log(f"[ORPHAN-RESOLVE] DB connect falhou (skip): {_e_db}")
            return stats

        try:
            open_trades = conn.execute("""
                SELECT id, entry_ticket, symbol, direction, entry_price,
                       entry_time, volume, multiplier, strategy, exit_time,
                       close_source, gross_pnl, net_pnl
                FROM trades
                WHERE (
                    -- Caminho A: exit_time IS NULL (posição fantasma,
                    -- MT5 fechou sozinho e nunca chamou close())
                    (exit_time IS NULL
                     AND entry_ticket IS NOT NULL
                     AND entry_ticket != '')
                    OR
                    -- Caminho B: já fechado por reconcile_positions_with_mt5
                    -- (GHOST/RECONCILE) mas PnL pode ter ficado zerado.
                    -- Re-reconcile: pega PnL real do broker-truth sem
                    -- sobrescrever exit_time/exit_reason.
                    (exit_time IS NOT NULL
                     AND close_source = 'RECONCILE'
                     AND (close_source IS NULL
                          OR close_source NOT LIKE 'ORPHAN_CLOSE_RESOLVED_%'))
                )
                ORDER BY id
            """).fetchall()
        except Exception as _e_q:
            log(f"[ORPHAN-RESOLVE] DB select falhou: {_e_q}")
            try:
                conn.close()
            except Exception:
                pass
            return stats

        if not open_trades:
            try:
                conn.close()
            except Exception:
                pass
            return stats

        stats["checked"] = len(open_trades)

        # ─── 2. Ler MT5: quais tickets estão abertos AGORA? ───
        try:
            mt5_status = status()
        except Exception as _e_st:
            log(f"[ORPHAN-RESOLVE] status() falhou (skip tick): {_e_st}")
            try:
                conn.close()
            except Exception:
                pass
            return stats

        if not isinstance(mt5_status, dict):
            try:
                conn.close()
            except Exception:
                pass
            return stats

        mt5_tickets = set()
        for p in (mt5_status.get("positions") or []):
            if isinstance(p, dict) and p.get("ticket"):
                mt5_tickets.add(str(p.get("ticket")))

        # ─── 3. Iterar trades abertos no DB ───
        # Agrupa símbolos para reduzir chamadas history() (1 por símbolo,
        # não 1 por trade)
        tickets_to_resolve = []  # [(row, ticket_str), ...]
        for row in open_trades:
            ticket_str = str(row["entry_ticket"] or "").strip()
            if not ticket_str:
                continue
            if ticket_str in mt5_tickets:
                # Ticket ainda ABERTO no MT5 → posição legítima, não é ghost
                stats["skipped_legit"] += 1
                continue
            # Idempotência: já resolvido por esta função antes?
            cs = row["close_source"] or ""
            if cs.startswith("ORPHAN_CLOSE_RESOLVED"):
                stats["skipped_legit"] += 1
                continue
            tickets_to_resolve.append((row, ticket_str))

        if not tickets_to_resolve:
            try:
                conn.close()
            except Exception:
                pass
            return stats

        # Agrupa tickets por símbolo para fazer 1 history() por símbolo
        symbols_needed = sorted({r["symbol"] for r, _ in tickets_to_resolve
                                 if r["symbol"]})
        # MT5 history por símbolo (cache local)
        deals_by_position = {}  # {position_id_str: deal}
        for sym in symbols_needed:
            try:
                hist = history(symbol=sym, days=2)
                if not isinstance(hist, dict):
                    continue
                deals = hist.get("history") or hist.get("deals") or []
                for d in deals:
                    if not isinstance(d, dict):
                        continue
                    pos_id = str(d.get("position_id") or d.get("entry_id") or "")
                    if not pos_id:
                        continue
                    # Só deals "out" (SELL fechar BUY, ou BUY fechar SELL)
                    d_type = d.get("type")
                    if d_type in (1, "SELL", "SELL_OUT"):
                        deals_by_position[pos_id] = d
                    elif pos_id not in deals_by_position:
                        deals_by_position[pos_id] = d
            except Exception as _e_h:
                log(f"[ORPHAN-RESOLVE] history({sym}) falhou: {_e_h} — tickets deste symbol skip")
                continue

        # ─── 4. Para cada ticket fora do MT5, preencher no DB ───
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        resolve_tag = f"ORPHAN_CLOSE_RESOLVED_{datetime.now().strftime('%H%M%S')}"

        for row, ticket_str in tickets_to_resolve:
            deal = deals_by_position.get(ticket_str)
            if deal is None:
                # Não tem deal no MT5 history → ou posição ainda aberta no
                # MT5 (legítimo) ou history() falhou. Não toca.
                stats["skipped_no_history"] += 1
                continue

            try:
                broker_profit = float(deal.get("profit") or 0)
                broker_commission = float(deal.get("commission") or 0)
                broker_swap = float(deal.get("swap") or 0)
                broker_net = broker_profit + broker_commission + broker_swap
                exit_price = float(deal.get("price") or 0)
                # exit_reason: respeitar o que veio do MT5 se for SL/TP
                mt5_reason = str(
                    deal.get("deal_type") or deal.get("reason") or ""
                ).upper()
                if "SL" in mt5_reason or "STOP" in mt5_reason:
                    new_reason = "SL_SERVIDOR"
                elif "TP" in mt5_reason or "TAKE" in mt5_reason:
                    new_reason = "TP_SERVIDOR"
                else:
                    new_reason = "SERVER_CLOSE_RESOLVED"

                was_open = (row["exit_time"] is None)

                if was_open:
                    # ─── Caminho A: exit_time IS NULL — UPDATE completo ───
                    conn.execute(
                        """
                        UPDATE trades SET
                            exit_time = ?,
                            exit_price = COALESCE(NULLIF(?, 0), entry_price),
                            exit_reason = ?,
                            swap = ?,
                            gross_pnl = ?,
                            net_pnl = ?,
                            notes = COALESCE(notes, '') || ?,
                            close_source = ?,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ? AND exit_time IS NULL
                        """,
                        (
                            now_str,
                            exit_price,
                            new_reason,
                            broker_swap,
                            broker_profit,
                            broker_net,
                            f"\n[{resolve_tag}] ticket={ticket_str} "
                            f"deal.profit=R${broker_profit:+.2f} "
                            f"broker_net=R${broker_net:+.2f}",
                            resolve_tag,
                            row["id"],
                        ),
                    )
                    if conn.total_changes > 0:
                        stats["resolved"] += 1
                        log(
                            f"[ORPHAN-RESOLVE] ✅ #{row['id']} {row['symbol']} "
                            f"{row['direction']} ticket={ticket_str} → "
                            f"closed @ {exit_price:.2f} "
                            f"PnL=R${broker_net:+.2f} ({new_reason})"
                        )
                else:
                    # ─── Caminho B: exit_time IS NOT NULL — UPDATE cirúrgico ───
                    # Já tinha exit_time (reconcile_positions_with_mt5 marcou
                    # GHOST, ou vt_history_reconcile preencheu). Apenas
                    # atualiza PnL se for diferente do que está no DB —
                    # preserva exit_reason/exit_time/close_source originais.
                    current_gross = float(row["gross_pnl"] or 0)
                    current_net = float(row["net_pnl"] or 0)
                    if abs(current_gross - broker_profit) < 0.005 and \
                       abs(current_net - broker_net) < 0.005:
                        # PnL já bate — nada a fazer (idempotente)
                        stats["skipped_legit"] += 1
                        continue
                    conn.execute(
                        """
                        UPDATE trades SET
                            exit_price = COALESCE(NULLIF(?, 0), exit_price),
                            swap = ?,
                            gross_pnl = ?,
                            net_pnl = ?,
                            notes = COALESCE(notes, '') || ?,
                            close_source = ?,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                        """,
                        (
                            exit_price,
                            broker_swap,
                            broker_profit,
                            broker_net,
                            f"\n[{resolve_tag}] PnL re-reconciled "
                            f"(was R${current_net:+.2f} → R${broker_net:+.2f}) "
                            f"ticket={ticket_str}",
                            resolve_tag,
                            row["id"],
                        ),
                    )
                    if conn.total_changes > 0:
                        stats["updated_pnl"] += 1
                        log(
                            f"[ORPHAN-RESOLVE] 🔧 #{row['id']} {row['symbol']} "
                            f"ticket={ticket_str} PnL re-reconciled "
                            f"R${current_net:+.2f} → R${broker_net:+.2f}"
                        )
            except sqlite3.OperationalError as _e_locked:
                stats["errors"] += 1
                log(f"[ORPHAN-RESOLVE] #{row['id']} DB locked, next tick will retry")
                continue
            except Exception as _e_one:
                stats["errors"] += 1
                log(f"[ORPHAN-RESOLVE] #{row['id']} ticket={ticket_str} falhou: {_e_one}")
                continue

        conn.commit()
    except Exception as _e_outer:
        log(f"[ORPHAN-RESOLVE] erro não-tratado (bot continua): {_e_outer}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if stats["resolved"] or stats["updated_pnl"] or stats["errors"]:
        log(
            f"[ORPHAN-RESOLVE] done: checked={stats['checked']} "
            f"resolved={stats['resolved']} pnl_updated={stats['updated_pnl']} "
            f"skipped_legit={stats['skipped_legit']} "
            f"skipped_no_history={stats['skipped_no_history']} "
            f"errors={stats['errors']}"
        )
    return stats


def reconcile_positions_with_mt5():
    """Reconcilia state.positions e DB com posições REAIS no MT5.

    Bruno 2026-07-01: anti-orphan. MT5 é fonte absoluta de verdade.

    ROOT CAUSE DO BUG DOS ORPHANS:
        Em _execute_entry(), após FILLED, o bot faz 3 ações:
        1. validate_order (L1472)
        2. log_entry() no DB (L1650)
        3. state.positions[key] = {...} (L1663)

        Se QUALQUER uma dessas falhar (DB locked, JSON marshal, exception
        qualquer), o exception sobe fora de _execute_entry e o state/DB
        fica INCONSISTENTE com o MT5. A posição está ABERTA no broker mas
        o bot acha que não tem nada. No próximo tick, _defenses_ok() checa
        state.positions (vazio) E MT5 (mas a checagem é por symbol+direction,
        não por ticket — então se vier sinal de OUTRA direção, passa) e abre
        MAIS uma posição. Cada ciclo gera mais orphans.

    COMPORTAMENTO:
        1. Get MT5 positions via status() (já disponível).
        2. Para cada posição no MT5 com magic correto e comment VibeTrading:
           - Se ticket NÃO está em state.positions[ticket]:
             a) Verificar se ticket já existe no DB (entry_ticket).
             b) Se não existe → INSERT OR IGNORE no DB como orphan.
             c) Adicionar em state.positions[f"{symbol}_{tf}"].
             d) Log [RECONCILE] Ingerido orphan.
        3. Para cada position em state.positions:
           - Se ticket NÃO está no MT5:
             a) UPDATE DB: exit_time=now, exit_reason='GHOST', notes.
             b) Remover de state.positions.
             c) Log [RECONCILE] Ghost detectado.
        4. State.save() ao final (best-effort).

    SAFETY:
        - Try/except broad: nunca crasha o bot.
        - Magic filter: 555501 (nossas ordens). Posições de outros EAs
          são ignoradas.
        - Comment filter: 'VibeTrading' (ordens nossas).
        - DB timeouts: passa por baixo de WAL/busy_timeout do sqlite3.
        - Idempotente: rodar N vezes tem mesmo efeito que rodar 1.
    """
    try:
        # sqlite3 já foi importado no topo do módulo (alinhamento com testes)
        # ── 1. Get MT5 positions ──
        try:
            mt5_status = status()
        except Exception as _e_mt5:
            log(f"[RECONCILE] status() falhou (skip reconcile): {_e_mt5}")
            return

        if not isinstance(mt5_status, dict):
            log(f"[RECONCILE] status() retornou tipo inválido {type(mt5_status)} (skip)")
            return

        mt5_positions = mt5_status.get("positions") or []
        if not isinstance(mt5_positions, list):
            log(f"[RECONCILE] positions não é list ({type(mt5_positions)}), skip")
            return

        # Indexar MT5 por ticket (string) — magia + comment
        mt5_by_ticket = {}
        for p in mt5_positions:
            if not isinstance(p, dict):
                continue
            # Filtro: só nossas ordens (magic 555501 + comment VibeTrading)
            magic = p.get("magic", 0)
            try:
                magic_int = int(magic) if magic is not None else 0
            except (ValueError, TypeError):
                magic_int = 0
            if magic_int != 555501:
                continue
            comment = (p.get("comment") or "").strip()
            if comment != "VibeTrading":
                continue
            tk = str(p.get("ticket", ""))
            if tk:
                mt5_by_ticket[tk] = p

        # Indexar state por ticket (entry_ticket dentro do dict)
        state_by_ticket = {}
        for k, v in list(state.positions.items()):
            tk = v.get("entry_ticket")
            if tk is not None:
                state_by_ticket[str(tk)] = (k, v)

        # ── 2. Ingerir orphans: MT5 tem, state não tem ──
        ingested = 0
        try:
            conn = sqlite3.connect("vt_trades.db", timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            try:
                for ticket_str, p in mt5_by_ticket.items():
                    if ticket_str in state_by_ticket:
                        continue  # já gerenciado
                    symbol = p.get("symbol", "")
                    direction = "BUY" if p.get("type") in (0, "BUY") else "SELL"
                    entry_price = float(p.get("price_open") or 0)
                    volume = float(p.get("volume") or 0)
                    # tf default: derivado do symbol (M5) — não temos como saber
                    # exatamente sem histórico, mas é o default conservador
                    tf = "M5"

                    # FIX 2026-07-01 (anti-lixo): entradas do MT5 com dados zerados
                    # ou com ticket vazio devem ser IGNORADAS (skip + warn), nunca
                    # persistidas. O bug original permitia INSERT com
                    # entry_price=0 (default) e sl_pts=0/atr=0, gerando linhas
                    # fake no DB e objetos fake em state.positions. MT5 é fonte
                    # absoluta de verdade — se não tem dado válido, não inventamos.
                    if not symbol or not ticket_str:
                        log(
                            f"[RECONCILE] ticket={ticket_str} symbol={symbol!r} "
                            f"com dado faltando no MT5, skip ingest"
                        )
                        continue
                    if entry_price <= 0 or volume <= 0:
                        log(
                            f"[RECONCILE] ticket={ticket_str} {symbol} com "
                            f"price_open={entry_price} volume={volume} — "
                            f"dado MT5 inválido, skip ingest (não inserir lixo)"
                        )
                        continue

                    # 2a) Verificar se ticket já existe no DB (filtrar
                    # explícito: exit_time IS NULL AND entry_time >= hoje).
                    # Sem esses filtros, uma row de JUNHO (já fechada) podia
                    # ser re-ingerida com strategy antiga e entry_price=
                    # completamente diferente. Foi a causa do objeto fake
                    # WDOQ26_M5 com entry_price=100/ticket=22222.
                    _today_iso = datetime.now().strftime("%Y-%m-%d 00:00:00")
                    row = conn.execute(
                        "SELECT id, strategy FROM trades "
                        "WHERE entry_ticket = ? "
                        "AND exit_time IS NULL "
                        "AND entry_time >= ?",
                        (ticket_str, _today_iso),
                    ).fetchone()
                    trade_id = None
                    strategy_in_db = "VWAP"
                    if row:
                        trade_id = row["id"]
                        strategy_in_db = row["strategy"] or "VWAP"
                    else:
                        # 2b) Inserir no DB (orphan recuperado)
                        try:
                            _multiplier_map = {
                                # W873: broker-truth MT5 (alinhado com watchdog/trade_log)
                                "WIN": 1.0, "WDO": 0.0015, "BIT": 0.01,
                                "DOL": 0.0018, "IND": 1.0, "WSP": 0.01,
                            }
                            _root_pv = next(
                                (r for r in _multiplier_map if r in symbol), "WIN"
                            )
                            _now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            _sig = {
                                "atr": 0, "rsi": 50, "sl_pts": 0,
                                "reconciled": True, "reconciled_at": _now_str,
                            }
                            cur = conn.execute(
                                """
                                INSERT OR IGNORE INTO trades (
                                    symbol, direction, volume, entry_time, entry_price,
                                    entry_sl, entry_ticket, timeframe, strategy,
                                    signal_detail, multiplier, notes
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    symbol, direction, volume, _now_str,
                                    entry_price, p.get("sl") or 0, ticket_str,
                                    tf, "RECONCILED",
                                    json.dumps(_sig, default=str),
                                    _multiplier_map.get(_root_pv, 0.20),
                                    f"RECONCILED_ORPHAN | ingested at {_now_str}",
                                ),
                            )
                            conn.commit()
                            trade_id = cur.lastrowid
                            strategy_in_db = "RECONCILED"
                        except sqlite3.IntegrityError:
                            # Race: outro tick inseriu entre o SELECT e o INSERT.
                            # Pega o id que o outro tick criou.
                            row2 = conn.execute(
                                "SELECT id, strategy FROM trades WHERE entry_ticket = ?",
                                (ticket_str,),
                            ).fetchone()
                            if row2:
                                trade_id = row2["id"]
                                strategy_in_db = row2["strategy"] or "RECONCILED"
                        except Exception as _e_db:
                            log(f"[RECONCILE] DB insert falhou para ticket={ticket_str}: {_e_db}")
                            # Continua — vai tentar colocar em state mesmo assim

                    # 2c) Adicionar ao state.positions
                    # Usar key symbol_tf — pode colidir se 2 TFs do mesmo symbol.
                    # Preferência: tf detectado do tempo de abertura (heurística simples).
                    # Para evitar colisão, vamos usar symbol como key se já não houver
                    # outra posição desse symbol no state.
                    _state_key = None
                    for _existing_k in state.positions.keys():
                        if _existing_k.startswith(f"{symbol}_"):
                            _state_key = _existing_k
                            break
                    if not _state_key:
                        _state_key = f"{symbol}_{tf}"

                    state.positions[_state_key] = {
                        "direction": direction,
                        "entry_price": entry_price,
                        "entry_ticket": ticket_str,
                        "sl_pts": 0,  # desconhecido — será re-sincronizado por manage
                        "atr": 0,
                        "trail_on": False,
                        "best_price": entry_price,
                        "bar_count": 1,
                        "trade_log_id": trade_id,
                        "strategy": strategy_in_db,
                        "entry_time": datetime.now(),
                        "volume": volume,
                        "tf": tf,
                        "reconciled": True,
                    }
                    ingested += 1
                    log(
                        f"[RECONCILE] Ingerido orphan ticket={ticket_str} "
                        f"{symbol} {direction} @ {entry_price:.2f}"
                    )
            finally:
                conn.close()
        except Exception as _e:
            log(f"[RECONCILE] erro na seção ingest: {_e}")

        # ── 3. Detectar ghosts: state tem, MT5 não tem ──
        # FIX 2026-07-01 (anti-lixo): validações rígidas antes de qualquer
        # INSERT/UPDATE. O bug original permitia criar INSERTs fantasma a
        # partir de entradas em state sem symbol/volume/entrada válidos
        # (ex.: state.positions['WDOQ26_M5']={direction:'SELL',
        # entry_price:100, ticket:'22222'} gerava INSERT com symbol do
        # direction e entry_price=100). Agora: ticket vazio/zumbi → só
        # remove do state (não polui DB). entry_price<=0 → skip + warn.
        ghosts = 0
        try:
            conn = sqlite3.connect("vt_trades.db", timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            try:
                for ticket_str, (state_key, pos) in list(state_by_ticket.items()):
                    if ticket_str in mt5_by_ticket:
                        continue  # ainda aberta no MT5
                    # FIX 2026-07-26 (P0 GHOST race — Qwen Code + Hermes):
                    # Grace period de 60s — se a posição entrou há menos de 60s,
                    # o manage_position pode não ter tido tempo de processar o
                    # SL_SERVIDOR/BROKER_CLOSE antes do reconcile rodar. Sem isso,
                    # o reconcile marca GHOST um trade que o manage ia fechar certo.
                    _entry_dt = pos.get("entry_time")
                    if _entry_dt and isinstance(_entry_dt, datetime):
                        _age_s = (datetime.now() - _entry_dt).total_seconds()
                        if _age_s < 60:
                            log(
                                f"[RECONCILE] Grace period: {state_key} ticket={ticket_str} "
                                f"tem {_age_s:.0f}s (< 60s) — pulando ghost check"
                            )
                            continue
                    # FIX: NÃO confiar em pos.get("direction") como symbol.
                    # Sem um symbol real do state/MT5, não inventamos.
                    direction = pos.get("direction", "?")
                    # Tentar extrair symbol do state_key (formato padrão
                    # 'WSPU26_M5' ou 'WINQ26_M5' usado em recover/entry).
                    _symbol_from_key = state_key.rsplit("_", 1)[0] if "_" in state_key else ""
                    # Se state_key contém '_', o chunk antes do último '_' é o symbol.
                    symbol = _symbol_from_key or pos.get("symbol") or "UNKNOWN"
                    entry_price = float(pos.get("entry_price", 0) or 0)
                    trade_log_id = pos.get("trade_log_id")
                    _pos_volume = float(pos.get("volume", 0) or 0)
                    log(
                        f"[RECONCILE] Ghost detectado: state tinha "
                        f"{symbol}/{direction} ticket={ticket_str} "
                        f"@ {entry_price}, MT5 não tem mais"
                    )

                    # 3a) Marcar no DB como GHOST (só se exit_time IS NULL)
                    if trade_log_id:
                        try:
                            _now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            _notes = (
                                f"GHOST_RECONCILED | state tinha, MT5 não tem mais | "
                                f"reconciled_at {_now_str}"
                            )

                            # FIX 1 (Wave 14.3 — 2026-07-14, Bruno): antes de zerar
                            # o PnL do GHOST, consultar MT5 history para tentar
                            # recuperar profit/swap/comissão reais do broker. Se
                            # broker tem o deal de saída com position_id ==
                            # entry_ticket, atualizamos o trade com exit_price,
                            # net_pnl e gross_pnl reais. FAIL-SAFE: se history
                            # não retornar nada (executor Wine quirk), mantém
                            # fallback GHOST pnl=0 — nunca piora o estado atual.
                            _mt5_exit = None
                            try:
                                # FIX 1.1 (Wave 14.3 — 2026-07-14, Bruno): filtrar
                                # por ticket (position=) em vez de symbol+days.
                                # Wine MT5 tem bug: history_deals_get(symbol=...) e
                                # history_deals_get(date_from=...) retornam [] mesmo
                                # com deals reais. Filtrar por position_id funciona.
                                # Wave 880.I (2026-07-20): except agora captura só
                                # erros esperados do MT5/Wine (não TypeError/Attribute
                                # Error de programmer bug, que devem propagar).
                                _mt5_hist = history(position=ticket_str)
                                for _d in _mt5_hist.get("history", []) or []:
                                    if str(_d.get("position_id", "")) == str(ticket_str):
                                        # deal de saída é o de type oposto à direction
                                        _want_type = "SELL" if direction == "BUY" else "BUY"
                                        if _d.get("type") == _want_type:
                                            _mt5_exit = _d
                                            break
                            except (OSError, ValueError, KeyError) as _e_hist:
                                # Erros esperados: Wine indisponível, JSON malformado,
                                # chave ausente em _mt5_hist. TypeError/AttributeError
                                # (signature drift, programmer bug) NÃO são capturados
                                # — propagam e aparecem no log como traceback visível.
                                log(f"[RECONCILE] history lookup falhou (ticket={ticket_str}): {_e_hist}")

                            if _mt5_exit:
                                _exit_price_real = float(_mt5_exit.get("price", 0) or 0)
                                _profit = float(_mt5_exit.get("profit", 0) or 0)
                                _commission = float(_mt5_exit.get("commission", 0) or 0)
                                _swap = float(_mt5_exit.get("swap", 0) or 0)
                                _fee = float(_mt5_exit.get("fee", 0) or 0)
                                _net_pnl = _profit + _commission + _swap + _fee
                                _exit_time_real = _mt5_exit.get("time", _now_str)
                                # FIX 2026-07-26 (P0 timezone — Qwen Code + Hermes):
                                # d.time do MT5 é epoch UTC. datetime.fromtimestamp()
                                # interpreta no fuso do HOST (que pode ser UTC, não BRT).
                                # Fix: converter explicitamente UTC → BRT (UTC-3).
                                try:
                                    if isinstance(_exit_time_real, int) or (
                                        isinstance(_exit_time_real, str) and _exit_time_real.isdigit()
                                    ):
                                        _ts_int = int(_exit_time_real)
                                        from datetime import timezone as _tz
                                        _BRT = _tz(timedelta(hours=-3))
                                        _exit_time_real = (
                                            datetime.fromtimestamp(_ts_int, tz=_tz.utc)
                                            .astimezone(_BRT)
                                            .strftime("%Y-%m-%d %H:%M:%S")
                                        )
                                except Exception:
                                    _exit_time_real = _now_str

                                conn.execute(
                                    """
                                    UPDATE trades SET
                                        exit_time = ?,
                                        exit_price = ?,
                                        exit_reason = 'BROKER_CLOSE',
                                        exit_ticket = ?,
                                        gross_pnl = ?,
                                        fees = ?,
                                        swap = ?,
                                        net_pnl = ?,
                                        notes = COALESCE(notes, '') || ?,
                                        updated_at = datetime('now', 'localtime'),
                                        close_source = 'RECONCILE_HISTORY'
                                    WHERE id = ? AND exit_time IS NULL
                                    """,
                                    (
                                        _exit_time_real, _exit_price_real,
                                        str(_mt5_exit.get("ticket", ticket_str)),
                                        _profit, abs(_commission + _fee), _swap, _net_pnl,
                                        _notes, trade_log_id,
                                    ),
                                )
                                log(
                                    f"[RECONCILE-GHOST-FIX] ticket={ticket_str} "
                                    f"symbol={symbol} PnL real broker=R${_net_pnl:+.2f} "
                                    f"(profit={_profit} swap={_swap} comm={_commission})"
                                )
                            else:
                                # FIX 2026-07-26 (P0 GHOST): antes de marcar GHOST,
                                # checar mt5_trade_events (pipeline EA → CSV → SQLite).
                                # Se o EA registrou o DEAL_ADD OUT desse ticket, temos
                                # o PnL real e NÃO é ghost — é um trade legítimo que o
                                # history do Wine não retornou.
                                _ev_exit = None
                                try:
                                    _ev_conn = sqlite3.connect("vt_trades.db", timeout=5.0)
                                    _ev_conn.execute("PRAGMA busy_timeout=5000")
                                    _ev_conn.row_factory = sqlite3.Row
                                    _ev_row = _ev_conn.execute("""
                                        SELECT deal_price, deal_profit, deal_commission,
                                               deal_swap, event_time
                                        FROM mt5_trade_events
                                        WHERE trans_type = 'DEAL_ADD'
                                          AND deal_entry = 'OUT'
                                          AND position_ticket = ?
                                        ORDER BY event_time DESC LIMIT 1
                                    """, (ticket_str,)).fetchone()
                                    _ev_conn.close()
                                    if _ev_row:
                                        _ev_exit = _ev_row
                                except Exception:
                                    pass

                                if _ev_exit:
                                    # Trade REAL encontrado nos events — não é ghost
                                    _ev_price = float(_ev_exit["deal_price"] or 0)
                                    _ev_profit = float(_ev_exit["deal_profit"] or 0)
                                    _ev_comm = float(_ev_exit["deal_commission"] or 0)
                                    _ev_swap = float(_ev_exit["deal_swap"] or 0)
                                    _ev_net = _ev_profit + _ev_comm + _ev_swap
                                    _ev_time = _ev_exit["event_time"] or _now_str
                                    conn.execute(
                                        """
                                        UPDATE trades SET
                                            exit_time = ?,
                                            exit_price = ?,
                                            exit_reason = 'SL_SERVIDOR',
                                            exit_ticket = ?,
                                            gross_pnl = ?,
                                            fees = ?,
                                            swap = ?,
                                            net_pnl = ?,
                                            notes = COALESCE(notes, '') || ?,
                                            updated_at = datetime('now', 'localtime'),
                                            close_source = 'MT5_EVENTS_RECONCILE'
                                        WHERE id = ? AND exit_time IS NULL
                                        """,
                                        (
                                            _ev_time, _ev_price,
                                            f"events_{ticket_str}",
                                            _ev_profit, abs(_ev_comm), _ev_swap, _ev_net,
                                            f" | RESOLVED_VIA_MT5_EVENTS (não era ghost) | {_notes}",
                                            trade_log_id,
                                        ),
                                    )
                                    log(
                                        f"[RECONCILE-EVENTS-FIX] ticket={ticket_str} "
                                        f"symbol={symbol} PnL events=R${_ev_net:+.2f} "
                                        f"(profit={_ev_profit} swap={_ev_swap} comm={_ev_comm}) "
                                        f"— NÃO era ghost"
                                    )
                                else:
                                    # Fallback FAIL-SAFE: sem history E sem events,
                                    # mantém GHOST pnl=0
                                    conn.execute(
                                        """
                                        UPDATE trades SET
                                            exit_time = ?,
                                            exit_price = COALESCE(NULLIF(exit_price, 0), entry_price),
                                            exit_reason = 'GHOST',
                                            exit_ticket = COALESCE(exit_ticket, 'ghost_reconcile'),
                                            notes = COALESCE(notes, '') || ?,
                                            updated_at = datetime('now', 'localtime'),
                                            close_source = 'RECONCILE'
                                        WHERE id = ? AND exit_time IS NULL
                                        """,
                                        (_now_str, _notes, trade_log_id),
                                    )
                            conn.commit()
                        except Exception as _e_ghost_db:
                            log(f"[RECONCILE] DB UPDATE ghost falhou (trade_id={trade_log_id}): {_e_ghost_db}")
                    else:
                        # FIX: trade_log_id ausente NÃO significa "temos dados
                        # suficientes para inserir um trade fantasma".
                        # Antes: o código inseria um INSERT direto com symbol
                        # = pos.direction (que é 'SELL'/'BUY', um lixo!), entry_price
                        # = 100 e volume = 1.0 (do state fake). Resultado: linha
                        # fantasma no DB com symbol='SELL'/'BUY' e entry_price lixo.
                        # Agora: SÓ insere se symbol E ticket coerentes E entry_price > 0
                        # E volume > 0. Caso contrário, apenas remove do state
                        # (já temos consistência: state limpo, MT5 zerado).
                        # Tickets MT5 reais têm 9-10 dígitos (e.g. 2467898858).
                        # Tickets curtos ou não-numéricos são LIXO deixado pelo
                        # bug anterior — não persistir.
                        _ticket_str_clean = str(ticket_str).strip() if ticket_str else ""
                        _ticket_is_numeric = _ticket_str_clean.isdigit()
                        _ticket_len_ok = len(_ticket_str_clean) >= 6
                        _can_insert_ghost = (
                            symbol not in ("?", "UNKNOWN", "")
                            and entry_price > 0
                            and _pos_volume > 0
                            and _ticket_is_numeric
                            and _ticket_len_ok
                        )
                        if _can_insert_ghost:
                            try:
                                _now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                conn.execute(
                                    """
                                    INSERT OR IGNORE INTO trades (
                                        symbol, direction, volume, entry_time, entry_price,
                                        entry_ticket, exit_time, exit_price, exit_reason,
                                        exit_ticket, notes, close_source, strategy
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        symbol, direction, _pos_volume,
                                        _now_str, entry_price, ticket_str,
                                        _now_str, entry_price, "GHOST",
                                        "ghost_reconcile",
                                        f"GHOST_RECONCILED | state-only | reconciled {_now_str}",
                                        "RECONCILE", "GHOST",
                                    ),
                                )
                                conn.commit()
                            except Exception as _e_g2:
                                log(f"[RECONCILE] DB INSERT ghost falhou (ticket={ticket_str}): {_e_g2}")
                        else:
                            # FIX: state-only ghost SEM dados confiáveis não
                            # vira INSERT. Apenas removemos do state e
                            # logamos warn. Isso impede o ciclo vicioso de
                            # INSERT fantasma propagado pelo reconcile.
                            log(
                                f"[RECONCILE] ⚠️ state-only ghost ticket={ticket_str} "
                                f"symbol={symbol!r} entry_price={entry_price} "
                                f"volume={_pos_volume} SEM trade_log_id e SEM dados "
                                f"válidos — apenas removido do state (DB limpo, "
                                f"nenhum INSERT criado)"
                            )

                    # 3b) Remover do state
                    state.positions.pop(state_key, None)
                    ghosts += 1
            finally:
                conn.close()
        except Exception as _e:
            log(f"[RECONCILE] erro na seção ghost: {_e}")

        # ── 4. Persistir state (best-effort) ──
        # Wave 12 (2026-07-01): sincroniza o cache de truth do SessionState
        # com o mt5_by_ticket que acabamos de computar. Garante que o filtro
        # dentro de state.save() use o MESMO truth (sem chamada Wine adicional
        # e sem race contra mudancas externas no estado do cache).
        try:
            SessionState._mt5_truth_symbols_cache = {p.get("symbol", "").strip()
                                                    for p in mt5_by_ticket.values()
                                                    if p.get("symbol")}
            SessionState._mt5_truth_symbols_ts = time.time()
        except Exception:
            pass
        if ingested > 0 or ghosts > 0:
            try:
                state.save()
            except Exception as _e_save:
                log(f"[RECONCILE] state.save() falhou: {_e_save}")

        if ingested > 0 or ghosts > 0:
            log(
                f"[RECONCILE] ciclo concluído: "
                f"ingested={ingested} ghosts={ghosts} "
                f"mt5_open={len(mt5_by_ticket)} state_managed={len(state.positions)}"
            )
    except Exception as _e_outer:
        # Última barreira: nunca crasha o bot
        log(f"[RECONCILE] erro não-tratado (bot continua): {_e_outer}")


def run_daemon():
    global CONFIG
    global _last_pause_state  # cache de borda do pause file (notify one-shot)
    init_db()
    _init_strategy_utils()
    load_strategies()
    state.started_at = datetime.now()

    # ─── Verificação de dia útil + feriados ───
    ok, motivo = is_trading_day()
    if not ok:
        log(f"⛔ Hoje NÃO é dia de trading: {motivo}")
        notify_telegram(f"⛔ *Mercado fechado hoje*\n📋 Motivo: {motivo}\n💤 Bot aguardando próximo dia útil...")
    else:
        log(f"✅ Hoje é dia de trading ({motivo})")

    # ─── Auto-resolução de vencimento de contratos ───
    # Bruno 2026-07-01: resolve_all_symbols agora é READ-ONLY por padrão.
    # Persistir em disco durante startup do autotrader É PERIGOSO — incidente
    # 2026-07-01 09h30 comeu 95% do config porque algum caller reescreveu
    # o JSON inteiro. Aqui só usamos em memória (retornado pela função) e o
    # loop abaixo já loga contrato+vencimento. Quem precisa persistir (cron
    # pre-flight 8h55) chama resolve_all_symbols(persist=True) explicitamente
    # com autotrader PAUSADO.
    log("📅 Verificando vencimentos dos contratos...")
    resolved = resolve_all_symbols()  # persist=False (default, safe)
    CONFIG = load_effective_config()  # Recarregar com eventuais atualizações externas
    for root, contract in resolved.items():
        _, month, year = _parse_contract_code(contract)
        if month:
            expiry = get_contract_expiry(root, month, year)
            days = 0
            check = date.today()
            while check < expiry:
                if is_trading_day(check)[0]:
                    days += 1
                check += timedelta(days=1)
            log(f"  {root} → {contract} (vence {expiry.strftime('%d/%m/%Y')}, {days} dias úteis)")
        else:
            log(f"  {root} → {contract}")

    # Log das estratégias (por TF — mostra a estratégia real que será executada)
    by_tf = CONFIG.get("strategy_by_tf", {})
    for sym in CONFIG["symbols"]:
        tfs = CONFIG.get("timeframes_by_symbol", {}).get(sym, CONFIG.get("timeframes", []))
        tf_strats = []
        for tf in tfs:
            key = f"{sym}_{tf}"
            strat = by_tf.get(key, CONFIG["strategy"].get(sym, "VWAP"))
            tf_strats.append(f"{tf}={strat}")
        log(f"  {sym}: {' | '.join(tf_strats)}")

    log("=" * 60)
    log("Vibe-Trading Autotrader SPLIT INICIADO")
    log(f"Símbolos: {CONFIG['symbols']}")

    # Wave 1110.C (Bruno 30/07): verifica profit lock no startup.
    # Se lock estava armado antes do restart, loga e mantém bloqueio.
    # Isso garante que o lock sobrevive a restarts do self-heal.
    if CONFIG.get("profit_lock_enabled", False):
        try:
            from core import vt_profit_lock as _pl_startup
            _pl_locked, _pl_state = _pl_startup.is_locked()
            if _pl_locked:
                log(f"🔒 PROFIT LOCK ativo desde {_pl_state.get('armed_at', '?')} "
                    f"(target R$ {_pl_state.get('target', 0):.2f}, "
                    f"PnL no arm R$ {_pl_state.get('armed_pnl', 0):.2f}, "
                    f"closed_n={_pl_state.get('closed_n', 0)}) — novas entradas bloqueadas")
        except Exception as e:
            log(f"⚠️ Falha verificando profit lock no startup: {e}")
    for _s in CONFIG["symbols"]:
        _p = CONFIG.get(_s.lower(), {})
        log(f"{_s}: SL {_p.get('sl_atr_mult', 1.5)}x ATR | Trail {_p.get('trail_activate', 1.5)}x/{_p.get('trail_distance', 0.5)}x ATR")
    # APENAS para o log de startup ('Max(x/dia efetivo)'). NÃO é o gate de
    # trades: o limite real é resolvido por _resolve_max_daily_trades
    # (params_by_tf > ativo > raiz > 15) + teto global min(global_max_daily_trades, 50).
    # A chave risk_management sequer existe no config atual, então cai sempre no
    # fallback CONFIG[symbol].max_daily_trades.
    rm_daily = CONFIG.get("risk_management", {}).get("daily_limits", {})
    wdo_eff = rm_daily.get("max_daily_trades_by_symbol", {}).get("WDO", CONFIG["wdo"]["max_daily_trades"])
    win_eff = rm_daily.get("max_daily_trades_by_symbol", {}).get("WIN", CONFIG["win"]["max_daily_trades"])
    log(f"WDO: Cooldown({CONFIG['wdo']['cooldown_seconds']}s) | Max({wdo_eff}/dia efetivo)")
    log(f"WIN: Cooldown({CONFIG['win']['cooldown_seconds']}s) | Max({win_eff}/dia efetivo)")
    log(f"Volume: {CONFIG['volume']} contrato(s)")
    log("=" * 60)

    _syms = ", ".join(CONFIG["symbols"])
    _strat_summary = []
    _by_tf = CONFIG.get("strategy_by_tf", {})
    for _sym in CONFIG["symbols"]:
        _tfs = CONFIG.get("timeframes_by_symbol", {}).get(_sym, CONFIG.get("timeframes", []))
        _tf_list = []
        for _tf in _tfs:
            _key = f"{_sym}_{_tf}"
            _st = _by_tf.get(_key, CONFIG["strategy"].get(_sym, "VWAP"))
            _tf_list.append(f"{_tf}={_st}")
        _strat_summary.append(f"{_sym}: {', '.join(_tf_list)}")
    _strat_block = "\n".join(f"  {s}" for s in _strat_summary)
    notify_telegram(
        f"🚀 *Vibe-Trading Autotrader*\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"📊 Estratégias por TF:\n{_strat_block}\n"
        f"🎯 Ativos: {_syms}\n"
        f"⏱️ Timeframes: {', '.join(CONFIG.get('timeframes', []))}"
    )

    recover_open_positions()

    # Pause file (Bruno 2026-07-20): se já subiu pausado, avisa uma única vez.
    # Inicializa _last_pause_state para suprimir renotificação no primeiro tick
    # do while True abaixo (que sempre detectaria a "borda" None→True).
    _last_pause_state = _is_paused()
    if _last_pause_state:
        log("⏸️  Daemon subindo em modo PAUSADO (data/autotrader.paused presente)")
        notify_telegram(
            "⏸️ *Daemon iniciou PAUSADO* — novas entradas bloqueadas; "
            "posições existentes serão gerenciadas"
        )

    # Bruno 30/06: defesa #2 — reconciliação no STARTUP (corrige drift após restart).
    # Para cada trade com exit_time IS NULL no DB, busca deal correspondente
    # no MT5 history e atualiza com PnL real do broker. Cobre o caso de
    # restart durante ciclo (vários restarts hoje: 10:13, 10:36, 11:09, 12:11).
    # Best-effort: se MT5 indisponível, loga e segue (autotrader não trava).
    if _HISTORY_RECONCILE_AVAILABLE:
        try:
            log("🔄 Reconciliando drift DB↔MT5 via history (startup)...")
            _recon = reconcile_db_with_mt5_history(
                symbols=CONFIG.get("symbols_resolved") or None,
                days=2,
                log_callable=lambda m: log(m),
            )
            if _recon.get("reconciled", 0) > 0:
                log(f"✅ Startup reconcile: {_recon['reconciled']} drifts corrigidos "
                    f"(checked={_recon['checked']}, still_open={_recon['still_open']})")
                # Re-sync state com DB agora que PnL real mudou
                _sync_daily_pnl_with_db(state)
            # Fechar trades EXCLUDED que ficaram com exit_time NULL
            _excluded_n = _reconcile_pending_excluded(log_callable=lambda m: log(m))
            if _excluded_n:
                log(f"✅ { _excluded_n} trades EXCLUDED auto-fechados")
        except Exception as _e_recon:
            log(f"⚠️  reconcile startup falhou (não-crash): {_e_recon}")

    while True:
        try:
            # Hot reload config + strategies
            # load_effective_config: aplica sidecar de overrides do copilot
            # em runtime (sem persistir em vt_config.json — Bruno 2026-07-01).
            CONFIG = load_effective_config()
            reload_strategies()

            # Pause file (Bruno 2026-07-20): detecta borda de transição para
            # notificar uma única vez via Telegram (não spam a cada 30s).
            # O bloqueio efetivo de novas entradas acontece dentro de
            # check_and_trade() via _is_paused() — aqui só cuidamos do aviso.
            _now_paused = _is_paused()
            if _now_paused != _last_pause_state:
                if _now_paused:
                    try:
                        _mtime_str = datetime.fromtimestamp(
                            PAUSE_FILE.stat().st_mtime
                        ).strftime("%H:%M:%S")
                    except OSError:
                        _mtime_str = "?"
                    log(
                        f"⏸️  PAUSE ativado ({PAUSE_FILE.name} desde {_mtime_str}) "
                        f"— novas entradas bloqueadas, posições abertas seguem gerenciadas"
                    )
                    notify_telegram(
                        f"⏸️ *Autotrader PAUSADO*\n"
                        f"🚫 Novas entradas bloqueadas\n"
                        f"📌 Posições abertas seguem sendo gerenciadas\n"
                        f"⏰ {_mtime_str}"
                    )
                else:
                    log("▶️  PAUSE removido — operação normal retomada")
                    notify_telegram("▶️ *Autotrader RETOMADO* — operação normal")
                _last_pause_state = _now_paused

            # Trailing Profit Lock (Wave 1110 — Bruno 2026-07-23): ratchet
            # progressivo no PnL diário. Ativa em 50% do target, garante
            # piso que sobe linearmente até 100% no target. Se PnL cai
            # abaixo do floor → fecha tudo. Se atinge target → delega ao
            # profit lock full (abaixo).
            if CONFIG.get("profit_lock_enabled", False) and CONFIG.get("trailing_profit_lock_enabled", True):
                try:
                    from core import vt_trailing_profit_lock as _tpl
                    from core import vt_profit_lock as _pl_for_trail
                    _tpl_locked, _ = _pl_for_trail.is_locked()
                    if not _tpl_locked:
                        # Target baseado em volume base do config (Bruno 30/07).
                        # NÃO soma volume das posições abertas (isso multiplica
                        # pelo nº de TFs). Usa o lote configurado: volume=1 →
                        # target R$250, volume=2 → R$500, etc.
                        _tpl_vol_base = float(CONFIG.get("volume", 1.0) or 1.0)
                        _tpl_per_lot = float(CONFIG.get("trailing_target_per_lot", 250.0))
                        _tpl_target = _tpl_per_lot * _tpl_vol_base
                        _tpl_pnl = _pl_for_trail.get_intraday_pnl_total()
                        # Wave 1111: detecta a transição inativo→ativo ANTES do
                        # update (o state é persistido dentro do update_trailing).
                        _tpl_was_active = bool(_tpl.get_trailing_state().get("activated"))
                        _tpl_decision = _tpl.update_trailing(_tpl_pnl, _tpl_target, CONFIG)

                        if _tpl_decision.action == _tpl.TrailingAction.BREACH:
                            log(f"🛑 TRAILING BREACH: PnL R$ {_tpl_pnl:.2f} < floor R$ {_tpl_decision.floor:.2f} "
                                f"(pico R$ {_tpl_decision.peak:.2f}, factor {_tpl_decision.trail_factor:.2f}) "
                                f"— fechando tudo para garantir lucro")
                            _tpl_closed = close_all_and_report(
                                close_source="TRAILING_STOP_LOSS",
                                exit_reason="TRAILING_STOP_LOSS",
                                notes=f"Trailing Profit Lock breach — PnL R$ {_tpl_pnl:.2f} "
                                      f"caiu abaixo do floor R$ {_tpl_decision.floor:.2f} "
                                      f"(pico R$ {_tpl_decision.peak:.2f}). "
                                      f"Fechamento para garantir lucro realizado.",
                            )
                            _tpl.reset_trailing()
                            _pl_for_trail.arm_lock(
                                _tpl_target, armed_pnl=_tpl_pnl, closed_n=_tpl_closed
                            )
                            notify_telegram(
                                f"🛑 *TRAILING PROFIT LOCK — BREACH*\n"
                                f"📉 PnL R$ {_tpl_pnl:.2f} < floor R$ {_tpl_decision.floor:.2f}\n"
                                f"📊 Pico foi R$ {_tpl_decision.peak:.2f} (factor {_tpl_decision.trail_factor:.2f})\n"
                                f"🔒 {_tpl_closed} posição(ões) fechada(s)\n"
                                f"⛔ Novas entradas bloqueadas até amanhã"
                            )

                        elif _tpl_decision.action == _tpl.TrailingAction.TIGHTEN:
                            log(f"📈 TRAILING TIGHTEN: pico R$ {_tpl_decision.peak:.2f} → "
                                f"floor R$ {_tpl_decision.floor:.2f} "
                                f"(progress {_tpl_decision.progress:.0%}, factor {_tpl_decision.trail_factor:.2f})")
                            # Wave 1111 (Bruno 2026-08-11): notifica a PRIMEIRA
                            # ativação do dia no Telegram (antes só log local —
                            # Bruno não recebia nada quando o trailing engajava).
                            if not _tpl_was_active:
                                notify_telegram(
                                    f"🔒 *Trailing Profit Lock ATIVADO*\n"
                                    f"📈 PnL R$ {_tpl_decision.pnl:.2f} ≥ 50% do target "
                                    f"R$ {_tpl_decision.target:.2f}\n"
                                    f"🧱 Floor garantido: R$ {_tpl_decision.floor:.2f} "
                                    f"(pico R$ {_tpl_decision.peak:.2f})\n"
                                    f"⏸️ Novas entradas bloqueadas até target/breach\n"
                                    f"⚡ Fast-check 5s engajado"
                                )

                        # Wave 1110.B: fast-check mode — ativa quando trailing
                        # está engajado (TIGHTEN ou HOLD com activated), desliga
                        # no BREACH/TARGET (posição fechada, lock armado).
                        if _tpl_decision.action in (_tpl.TrailingAction.TIGHTEN,):
                            if not _trailing_fast_mode[0]:
                                _trailing_fast_mode[0] = True
                                _fast_iv = CONFIG.get("trailing_fast_interval", 5)
                                log(f"⚡ FAST-CHECK ATIVADO: loop acelerado para {_fast_iv}s "
                                    f"(trailing profit lock engajado)")
                        elif _tpl_decision.action in (_tpl.TrailingAction.BREACH, _tpl.TrailingAction.TARGET):
                            if _trailing_fast_mode[0]:
                                _trailing_fast_mode[0] = False
                                log("⚡ FAST-CHECK DESATIVADO: trailing concluído (breach/target)")
                        elif _tpl_decision.action == _tpl.TrailingAction.HOLD:
                            # Cobre restart do daemon: trailing já ativado (state
                            # persistido) mas sem novo pico → HOLD. Re-ativa fast.
                            _tpl_st = _tpl.get_trailing_state()
                            if _tpl_st.get("activated") and not _trailing_fast_mode[0]:
                                _trailing_fast_mode[0] = True
                                _fast_iv = CONFIG.get("trailing_fast_interval", 5)
                                log(f"⚡ FAST-CHECK RE-ATIVADO: trailing ativo (pico R$ {_tpl_st.get('peak', 0):.2f}, "
                                    f"floor R$ {_tpl_st.get('floor', 0):.2f})")

                except Exception as _e_tpl_tick:
                    log(f"[TRAILING-PL] tick falhou (não-crash): {_e_tpl_tick}")

            # Profit Lock (Wave 880.H — Bruno 2026-07-20): se o PnL diário
            # (realizado + flutuante) atingir o target adaptativo, fecha tudo
            # a mercado, realiza o lucro e bloqueia novas até o dia seguinte.
            # Defesa contra "o mercado comer o lucro do dia".
            # Gate em check_and_trade() (is_locked) impede novas entradas
            # depois do arm. Liberação automática: state.date != today.
            if CONFIG.get("profit_lock_enabled", False):
                try:
                    from core import vt_profit_lock
                    _pl_locked, _ = vt_profit_lock.is_locked()
                    if not _pl_locked:
                        _pl_target = vt_profit_lock.get_target(CONFIG)
                        _pl_pnl = vt_profit_lock.get_intraday_pnl_total()
                        if _pl_pnl >= _pl_target and _pl_pnl > 0:
                            # ATINGIU — fecha tudo e arma o lock.
                            log(f"🎯 PROFIT TARGET R$ {_pl_target:.2f} atingido "
                                f"(PnL R$ {_pl_pnl:.2f}) — fechando tudo e travando")
                            _pl_closed = close_all_and_report(
                                close_source="PROFIT_LOCK",
                                exit_reason="PROFIT_LOCK",
                                notes=f"Profit Lock armado — target R$ {_pl_target:.2f} "
                                      f"atingido com PnL R$ {_pl_pnl:.2f}. "
                                      f"Fechamento forçado para realizar lucro.",
                            )
                            vt_profit_lock.arm_lock(
                                _pl_target, armed_pnl=_pl_pnl, closed_n=_pl_closed
                            )
                            notify_telegram(
                                f"🎯 *PROFIT LOCK ARMADO*\n"
                                f"💰 Target R$ {_pl_target:.2f} atingido\n"
                                f"📊 PnL realizado+flut R$ {_pl_pnl:.2f}\n"
                                f"🔒 {_pl_closed} posição(ões) fechada(s)\n"
                                f"⛔ Novas entradas bloqueadas até amanhã"
                            )
                except Exception as _e_pl_tick:
                    log(f"[PROFIT-LOCK] tick falhou (não-crash): {_e_pl_tick}")

            if is_close_time() and not state.closed:
                close_all_and_report()
                time.sleep(10)
                continue

            if not is_trading_time():
                if state.started_at:
                    log("Fora do horário de trading. Aguardando...")
                time.sleep(60)
                continue

            # Bruno 01/07: reconcile on each tick — anti-orphan.
            # MT5 é fonte absoluta. state e DB devem refletir MT5.
            # Idempotente: rodar N vezes = rodar 1 vez.
            # Failure-safe: nunca crasha o bot.
            #
            # ORDEM IMPORTANTE (trindade anti-orphan):
            #   1) _resolve_orphan_closes() — preenche trades cujo MT5 fechou
            #      sozinho (SL_SERVIDOR / server-side close). Sincroniza
            #      exit_time/exit_price/PnL via MT5 history. ANTES do
            #      reconcile para evitar marcação de GHOST com PnL=0.
            #   2) reconcile_positions_with_mt5() — anti-orphan state e DB
            #      (commit ce026460). Detecta drift DB↔MT5↔state.
            #   3) _persist_close_to_db() em close() manual (commit dc447fd6)
            #      já é coberto pelos call sites de close/close_all.
            try:
                _resolve_orphan_closes()
            except Exception as _e_orphan:
                log(f"[ORPHAN-RESOLVE] tick falhou (não-crash): {_e_orphan}")

            try:
                reconcile_positions_with_mt5()
            except Exception as _e_recon_tick:
                log(f"[RECONCILE] tick falhou (não-crash): {_e_recon_tick}")

            # Wave 11 — periodic MT5 sync (snapshot vivo via get_truth_from_mt5).
            # Garante que balance/equity/positions locais nunca fiquem off-by-one
            # em relação ao broker. Log [MT5_SYNC] serve de audit trail.
            try:
                _truth = get_truth_from_mt5()
                if _truth.get("ok"):
                    log(
                        f"[MT5_SYNC] balance=R${_truth['balance']:,.2f} "
                        f"equity=R${_truth['equity']:,.2f} "
                        f"margin_free=R${_truth['margin_free']:,.2f} "
                        f"n_pos={_truth['n_positions']} "
                        f"pnl_flut=R${_truth['pnl_flutuante']:+.2f}"
                    )
                    # Re-sincroniza state com truth (positions, equity)
                    if hasattr(state, "update_from_truth"):
                        state.update_from_truth(_truth)

                # GUARD DE CONTA (Bruno 05/08): só opera na conta permitida.
                # Se o MT5 estiver logado numa conta fora da lista (ex: PRD
                # 2257579 relogada manualmente), BLOQUEIA novas operações.
                _login_ativo = _truth.get("account_login") or 0
                if ALLOWED_ACCOUNT_LOGINS and _login_ativo and _login_ativo not in ALLOWED_ACCOUNT_LOGINS:
                    log(
                        f"[GUARD-CONTA] 🚫 Login {_login_ativo} NÃO permitido "
                        f"(permitidos: {ALLOWED_ACCOUNT_LOGINS}). Operação bloqueada."
                    )
                    time.sleep(30)
                    continue
            except Exception as _e_sync:
                log(f"[MT5_SYNC] falha: {_e_sync}")

            check_and_trade()
        except Exception as e:
            log(f"[ERRO] {e}")
            import traceback
            traceback.print_exc()

        # Bruno 30/06: defesa #3 — reconciliação periódica (a cada 10 iterações ≈ 5min).
        # Counter simples em escopo de módulo (counter persiste entre iterações
        # porque run_daemon() é single-threaded e while loop é o mesmo frame).
        if _HISTORY_RECONCILE_AVAILABLE:
            try:
                _iter_counter[0] += 1
                if _iter_counter[0] % 10 == 0:
                    _recon = reconcile_db_with_mt5_history(
                        symbols=CONFIG.get("symbols_resolved") or None,
                        days=2,
                        log_callable=lambda m: log(m),
                    )
                    if _recon.get("reconciled", 0) > 0:
                        log(f"[AUTO-SYNC] Reconciled {_recon['reconciled']} trades with MT5 "
                            f"(checked={_recon['checked']}, still_open={_recon['still_open']})")
                        _sync_daily_pnl_with_db(state)
            except Exception as _e_periodic:
                log(f"[AUTO-SYNC] falha (não-crash): {_e_periodic}")

        # Wave N+3B (2026-07-08): edge estimator update a cada 10 ticks ≈ 5min.
        # Cria snapshot da expectancy viva por (symbol, tf, strategy) no
        # DB edge_estimator; vt_sizing pode usar get_recommended_size_scale
        # para degradação automática de exposição.
        try:
            from core.vt_edge_estimator import update as _ee_update
            for _key, _strat in (CONFIG.get("strategy_by_tf") or {}).items():
                # _key formato "<WIN>_<M5>" — splita no '_' final
                if "_" not in _key:
                    continue
                _sym_root, _tf = _key.rsplit("_", 1)
                if _sym_root in CONFIG.get("symbols", []):
                    _ee_update(_sym_root, _tf, _strat, config=CONFIG)
        except Exception as _e_ee:
            log(f"[EDGE-EST] tick falhou: {_e_ee!r}")

        # Wave 1110.B: fast-check mode — quando trailing profit lock está
        # engajado, acelera o loop de check_interval (30s) para
        # trailing_fast_interval (5s) pra capturar o pico com precisão.
        if _trailing_fast_mode[0]:
            time.sleep(CONFIG.get("trailing_fast_interval", 5))
        else:
            time.sleep(CONFIG["check_interval"])


def main():
    if "--once" in sys.argv:
        run_once()
    elif "--close" in sys.argv:
        init_db()
        close_all_and_report()
    elif "--status" in sys.argv:
        init_db()
        s = status()
        print(json.dumps(s, indent=2, default=str))
    else:
        RESTART_FLAG = "/tmp/vt_autotrader_restart"

        def _close_positions_on_shutdown(state):
            """Wave 15 (Bruno 2026-07-13): substitui close_all_and_report() no
            signal_handler. Mesmo comportamento de fechamento mas rotula
            exit_reason='USER_CLOSE' em vez de 'EOD_16:45' no DB — porque
            SIGTERM/SIGINT não é fim de dia, é encerramento operacional.
            """
            from datetime import datetime as _dt
            log("=== FECHANDO TUDO (USER_CLOSE / shutdown) ===")
            for key, pos in list(state.positions.items()):
                parts = key.rsplit("_", 1)
                symbol = parts[0]
                tf = parts[1] if len(parts) > 1 else "M5"

                result = safe_close(symbol)
                log(f"Fechei {symbol}: {result}")

                tick_data = tick(symbol)
                exit_price = (
                    tick_data.get("bid", pos["entry_price"])
                    if tick_data else pos["entry_price"]
                )

                exit_result = log_exit(
                    pos["trade_log_id"],
                    exit_price=exit_price,
                    exit_reason="USER_CLOSE",
                    exit_ticket="shutdown_bruno",
                    notes=("Fechamento por shutdown do daemon (SIGTERM/SIGINT). "
                           "NÃO é EOD_16:45 — rótulo corrigido pelo Wave 15 em "
                           f"{_dt.now().strftime('%Y-%m-%d %H:%M')}."),
                    close_source="SHUTDOWN_CLOSE",
                )
                if exit_result:
                    pnl = exit_result.get("net_pnl", 0)
                    state.daily_pnl += pnl
                    state.trade_count += 1

            # Relatório EOD fica com o cron monitoring/vt_daily_report.py às 17:00.
            state.closed = True
            state.save()

        def signal_handler(sig, frame):
            # Se flag de restart existe, NÃO fechar posições
            if os.path.exists(RESTART_FLAG):
                log("Restart detectado — NÃO fechando posições abertas")
                os.remove(RESTART_FLAG)
                sys.exit(0)
            log("Sinal de encerramento recebido. Fechando tudo...")
            if state.positions and not state.closed:
                # Wave 15: antes era close_all_and_report() que rotulava tudo
                # como exit_reason='EOD_16:45'. Agora usa _close_positions_on_shutdown
                # que classifica corretamente como USER_CLOSE.
                _close_positions_on_shutdown(state)
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        run_daemon()


if __name__ == "__main__":
    main()
