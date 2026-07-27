#!/usr/bin/env python3
"""
Vibe-Trading Copilot — Script autônomo que roda via cron do sistema.
Faz health check, reconciliação de órfãos, ajustes automáticos e envia relatório.
ZERO dependência do Hermes/LLM — roda com Python puro + hermes CLI pra notificar.

Uso:
    python3 vt_copilot.py              # Análise completa + ações automáticas
    python3 vt_copilot.py --health     # Só health check do autotrader
    python3 vt_copilot.py --reconcile  # Só reconciliação de órfãos
    python3 vt_copilot.py --report     # Só gera relatório (sem ações)
"""

import sys
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

# Adicionar projeto ao path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Self-healing monitor (Fase 2.2) — 6 health checks + auto-cura conservadora.
# Import defensivo: se vt_self_heal falhar a importar, o copilot NÃO derruba
# (Lei: monitoramento nunca bloqueia o fluxo principal).
try:
    from monitoring.vt_self_heal import run_once as self_heal_run_once
except Exception as _self_heal_import_error:  # pragma: no cover
    self_heal_run_once = None


def run_self_heal_hook():
    """Executa self-heal no início do copilot. Nunca levanta.

    Retorna string-resumo das ações (p/ incluir no relatório) ou "" se
    indisponível/saudável. Auto-cura conservadora: só age em CRITICAL/HIGH
    curáveis (autotrader morto, MT5 offline, lock órfão); demais só alertam.
    """
    if self_heal_run_once is None:
        return ""  # import falhou — copilot continua sem self-heal
    try:
        report = self_heal_run_once(heal=True)
    except Exception as e:  # pragma: no cover — nunca derruba o copilot
        log(f"[SELF-HEAL] exceção (ignorada): {e}")
        return ""
    parts = []
    if not report.healthy:
        parts.append(f"🛡️ self-heal: {len(report.issues)} issue(s)")
        for r in report.heal_results:
            icon = "✅" if r.success else "❌"
            parts.append(f"   {icon} {r.issue_type}: {r.action}")
    return "\n".join(parts)

from mt5.mt5_orchestrator import status as mt5_status, _run_wine, EXECUTOR_WIN, history as mt5_history
from core.vt_config_loader import load_config
import sys as _sys
from pathlib import Path as _Path
_balhist_path = str(_Path(__file__).parent)
if _balhist_path not in _sys.path:
    _sys.path.insert(0, _balhist_path)
from vt_balance_history import (
    append_snapshot as _bh_append_snapshot,
    read_history as _bh_read_history,
    DEFAULT_PATH as _BH_DEFAULT_PATH,
)

from core.vt_autotrader import get_truth_from_mt5

# ===== TRUTH LAYER (FASE 1) =====
# Cache TTL para get_daily_pnl_truth(). PnL diario cresce monotonicamente,
# entao 5s e folgado. Intraday report sempre invalida o cache antes de
# calcular (padrao do architecture_proposal_2026_07_01.md, secao 3.4).
_PNL_TRUTH_TTL_SECONDS = 5.0
_pnl_truth_cache = {
    "ts": 0.0,         # time.monotonic()
    "data": None,      # dict abaixo
    "key": None,       # (days, today) — invalida se dia trocar
}


def _invalidate_pnl_truth_cache():
    """Forca re-leitura do MT5 history na proxima chamada."""
    _pnl_truth_cache["ts"] = 0.0
    _pnl_truth_cache["data"] = None
    _pnl_truth_cache["key"] = None


