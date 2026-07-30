"""
monitoring/vt_self_heal.py
==========================
Self-healing monitor (Fase 2.2) — detecta falhas de saúde e auto-cura.

Filosofia (alinhada às 5 Leis de Ouro)
--------------------------------------
- **Conservadora**: se não tem 100% de certeza, só ALERTA (Telegram + log).
- **Lei 2 (Integridade de Escopo)**: NUNCA desabilita símbolo/TF em nome de
  "safety". Auto-cura de trading = restart de processo/MT5, nunca mute de config.
- **Lei 3/4 (Segurança/Garantia)**: nunca envia ordens; nunca toca em SL.
- **Lei 1 (Zero hardcode)**: thresholds ficam em constantes nomeadas no topo
  (controle de infra, não de estratégia) e são override-able via kwargs/CLI.

6 Health Checks
---------------
1. **autotrader_alive** — `pgrep -f core/vt_autotrader.py` + log fresco (<5min)
2. **mt5_reachable** — `mt5_orchestrator.status()` responde em <2s com account_info
3. **db_accessible** — SQLite WAL `SELECT 1` responde
4. **state_fresh** — `/tmp/vt_autotrader_state.json` mtime < 30min (se existir)
5. **config_lock_stale** — `vt_config.json.lock` com idade > 10min (lock órfão)
6. **cron_drift** — `crontab -l` bate com `crontab.txt` (jobs esperados presentes)

Auto-cura (apenas para issues HIGH/CRITICAL)
--------------------------------------------
- autotrader morto         → restart_autotrader() (pkill + start, caminho CORRETO)
- MT5 indisponível         → scripts/start_mt5linux.sh (Xvfb :99 + Wine)
- DB locked                → só ALERTA (kill de tx é destrutivo demais p/ auto)
- state stale              → só ALERTA (state é projection-only, autotrader rebuilda)
- lock config órfão        → rm vt_config.json.lock (com WARN — é seguro, sidecar)
- cron drift               → só ALERTA (reinstalar cron é ação humana)

Uso
---
    # uma rodada (modo cron)
    python3 monitoring/vt_self_heal.py
    # health-check sem auto-curar
    python3 monitoring/vt_self_heal.py --health-check-only
    # loop contínuo (modo foreground)
    python3 monitoring/vt_self_heal.py --loop --interval 300
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# ── sys.path (espelha vt_trade_watchdog.py:32-33) ───────────────────────────
_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "core"))
sys.path.insert(0, str(_PROJECT / "mt5"))

# ── Constantes (Lei 1: infra, não estratégia) ───────────────────────────────
DB_PATH = _PROJECT / "vt_trades.db"
LOG_PATH = Path("/tmp/vt_autotrader.log")
STATE_PATH = Path("/tmp/vt_autotrader_state.json")
CONFIG_PATH = _PROJECT / "vt_config.json"
CONFIG_LOCK_PATH = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".lock")
CRONTAB_FILE = _PROJECT / "crontab.txt"
AUTOTRADER_SCRIPT = _PROJECT / "core" / "vt_autotrader.py"   # CAMINHO CORRETO
MT5_START_SCRIPT = _PROJECT / "scripts" / "start_mt5linux.sh"
START_AUTOTRADER_SH = _PROJECT / "scripts" / "start_autotrader.sh"
TELEGRAM_TARGET = "telegram:-1004284773048"

# Thresholds (override-able via kwargs para testes)
LOG_STALE_MINUTES = 5
STATE_STALE_MINUTES = 30
LOCK_STALE_SECONDS = 300          # espelha vt_config_loader._STALE_LOCK_SECONDS
MT5_TIMEOUT_SEC = 5  # Wave VPS-prep: 2s era apertado demais (Wine idle cold-start ~2-3s)
DB_TIMEOUT_SEC = 5

# Severity ladder
SEV_INFO = "info"
SEV_LOW = "low"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

log = logging.getLogger("vt_self_heal")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [SELF-HEAL] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Dataclasses de report ───────────────────────────────────────────────────
@dataclass
class HealthIssue:
    type: str          # ex. 'autotrader_dead'
    severity: str      # info|low|high|critical
    detail: str
    auto_healable: bool = False


@dataclass
class HealResult:
    issue_type: str
    action: str
    success: bool
    detail: str = ""


@dataclass
class HealthReport:
    checked_at: float = field(default_factory=time.time)
    issues: List[HealthIssue] = field(default_factory=list)
    heal_results: List[HealResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not any(i.severity in (SEV_HIGH, SEV_CRITICAL) for i in self.issues)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == SEV_CRITICAL)

    def add(self, issue: HealthIssue) -> None:
        self.issues.append(issue)


# ── Telegram (cópia do padrão vt_trade_watchdog.py) ─────────────────────────
def _notify_telegram(msg: str) -> bool:
    """Envia alerta via hermes. Nunca levanta — loga em caso de falha."""
    try:
        from core.vt_hermes_helper import hermes_send
        return hermes_send(TELEGRAM_TARGET, msg)
    except Exception as e:  # pragma: no cover — transporte externo
        log.error("notify falhou: %s", e)
        return False


# ── Health checks (6) ───────────────────────────────────────────────────────
def _check_autotrader_alive() -> Optional[HealthIssue]:
    """Check 1: pgrep -f core/vt_autotrader.py + log freshness."""
    # Wave Lifecycle (Bruno 12/07): se NÃO é dia/hora de trading, autotrader
    # "morto" é o comportamento esperado. O daemon sai sozinho pós-EOD
    # (sys.exit após 10min de reconcile); não deve ser ressuscitado fora de
    # horário ou em fim de semana. Sem este guard, o self-heal reinicia o
    # autotrader no sábado/domingo via start_autotrader.sh, e o daemon fica
    # idle ("Fora do horário de trading") até ser morto de novo.
    try:
        from core.vt_calendar import is_trading_day
        from datetime import date as _date
        _ok_day, _motivo = is_trading_day(_date.today())
        if not _ok_day:
            return None  # fim de semana/feriado — autotrader ausente é normal
        # Dentro de dia útil mas fora de horário 09:05-16:50?
        # (5min de buffer pós-close para o daemon terminar o reconcile e sair)
        _now_min = datetime.now().hour * 60 + datetime.now().minute
        _start = 9 * 60 + 5    # 09:05
        _end = 16 * 60 + 50    # 16:50
        if _now_min < _start or _now_min >= _end:
            return None  # fora de horário — autotrader ausente é normal
    except Exception:
        # Fail-open: se o import/check falhar, mantém o pgrep original como
        # rede de segurança. Não quebra o self-heal por causa de um import.
        pass

    try:
        result = subprocess.run(
            ["pgrep", "-f", "core/vt_autotrader.py"],
            capture_output=True, text=True, timeout=5,
        )
        pid = result.stdout.strip()
    except Exception as e:
        return HealthIssue("autotrader_check_error", SEV_LOW,
                           f"pgrep falhou: {e}")
    if not pid:
        return HealthIssue(
            "autotrader_dead", SEV_CRITICAL,
            "Autotrader NÃO está rodando (pgrep vazio).",
            auto_healable=True,
        )
    # Log freshness
    if LOG_PATH.exists():
        age_min = (time.time() - LOG_PATH.stat().st_mtime) / 60
        if age_min > LOG_STALE_MINUTES:
            return HealthIssue(
                "autotrader_log_stale", SEV_HIGH,
                f"Autotrader PID {pid} mas log stale ({age_min:.0f}min). "
                f"Pode estar travado.",
                auto_healable=True,
            )
    return None  # saudável


def _check_mt5_reachable() -> Optional[HealthIssue]:
    """Check 2: mt5_orchestrator.status() responde em <2s."""
    try:
        from mt5.mt5_orchestrator import status as mt5_status
    except Exception as e:
        return HealthIssue("mt5_import_error", SEV_HIGH,
                           f"mt5_orchestrator import falhou: {e}")
    try:
        t0 = time.time()
        data = mt5_status()
        elapsed = time.time() - t0
        if elapsed > MT5_TIMEOUT_SEC:
            return HealthIssue(
                "mt5_slow", SEV_HIGH,
                f"MT5 status() demorou {elapsed:.1f}s (>{MT5_TIMEOUT_SEC}s).",
                auto_healable=True,
            )
        # status() pode vir com error_code se MT5 indisponível
        if isinstance(data, dict) and (
            data.get("error_code") == "NO_ACCOUNT" or data.get("status") == "error"
        ):
            return HealthIssue(
                "mt5_unreachable", SEV_CRITICAL,
                f"MT5 respondeu erro: {data.get('error_code') or data.get('error')}",
                auto_healable=True,
            )
    except subprocess.TimeoutExpired:
        return HealthIssue("mt5_timeout", SEV_CRITICAL,
                           "MT5 status() timeout (>2s).",
                           auto_healable=True)
    except Exception as e:
        return HealthIssue("mt5_unreachable", SEV_CRITICAL,
                           f"MT5 status() exceção: {e}",
                           auto_healable=True)
    return None


# ── Fase 3.2 — Health checks MT5 específicos (4 novos) ─────────────────────
# O check #2 (mt5_reachable) já cobre "ping". Estes 4 aprofundam:
#   #2a margin livre baixa, #2b tick freshness, #2c symbol map, #2d trade_allowed
# Todos read-only e conservadores: só ALERTAM (nunca desabilitam símbolo — Lei 2).
# Reaproveitam o status() já chamado quando possível via _mt5_status_cached().

# Thresholds MT5 (Lei 1: infra, override-able via kwargs em testes)
MT5_MARGIN_FLOOR_PCT = 30.0       # free_margin/equity < 30% → alerta
MT5_TICK_STALE_MINUTES = 5        # tick sem update > 5min → alerta


def _mt5_status_safe() -> Optional[dict]:
    """Chama mt5 status() com timeout curto. None se indisponível."""
    try:
        from mt5.mt5_orchestrator import status as mt5_status
        data = mt5_status()
        if isinstance(data, dict) and data.get("error_code") != "NO_ACCOUNT":
            return data
    except Exception:
        pass
    return None


def _check_mt5_margin() -> Optional[HealthIssue]:
    """Check #2a: margem livre > 30% do equity (proteção contra margin call)."""
    data = _mt5_status_safe()
    if not data:
        return None  # já coberto pelo _check_mt5_reachable
    acct = data.get("account") or {}
    equity = acct.get("equity", 0) or 0
    free = acct.get("free_margin", 0) or 0
    if equity <= 0:
        return None
    pct = (free / equity) * 100
    if pct < MT5_MARGIN_FLOOR_PCT:
        return HealthIssue(
            "mt5_low_margin", SEV_HIGH,
            f"Margem livre {pct:.1f}% < {MT5_MARGIN_FLOOR_PCT}% "
            f"(free=R${free:.0f} equity=R${equity:.0f}). Risco de margin call.",
        )
    return None


