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

# Critérios de pausa automática
PAUSE_CRITERIA = {
    "min_trades": 15,      # Mínimo de trades pra decidir pausar
    "max_wr": 35,          # Win rate máximo pra pausar (%)
    "max_pnl": 0,          # PnL máximo (negativo = perda)
}

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

        if ops >= PAUSE_CRITERIA["min_trades"]:
            wr = (wins / ops * 100) if ops > 0 else 0
            if wr < PAUSE_CRITERIA["max_wr"] and total_pnl < PAUSE_CRITERIA["max_pnl"]:
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


def generate_report():
    """Gera relatório completo para notificação."""
    report = []

    # Status do autotrader
    health = check_autotrader_health()
    if health["running"]:
        report.append(f"✅ Autotrader: rodando (PID {health['pid']})")
    else:
        report.append("❌ Autotrader: PARADO")

    # Performance (últimos 5 dias)
    stats = check_performance()
    report.append("\n📊 *Performance (5 dias)*")

    for row in stats["sym_tf"]:
        symbol, tf, ops, wins, losses, avg_pnl, total_pnl = row
        if ops > 0:
            wr = wins / ops * 100
            report.append(f"  {symbol} {tf}: {ops} ops | W:{wins} L:{losses} | "
                         f"WR {wr:.0f}% | PnL R${total_pnl:+.2f}")

    # WDO
    wdo_status = check_wdo_activity()
    report.append(f"\n🟡 WDO: {wdo_status}")

    # Trades abertos
    conn = sqlite3.connect(str(DB_PATH))
    open_count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE exit_time IS NULL"
    ).fetchone()[0]
    conn.close()

    if open_count > 0:
        report.append(f"⚠️ {open_count} posição(ões) aberta(s)")

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
        
        # 5. Gerar e enviar relatório
        report = generate_report()
        
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
        
        log("=" * 50)
        log(f"Copilot finalizado. {len(actions)} ações tomadas.")
        log("=" * 50)


if __name__ == "__main__":
    main()
