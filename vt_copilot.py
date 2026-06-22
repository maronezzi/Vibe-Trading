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
import os
import sqlite3
import subprocess
import signal
from datetime import datetime, timedelta
from pathlib import Path

# Adicionar projeto ao path
sys.path.insert(0, str(Path(__file__).parent))

from mt5_orchestrator import status as mt5_status, _run_wine, EXECUTOR_WIN
from vt_config_loader import load_config

# ===== CONFIGURAÇÃO =====
DB_PATH = Path(__file__).parent / "vt_trades.db"
LOG_PATH = Path("/tmp/vt_autotrader.log")
TELEGRAM_TARGET = "telegram:-1004284773048"

# Critérios de pausa automática — LIDOS DO vt_config.json (pause_criteria)
# Não hardcodar aqui. Fonte única: vt_config.json
PAUSE_CRITERIA = {"min_trades": 15, "max_wr": 35, "max_pnl": 0}  # fallback (config é autoridade)


def _load_pause_criteria():
    """Carrega pause_criteria do vt_config.json. Se disabled ou ausente, retorna None."""
    try:
        from vt_config_loader import load_config
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
        log(f"Notificação enviada pro grupo")
    except Exception as e:
        log(f"[ERRO] Falha ao enviar notificação: {e}")


def notify_telegram_media(media_path, caption=""):
    """Envia mídia (PNG) pro Telegram via hermes CLI. Caption limitado a 1024 chars.

    O Hermes envia mídia inline com prefixo MEDIA: no texto (Telegram, Discord, etc).
    Caption é o texto que aparece junto.
    """
    import subprocess
    try:
        # Caption curta (Telegram aceita 1024)
        body = f"MEDIA:{media_path}"
        if caption:
            body = f"{caption}\n\n{body}"
        cmd = ["hermes", "send", "--to", TELEGRAM_TARGET, body]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log(f"Mídia enviada: {media_path}")
        else:
            log(f"[WARN] hermes retornou {result.returncode}: {result.stderr[:200]}")
    except Exception as e:
        log(f"[ERRO] Falha ao enviar mídia: {e}")


def check_autotrader_health():
    """Verifica se o autotrader está rodando e com log fresco."""
    import subprocess
    
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
    import subprocess
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
        cwd=str(Path(__file__).parent),
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
                multiplier = 0.20 if "WIN" in trade["symbol"] else 1.00
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
    from vt_config_loader import load_config, save_full_config

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
        log(f"[PAUSA] Config atualizado. Autotrader fará hot-reload.")
    else:
        log(f"[PAUSA] Nenhuma alteração necessária")


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
    import json

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

    Substitui o antigo check_performance() (janela 5 dias) como entrada do
    generate_report(). Mantém evaluate_and_pause() usando a janela maior.

    Retorna dict com:
        ops, wins, losses, pnl_realized: agregados dos trades fechados hoje
        open_count, open_pnl: posições abertas via MT5 status()
        pnl_total: pnl_realized + open_pnl
        pnl_cum: lista [(exit_time_iso, pnl_acumulado)] em ordem cronológica
        max_drawdown: pior queda do peak até o fundo
        best_trade, worst_trade: extremos do dia
    """
    conn = sqlite3.connect(str(DB_PATH))
    today = datetime.now().strftime("%Y-%m-%d")

    # Realizado: trades fechados hoje (exclui stale_close — contratos antigos limpos manualmente)
    closed = conn.execute("""
        SELECT COUNT(*) ops,
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) losses,
               COALESCE(SUM(net_pnl), 0) pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND date(exit_time) = ?
          AND exit_reason != 'stale_close'
    """, (today,)).fetchone()

    # Série temporal ordenada
    pnl_series = conn.execute("""
        SELECT exit_time, net_pnl
        FROM trades
        WHERE exit_time IS NOT NULL
          AND date(exit_time) = ?
          AND exit_reason != 'stale_close'
        ORDER BY exit_time
    """, (today,)).fetchall()
    conn.close()

    # Acumulado + max drawdown
    pnl_cum = []
    acc = 0.0
    peak = 0.0
    max_dd = 0.0
    for t, p in pnl_series:
        acc += p
        pnl_cum.append((t, round(acc, 2)))
        peak = max(peak, acc)
        max_dd = min(max_dd, acc - peak)

    # Posições abertas via MT5 (fonte da verdade, nao o DB)
    open_count, open_pnl = 0, 0.0
    try:
        mt5_state = mt5_status()
        positions = mt5_state.get("positions", [])
        open_count = len(positions)
        open_pnl = round(sum(p.get("profit", 0) for p in positions), 2)
    except Exception as e:
        log(f"[WARN] Não foi possível obter MT5 status: {e}")

    pnl_realized = round(closed[3] or 0.0, 2)

    return {
        "ops": closed[0] or 0,
        "wins": closed[1] or 0,
        "losses": closed[2] or 0,
        "pnl_realized": pnl_realized,
        "open_count": open_count,
        "open_pnl": open_pnl,
        "pnl_total": round(pnl_realized + open_pnl, 2),
        "pnl_cum": pnl_cum,
        "max_drawdown": round(max_dd, 2),
        "best_trade": round(max((p for _, p in pnl_series), default=0.0), 2),
        "worst_trade": round(min((p for _, p in pnl_series), default=0.0), 2),
    }


def render_pnl_chart(pnl_cum: list, today: str) -> Path:
    """Gera PNG da evolução intraday do PnL realizado.

    Tema escuro, igual ao terminal/IDE. Linha verde se último valor >= 0,
    vermelha se < 0. Se pnl_cum vazio, mostra placeholder.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

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
    """Relatório INTRADAY: evolução do dia até o momento (sem histórico 5d)."""
    report = []

    # 1. Status do autotrader
    health = check_autotrader_health()
    if health["running"]:
        report.append(f"✅ Autotrader: rodando (PID {health['pid']})")
    else:
        report.append("❌ Autotrader: PARADO")

    # 2. Estatísticas intraday
    s = check_intraday_stats()
    wr = (s["wins"] / s["ops"] * 100) if s["ops"] > 0 else 0

    report.append("")
    report.append(f"📈 *Intrade* ({datetime.now().strftime('%H:%M')})")
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
        report.append(f"  Sem trades fechados hoje")
        if s["open_count"] > 0:
            report.append(
                f"  PnL flutuante ({s['open_count']} abertas): R$ {s['open_pnl']:+.2f}"
            )

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
    from vt_config_loader import load_config, save_full_config

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
    log(f"[RESTORE] disabled_symbols/timeframes limpos. Autotrader fará hot-reload.")
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
        return
    
    else:  # --full (padrão)
        # 0. No primeiro run do dia (10h), restaurar pausas do dia anterior
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
        chart_path = render_pnl_chart(stats["pnl_cum"], today_str)

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
