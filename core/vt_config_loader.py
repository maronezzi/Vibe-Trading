"""
vt_config_loader — Hot-reload config do Vibe-Trading.

Uso no autotrader:
    from vt_config_loader import load_config
    CONFIG = load_config()  # no início
    CONFIG = load_config()  # a cada ciclo (hot reload se mudou)

Uso nos scripts de otimização (CANÔNICO):
    from vt_config_loader import save_params, save_full_config
    save_params("wdo", params, updated_by="agi_17h")
    save_full_config(config, updated_by="optimizer")

================================================================
REGRA DE OURO (Bruno 2026-07-01):
================================================================
"a escrita do arquivo json, deve ser feita no AGI, autotrader e demais só devem ler"

Apenas 3 categorias de módulos podem CHAMAR json.dump/_atomic_write/
save_params/save_full_config:
  1. core/vt_config_loader.py (API canônica: save_params, save_full_config)
  2. optimization/agi_tuning_17h.py e filhos (AGI, autorização canônica)
  3. scripts/ que rodem COM AUTOTRADER PAUSADO (nunca em paralelo)

TODOS os outros módulos (autotrader, watchdog, copilot, etc.) só podem
chamar load_config().

Para enforcement mecânico, este módulo oferece:
  - acquire_write_lock() / release_write_lock(): cria sidecar .lock
  - is_authorized_writer(module_path): True se módulo está na whitelist
  - save_params/save_full_config: auto-adquire lock e checa whitelist
  - load_config: WARN se lock existir (lock stale = provável race)
================================================================
"""

import json
import os
import logging
import time
from pathlib import Path
from datetime import datetime

log = logging.getLogger("vt_config")

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"
LOCK_PATH = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".lock")