def get_daily_pnl_from_events(days: int = 1) -> dict:
    """PnL diario via mt5_trade_events (EA TradeLogger → CSV → watcher → SQLite).

    Fonte broker-truth LOCAL (~1ms) — substitui a chamada Wine (~200ms) quando
    o pipeline EA/watcher está saudável. Retorna mesmo formato de
    get_daily_pnl_truth() para drop-in replacement.

    Retorna dict com:
        source: 'MT5_EVENTS' (broker-truth local)
        deals_total, pnl_profit, pnl_commission, pnl_swap, pnl_net
        deals: lista de dicts (ticket, time, symbol, type, profit, commission, swap)
        ok: bool — True se encontrou dados E heartbeat fresco (<10min)
        error: str | None
        stale: bool — False (sempre fresco, sem cache)
        ts: ISO timestamp
    """
    result = {
        "source": "MT5_EVENTS",
        "deals_total": 0,
        "pnl_profit": 0.0,
        "pnl_commission": 0.0,
        "pnl_swap": 0.0,
        "pnl_net": 0.0,
        "deals": [],
        "ok": False,
        "error": None,
        "stale": False,
        "ts": datetime.now().isoformat(),
    }

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")

        # Staleness check: último HEARTBEAT ou LOGGER_START deve ter < 10 min
        # (EA manda heartbeat a cada 5 min; se > 10 min, pipeline provavelmente morto)
        cutoff_stale = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        last_hb = conn.execute(
            "SELECT MAX(event_time) FROM mt5_trade_events "
            "WHERE trans_type IN ('HEARTBEAT', 'LOGGER_START', 'DEAL_ADD', 'ORDER_ADD')"
        ).fetchone()[0]

        if last_hb is None:
            result["error"] = "mt5_trade_events vazia (EA nunca rodou)"
            conn.close()
            return result

        if last_hb < cutoff_stale:
            result["error"] = f"eventos stale (ultimo: {last_hb}, cutoff: {cutoff_stale})"
            conn.close()
            return result

        # PnL do dia: DEAL_ADD com deal_entry='OUT' (fechamento de posição)
        # Dedup por deal_ticket: um mesmo deal pode aparecer 2x — capturado ao
        # vivo (seq=g_event_seq) E no backfill do EA (seq=deal_ticket) após um
        # restart. GROUP BY deal_ticket pega 1 linha por deal (MAX(id)); profit/
        # commission/swap são idênticos por ticket (broker-truth), então não
        # double-counta.
        date_filter = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT deal_ticket, event_time, symbol, deal_type,
                   deal_profit, deal_commission, deal_swap, deal_price, deal_volume,
                   position_ticket
            FROM mt5_trade_events
            WHERE id IN (
                SELECT MAX(id) FROM mt5_trade_events
                WHERE trans_type = 'DEAL_ADD'
                  AND deal_entry = 'OUT'
                  AND date(event_time) >= ?
                GROUP BY deal_ticket
            )
            ORDER BY event_time
        """, (date_filter,)).fetchall()
        conn.close()

        if not rows:
            result["error"] = "sem deals OUT no periodo (mercado fechado ou sem trades)"
            # Ainda ok=True se heartbeat fresco (mercado pode estar calmo)
            result["ok"] = True
            return result

        pnl_p = 0.0
        pnl_c = 0.0
        pnl_s = 0.0
        light_deals = []
        for r in rows:
            p = float(r[4] or 0)
            c = float(r[5] or 0)
            s = float(r[6] or 0)
            pnl_p += p
            pnl_c += c
            pnl_s += s
            light_deals.append({
                "ticket": r[0],
                "time": r[1],
                "symbol": r[2],
                "type": r[3],
                "profit": round(p, 2),
                "commission": round(c, 2),
                "swap": round(s, 2),
            })

        result["deals_total"] = len(rows)
        result["pnl_profit"] = round(pnl_p, 2)
        result["pnl_commission"] = round(pnl_c, 2)
        result["pnl_swap"] = round(pnl_s, 2)
        result["pnl_net"] = round(pnl_p + pnl_c + pnl_s, 2)
        result["deals"] = light_deals
        result["ok"] = True

    except Exception as e:
        result["error"] = f"excecao ao ler mt5_trade_events: {e}"

    return result


def get_daily_pnl_truth(days: int = 1, force_refresh: bool = False) -> dict:
    """PnL diario do broker (fonte autoritativa — MT5 history).

    Implementacao FASE 1 do refactor (data/architecture_proposal_2026_07_01.md
    linha 280-320): le MT5 history direto via mt5_orchestrator.history() e
    soma profit+commission+swap dos deals. Cache in-memory com TTL 5s para
    evitar custo de uma chamada Wine (~200ms) por tick.

    Retorna dict com:
        source: 'MT5_HISTORY' (broker-truth) ou 'MT5_EMPTY' (sem deals hoje)
        deals_total: int — total de deals retornados pelo MT5 no periodo
        pnl_profit, pnl_commission, pnl_swap, pnl_net: floats (R$)
        deals: lista de dicts com ticket, time, profit, commission, swap, symbol
        ok: bool — True se MT5 respondeu sem erro
        error: str | None
        stale: bool — True se cache foi usado (nao MT5 ao vivo)
        ts: ISO timestamp da coleta

    Idempotente: pode ser chamado multiplas vezes no tick (cache TTL 5s).
    Se force_refresh=True, ignora cache.

    Fallback: NAO faz fallback automatico para DB. Quem chama decide se cai
    no DB (ex: check_intraday_stats()). Razao: misturar as duas fontes
    aqui dentro esconde o drift — quem chama precisa ver source='MT5_HISTORY'
    vs source='DB_FALLBACK' explicitamente.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = (days, today)

    # Cache hit (TTL dentro da janela, mesmo dia)
    now = time.monotonic()
    if not force_refresh and _pnl_truth_cache["data"] is not None:
        if _pnl_truth_cache["key"] == cache_key:
            if (now - _pnl_truth_cache["ts"]) < _PNL_TRUTH_TTL_SECONDS:
                cached = dict(_pnl_truth_cache["data"])
                cached["stale"] = True
                return cached

    # Cache miss — tentar mt5_trade_events PRIMEIRO (local, ~1ms)
    # Se pipeline EA/watcher saudável (heartbeat <10min), usa events.
    # Senão, cai no Wine/MT5 history (~200ms) como fallback.
    events_result = get_daily_pnl_from_events(days=days)
    if events_result["ok"]:
        # Pipeline saudável — usar events como broker-truth local
        _pnl_truth_cache["ts"] = now
        _pnl_truth_cache["data"] = dict(events_result)
        _pnl_truth_cache["key"] = cache_key
        return events_result

    # Events indisponível/stale — logar e cair no Wine
    log(f"[TRUTH] mt5_trade_events indisponivel ({events_result.get('error')}), usando Wine/MT5 history")

    result = {
        "source": "MT5_HISTORY",
        "deals_total": 0,
        "pnl_profit": 0.0,
        "pnl_commission": 0.0,
        "pnl_swap": 0.0,
        "pnl_net": 0.0,
        "deals": [],
        "ok": False,
        "error": None,
        "stale": False,
        "ts": datetime.now().isoformat(),
    }

    try:
        raw = mt5_history(symbol=None, days=days)
        if not isinstance(raw, dict):
            result["source"] = "MT5_EMPTY"
            result["error"] = f"MT5 history retornou tipo invalido: {type(raw).__name__}"
        elif "error" in raw and "history" not in raw:
            # Erro real do MT5 (timeout, Wine down, etc.)
            result["source"] = "MT5_EMPTY"
            result["error"] = str(raw.get("error", "unknown"))
        else:
            deals = raw.get("history", []) or []
            result["deals_total"] = len(deals)
            result["ok"] = True

            if not deals:
                result["source"] = "MT5_EMPTY"
            else:
                # Soma broker-truth por deal: profit + commission + swap
                pnl_p = 0.0
                pnl_c = 0.0
                pnl_s = 0.0
                light_deals = []
                for d in deals:
                    p = float(d.get("profit", 0) or 0)
                    c = float(d.get("commission", 0) or 0)
                    s = float(d.get("swap", 0) or 0)
                    pnl_p += p
                    pnl_c += c
                    pnl_s += s
                    light_deals.append({
                        "ticket": d.get("ticket"),
                        "time": d.get("time"),
                        "symbol": d.get("symbol"),
                        "type": d.get("type"),
                        "profit": round(p, 2),
                        "commission": round(c, 2),
                        "swap": round(s, 2),
                    })
                result["pnl_profit"] = round(pnl_p, 2)
                result["pnl_commission"] = round(pnl_c, 2)
                result["pnl_swap"] = round(pnl_s, 2)
                # pnl_net e o que aparece no relatorio: profit+commission+swap
                # (commission e swap sao negativos no broker — ja vem com sinal)
                result["pnl_net"] = round(pnl_p + pnl_c + pnl_s, 2)
                result["deals"] = light_deals
    except Exception as e:
        result["source"] = "MT5_EMPTY"
        result["error"] = f"excecao ao chamar mt5_history: {e}"

    # Grava cache so se chamada foi OK (cache de dados validos)
    if result["ok"] or result["source"] == "MT5_EMPTY":
        _pnl_truth_cache["ts"] = now
        _pnl_truth_cache["data"] = dict(result)
        _pnl_truth_cache["key"] = cache_key

    return result

# ===== CONFIGURAÇÃO =====
DB_PATH = Path(__file__).parent.parent / "vt_trades.db"
LOG_PATH = Path("/tmp/vt_autotrader.log")
TELEGRAM_TARGET = "telegram:-1004284773048"