def _check_mt5_tick_freshness() -> Optional[HealthIssue]:
    """Check #2b: último tick dos símbolos ativos < 5min (dados frescos)."""
    try:
        from core.vt_config_loader import load_config
        cfg = load_config()
        resolved = cfg.get("resolved_symbols", {}) or {}
        if not resolved:
            return None
        from mt5.mt5_orchestrator import tick as mt5_tick
    except Exception:
        return None
    # Testa o primeiro símbolo resolvido (amostra — não todos, p/ não pesar)
    sample_sym = next(iter(resolved.values()), None)
    if not sample_sym:
        return None
    try:
        tk = mt5_tick(sample_sym)
    except Exception as e:
        return HealthIssue("mt5_tick_error", SEV_LOW,
                           f"tick({sample_sym}) falhou: {e}")
    if not tk or not isinstance(tk, dict):
        return None
    # tick pode trazer 'time' (epoch) — se ausente, não conseguimos validar
    t = tk.get("time")
    if t is None:
        return None
    try:
        age_min = (time.time() - float(t)) / 60
    except (ValueError, TypeError):
        return None
    if age_min > MT5_TICK_STALE_MINUTES:
        # Wave VPS-prep: não alertar tick stale fora do pregão (B3 09:00-17:00)
        from datetime import datetime as _dt
        _now = _dt.now()
        _h = _now.hour + _now.minute / 60
        if _h < 9.0 or _h >= 17.5:
            return None  # pós-mercado — tick parado é normal
        return HealthIssue(
            "mt5_tick_stale", SEV_HIGH,
            f"Tick {sample_sym} stale ({age_min:.0f}min). Dados de mercado "
            f"desatualizados — pode ser feed quebrado.",
        )
    return None