# Whitelist canônica de AUTORIZADOS a persistir config.
# Regra Bruno 2026-07-01:
#   - core/vt_config_loader.py: API canônica (esta mesma)
#   - optimization/agi_tuning_17h.py + filhos: AGI (única autoridade canônica)
#   - scripts/*: APENAS se rodarem com autotrader PAUSADO (responsabilidade
#     do operador)
# Qualquer outro módulo que escrever aqui vai cair em RuntimeError (ver
# _assert_authorized_writer).
ALLOWED_WRITERS = (
    # este próprio módulo (loader) — entry point via save_params/save_full_config
    "core/vt_config_loader.py",
    # Wave N+1 (2026-07-08): signal_journal escreve em signal_blocked_log
    # (mesma DB do orchestrator, tabela separada). Adicionado via whitelist
    # para que check_write_authorized() não bloqueie quando autotrader
    # chama signal_journal.log_blocked_signal() em tick loop.
    "core/vt_signal_journal.py",
    # AGI v4 canônico (otimizador oficial desde W873, 2026-07-07).
    # NOTA: agi_tuning_17h.py (AGI v3) foi DESCONTINUADO em W873 — era writer,
    # reintroduziu o ERRO 6 (multipliers bugados) e entrou em race com o v4.
    # Agora é um shim inerte que só redireciona para o v4; REMOVIDO daqui para
    # que mesmo que algo o invoque, não possa escrever no config (defesa em
    # profundidade). NÃO re-adicionar.
    "optimization/agi_bayesian_optimizer.py",
    "optimization/agi_evidence_validator.py",
    "optimization/strategy_explorer.py",
    "optimization/exhaustive_strategy_search.py",
    # Wave AGI-super (Bruno 13/08): o AGI calibra os próprios parâmetros de
    # risco (stop diário por símbolo, alvo do profit lock, tolerância de
    # slippage) por simulação counterfactual nos trades reais. Escrita só
    # nessas chaves, com evidência mínima (ver módulo).
    "optimization/agi_v4/risk_calibrator.py",
    # scripts de manutenção (devem rodar com autotrader PAUSADO)
    "scripts/vt_meio_dia_tuning.py",
    "scripts/migrate_today_trades_w13_2.py",
    "scripts/preflight_dryrun.py",
    "scripts/simulate_today_wave9.py",
    "scripts/warmup_search.py",
    "scripts/reenable_scope_and_live_vol.py",
    # scripts/w873_recovery_20260707.py: recovery do incidente W873 — re-aplica
    # calibração broker-truth contract_specs sobrescrita pelo AGI v3 restaurado.
    # Uso ÚNICO/PONTUAL; rodar com autotrader fora do pregão (como agora).
    "scripts/w873_recovery_20260707.py",
    # scripts/w_protect_monday_20260727.py: proteção anti-repetição sexta 24/07.
    # max_consecutive_losses 999→5, cooldown 60min/3, RSI→ENHANCED_RSI.
    # Autorizado Bruno 2026-07-26. Rodar com autotrader PAUSADO.
    "scripts/w_protect_monday_20260727.py",
    # scripts/w873_pause_losing_tfs_20260707.py: pause dos 12 TFs negativos
    # após AGI v4 não convergir (specs W873 corretas). Lei 5 (nunca negativo).
    "scripts/w873_pause_losing_tfs_20260707.py",
    # scripts/unblock_tuesday_sell_win.py: remove [1,"SELL"] do
    # blocked_day_directions (decisão humana 2026-07-08 ter). Rodar com
    # autotrader pausado.
    "scripts/unblock_tuesday_sell_win.py",
    # scripts/unblock_win_buy_20260708.py: libera win.buy_enabled=true
    # (decisão humana 2026-07-08 ter). Rodar com autotrader pausado.
    "scripts/unblock_win_buy_20260708.py",
    # scripts/w881_reenable_wdom15_20260803.py: reabilita WDO_M15 (Wave 881 —
    # stand-in LLM). EMA_PULLBACK validado por TODOS os gates do AGI v4 (PF
    # 1.49, WR 76.9%, 26t, Sharpe 2.12, walk-forward 2/3). Autorizado Bruno
    # 2026-08-03. save_full_config atômico → seguro durante runtime.
    "scripts/w881_reenable_wdom15_20260803.py",
    # scripts/w881_apply_optimization_20260803.py: aplica 6 otimizações de
    # estratégia/params (Wave 881) validadas por sweep do Stage 3 do AGI v4 em
    # 13 pares lucrativos. Todas WF≥75%. Mescla params preservando chaves de
    # risco ao vivo. Autorizado Bruno 2026-08-03.
    "scripts/w881_apply_optimization_20260803.py",
    # scripts/w874_13h_trader_ia_pause_losing.py: trader-IA sessão 13h W874
    # pausa BIT_M5/BIT_M30/WIN_M15 (Lei 5: WR<35% E PnL<0 E n>=15) + ajusta
    # breakeven/cooldown/max_daily em WIN/WDO/WSP para reduzir overtrading.
    # Rodar com autotrader PAUSADO (data/autotrader.paused presente).
    "scripts/w874_13h_trader_ia_pause_losing.py",
    # scripts/w13_5_apply_expanded_grids.py (Wave 13.5, Bruno 2026-07-13):
    # aplica candidatos BIT_M5:SUPERTREND + WIN_H1:RSI_REVERSION descobertos
    # pelo AGI v4 com grids expandidos (MAX_COMBOS 30→80, UNIVERSAL 768→16464).
    # BIT_M5 validado em walk-forward 4/4 (Sharpe=4.09). WIN_H1 validado 3/4
    # (janela 4: -R$4.549 risco conhecido, baseline pior R$-2.180). Autorizado
    # explicitamente pelo Bruno na sessão 13/07 18:30. Rodar com autotrader
    # PAUSADO (fora do horário de trading, como em todas as aplicações manuais).
    "scripts/w13_5_apply_expanded_grids.py",
    # scripts/w14_1_apply_agi_v4_candidates.py (Wave 14.1, Bruno 2026-07-14):
    # aplica 7 candidatos WIN_M5/M15/M30/H1 + BIT_M5/M15/M30 do AGI v4 com
    # grids expandidos. Delta projetado: +R$ 14.288 em 30d. Reverte BIT_M5
    # SUPERTREND (Wave 13.5) para MACD_MOMENTUM. Autorizado pelo Bruno em
    # sessão 14/07 19:50. Rodar com autotrader PAUSADO.
    "scripts/w14_1_apply_agi_v4_candidates.py",
    # scripts/w14_6_meio_dia_20260715.py (Wave 14.6, Bruno 2026-07-15):
    # tuning meio-dia conservador. Briefing: WIN M5/M15/M30/H1 com WR=0%
    # (-R$ 641 total); BIT +R$ 1615 (skip); WSP/WDO com disabled_timeframes
    # (skip); IND disabled_symbols (skip Bruno W14). Aplicar 2 params WIN
    # (bb_std 2.0→2.2, rsi_oversold 15→25) — NÃO troca estratégia, NÃO
    # mexe em sl_atr_mult, NÃO remove de disabled_timeframes. Rodar com
    # autotrader PAUSADO.
    "scripts/w14_6_meio_dia_20260715.py",
    # scripts/w_cleanup_time_blocks_orphan_20260726.py (Bruno 2026-07-26, dom):
    # remove time_blocks["WINQ26"] órfão (block VWAP 9-17h criado na Wave 8.4
    # quando WIN usava VWAP; AGI depois trocou todos os TFs do WIN para
    # SMART_EMA/HTF_BIAS/RSI_REVERSION — nenhum usa VWAP). Verificado em
    # core/vt_autotrader.py:1093-1101 que o block só age se active_strategy
    # == block.strategy, então é inócuo. Limpeza pura, zero efeito
    # comportamental. Mantém BITM26 intacto. Rodar com autotrader PAUSADO.
    "scripts/w_cleanup_time_blocks_orphan_20260726.py",
    # scripts/apply_wsp_m5_optimization.py (Bruno 2026-08-04, ter):
    # aplica WSP_M5 BOLLINGER→EMA_CROSSOVER (ema_fast=8, ema_slow=20,
    # sl_atr_mult=1.8, cooldown=60). Candidato aprovado em TODOS os gates do
    # AGI v4 (PF=2.22, WR=81%, 31t, Sharpe=3.86, walk-forward aprovado) por
    # busca exaustiva de 600 combos (47 estratégias × grid). Antes BOLLINGER
    # estava R$-72 (PF=0.96). Autorizado pelo Bruno na sessão 04/08 19h.
    # Rodar com autotrader PAUSADO.
    "scripts/apply_wsp_m5_optimization.py",
    "backtest/apply_optimization.py",
    # monitoring/vt_pre_flight.py roda 8h55 ANTES do autotrader (pre-flight
    # gate) — é seguro persistir resolved_symbols nessa janela.
    "monitoring/vt_pre_flight.py",
    # monitoring/vt_resolve_symbols.py: script CLI manual de sincronização
    # de contratos. Requer autotrader PAUSADO para uso.
    "monitoring/vt_resolve_symbols.py",
    # core/vt_calendar.py: módulo canônico de resolução de contratos
    # (WIN→WIN$, BIT→BIT$, rollover WINN26→WINQ26 etc.). Persiste
    # resolved_symbols em vt_config.json via save_full_config
    # (updated_by="calendar_resolve"). GAP original: AGENTS.md/CLAUDE.md
    # listam calendar como tarefa legítima de escrita, mas a whitelist
    # só permitia monitoring/vt_pre_flight.py e monitoring/vt_resolve_symbols.py
    # chamarem-no. Em 2026-07-13 (segunda pós-vencimento WIN) isso travou o
    # pre-flight às 08:50/08:55 e o autotrader abriu sem resolved_symbols
    # atualizado — corrigido aqui. Bruno autorizou 2026-07-13.
    "core/vt_calendar.py",
    # Wave Per-TF (Bruno) — AGI apply changes (script cirúrgico de mudanças)
    "optimization/agi_apply_changes.py",
    # ─── Wave W874 (2026-07-08): FIX BUG CRÍTICO ──────────────────────────
    # agi_v4/stage5_apply.py é o ÚNICO módulo do AGI v4 que escreve no config
    # (chama save_full_config em line 160). Runner/pipeline/stage1-4 só orquestram.
    # Bug original: este módulo NÃO estava na whitelist, então o cron 12:00/17:10
    # do AGI v4 falhava silenciosamente em aplicar QUALQUER mudança desde o
    # deploy W873 (2026-07-07). Telegram continuava reportando winners (não
    # depende do apply) mas vt_config.json nunca era atualizado. Descoberto
    # durante W874 ao tentar apply do winner HTF_BIAS_LTF_ENTRY.
    # DOC: data/W874_STRATEGIES_HANDOFF_20260708.md seção 5.
    "optimization/agi_v4/stage5_apply.py",
    # scripts/w876_11h_cron_trader_ia_regime_tighten.py: trader-IA sessão 11h W876
    # tightening defensivo de WIN_M15 RSI (OB 75→78, OS 30→28) por regime atual.
    # Backtest 60 barras justificou (n=6 WR=50% vs n=9 WR=33%, +R$565). Rodar
    # com autotrader PAUSADO (data/autotrader.paused presente).
    "scripts/w876_11h_cron_trader_ia_regime_tighten.py",
    # Wave 877 (2026-07-13, seg, 11h00-11h10): Trader-IA estratégia swap
    # cirúrgico WIN_M15 (HTF_BIAS_LTF_ENTRY → STRONG_TREND) e BIT_M30
    # (PIVOT_POINTS → RSI_REVERSION). Validação: backtest 30d/60d via
    # backtest_v944.py + mt5_fetch. WIN_M15: +R$2764/mês 30d (60d: +R$4176);
    # BIT_M30: n triplica (22→66), +R$137/mês. Demais pares inalterados
    # (regra: Bruno não aceita swap marginal <30% improvement).
    "scripts/w877_11h_cron_trader_ia_strategy_swap.py",
    # Wave 12 (2026-07-12, Sunday, Bruno): SUPER-AGI v5 — busca exaustiva densa
    # (60 combos/estratégia × 27 estratégias × 16 pares) com walk-forward 4 janelas
    # e gates permissivos (PF>=1.05, n>=12, WF>=50%). NUNCA escreve direto —
    # usa save_full_config via verify_super_agi_v5.py. Auditado em snapshot
    # vt_config.json.bak.super_agi_pre_<ts>. Rodar com autotrader PAUSADO (Domingo).
    "optimization/super_agi_v5.py",
    "optimization/verify_super_agi_v5.py",
    # Wave 878 (2026-07-17, sex, 11h00-11h15): Copilot sessão 11h tightening
    # defensivo de cooldown/max_consecutive_losses em WIN_M15/M30 (HTF_BIAS_LTF_ENTRY
    # com 11L/13 trades hoje) + BIT_M15 (EMA_PULLBACK com -R$215). NÃO troca
    # estratégia, NÃO mexe em sl_atr_mult, NÃO remove de disabled_timeframes.
    # Rodar com autotrader PAUSADO (data/autotrader.paused presente).
    "scripts/w878_11h_copilot_overtrading_cooldown.py",
    # Wave 880.G (2026-07-20, Bruno): corrige contract_specs.mult no config
    # (WIN 1.0→0.20 mini, WDO/BIT/DOL também). Rodar com autotrador PAUSADO.
    "scripts/w14_7_fix_contract_specs_mult_20260720.py",
    # Wave 880.H (2026-07-20, Bruno): Profit Lock Adaptativo — adiciona chaves
    # profit_lock_* no config. Rodar com autotrador PAUSADO.
    "scripts/w14_9_enable_profit_lock_20260720.py",
    # scripts/w_enable_all_win_tfs_20260731.py: habilita todos TFs do WIN
    # (remove WIN_* de disabled_timeframes + day_trade_intent=true M30/H1).
    # Autorizado Bruno 2026-07-31. Rodar com autotrader PAUSADO.
    "scripts/w_enable_all_win_tfs_20260731.py",
    # scripts/w_roll_bit_q26_20260731.py: rolagem BITN26→BITQ26 (vencimento 31/07).
    # Autorizado Bruno 2026-07-31. Rodar com autotrader PAUSADO.
    "scripts/w_roll_bit_q26_20260731.py",
    # scripts/w880_nightly_super_agi_apply_20260818.py: apply noturna AUTÔNOMA
    # dos candidatos do super_agi_v5 (walk-forward 4 janelas) que superarem a
    # estratégia atual NAS MESMAS BARRAS com gates conservadores: PF>1.05,
    # n>=15, PnL >= 1.3x o atual (>=2x para live-winners tipo WIN_M15),
    # WF >= 0.75 com >=3/4 janelas, máx 4 trocas/noite, só pares JÁ ativos
    # (nunca reativa desabilitados), janela <08:30, daemon parado, backup
    # snapshot pré-escrita e conferência pós-escrita. Resumo via Telegram.
    # Autorizado Bruno 2026-08-18 ("pode executar sozinho" p/ pregão 19/08).
    "scripts/w880_nightly_super_agi_apply_20260818.py",
)