# Critérios de pausa automática — LIDOS DO vt_config.json (pause_criteria)
# Não hardcodar aqui. Fonte única: vt_config.json
PAUSE_CRITERIA = {"min_trades": 15, "max_wr": 35, "max_pnl": 0}  # fallback (config é autoridade)


def _load_pause_criteria():
    """Carrega pause_criteria do vt_config.json. Se disabled ou ausente, retorna None."""
    try:
        from core.vt_config_loader import load_config
        cfg = load_config()
        pc = cfg.get("pause_criteria", {})
        if not pc.get("enabled", False):
            return None  # pausa automática desativada
        return {
            "min_trades": pc.get("min_trades", 15),
            "max_wr": pc.get("max_wr_pct", 35),
            "max_pnl": pc.get("max_pnl", 0),
        }
    except Exception:
        return PAUSE_CRITERIA  # fallback hardcoded

# ===== FUNÇÕES =====

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify_telegram(msg):
    """Envia notificação via hermes CLI."""
    try:
        from vt_hermes_helper import hermes_send
        hermes_send(TELEGRAM_TARGET, msg)
        log("Notificação enviada pro grupo")
    except Exception as e:
        log(f"[ERRO] Falha ao enviar notificação: {e}")


def notify_telegram_media(media_path, caption=""):
    """Envia mídia (PNG) pro Telegram via hermes. Caption limitado a 1024 chars.

    Convenção hermes: corpo 'MEDIA:<path>' (caption opcional antes) envia mídia
    inline. Usa hermes_send() do vt_hermes_helper — find_hermes() resolve o
    binário mesmo no PATH restrito do cron (/usr/bin:/bin), que era a causa do
    'No such file or directory: hermes' (o chart era gerado mas nunca chegava).
    """
    try:
        from vt_hermes_helper import hermes_send
        body = f"MEDIA:{media_path}"
        if caption:
            body = f"{caption}\n\n{body}"
        if hermes_send(TELEGRAM_TARGET, body):
            log(f"Mídia enviada: {media_path}")
        else:
            log("[WARN] Falha ao enviar mídia (hermes indisponível ou rc!=0)")
    except Exception as e:
        log(f"[ERRO] Falha ao enviar mídia: {e}")


def check_autotrader_health():
    """Verifica se o autotrader está rodando e com log fresco."""

    # Verificar processo
    result = subprocess.run(
        ["pgrep", "-f", "vt_autotrader.py"],
        capture_output=True, text=True
    )
    pid = result.stdout.strip()

    if not pid:
        log("[SAÚDE] Autotrader NÃO está rodando!")
        return {"running": False, "pid": None, "log_fresh": False}

    # Verificar log freshness
    log_fresh = False
    if LOG_PATH.exists():
        mtime = LOG_PATH.stat().st_mtime
        age_min = (datetime.now().timestamp() - mtime) / 60
        log_fresh = age_min < 5
        log(f"[SAÚDE] Autotrader PID {pid} rodando. Log: {age_min:.0f}min atrás")
    else:
        log(f"[SAÚDE] Autotrader PID {pid} rodando. Sem log encontrado")

    return {"running": True, "pid": pid, "log_fresh": log_fresh}


def restart_autotrader():
    """Reinicia o autotrader."""
    log("[AÇÃO] Reiniciando autotrader...")

    # Matar processo atual
    subprocess.run(["pkill", "-9", "-f", "vt_autotrader.py"],
                   capture_output=True, timeout=10)

    # Matar processos MT5 pendurados
    subprocess.run(["pkill", "-9", "-f", "mt5_executor|mt5_resolve"],
                   capture_output=True, timeout=10)

    import time
    time.sleep(3)

    # Iniciar novo
    subprocess.Popen(
        ["python3", "vt_autotrader.py"],
        cwd=str(Path(__file__).parent.parent),
        stdout=open(LOG_PATH, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True
    )

    time.sleep(5)

    # Verificar se iniciou
    result = subprocess.run(["pgrep", "-f", "vt_autotrader.py"],
                           capture_output=True, text=True)
    if result.stdout.strip():
        log(f"[AÇÃO] Autotrader reiniciado. PID: {result.stdout.strip()}")
        return True
    else:
        log("[ERRO] Falha ao reiniciar autotrader!")
        return False


def reconcile_orphans():
    """Compara MT5 vs banco e reconcilia órfãos."""
    log("[RECONCILIAÇÃO] Verificando posições órfãs...")

    # Posições no MT5
    try:
        mt5_data = mt5_status()
        mt5_positions = mt5_data.get("positions", [])
    except Exception as e:
        log(f"[ERRO] Falha ao conectar MT5: {e}")
        return 0

    mt5_tickets = {str(p["ticket"]) for p in mt5_positions}

    # Trades abertos no banco
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    open_trades = conn.execute(
        "SELECT id, symbol, direction, timeframe, entry_price, entry_ticket "
        "FROM trades WHERE exit_time IS NULL"
    ).fetchall()

    reconciled = 0
    for trade in open_trades:
        ticket = str(trade["entry_ticket"])

        if ticket not in mt5_tickets:
            # Posição não existe mais no MT5 → marcar como fechada
            exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Tentar pegar preço atual do símbolo pra calcular PnL
            try:
                tick_data = _run_wine(EXECUTOR_WIN, "tick", trade["symbol"])
                current_price = tick_data.get("bid", trade["entry_price"])
            except Exception:
                current_price = trade["entry_price"]

            # Calcular PnL básico
            if trade["direction"] == "BUY":
                pnl_pts = current_price - trade["entry_price"]
            else:
                pnl_pts = trade["entry_price"] - current_price

            # Converter pra R$ usando get_multiplier (cobre todos ativos)
            try:
                from vt_trade_log import get_multiplier
                multiplier = get_multiplier(trade["symbol"])
            except Exception:
                # W873: fallback alinhado ao watchdog (WIN/IND=1.0). Outros
                # mini-contratos caem em 1.0 (conservador — prefere super a sub).
                multiplier = 1.0 if ("WIN" in trade["symbol"] or "IND" in trade["symbol"]) else 1.0
            net_pnl = pnl_pts * multiplier * (trade["volume"] if trade["volume"] is not None else 1)

            conn.execute("""
                UPDATE trades 
                SET exit_time=?, exit_price=?, net_pnl=?, 
                    exit_reason='ORFAO_FECHADO', exit_ticket='reconciled'
                WHERE id=?
            """, (exit_time, current_price, net_pnl, trade["id"]))

            log(f"  #{trade['id']} {trade['direction']} {trade['symbol']} "
                f"{trade['timeframe']} → ORFAO_FECHADO (PnL R$ {net_pnl:+.2f})")
            reconciled += 1

    # Trades no MT5 sem registro no banco (criar registro básico)
    for pos in mt5_positions:
        ticket = str(pos["ticket"])
        comment = pos.get("comment", "")

        if comment == "VibeTrading":
            # Verificar se já existe no banco
            exists = conn.execute(
                "SELECT id FROM trades WHERE entry_ticket=?", (ticket,)
            ).fetchone()

            if not exists:
                # Criar registro básico
                symbol = pos["symbol"]
                direction = "BUY" if pos["type"] in (0, "BUY") else "SELL"
                entry_price = pos["price_open"]
                entry_time = datetime.fromtimestamp(pos["time"]).strftime("%Y-%m-%d %H:%M:%S")

                conn.execute("""
                    INSERT INTO trades (symbol, direction, volume, timeframe, entry_price,
                                       entry_ticket, entry_time, strategy)
                    VALUES (?, ?, ?, 'M5', ?, ?, ?, 'VWAP')
                """, (symbol, direction, pos.get("volume", 1), entry_price, ticket, entry_time))

                log(f"  Novo registro: {direction} {symbol} @ {entry_price} (ticket {ticket})")
                reconciled += 1

    conn.commit()
    conn.close()

    log(f"[RECONCILIAÇÃO] {reconciled} posições reconciliadas")
    return reconciled


def check_wdo_activity():
    """Verifica por que WDO não está operando."""
    conn = sqlite3.connect(str(DB_PATH))
    today = datetime.now().strftime("%Y-%m-%d")

    wdo_trades = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE symbol LIKE '%WDO%' AND date(entry_time)=?",
        (today,)
    ).fetchone()[0]

    conn.close()

    if wdo_trades == 0:
        log("[WDO] Sem operações hoje. Investigando...")

        # Símbolo mais líquido do config
        wdo_symbol = load_config().get("resolved_symbols", {}).get("WDO", "WDON26")

        # Verificar se WDO tem dados
        try:
            bars = _run_wine(EXECUTOR_WIN, "bars", wdo_symbol, "M5", "30")
            if "bars" in bars and bars["bars"]:
                # Calcular volatilidade
                closes = [b["close"] for b in bars["bars"]]
                atr = max(closes) - min(closes)
                avg_price = sum(closes) / len(closes)
                atr_pct = (atr / avg_price) * 100

                log(f"[WDO] ATR={atr:.2f} ({atr_pct:.3f}% do preço). "
                    f"Range: {min(closes):.2f}-{max(closes):.2f}")

                if atr_pct < 0.15:
                    log("[WDO] Mercado muito calmo (< 0.15%). Threshold adaptativo deve ajudar.")
                    return "calmo"
                else:
                    log("[WDO] Volatilidade OK. Verificar thresholds.")
                    return "ok"
            else:
                log("[WDO] Sem dados de barras!")
                return "sem_dados"
        except Exception as e:
            log(f"[ERRO] Falha ao verificar WDO: {e}")
            return "erro"

    log(f"[WDO] {wdo_trades} operações hoje")
    return "operando"