def _check_mt5_symbol_map() -> Optional[HealthIssue]:
    """Check #2c: símbolos resolvidos continuam mapeáveis no MT5."""
    try:
        from core.vt_config_loader import load_config
        cfg = load_config()
        resolved = cfg.get("resolved_symbols", {}) or {}
        if not resolved:
            return None
        from mt5.mt5_orchestrator import info as mt5_info
    except Exception:
        return None
    missing = []
    for root, full_sym in list(resolved.items())[:4]:  # amostra 4
        if root == "IND":
            continue  # IND ignorado (Lei 2 / hard-kill)
        try:
            inf = mt5_info(full_sym)
            if isinstance(inf, dict) and inf.get("error"):
                missing.append(full_sym)
        except Exception:
            missing.append(full_sym)
    if missing:
        return HealthIssue(
            "mt5_symbol_map_broken", SEV_HIGH,
            f"Símbolos não mapeáveis no MT5: {missing}. "
            f"Verificar vt_resolve_symbols / mudança de contrato.",
        )
    return None


def _check_mt5_trade_allowed() -> Optional[HealthIssue]:
    """Check #2d: account_info.trade_allowed True (conta autorizada a operar)."""
    data = _mt5_status_safe()
    if not data:
        return None
    acct = data.get("account") or {}
    if acct.get("trade_allowed") is False:
        return HealthIssue(
            "mt5_trade_blocked", SEV_CRITICAL,
            "trade_allowed=False no MT5. Conta não autorizada a operar "
            "(sessão fechada / auto-trading desligado no terminal).",
        )
    return None