# Cache
_config = None
_mtime = 0


# ============================================================
# Lock API (failsafe contra escrita concorrente)
# ============================================================
#
# Sidecar: vt_config.json.lock
# Conteúdo (json):
#     {
#       "operator":  "<humano/agent name>",
#       "reason":    "<motivo da escrita — save_full_config / save_params / etc.>",
#       "started_at": "<ISO 8601 string>",
#       "started_at_ts": <epoch seconds — usado pelo stale checker>,
#       "pid":       <os.getpid()>
#     }
# Stale threshold: 300s (mais que isso = lock morto, novo acquire sobrescreve).
#
# Anti-race crítico (incidente 01/07/2026): 2x em poucas horas, autotrader
# sobrescreveu vt_config.json (580→18 linhas, perdeu 49 chaves). Lock file
# garante serialização: writers se protegem MUTUAMENTE contra reescritas
# parciais concorrente.

class ConfigLockError(RuntimeError):
    """Levantada quando outro writer está vivo e bloqueando o arquivo."""


# Stale threshold (segundos). Lock mais velho que isso é tratado como morto
# e sobrescrito (forçar acquire = seguro).
_STALE_LOCK_SECONDS = 300


def _read_lock_meta() -> dict | None:
    """Lê sidecar .lock. Retorna dict ou None se não existe / corrompido."""
    if not LOCK_PATH.exists():
        return None
    try:
        meta = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _lock_age_seconds(meta: dict) -> float:
    """Idade do lock em segundos. Compat com campos velhos (`ts`) e novos (`started_at_ts`)."""
    for key in ("started_at_ts", "ts", "started_at"):
        v = meta.get(key)
        if isinstance(v, (int, float)):
            return time.time() - float(v)
    return 0.0