def check_performance():
    """Verifica performance por símbolo+timeframe (últimos 5 dias)."""
    conn = sqlite3.connect(str(DB_PATH))
    five_days_ago = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

    # Por símbolo+timeframe (granular — pra pausar só o que tá perdendo)
    sym_tf_stats = conn.execute("""
        SELECT symbol, timeframe,
               COUNT(*) ops,
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) losses,
               ROUND(AVG(net_pnl), 2) avg_pnl,
               SUM(net_pnl) total_pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND date(entry_time) >= ?
        GROUP BY symbol, timeframe
    """, (five_days_ago,)).fetchall()

    # Por símbolo (agg)
    sym_stats = conn.execute("""
        SELECT symbol,
               COUNT(*) ops,
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) losses,
               ROUND(AVG(net_pnl), 2) avg_pnl,
               SUM(net_pnl) total_pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND date(entry_time) >= ?
        GROUP BY symbol
    """, (five_days_ago,)).fetchall()

    # Por timeframe (agg)
    tf_stats = conn.execute("""
        SELECT timeframe,
               COUNT(*) ops,
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) losses,
               ROUND(AVG(net_pnl), 2) avg_pnl,
               SUM(net_pnl) total_pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND date(entry_time) >= ?
        GROUP BY timeframe
    """, (five_days_ago,)).fetchall()

    conn.close()

    return {"sym_tf": sym_tf_stats, "timeframes": tf_stats, "symbols": sym_stats}


def _apply_pauses(paused_items: list, today: str):
    """Desativa símbolos/timeframes no config (disabled_symbols/disabled_timeframes).
    paused_items: lista de strings como "WIN", "WDO_M15" etc."""
    from core.vt_config_loader import load_config, save_full_config

    config = load_config(force=True)
    disabled_syms = set(config.get("disabled_symbols", []))
    disabled_tfs = set(config.get("disabled_timeframes", []))
    anything_changed = False

    for item in paused_items:
        if "_" in item:
            sym, tf = item.split("_", 1)
            tf_key = f"{sym}_{tf}"
            if tf_key not in disabled_tfs:
                disabled_tfs.add(tf_key)
                log(f"[PAUSA] Desativado timeframe {tf_key}")
                anything_changed = True
        else:
            sym = item
            if sym not in disabled_syms:
                disabled_syms.add(sym)
                log(f"[PAUSA] Desativado símbolo {sym}")
                anything_changed = True

    if anything_changed:
        config["disabled_symbols"] = sorted(disabled_syms)
        config["disabled_timeframes"] = sorted(disabled_tfs)
        save_full_config(config, updated_by="copilot_pausa")
        log("[PAUSA] Config atualizado. Autotrader fará hot-reload.")
    else:
        log("[PAUSA] Nenhuma alteração necessária")