def _check_db_accessible() -> Optional[HealthIssue]:
    """Check 3: SQLite WAL SELECT 1 responde."""
    if not DB_PATH.exists():
        return HealthIssue("db_missing", SEV_HIGH,
                           f"DB não existe: {DB_PATH}")
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=DB_TIMEOUT_SEC)
        conn.execute("SELECT 1")
        conn.close()
    except sqlite3.OperationalError as e:
        return HealthIssue("db_locked", SEV_HIGH,
                           f"DB locked/erro: {e}")
    except Exception as e:
        return HealthIssue("db_error", SEV_HIGH,
                           f"DB exceção: {e}")
    return None


def _check_state_fresh() -> Optional[HealthIssue]:
    """Check 4: state.json mtime < 30min (se existir).

    Nota: state é projection-only (FASE 3). Ausência ou staleness não é fatal —
    o autotrader rebuilda do MT5 a cada tick. Reportamos como LOW (informacional).
    """
    if not STATE_PATH.exists():
        return None  # projection-only: ausência é OK
    age_min = (time.time() - STATE_PATH.stat().st_mtime) / 60
    if age_min > STATE_STALE_MINUTES:
        return HealthIssue(
            "state_stale", SEV_LOW,
            f"State file stale ({age_min:.0f}min). Projection-only — "
            f"autotrader rebuilda do MT5. Informativo.",
        )
    return None


def _check_config_lock_stale() -> Optional[HealthIssue]:
    """Check 5: vt_config.json.lock órfão (idade > 5min)."""
    if not CONFIG_LOCK_PATH.exists():
        return None
    age_sec = time.time() - CONFIG_LOCK_PATH.stat().st_mtime
    if age_sec > LOCK_STALE_SECONDS:
        return HealthIssue(
            "config_lock_stale", SEV_LOW,
            f"Config lock órfão ({age_sec/60:.0f}min). Seguro remover (sidecar).",
            auto_healable=True,
        )
    return None


def _check_cron_drift() -> Optional[HealthIssue]:
    """Check 6: jobs esperados do crontab.txt estão instalados.

    Não comparamos byte-a-byte (cron instalado pode ter paths absolutos
    diferentes). Verificamos apenas que cada job esperado tem um script
    correspondente no `crontab -l` instalado. Só ALERTA (Lei: instalar cron
    é ação humana).
    """
    if not CRONTAB_FILE.exists():
        return HealthIssue("cron_file_missing", SEV_LOW,
                           "crontab.txt não encontrado — sem baseline.")
    try:
        installed = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as e:
        return HealthIssue("cron_read_error", SEV_LOW,
                           f"crontab -l falhou: {e}")
    # Scripts esperados (extraídos das linhas não-comentário do crontab.txt)
    expected_scripts = set()
    for line in CRONTAB_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # linha cron típica: "... /path/to/script.py ..." — pega o último .py/.sh
        for tok in line.split():
            if tok.endswith((".py", ".sh")):
                expected_scripts.add(Path(tok).name)
    missing = [s for s in expected_scripts if s not in installed]
    if missing:
        return HealthIssue(
            "cron_drift", SEV_LOW,
            f"Cron drift — scripts não instalados: {', '.join(sorted(missing))}. "
            f"Reinstalar é ação humana.",
        )
    return None