def _is_lock_process_alive(meta: dict) -> bool:
    """True se PID do lock ainda existe no sistema (não é stale por pid-sumido)."""
    pid = meta.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def is_write_locked() -> bool:
    """True se existe lock de escrita ativo (vivo). NÃO verifica stale.

    Um lock stale (>300s) é retornado como NÃO-locked pelo critério do
    acquire_write_lock; aqui reportamos PRESENÇA do sidecar (compat com
    chamadas que só querem saber se há .lock no disco). Use
    assert_write_unlocked() / acquire_write_lock() para comportamento
    fail-safe de produção.
    """
    return LOCK_PATH.exists()


def assert_write_unlocked() -> None:
    """Levanta RuntimeError se .lock existe E está vivo.

    Use em callers externos que querem fazer pre-check (sem precisar do
    retorno bool). Internamente save_full_config/save_params usam isto.
    """
    meta = _read_lock_meta()
    if meta is None:
        return
    # Stale? → ignora
    if _lock_age_seconds(meta) > _STALE_LOCK_SECONDS:
        return
    # Mesmo processo re-adquirindo? Permite (re-entrant é OK).
    if meta.get("pid") == os.getpid():
        return
    # Outro processo vivo → bloqueia
    if _is_lock_process_alive(meta):
        raise RuntimeError(
            f"Config locked by another writer "
            f"(operator={meta.get('operator', '?')}, "
            f"reason={meta.get('reason', '?')}, "
            f"pid={meta.get('pid', '?')}, "
            f"age={_lock_age_seconds(meta):.0f}s). "
            f"Re-aborted write protected against race."
        )