def evaluate_and_pause():
    """Avalia performance por símbolo+timeframe e pausa se necessário.
    Retorna lista de itens pausados."""
    # Verificar se pausa automática está habilitada no config
    pc = _load_pause_criteria()
    if pc is None:
        log("[PAUSA] Pausa automática desativada no config (pause_criteria.enabled=false)")
        return []

    stats = check_performance()
    paused = []

    # Carregar pausas ativas do arquivo
    pause_file = Path("/tmp/vt_paused_timeframes.json")
    active_pauses = {}
    if pause_file.exists():
        try:
            active_pauses = json.loads(pause_file.read_text())
        except Exception:
            pass

    today = datetime.now().strftime("%Y-%m-%d")

    # Avaliar por símbolo+timeframe (granular)
    for row in stats["sym_tf"]:
        symbol, tf, ops, wins, losses, avg_pnl, total_pnl = row
        # Extrair root do símbolo (WINQ26 → WIN)
        sym_root = ""
        for root in ["WIN", "WDO", "IND", "DOL", "BIT", "WSP"]:
            if root in symbol:
                sym_root = root
                break
        if not sym_root:
            continue

        if ops >= pc["min_trades"]:
            wr = (wins / ops * 100) if ops > 0 else 0
            if wr < pc["max_wr"] and total_pnl < pc["max_pnl"]:
                pause_key = f"{sym_root}_{tf}"
                log(f"[PAUSA] {pause_key} qualifica: WR={wr:.1f}% "
                    f"E PnL=R${total_pnl:+.2f} E ops={ops}")
                active_pauses[pause_key] = {
                    "date": today,
                    "reason": f"WR={wr:.1f}% PnL=R${total_pnl:.2f}",
                    "trades": ops,
                }
                paused.append(pause_key)

    # Se TODOS os timeframes de um símbolo foram pausados → pausar símbolo inteiro
    sym_roots = set()
    for row in stats["sym_tf"]:
        for root in ["WIN", "WDO", "IND", "DOL", "BIT", "WSP"]:
            if root in row[0]:
                sym_roots.add(root)
    for sym_root in sym_roots:
        sym_tfs_paused = [p for p in paused if p.startswith(sym_root + "_")]
        all_tfs = [row for row in stats["sym_tf"] if row[0] and sym_root in row[0]]
        if sym_tfs_paused and len(sym_tfs_paused) >= len(all_tfs):
            if sym_root not in paused:
                paused.append(sym_root)
                log(f"[PAUSA] {sym_root} — todos os timeframes pausados, removendo símbolo inteiro")

    # Aplicar pausas
    if paused:
        _apply_pauses(paused, today)

    # Salvar pausas
    try:
        pause_file.write_text(json.dumps(active_pauses, indent=2))
    except Exception:
        pass

    return paused