# ── API pública de health check ─────────────────────────────────────────────
def health_check() -> HealthReport:
    """Roda os 6 health checks. Retorna HealthReport com issues."""
    report = HealthReport()
    for check in (
        _check_autotrader_alive,
        _check_mt5_reachable,
        _check_mt5_margin,            # Fase 3.2
        _check_mt5_tick_freshness,    # Fase 3.2
        _check_mt5_symbol_map,        # Fase 3.2
        _check_mt5_trade_allowed,     # Fase 3.2
        _check_db_accessible,
        _check_state_fresh,
        _check_config_lock_stale,
        _check_cron_drift,
    ):
        try:
            issue = check()
        except Exception as e:  # pragma: no cover — health check nunca crasha
            issue = HealthIssue(check.__name__, SEV_LOW,
                                f"check exception: {e}")
        if issue is not None:
            report.add(issue)
    return report


# ── Auto-cura ───────────────────────────────────────────────────────────────
def _heal_autotrader_dead(issue: HealthIssue) -> HealResult:
    """Restart do autotrader via script idempotente (caminho CORRETO)."""
    try:
        # start_autotrader.sh já faz pkill + start idempotente com caminho certo
        if START_AUTOTRADER_SH.exists():
            subprocess.run(
                ["bash", str(START_AUTOTRADER_SH)],
                capture_output=True, timeout=30,
            )
            action = f"start_autotrader.sh executado"
        else:
            # fallback: pkill + start direto (caminho CORRETO core/vt_autotrader.py)
            subprocess.run(["pkill", "-9", "-f", "core/vt_autotrader.py"],
                           capture_output=True, timeout=10)
            time.sleep(2)
            subprocess.Popen(
                ["python3", str(AUTOTRADER_SCRIPT)],
                cwd=str(_PROJECT),
                stdout=open(LOG_PATH, "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            action = f"pkill + start {AUTOTRADER_SCRIPT.name}"
        time.sleep(5)
        result = subprocess.run(
            ["pgrep", "-f", "core/vt_autotrader.py"],
            capture_output=True, text=True, timeout=5,
        )
        success = bool(result.stdout.strip())
        new_pid = result.stdout.strip().split("\n")[0] if success else None
        return HealResult(issue.type, action, success,
                          f"new_pid={new_pid}")
    except Exception as e:
        return HealResult(issue.type, "restart attempt", False, str(e))


def _heal_mt5_unreachable(issue: HealthIssue) -> HealResult:
    """Restart do MT5/Wine via start_mt5linux.sh."""
    if not MT5_START_SCRIPT.exists():
        return HealResult(issue.type, "skipped (no script)", False,
                          f"{MT5_START_SCRIPT} ausente")
    # Wave VPS-prep: se MT5 + RPyC já estão rodando, não restartar
    # (evita timeout de 60s quando o script espera RPyC que já está up)
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 5001), timeout=2):
            # RPyC já está ouvindo — MT5 provavelmente só está lento (idle)
            return HealResult(issue.type, "skipped (RPyC :5001 already up)", True,
                              "MT5 bridge ativo, latência transitória")
    except (ConnectionRefusedError, OSError, socket.timeout):
        pass  # RPyC realmente down — prosseguir com restart
    try:
        subprocess.run(
            ["bash", str(MT5_START_SCRIPT)],
            capture_output=True, timeout=60,
        )
        time.sleep(10)
        # Recheck rápido
        from mt5.mt5_orchestrator import status as mt5_status
        data = mt5_status()
        ok = (isinstance(data, dict)
              and data.get("error_code") != "NO_ACCOUNT"
              and data.get("status") != "error")
        return HealResult(issue.type, "start_mt5linux.sh", ok,
                          f"status_after={data.get('status') if isinstance(data, dict) else data}")
    except Exception as e:
        return HealResult(issue.type, "mt5 restart attempt", False, str(e))


def _heal_config_lock_stale(issue: HealthIssue) -> HealResult:
    """Remove lock sidecar órfão (seguro — é só sidecar)."""
    try:
        CONFIG_LOCK_PATH.unlink()
        return HealResult(issue.type, "rm vt_config.json.lock", True,
                          "lock sidecar removido")
    except FileNotFoundError:
        return HealResult(issue.type, "rm lock", True, "já não existia")
    except Exception as e:
        return HealResult(issue.type, "rm lock", False, str(e))