def acquire_write_lock(operator: str, reason: str = "") -> bool:
    """Tenta adquirir lock exclusivo de escrita do vt_config.json.

    Comportamento (cirúrgico, anti-race 01/07/2026):
      - Sem sidecar: cria {operator, reason, started_at, started_at_ts, pid},
        retorna True.
      - Sidecar existe, lock FRESCO (<300s) e PID diferente vivo:
        NÃO sobrescreve, retorna False. (Protege contra race.)
      - Sidecar existe, lock STALE (>300s) OU pid morto:
        sobrescreve e retorna True.
      - Mesmo PID já tem lock: re-adquire (overwrites, retorna True).

    Args:
        operator: nome lógico de quem está adquirindo (ex: 'agi_17h_llm',
            'optimizer', 'pre_flight_resolve')
        reason: descrição da escrita (ex: 'save_full_config',
            'save_params:wdo', 'manual_resync'). Útil para diagnóstico.

    Returns:
        True se lock foi adquirido; False se outro writer vivo está ativo.
    """
    meta = _read_lock_meta()

    if meta is not None and _lock_age_seconds(meta) <= _STALE_LOCK_SECONDS:
        # Lock fresco — só sobrescreve se (a) mesmo PID (re-entrant) ou
        # (b) PID do lock antigo já morreu (defesa em profundidade).
        same_pid = meta.get("pid") == os.getpid()
        pid_alive = _is_lock_process_alive(meta)
        if not same_pid and pid_alive:
            log.warning(
                f"⚠️ acquire_write_lock: sidecar ativo de "
                f"operator={meta.get('operator')} pid={meta.get('pid')} "
                f"reason={meta.get('reason')} age={_lock_age_seconds(meta):.0f}s. "
                f"Retornando False (não sobrescreve lock vivo)."
            )
            return False

    # Adquire (cria ou sobrescreve stale / re-entrant / pid-morto)
    now = time.time()
    payload = {
        "operator": operator,
        "reason": reason or "",
        "started_at": datetime.now().isoformat(),
        "started_at_ts": now,
        "pid": os.getpid(),
        "config_path": str(CONFIG_PATH),
    }
    try:
        LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        log.error(f"Falha ao criar sidecar lock: {e}")
        return False