def check_intraday_stats() -> dict:
    """Métricas INTRADAY (somente HOJE): PnL realizado, flutuante, contadores, série.

    FASE 1 do refactor (data/architecture_proposal_2026_07_01.md linha 280-320):
    PnL realizado vem do MT5 history (broker-truth) via get_daily_pnl_truth().
    DB SQLite é só fallback — se MT5 indisponível, loga source='DB_FALLBACK'
    e usa net_pnl da tabela trades.

    Substitui o antigo check_performance() (janela 5 dias) como entrada do
    generate_report(). Mantém evaluate_and_pause() usando a janela maior.

    Retorna dict com:
        ops, wins, losses, pnl_realized: agregados (broker-truth se source=MT5)
        open_count, open_pnl: posições abertas via get_truth_from_mt5() (helper centralizado)
        pnl_total: pnl_realized + open_pnl
        pnl_cum: lista [(exit_time_iso, pnl_acumulado)] em ordem cronológica
        max_drawdown: pior queda do peak até o fundo
        best_trade, worst_trade: extremos do dia
        source: 'MT5_HISTORY' (broker-truth) | 'DB_FALLBACK' (MT5 falhou)
        truth_error: str | None — mensagem de erro do MT5 se houve fallback
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # 1) FONTE AUTORITATIVA: MT5 history (broker-truth)
    #    Invalida cache para que o intraday report sempre reflita o estado
    #    atual do broker (padrao architecture_proposal_2026_07_01.md, secao 3.4)
    _invalidate_pnl_truth_cache()
    pnl_truth = get_daily_pnl_truth(days=1, force_refresh=True)

    source = pnl_truth.get("source", "MT5_HISTORY")
    truth_error = None
    pnl_realized = 0.0
    ops = 0
    wins = 0
    losses = 0
    pnl_series = []  # lista de (exit_time_iso, pnl) ordenados

    if pnl_truth["ok"] and pnl_truth["deals_total"] > 0:
        # Broker-truth: somar profit+commission+swap por deal, contar wins/losses
        pnl_realized = pnl_truth["pnl_net"]
        ops = pnl_truth["deals_total"]
        # wins/losses pelo deal (profit > 0 conta como win; commission/swap
        # nao interferem — sao custos do broker)
        wins = sum(1 for d in pnl_truth["deals"] if d["profit"] > 0)
        losses = sum(1 for d in pnl_truth["deals"] if d["profit"] <= 0)
        # Serie temporal a partir dos deals (time do MT5)
        for d in pnl_truth["deals"]:
            # time do MT5 vem como string de timestamp epoch ou ISO — normalizar
            t_raw = d.get("time")
            t_iso = _normalize_deal_time(t_raw)
            pnl_series.append((t_iso, round(d["profit"] + d["commission"] + d["swap"], 2)))
        pnl_series.sort(key=lambda x: x[0])
    elif pnl_truth["ok"] and pnl_truth["deals_total"] == 0:
        # Pipeline saudável mas 0 deals (mercado calmo / sem trades hoje).
        # PnL=0 é a resposta correta — NÃO cair no DB fallback.
        # source já vem como MT5_EVENTS ou MT5_HISTORY do pnl_truth.
        pass
    else:
        # 2) FALLBACK: DB SQLite (cache). Fonte nao-confiavel mas melhor que nada.
        source = "DB_FALLBACK"
        truth_error = pnl_truth.get("error") or "MT5 sem deals no periodo"
        log(f"[WARN] PnL intraday usando DB fallback (MT5 indisponivel: {truth_error})")

        conn = sqlite3.connect(str(DB_PATH))

        # Wave 1C.2 (Bruno 02/07 11:14): exlcuir GHOST do PnL realizado.
        # Trades GHOST tem exit_time mas net_pnl=0 (bug autotrader: nao
        # encontrou exit_price real porque MT5 history vazio). Se contasse,
        # reportaria "WR 0%" mesmo com varios losses reais. Excluir evita
        # esse teatro. Os losses reais aparecem via MT5_BALANCE_DELTA abaixo.
        # Wave N+1C (Bruno 09/07): filtra [EXCLUDED] alem de GHOST/stale_close.
        closed = conn.execute("""
            SELECT COUNT(*) ops,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) losses,
                   COALESCE(SUM(net_pnl), 0) pnl
            FROM trades
            WHERE exit_time IS NOT NULL
              AND date(exit_time) = ?
              AND exit_reason != 'stale_close'
              AND exit_reason != 'GHOST'
              AND strategy NOT LIKE '%[EXCLUDED]%'
        """, (today,)).fetchone()
        pnl_series = conn.execute("""
            SELECT exit_time, net_pnl
            FROM trades
            WHERE exit_time IS NOT NULL
              AND date(exit_time) = ?
              AND exit_reason != 'stale_close'
              AND exit_reason != 'GHOST'
              AND strategy NOT LIKE '%[EXCLUDED]%'
            ORDER BY exit_time
        """, (today,)).fetchall()
        pnl_series = [(t, p) for t, p in pnl_series]  # ja vem como tuplas
        ops = closed[0] or 0
        wins = closed[1] or 0
        losses = closed[2] or 0
        pnl_realized = round(closed[3] or 0.0, 2)
        conn.close()

        # 2.5) MT5_BALANCE_DELTA (Wave 1C.2 + Wave 1C.3): se MT5 history
        # vazio (broker demo nao persiste deals), usar variacao do saldo
        # MT5 como PnL realizado broker-truth.
        #
        # Wave 1C.3 (08/07): o baseline agora vem de `core.vt_starting_balance`
        # (gravado pelo autotrader no startup em /tmp/vt_intraday_starting_
        # balance.json). Antes, o hardcoded 1002230.57 causava drift
        # acumulado (R$403,83 entre 02/07 e 08/07) — bug pre-diagnosticado
        # por Hermes + flagado como Pitfall #20 no skill
        # `vibe-trading-watchdog-sync`.
        #
        # SEMPRE roda quando MT5 history vazio (mesmo se DB tem dados, o
        # delta do saldo e a verdade do broker e sobrescreve o DB PnL).
        try:
            # Bruno 02/07: vt_copilot roda como standalone (sem autotrader
            # injetar sys.path). Padrao igual a core/vt_autotrader.py:37.
            import sys as _sys
            from pathlib import Path as _Path
            _mt5_path = str(_Path(__file__).parent.parent / "mt5")
            if _mt5_path not in _sys.path:
                _sys.path.insert(0, _mt5_path)
            from mt5_orchestrator import status as _mt5_status
            mt5_now = _mt5_status()
            current_balance = mt5_now.get("account", {}).get("balance", 0.0)
            # Saldo de abertura do dia: tenta pegar do helper centralizado
            # (Wave 1C.3) ou usa 1.002.230,57 como ULTIMO RECURSO fallback
            # hardcoded — mantido por seguranca, so eh usado se o helper nao
            # tem snapshot (autotrader nunca rodou hoje OU MT5 estava down
            # no startup).
            base_balance = 1002230.57
            # Wave 1C.3: usa helper centralizado (autotrader grava no startup).
            # Se helper retorna None (sem snapshot de hoje OU falha de I/O),
            # mantemos o hardcoded 1002230.57 como last-resort.
            try:
                from core.vt_starting_balance import get_today_starting_balance
                _today_balance = get_today_starting_balance()
                if _today_balance is not None and _today_balance > 0:
                    base_balance = float(_today_balance)
            except Exception as _e:
                log(f"[FALLBACK-BALANCE] starting_balance helper falhou: {_e}")
            balance_delta = round(current_balance - base_balance, 2)
            # Sobrescreve pnl_realized com a verdade do broker
            pnl_realized = balance_delta
            log(f"[FALLBACK-BALANCE] MT5 broker-truth PnL: R$ {balance_delta:+.2f} (base {base_balance:,.2f} -> now {current_balance:,.2f})")
            # Wave N+1B (09/07/2026): gravar snapshot para gráfico intraday.
            # Se MT5/DB ficarem vazios, plotaremos a evolução do saldo.
            try:
                _bh_append_snapshot(
                    balance=current_balance,
                    pnl_delta=balance_delta,
                    source="MT5_STATUS_FALLBACK",
                )
            except Exception as _e:
                log(f"[BH-SNAPSHOT] erro ao gravar balance history: {_e}")
        except Exception as _e:
            log(f"[FALLBACK-BALANCE] erro ao ler MT5 status: {_e}")

    # Acumulado + max drawdown (mesmo calculo, agora sobre fonte broker-truth)
    pnl_cum = []
    acc = 0.0
    peak = 0.0
    max_dd = 0.0
    for t, p in pnl_series:
        acc += p
        pnl_cum.append((t, round(acc, 2)))
        peak = max(peak, acc)
        max_dd = min(max_dd, acc - peak)

    # Posicoes abertas via get_truth_from_mt5() (helper centralizado,
    # fonte da verdade para balance/equity/positions segundo Wave 12.1).
    # Se MT5 falhar aqui, loga e segue com zeros (open_count=0).
    open_count, open_pnl = 0, 0.0
    truth = get_truth_from_mt5()
    if truth.get("ok"):
        open_count = truth.get("n_positions", 0) or 0
        open_pnl = round(truth.get("pnl_flutuante", 0.0) or 0.0, 2)
    else:
        log(f"[WARN] MT5 indisponivel para posicoes abertas: {truth.get('error')}")

    return {
        "ops": ops,
        "wins": wins,
        "losses": losses,
        "pnl_realized": pnl_realized,
        "open_count": open_count,
        "open_pnl": open_pnl,
        "pnl_total": round(pnl_realized + open_pnl, 2),
        "pnl_cum": pnl_cum,
        "max_drawdown": round(max_dd, 2),
        "best_trade": round(max((p for _, p in pnl_series), default=0.0), 2),
        "worst_trade": round(min((p for _, p in pnl_series), default=0.0), 2),
        "source": source,           # MT5_HISTORY | DB_FALLBACK
        "truth_error": truth_error,
        "deals_total": pnl_truth.get("deals_total", 0),
    }


def _normalize_deal_time(t_raw) -> str:
    """Normaliza timestamp de deal do MT5 para ISO string.

    MT5 retorna time como int (epoch) ou str. Para a serie temporal do
    grafico, aceitamos ambos formatos.
    """
    if t_raw is None:
        return ""
    if isinstance(t_raw, (int, float)):
        try:
            return datetime.fromtimestamp(int(t_raw)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(t_raw)
    return str(t_raw)


def build_pnl_series_from_balance_history(path: str | None = None) -> list[tuple[str, float]]:
    """Wave N+1B: constrói série temporal (ts, pnl_delta) a partir do histórico
    de saldo MT5. Retorna [] se histórico vazio/ausente/inválido.

    Cada elemento é (timestamp ISO, PnL relativo ao baseline do dia).
    baseline = primeiro snapshot de hoje (= saldo de abertura).
    """
    from pathlib import Path as _P
    p = _P(path) if path else _BH_DEFAULT_PATH
    history = _bh_read_history(p)
    if not history or len(history) < 1:
        return []
    try:
        baseline = history[0]["balance"]
    except (KeyError, IndexError):
        return []
    series = []
    for h in history:
        try:
            series.append(
                (h["ts"], round(float(h["balance"]) - float(baseline), 2))
            )
        except (KeyError, ValueError, TypeError):
            continue
    return series


def render_pnl_chart(pnl_cum: list, today: str, balance_history_path: str | None = None) -> Path:
    """Gera PNG da evolução intraday do PnL realizado.
    Tema escuro, igual ao terminal/IDE. Linha verde se último valor >= 0,
    vermelha se < 0. Se pnl_cum vazio, mostra placeholder — exceto quando
    balance_history_path é fornecido E o histórico tem snapshots de hoje:
    nesse caso plota a evolução do saldo MT5 (delta a partir do baseline),
    que é o broker-truth disponível mesmo quando MT5 deals e DB trades
    estão vazios.

    Wave N+1B (09/07/2026): corrige bug do gráfico placeholder quando
    MT5 demo não persiste deals + DB trades todos GHOST.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

    # Se pnl_cum vazio mas há histórico do saldo → plota broker-truth (delta)
    if not pnl_cum and balance_history_path is not None:
        series = build_pnl_series_from_balance_history(balance_history_path)
        if series:
            pnl_cum = series

    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=110)
    fig.patch.set_facecolor("#1e1e1e")
    ax.set_facecolor("#1e1e1e")

    if not pnl_cum:
        ax.text(0.5, 0.5, f"Sem trades fechados em {today}",
                ha="center", va="center", color="#cccccc",
                transform=ax.transAxes, fontsize=14)
    else:
        times = [datetime.fromisoformat(t) for t, _ in pnl_cum]
        vals = [v for _, v in pnl_cum]
        last = vals[-1]
        line_color = "#4caf50" if last >= 0 else "#ef5350"
        ax.plot(times, vals, color=line_color, linewidth=2.2, marker="o", markersize=4)
        ax.fill_between(times, vals, 0, alpha=0.18, color=line_color)
        ax.axhline(0, color="#666666", linewidth=0.8, linestyle="--")
        # Anotação do valor final
        ax.annotate(f"R$ {last:+.2f}", xy=(times[-1], vals[-1]),
                    xytext=(8, 0), textcoords="offset points",
                    color=line_color, fontsize=12, fontweight="bold", va="center")
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M"))
        fig.autofmt_xdate()

    ax.set_title(f"Vibe-Trading — PnL acumulado · {today}",
                 color="#ffffff", fontsize=14, fontweight="bold", pad=14)
    ax.set_xlabel("Hora", color="#aaaaaa", fontsize=10)
    ax.set_ylabel("PnL realizado (R$)", color="#aaaaaa", fontsize=10)
    ax.tick_params(colors="#cccccc")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#444444")
    ax.spines["bottom"].set_color("#444444")
    ax.grid(True, alpha=0.15)

    out = Path(f"/tmp/vt_intraday_{today}.png")
    fig.tight_layout()
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