# Mapa issue.type → healer (auto-cura só para HIGH/CRITICALexplicitamente curáveis)
_HEALERS = {
    "autotrader_dead": _heal_autotrader_dead,
    "autotrader_log_stale": _heal_autotrader_dead,   # travado → restart tb
    "mt5_unreachable": _heal_mt5_unreachable,
    "mt5_timeout": _heal_mt5_unreachable,
    "mt5_slow": _heal_mt5_unreachable,
    "config_lock_stale": _heal_config_lock_stale,
}


def auto_heal(issue: HealthIssue) -> HealResult:
    """Aplica auto-cura para um issue. Issues não-curáveis viram result noop."""
    healer = _HEALERS.get(issue.type)
    if healer is None or not issue.auto_healable:
        return HealResult(issue.type, "alert_only", False,
                          "auto-cura não disponível — só alerta")
    try:
        return healer(issue)
    except Exception as e:  # pragma: no cover — healer nunca crasha o monitor
        return HealResult(issue.type, "heal exception", False, str(e))


# ── Loop principal ──────────────────────────────────────────────────────────
def run_once(heal: bool = True) -> HealthReport:
    """Uma rodada: health_check + (opcional) auto_heal para HIGH/CRITICAL."""
    report = health_check()
    if not heal:
        return report

    for issue in report.issues:
        if issue.severity not in (SEV_HIGH, SEV_CRITICAL):
            continue
        if not issue.auto_healable:
            continue
        result = auto_heal(issue)
        report.heal_results.append(result)
        log.info("auto-heal %s → %s (success=%s) %s",
                 issue.type, result.action, result.success, result.detail)
        if not result.success:
            # Auto-cura falhou → Telegram para Bruno
            _notify_telegram(
                f"⚠️ [SELF-HEAL] {issue.type}: auto-cura FALHOU\n"
                f"Detail: {issue.detail}\n"
                f"Heal: {result.action} → {result.detail}"
            )
    # Resumo CRITICAL não-curado
    unhealed_critical = [
        i for i in report.issues
        if i.severity == SEV_CRITICAL
        and not any(r.issue_type == i.type and r.success
                    for r in report.heal_results)
    ]
    if unhealed_critical:
        msg = "🚨 [SELF-HEAL] CRITICAL sem auto-cura:\n" + "\n".join(
            f"• {i.type}: {i.detail}" for i in unhealed_critical
        )
        _notify_telegram(msg)
    return report


def run_loop(interval_sec: int = 300) -> None:
    """Loop contínuo (modo foreground). Default 5min."""
    log.info("self-heal loop iniciado (interval=%ds)", interval_sec)
    while True:
        try:
            report = run_once(heal=True)
            log.info("rodada: healthy=%s issues=%d heals=%d",
                     report.healthy, len(report.issues),
                     len(report.heal_results))
        except Exception as e:  # pragma: no cover — loop nunca morre
            log.error("rodada crashou (continuando): %s", e)
        time.sleep(interval_sec)


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    health_only = "--health-check-only" in argv
    loop_mode = "--loop" in argv
    interval = 300
    if "--interval" in argv:
        try:
            interval = int(argv[argv.index("--interval") + 1])
        except (ValueError, IndexError):
            pass

    if loop_mode:
        run_loop(interval_sec=interval)
        return 0

    report = run_once(heal=not health_only)
    # Resumo stdout (cron captura p/ log)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] self-heal: healthy={report.healthy} "
          f"issues={len(report.issues)} critical={report.critical_count} "
          f"heals={len(report.heal_results)}")
    for issue in report.issues:
        print(f"  [{issue.severity.upper():8}] {issue.type}: {issue.detail}")
    for r in report.heal_results:
        status = "OK" if r.success else "FAIL"
        print(f"  [HEAL {status:4}] {r.issue_type}: {r.action} — {r.detail}")
    # Exit code: 0 saudável, 1 se houver critical não curado
    unhealed_critical = [
        i for i in report.issues
        if i.severity == SEV_CRITICAL
        and not any(r.issue_type == i.type and r.success
                    for r in report.heal_results)
    ]
    return 1 if unhealed_critical else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