def release_write_lock() -> None:
    """Remove sidecar .lock (se existir). Idempotente.

    Use sempre em try/finally após acquire_write_lock ter retornado True.
    """
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"Falha ao remover sidecar lock: {e}")


# ============================================================
# Whitelist enforcement
# ============================================================

def is_authorized_writer(module_path: str) -> bool:
    """True se module_path está na whitelist de writers autorizados."""
    if not module_path:
        return False
    # Normaliza: path absoluto → relativo ao PROJECT_ROOT
    try:
        rel = str(Path(module_path).resolve().relative_to(Path(__file__).resolve().parent.parent))
    except (ValueError, OSError):
        rel = module_path
    rel = rel.replace("\\", "/")
    return any(rel == allowed or rel.endswith("/" + allowed) for allowed in ALLOWED_WRITERS)


def _assert_authorized_writer():
    """Inspeciona o call stack e verifica se o caller está na whitelist.

    Levanta RuntimeError se não estiver — defesa em profundidade contra
    refactors que reintroduzam writes não-autorizados.
    """
    import inspect

    # Pega o frame do chamador (ignora este próprio _assert_authorized_writer
    # E save_params/save_full_config — ambos no MESMO módulo).
    this_module_file = __file__
    try:
        current = inspect.currentframe()
        if current is None:
            return
        # Walk up the stack até achar um frame de módulo DIFERENTE do nosso
        caller_frame = current.f_back
        caller_module = None
        while caller_frame is not None:
            mod = inspect.getmodule(caller_frame)
            if mod is not None:
                mod_file = getattr(mod, "__file__", None)
                if mod_file and not _same_module(mod_file, this_module_file):
                    caller_module = mod
                    break
            caller_frame = caller_frame.f_back

        if caller_module is None:
            return  # todos os frames são do nosso módulo → trusted
        caller_file = getattr(caller_module, "__file__", None)
        if not caller_file:
            return  # módulo sem __file__ (REPL/builtin) → não enforça
        caller_path: str = caller_file
    except Exception:
        return  # falha em inspecionar → não bloqueia (fail-open conservador)

    if not is_authorized_writer(caller_path):
        raise RuntimeError(
            f"🚨 WRITE NÃO AUTORIZADO em vt_config.json!\n"
            f"   Módulo chamador: {caller_path}\n"
            f"   Regra Bruno 2026-07-01: 'autotrader e demais só devem ler'.\n"
            f"   Whitelist: core/vt_config_loader.py, optimization/agi_tuning_17h.py "
            f"e filhos, scripts/ com autotrader PAUSADO.\n"
            f"   Se você REALMENTE precisa escrever aqui, pause o autotrader "
            f"OU adicione seu módulo em ALLOWED_WRITERS (vt_config_loader.py) "
            f"COM AUTORIZAÇÃO EXPLÍCITA DO BRUNO."
        )