def generate_report():
    """Relatório INTRADAY: evolução do dia até o momento (sem histórico 5d).

    FASE 1 (data/architecture_proposal_2026_07_01.md linha 280-320):
    - PnL realizado vem do MT5 history (broker-truth) via check_intraday_stats()
    - Balance/equity vem de get_truth_from_mt5() (helper centralizado, Wave 12.1)
    - Emite aviso quando MT5 indisponível (não silencia)
    """
    report = []

    # 1. Status do autotrader
    health = check_autotrader_health()
    if health["running"]:
        report.append(f"✅ Autotrader: rodando (PID {health['pid']})")
    else:
        report.append("❌ Autotrader: PARADO")

    # 1.5 Balance/equity do MT5 (helper centralizado Wave 12.1).
    # Falha aqui = aviso explicito (NUNCA silenciar).
    truth = get_truth_from_mt5()
    if truth.get("ok"):
        bal = truth.get("balance", 0.0)
        eq = truth.get("equity", 0.0)
        report.append(f"💰 Saldo MT5: R$ {bal:,.2f} · Equity: R$ {eq:,.2f}")
    else:
        report.append(f"⚠️ MT5 indisponível ({truth.get('error', '?')})")

    # 2. Estatísticas intraday
    s = check_intraday_stats()
    wr = (s["wins"] / s["ops"] * 100) if s["ops"] > 0 else 0

    report.append("")
    report.append(f"📈 *Intrade* ({datetime.now().strftime('%H:%M')})")
    # Mostra a fonte do PnL realizado (FASE 1: MT5_EVENTS vs MT5_HISTORY vs DB_FALLBACK)
    _src = s.get("source", "")
    if _src == "MT5_EVENTS":
        source_label = "broker-truth (EA events, local)"
    elif _src == "MT5_HISTORY":
        source_label = "broker-truth (MT5 Wine)"
    else:
        # DB_FALLBACK: MT5 pode estar ON, mas o pipeline EA events ficou stale
        # (ou MT5 history vazio). Mostra o motivo real em vez do antigo
        # "MT5 off" (enganoso — o saldo/posições vinham do MT5 normalmente).
        _err = s.get("truth_error")
        source_label = f"DB fallback ({_err})" if _err else "DB fallback"
    report.append(f"  _PnL realizado: {source_label}_")
    if s["ops"] > 0:
        report.append(
            f"  Trades: {s['ops']} (W:{s['wins']} L:{s['losses']} · WR {wr:.0f}%)"
        )
        report.append(f"  PnL realizado: R$ {s['pnl_realized']:+.2f}")
        report.append(
            f"  PnL flutuante ({s['open_count']} abertas): R$ {s['open_pnl']:+.2f}"
        )
        report.append(f"  *PnL total: R$ {s['pnl_total']:+.2f}*")
        report.append(
            f"  Melhor trade: R$ {s['best_trade']:+.2f} · Pior: R$ {s['worst_trade']:+.2f}"
        )
        report.append(f"  Max drawdown: R$ {s['max_drawdown']:.2f}")
    else:
        # Wave 1C.2: mesmo com 0 trades "normais" fechados, o FALLBACK-BALANCE
        # pode ter capturado PnL broker-truth. Mostrar.
        report.append("  Sem trades fechados hoje (DB limpo)")
        if s.get("pnl_realized", 0.0) != 0.0:
            # CAUTION: GHOST trades podem ter ocorrido. Mostrar aviso.
            try:
                conn = sqlite3.connect(str(DB_PATH))
                n_ghost = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE date(exit_time)=date('now','localtime') "
                    "AND exit_reason='GHOST'"
                ).fetchone()[0]
                conn.close()
                if n_ghost > 0:
                    report.append(
                        f"  ⚠️ {n_ghost} trade(s) GHOST (PnL real indisponivel — bug autotrader/MT5 demo)"
                    )
            except Exception:
                pass
            report.append(f"  PnL realizado (broker-truth via saldo): R$ {s['pnl_realized']:+.2f}")
        if s["open_count"] > 0:
            report.append(
                f"  PnL flutuante ({s['open_count']} abertas): R$ {s['open_pnl']:+.2f}"
            )
        if s.get("pnl_total", 0.0) != 0.0:
            report.append(f"  *PnL total: R$ {s['pnl_total']:+.2f}*")

    # 3. Posições abertas (detalhe)
    if s["open_count"] > 0:
        try:
            mt5_state = mt5_status()
            report.append("")
            report.append(f"⚠️ *{s['open_count']} posição(ões) aberta(s)*")
            for p in mt5_state.get("positions", [])[:5]:
                pnl = p.get("profit", 0)
                icon = "🟢" if pnl >= 0 else "🔴"
                report.append(
                    f"  {icon} {p.get('symbol')} {p.get('type')} · PnL R$ {pnl:+.2f}"
                )
        except Exception:
            pass

    return "\n".join(report)


