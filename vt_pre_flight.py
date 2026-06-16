#!/usr/bin/env python3
"""
vt_pre_flight.py — Pre-flight check executado às 08:55 (antes do autotrader).

Responsabilidades (em ordem):
 1. Validar dia útil (B3 holidays) e sessão de trading
 2. Verificar/criar config fresh (hot-reload detecta mtime)
 3. Resolver símbolos ativos (vence em <=3 dias úteis → rolla)
 4. Validar integridade do /tmp/vt_autotrader_state.json (limpar stale)
 5. Confirmar MT5 up + saldo + margem
 6. Confirmar hermes binário achável (PATH do cron)
 7. Confirmar LLM OK (ping pequeno via hermes -z)
 8. Notificar Telegram com status completo

Exit code 0 = tudo OK, autotrader pode iniciar.
Exit code != 0 = problema, autotrader NÃO deve iniciar.

Uso:
    python3 vt_pre_flight.py
    python3 vt_pre_flight.py --no-notify  # silencioso (testes)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "vt_config.json"
STATE_PATH = Path("/tmp/vt_autotrader_state.json")
LOG_PATH = Path("/tmp/vt_pre_flight.log")

sys.path.insert(0, str(ROOT))

# Importações do projeto (com fallback se não disponível)
try:
    from vt_calendar import is_trading_day, resolve_all_symbols, B3_HOLIDAYS
    from vt_hermes_helper import hermes_send, find_hermes
except Exception as e:
    print(f"[FATAL] Falha ao importar módulos do projeto: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def section(title: str) -> None:
    log("")
    log(f"{'=' * 60}")
    log(f"  {title}")
    log(f"{'=' * 60}")


# ── 1. Dia útil ──────────────────────────────────────────────
def check_trading_day() -> tuple[bool, str]:
    section("1. VALIDAÇÃO DE DIA ÚTIL")
    ok, reason = is_trading_day(date.today())
    log(f"Hoje: {date.today().isoformat()} ({date.today().strftime('%A')})")
    log(f"É dia útil B3: {ok} ({reason})")
    if not ok:
        return False, f"Não é dia útil: {reason}"
    return True, "OK"


# ── 2. Config ────────────────────────────────────────────────
def check_config() -> tuple[bool, str, dict]:
    section("2. CONFIGURAÇÃO")
    if not CONFIG_PATH.exists():
        return False, f"Config não encontrado: {CONFIG_PATH}", {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        return False, f"JSON inválido: {e}", {}

    version = cfg.get("_version", "?")
    updated = cfg.get("_updated_at", "?")
    log(f"Versão: {version} | Updated: {updated}")

    symbols = cfg.get("symbols", [])
    log(f"Símbolos ativos: {symbols}")
    log(f"Estratégia: {cfg.get('strategy', {})}")

    disabled_syms = cfg.get("disabled_symbols", [])
    disabled_tfs = cfg.get("disabled_timeframes", [])
    if disabled_syms or disabled_tfs:
        log(f"⚠️ Desativados: syms={disabled_syms} tfs={disabled_tfs}")
    else:
        log("Nenhum ativo/TF desativado")

    log(f"max_daily_loss: R$ {cfg.get('max_daily_loss', -500):.2f}")
    log(f"Sessão: {cfg.get('start_hour', 9):02d}:{cfg.get('start_minute', 5):02d}"
        f" – {cfg.get('close_hour', 16):02d}:{cfg.get('close_minute', 45):02d}")

    return True, "OK", cfg


# ── 3. Símbolos / Contratos ─────────────────────────────────
def check_symbols(cfg: dict) -> tuple[bool, str]:
    section("3. RESOLUÇÃO DE CONTRATOS")

    # Resolver (auto-rolla se vence em <=3 dias)
    try:
        resolved = resolve_all_symbols()
        log(f"Contratos resolvidos: {resolved}")
        # resolve_all_symbols() já persiste no config se houve mudança de contrato.
        # Não tocamos _updated_at/_updated_by para não poluir histórico de autoria.
        log(f"OK")
    except Exception as e:
        log(f"❌ Falha ao resolver símbolos: {e}")
        return False, f"resolve_all_symbols falhou: {e}"

    return True, "OK"


# ── 4. State file ────────────────────────────────────────────
def check_state() -> tuple[bool, str]:
    section("4. STATE FILE")
    if not STATE_PATH.exists():
        log("Sem state file (sessão limpa — OK)")
        return True, "OK (sem state)"

    try:
        state = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError as e:
        log(f"❌ State file corrompido: {e}. Removendo.")
        STATE_PATH.unlink()
        return True, "Recriado (corrompido removido)"

    positions = state.get("positions", {}) or {}
    state_day = state.get("current_day")

    log(f"current_day salvo: {state_day}")
    log(f"Posições em state: {len(positions)}")
    log(f"daily_pnl salvo: R$ {state.get('daily_pnl', 0):.2f}")

    # Backup antes de qualquer limpeza
    backup = STATE_PATH.with_suffix(".json.bak")
    shutil.copy2(STATE_PATH, backup)
    log(f"Backup: {backup}")

    # Limpar SLs absurdos/invertidos
    cleaned = 0
    for ticket, pos in list(positions.items()):
        sl = pos.get("sl_pts", 0)
        direction = pos.get("direction", "")
        # Filtros de sanity
        if abs(sl) > 5000:
            log(f"  ❌ Removendo {ticket}: sl_pts={sl} (absurdo)")
            del positions[ticket]
            cleaned += 1
            continue
        if direction == "BUY" and sl < 0:
            log(f"  ❌ Removendo {ticket}: BUY com sl_pts={sl} (negativo)")
            del positions[ticket]
            cleaned += 1
            continue
        if direction == "SELL" and sl < 0 and abs(sl) < 5000:
            log(f"  ❌ Removendo {ticket}: SELL com sl_pts={sl} (negativo)")
            del positions[ticket]
            cleaned += 1

    if cleaned > 0:
        state["positions"] = positions
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
        log(f"State limpo: {cleaned} posições removidas")
    else:
        log("State OK — sem SLs corrompidos")

    return True, "OK"


# ── 5. MT5 ───────────────────────────────────────────────────
def check_mt5() -> tuple[bool, str]:
    section("5. META TRADER 5")
    try:
        from mt5_orchestrator import status
        s = status()
        if "error" in s:
            log(f"❌ MT5 erro: {s['error']}")
            return False, f"MT5 erro: {s['error']}"
        account = s.get("account", {})
        log(f"Conta: {account.get('login', '?')} | Servidor: {account.get('server', '?')}")
        log(f"Saldo: R$ {account.get('balance', 0):.2f}")
        log(f"Equity: R$ {account.get('equity', 0):.2f}")
        log(f"Margem livre: R$ {account.get('margin_free', 0):.2f}")
        pos = s.get("positions", [])
        log(f"Posições abertas no MT5: {len(pos)}")
        for p in pos[:10]:
            log(f"  • {p.get('symbol')} {p.get('type')} vol={p.get('volume')} "
                f"PNL=R$ {p.get('profit', 0):.2f}")
        return True, "OK"
    except Exception as e:
        log(f"❌ Falha ao conectar MT5: {e}")
        return False, str(e)


# ── 6. Hermes ────────────────────────────────────────────────
def check_hermes() -> tuple[bool, str]:
    section("6. HERMES BINARY")
    bin_path = find_hermes()
    if not bin_path:
        log(f"❌ Hermes não encontrado em ~/.local/bin nem ~/.hermes/...")
        return False, "hermes binário não achável"
    log(f"Hermes binary: {bin_path}")
    # Testar versão
    try:
        r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=10)
        ver = r.stdout.strip() or r.stderr.strip()
        log(f"Versão: {ver[:80]}")
    except Exception as e:
        log(f"⚠️ Não foi possível obter versão: {e}")
    return True, "OK"


# ── 7. LLM ping (não-bloqueante) ─────────────────────────────
def check_llm() -> tuple[bool, str]:
    section("7. LLM PING (MiniMax-M3)")
    hermes_bin = find_hermes()
    if not hermes_bin:
        return False, "sem hermes"
    try:
        # Ping rápido: pede "ok" em <100 tokens
        # Provider: minimax-oauth (ativo no Hermes), fallback automático
        r = subprocess.run(
            [hermes_bin, "-z", "responda apenas: OK",
             "-m", "MiniMax-M3", "--provider", "minimax-oauth"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and "OK" in r.stdout.upper()[:50]:
            log(f"LLM respondeu: {r.stdout.strip()[:60]}")
            return True, "OK"
        else:
            # Detectar 429 / quota
            err = r.stderr.lower() if r.stderr else r.stdout.lower()
            if "429" in err or "balance" in err or "quota" in err:
                log(f"⚠️ LLM sem saldo/quota (HTTP 429). Validator vai rodar em modo degraded.")
                return True, "DEGRADED (sem saldo LLM — local checks ativos)"
            log(f"⚠️ LLM resposta inesperada: rc={r.returncode} out={r.stdout[:80]}")
            return True, "DEGRADED"
    except subprocess.TimeoutExpired:
        log("⚠️ LLM timeout (>30s) — segue em modo degraded")
        return True, "DEGRADED (timeout)"
    except Exception as e:
        log(f"⚠️ Erro no LLM ping: {e}")
        return True, "DEGRADED (erro)"


# ── 8. Orquestrador ─────────────────────────────────────────
def check_orchestrator() -> tuple[bool, str]:
    section("8. ORQUESTRADOR / MÓDULOS")
    modules = [
        "vt_autotrader", "vt_config_loader", "vt_strategy_loader",
        "vt_order_validator", "vt_trade_log", "mt5_orchestrator",
        "mt5_error_recovery", "vt_calendar",
    ]
    fail = []
    for m in modules:
        try:
            __import__(m)
            log(f"  ✅ {m}")
        except Exception as e:
            log(f"  ❌ {m}: {e}")
            fail.append(m)
    if fail:
        return False, f"módulos faltando: {fail}"
    return True, "OK"


# ── Estratégia sample test ───────────────────────────────────
def test_params_lookup(cfg: dict) -> tuple[bool, str]:
    """Simula _get_params_for_tf e _get_strategy_for_tf para todos os 24 combos."""
    section("9. SIMULAÇÃO _get_params_for_tf / _get_strategy_for_tf")
    # Carregar dinamicamente a função do autotrader
    try:
        from vt_autotrader import _get_params_for_tf, _get_strategy_for_tf, _calc_sl
        tfs_by = cfg.get("timeframes_by_symbol", {})
        ok = 0
        for sym, tfs in tfs_by.items():
            for tf in tfs:
                params = _get_params_for_tf(sym, tf)
                strat = _get_strategy_for_tf(sym, tf)
                sl = params.get("sl_atr_mult", "?")
                log(f"  {sym:5s} {tf:3s} → {strat:15s} | sl_atr_mult={sl}")
                ok += 1
        log(f"Total simulado: {ok}/24")
        return True, f"{ok}/24"
    except Exception as e:
        log(f"❌ Erro: {e}")
        return False, str(e)


# ── Telegram ─────────────────────────────────────────────────
def send_telegram_report(results: list[tuple[str, bool, str]]) -> None:
    section("10. NOTIFICAÇÃO TELEGRAM")
    icon = lambda ok: "✅" if ok else "❌"
    overall_ok = all(r[1] for r in results if r[0] not in ("LLM",))  # LLM degraded é aceitável
    title = "✅ PRE-FLIGHT OK — AUTOTRADER LIBERADO" if overall_ok else "❌ PRE-FLIGHT FALHOU"
    lines = [f"🛫 {title}", f"📅 {date.today().isoformat()} {datetime.now().strftime('%H:%M')}"]
    for name, ok, msg in results:
        lines.append(f"{icon(ok)} {name}: {msg}")
    msg = "\n".join(lines)
    try:
        # Tenta descobrir o target Telegram a partir de env ou usa default
        target = os.environ.get("VT_TELEGRAM_TARGET", "telegram")
        hermes_send(target, msg)
        log(f"Telegram enviado para {target}")
    except Exception as e:
        log(f"⚠️ Falha ao enviar Telegram: {e}")


# ── Main ─────────────────────────────────────────────────────
def main() -> int:
    log("=" * 60)
    log("  VIBE-TRADING PRE-FLIGHT")
    log(f"  {datetime.now().isoformat()}")
    log("=" * 60)

    results: list[tuple[str, bool, str]] = []

    # 1. dia útil
    ok, msg = check_trading_day()
    results.append(("DIA ÚTIL", ok, msg))
    if not ok:
        # Dia não útil: ainda assim notifica e sai OK (autotrader só não inicia, está OK não rodar)
        send_telegram_report(results)
        return 0

    # 2. config
    ok, msg, cfg = check_config()
    results.append(("CONFIG", ok, msg))
    if not ok:
        send_telegram_report(results)
        return 1

    # 3. símbolos
    ok, msg = check_symbols(cfg)
    results.append(("SÍMBOLOS", ok, msg))
    if not ok:
        send_telegram_report(results)
        return 1

    # 4. state file
    ok, msg = check_state()
    results.append(("STATE FILE", ok, msg))

    # 5. MT5
    ok, msg = check_mt5()
    results.append(("MT5", ok, msg))
    if not ok:
        send_telegram_report(results)
        return 1

    # 6. hermes
    ok, msg = check_hermes()
    results.append(("HERMES", ok, msg))
    if not ok:
        send_telegram_report(results)
        return 1

    # 7. LLM
    ok, msg = check_llm()
    results.append(("LLM", ok, msg))

    # 8. módulos
    ok, msg = check_orchestrator()
    results.append(("ORQUESTRADOR", ok, msg))
    if not ok:
        send_telegram_report(results)
        return 1

    # 9. simulação
    ok, msg = test_params_lookup(cfg)
    results.append(("PARAMS LOOKUP", ok, msg))

    # 10. telegram
    send_telegram_report(results)

    log("")
    log("=" * 60)
    if all(r[1] for r in results):
        log("  ✅ PRE-FLIGHT OK — AUTOTRADER PODE INICIAR")
        log("=" * 60)
        return 0
    else:
        log("  ❌ PRE-FLIGHT FALHOU — VERIFICAR ACIMA")
        log("=" * 60)
        return 1


if __name__ == "__main__":
    code = main()
    sys.exit(code)