def _same_module(file_a: str, file_b: str) -> bool:
    """True se dois paths apontam pro mesmo arquivo .py (normalizado)."""
    try:
        return os.path.samefile(file_a, file_b)
    except (OSError, ValueError, AttributeError):
        # Fallback: comparação de string normalizada
        return os.path.normpath(os.path.abspath(file_a)) == os.path.normpath(os.path.abspath(file_b))


# ============================================================
# Read API
# ============================================================

# Sidecar de overrides aplicado pelo copilot (NÃO persistido em vt_config.json).
# O autotrader lê este sidecar em runtime e mescla com o config oficial.
# Bruno 2026-07-01: copilot não escreve no config (concorrente), apenas
# deixa intenção aqui. Autotrader aplica em memória.
COPILOT_OVERRIDE_PATH = Path("/tmp/vt_copilot_overrides.json")


def load_copilot_overrides() -> dict | None:
    """Lê sidecar /tmp/vt_copilot_overrides.json (intenções do copilot).

    Returns:
        dict com {disabled_symbols, disabled_timeframes, updated_at, updated_by}
        OU None se sidecar não existe / está corrompido.

    NÃO escreve em disco. Read-only.
    """
    if not COPILOT_OVERRIDE_PATH.exists():
        return None
    try:
        data = json.loads(COPILOT_OVERRIDE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def load_effective_config(force: bool = False) -> dict:
    """Carrega config + aplica overrides do copilot (sidecar) em memória.

    Use esta função em runtime (autotrader) ao invés de load_config() puro,
    para que desativações do copilot tenham efeito imediato sem persistir
    no vt_config.json.
    """
    cfg = load_config(force=force)
    overrides = load_copilot_overrides()
    if not overrides:
        return cfg

    # Merge: sidecar sobrescreve disabled_* (é o ponto inteiro do override)
    if "disabled_symbols" in overrides:
        cfg["disabled_symbols"] = list(overrides["disabled_symbols"])
    if "disabled_timeframes" in overrides:
        cfg["disabled_timeframes"] = list(overrides["disabled_timeframes"])
    # Wave 880.B2-PARIDADE (Bruno 2026-08-05): overlay do stop_level simulado
    # para paridade demo-real. A DEMO retorna trade_stops_level≈0 (aceita SLs a
    # poucos pts); a REAL rejeita. Este override faz o autotrader simular o
    # stop_level da real na demo, rejeitando os mesmos SLs. Schema:
    # {"simulated_stop_level": {"WIN": 300, "WDO": 200, "BIT": 500, "WSP": 200}}
    # Valores em pontos nativos. Estimativas conservadoras (confirmar c/ XP).
    if "simulated_stop_level" in overrides:
        cfg["simulated_stop_level"] = dict(overrides["simulated_stop_level"])

    return cfg


def load_config(force: bool = False) -> dict:
    """Carrega config do JSON. Hot-reload se arquivo mudou (mtime).

    Se um .lock existir (de write concorrente), emite WARNING. Locks com
    mais de 5min são considerados stale e ignorados.
    """
    global _config, _mtime

    # ── Check de lock (failsafe) ──
    if LOCK_PATH.exists():
        meta = _read_lock_meta()
        if meta is not None:
            age = _lock_age_seconds(meta)
            if age < _STALE_LOCK_SECONDS:
                log.warning(
                    f"⚠️ vt_config.json tem .lock ativo: operator={meta.get('operator')} "
                    f"pid={meta.get('pid')} reason={meta.get('reason')} age={age:.0f}s. "
                    f"Provável escrita concorrente — verifique."
                )
            else:
                log.warning(
                    f"⚠️ vt_config.json tem .lock STALE (age={age:.0f}s > {_STALE_LOCK_SECONDS}s). "
                    f"Operator: {meta.get('operator')} reason: {meta.get('reason')}."
                )

    try:
        current_mtime = os.path.getmtime(CONFIG_PATH)
    except FileNotFoundError:
        log.error(f"Config não encontrado: {CONFIG_PATH}")
        return _config or {}

    if not force and _config is not None and current_mtime == _mtime:
        return _config  # sem mudança

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            new_config = json.load(f)

        # Validação mínima
        for key in ["symbols", "strategy", "wdo", "win"]:
            if key not in new_config:
                log.error(f"Config inválido: falta chave '{key}'")
                return _config or {}

        # Detectar mudança de versão
        old_ver = _config.get("_version", 0) if _config else 0
        new_ver = new_config.get("_version", 0)

        _config = new_config
        _mtime = current_mtime

        if old_ver != new_ver or force:
            by = new_config.get("_updated_by", "?")
            log.info(f"🔄 Config recarregada! v{old_ver}→v{new_ver} (by {by})")

        return _config

    except (json.JSONDecodeError, IOError) as e:
        log.error(f"Erro ao ler config: {e}")
        return _config or {}


# ============================================================
# Write API (auto-enforces whitelist + lock)
# ============================================================

def save_params(symbol_root: str, params: dict, updated_by: str = "optimizer"):
    """Salva parâmetros de um símbolo no JSON (usado por scripts de otimização).

    Args:
        symbol_root: 'wdo', 'win', etc.
        params: dict de parâmetros para merge
        updated_by: nome do writer (vai para _updated_by no JSON)

    Raises:
        RuntimeError: se outro writer tem lock vivo (anti-race 01/07/2026)
    """
    _assert_authorized_writer()
    if is_write_locked():
        # Pre-check rápido — defesa em profundidade. O try/except+finally
        # abaixo também protege, mas o check upfront dá erro mais óbvio.
        assert_write_unlocked()
    if not acquire_write_lock(updated_by, reason=f"save_params:{symbol_root.lower()}"):
        raise RuntimeError(
            f"Config locked by another writer — save_params({symbol_root}) "
            f"abortou para proteger contra race."
        )
    try:
        cfg = load_config(force=True)

        key = symbol_root.lower()
        # Merge: mantém chaves existentes, atualiza as novas
        if key in cfg:
            cfg[key].update(params)
        else:
            cfg[key] = params

        cfg["_version"] = cfg.get("_version", 0) + 1
        cfg["_updated_at"] = datetime.now().isoformat()
        cfg["_updated_by"] = updated_by

        return _atomic_write(cfg)
    finally:
        release_write_lock()


def save_full_config(cfg: dict, updated_by: str = "optimizer"):
    """Salva config completa no JSON (usado pelo AGI).

    Args:
        cfg: dict COMPLETO (todas as 49+ chaves) — NÃO subset.
        updated_by: nome do writer (canonical: 'agi_17h_llm', 'optimizer',
            'pre_flight_resolve', etc.)

    Raises:
        RuntimeError: se outro writer tem lock vivo (anti-race 01/07/2026)

    Incidente 01/07/2026: 2x em poucas horas, vt_config.json foi reescrito
    com subset parcial (580→18 linhas, perdeu 49 chaves). Lock file com
    try/finally aqui é a defesa canônica.
    """
    _assert_authorized_writer()
    if is_write_locked():
        assert_write_unlocked()
    if not acquire_write_lock(updated_by, reason="save_full_config"):
        raise RuntimeError(
            "Config locked by another writer — save_full_config abortou "
            "para proteger contra race."
        )
    try:
        cfg["_version"] = cfg.get("_version", 0) + 1
        cfg["_updated_at"] = datetime.now().isoformat()
        cfg["_updated_by"] = updated_by

        return _atomic_write(cfg)
    finally:
        release_write_lock()


def _atomic_write(cfg: dict) -> bool:
    """Escrita atômica: tmp + rename (evita corrupção).

    Função interna: assume lock já adquirido por save_params/save_full_config.
    """
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_PATH)
        log.info(f"✅ Config salva v{cfg.get('_version', '?')} (by {cfg.get('_updated_by', '?')})")
        return True
    except IOError as e:
        log.error(f"Erro ao salvar config: {e}")
        return False