def _restore_pauses_if_needed():
    """No primeiro run do dia, reativa símbolos/timeframes desativados no dia anterior.
    Limpa disabled_symbols/disabled_timeframes do config."""
    from core.vt_config_loader import load_config, save_full_config

    pause_file = Path("/tmp/vt_paused_timeframes.json")
    if not pause_file.exists():
        return

    try:
        active_pauses = json.loads(pause_file.read_text())
    except Exception:
        return

    if not active_pauses:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Só restaurar se as pausas são de ontem
    old_pauses = {k: v for k, v in active_pauses.items() if v.get("date") == yesterday}
    if not old_pauses:
        return

    log(f"[RESTORE] Restaurando {len(old_pauses)} pausas do dia anterior...")

    config = load_config(force=True)
    config["disabled_symbols"] = []
    config["disabled_timeframes"] = []
    save_full_config(config, updated_by="copilot_restore")
    log("[RESTORE] disabled_symbols/timeframes limpos. Autotrader fará hot-reload.")
    pause_file.write_text("{}")


def main():
    """Execução principal do Copilot."""
    log("=" * 50)
    log("Vibe-Trading Copilot INICIADO")
    log("=" * 50)

    # Determinar o que fazer
    mode = sys.argv[1] if len(sys.argv) > 1 else "--full"

    actions = []

    if mode == "--health":
        health = check_autotrader_health()
        if not health["running"]:
            if restart_autotrader():
                actions.append("Autotrader reiniciado")
        elif not health["log_fresh"]:
            actions.append("Autotrader rodando mas log antigo (>5min)")
        return

    elif mode == "--reconcile":
        reconciled = reconcile_orphans()
        if reconciled > 0:
            actions.append(f"{reconciled} órfãos reconciliados")
        return

    elif mode == "--report":
        report = generate_report()
        notify_telegram(f"🤖 *Copilot {datetime.now().strftime('%Hh%M')}*\n\n{report}")

        # Gráfico intraday (broker-truth via EA events) — mesmo padrão do --full.
        stats = check_intraday_stats()
        today_str = datetime.now().strftime("%Y-%m-%d")
        chart_path = render_pnl_chart(
            stats["pnl_cum"], today_str, balance_history_path=str(_BH_DEFAULT_PATH)
        )
        chart_caption = (
            f"📊 PnL realizado · {datetime.now().strftime('%d/%m %H:%M')} · "
            f"Total: R$ {stats['pnl_total']:+.2f}"
        )
        notify_telegram_media(chart_path, chart_caption)
        return

    elif mode == "--self-heal":
        # Modo isolado: só roda o self-heal monitor (6 checks + auto-cura).
        summary = run_self_heal_hook()
        log(f"[SELF-HEAL] {summary or 'saudável'}")
        return

    else:  # --full (padrão)
        # 0. Self-heal hook (Fase 2.2) — roda ANTES do health check do copilot.
        #    Complementa (não substitui): o copilot checa só autotrader; o
        #    self-heal checa também MT5/DB/state/lock/cron. Auto-cura é
        #    conservadora e nunca bloqueia o fluxo nem desabilita símbolo (Lei 2).
        self_heal_summary = run_self_heal_hook()
        if self_heal_summary:
            actions.append(self_heal_summary)

        # 0b. No primeiro run do dia (10h), restaurar pausas do dia anterior
        if datetime.now().hour == 10:
            _restore_pauses_if_needed()

        # 1. Health check
        health = check_autotrader_health()
        if not health["running"]:
            if restart_autotrader():
                actions.append("🔄 Autotrader reiniciado")
            else:
                actions.append("❌ Falha ao reiniciar autotrader!")
        elif not health["log_fresh"]:
            actions.append("⚠️ Autotrader com log antigo")

        # 2. Reconciliação de órfãos
        reconciled = reconcile_orphans()
        if reconciled > 0:
            actions.append(f"🔧 {reconciled} órfãos reconciliados")

        # 3. Verificar WDO
        wdo = check_wdo_activity()
        if wdo == "calmo":
            actions.append("🟡 WDO: mercado calmo (threshold adaptativo ativo)")
        elif wdo == "sem_dados":
            actions.append("❌ WDO: sem dados de barras!")

        # 4. Avaliar performance e pausar se necessário
        paused = evaluate_and_pause()
        if paused:
            actions.append(f"⏸️ Pausado: {', '.join(paused)}")

        # 5. Gerar e enviar relatório + gráfico intraday
        report = generate_report()
        stats = check_intraday_stats()
        today_str = datetime.now().strftime("%Y-%m-%d")
        chart_path = render_pnl_chart(
            stats["pnl_cum"], today_str, balance_history_path=str(_BH_DEFAULT_PATH)
        )

        # Montar mensagem final
        msg_parts = [
            f"🤖 *Copilot {datetime.now().strftime('%Hh%M')}*",
            "",
            report,
        ]

        if actions:
            msg_parts.extend([
                "",
                "⚡ *Ações tomadas:*",
                "\n".join(f"  • {a}" for a in actions)
            ])

        notify_telegram("\n".join(msg_parts))

        # Envia gráfico (caption curta, Telegram aceita 1024)
        chart_caption = (
            f"📊 PnL realizado · {datetime.now().strftime('%d/%m %H:%M')} · "
            f"Total: R$ {stats['pnl_total']:+.2f}"
        )
        notify_telegram_media(chart_path, chart_caption)

        log("=" * 50)
        log(f"Copilot finalizado. {len(actions)} ações tomadas.")
        log("=" * 50)


if __name__ == "__main__":
    main()
