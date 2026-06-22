#!/usr/bin/env python3
"""
Vibe-Trading Market Analyst — coleta métricas a cada barra e detecta
eventos que justificam uma análise LLM.

Funciona em 3 camadas:
  1. COLETA (cada barra, zero tokens): grava snapshot em JSON
  2. DETECÇÃO (cada barra, zero tokens): compara com médias, marca anomalias
  3. ALERTA (quando detecta): manda Telegram rápido (zero tokens)
  4. ANÁLISE (cron 1x/hora ou sob demanda): chama LLM com contexto rico

Eventos que disparam alerta:
  - Volume spike (> 2x média das últimas 20 barras)
  - Volatilidade spike (ATR > 2x média)
  - Trade aberto com drawdown > 50% do ATR
  - 3+ trades consecutivos perdendo
  - Reversão forte (close 2x ATR contra posição)
  - VWAP cruzamento (preço atravessou VWAP)
  - Breakout (preço rompeu máxima/mínima das últimas 10 barras)

Uso:
    python vt_analyst.py              # Uma análise com dados atuais
    python vt_analyst.py --watch      # Monitora continuamente (loop 30s)
    python vt_analyst.py --snapshot   # Salva snapshot sem analisar
"""

import sys
import json
import os
import time
from datetime import datetime
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).parent.parent))

from mt5.mt5_orchestrator import _run_wine, EXECUTOR_WIN, status, tick
from core.vt_config_loader import load_config

SNAPSHOT_FILE = Path("/tmp/vt_market_state.json")
ANOMALY_FILE = Path("/tmp/vt_anomalies.jsonl")
SNAPSHOT_HISTORY = Path("/tmp/vt_market_history.jsonl")
AUTOTRADER_STATE_FILE = Path("/tmp/vt_autotrader_state.json")


def load_state_from_disk() -> dict:
    """Lê o state do autotrader do disco, com fallback seguro.

    Returns:
        Dict com state, ou dict vazio se arquivo não existe / malformado.
    """
    try:
        if AUTOTRADER_STATE_FILE.exists():
            import json as _j
            with open(AUTOTRADER_STATE_FILE) as _f:
                return _j.load(_f)
    except Exception:
        pass
    return {}


def find_real_position(symbol: str, tf_hint: str = "") -> tuple[dict | None, str]:
    """Busca a posição REAL aberta no state do autotrader.

    Diferente de fetch_snapshot (que usa status()["positions"] e retorna agregado
    do MT5 por symbol), esta função busca em state.positions que tem chaves
    f"{symbol}_{tf}" — uma chave por trade específica. Isso garante volume, tf
    e atr corretos nos alerts (DRAWDOWN, VOLATILITY_SPIKE etc).

    Args:
        symbol: Símbolo do ativo (ex: "INDM26").
        tf_hint: TF preferido (ex: "M30"). Se vazio, pega a primeira posição
            aberta do symbol.

    Returns:
        (position_dict, tf_real) — position pode ter campos extras como
        entry_price, atr, volume, tf, sl_pts. tf_real é o TF da posição.
        Retorna (None, "") se não achar.
    """
    state = load_state_from_disk()
    positions = state.get("positions", {})

    # Primeiro tenta a chave exata com tf_hint
    if tf_hint:
        key = f"{symbol}_{tf_hint}"
        if key in positions:
            return positions[key], tf_hint

    # Senão pega a primeira posição do symbol (qualquer TF)
    for key, pos in positions.items():
        if key.startswith(f"{symbol}_"):
            tf_real = key.split("_", 1)[1] if "_" in key else ""
            return pos, tf_real

    return None, ""

# Médias históricas (populadas ao longo do dia) — inicializadas para TODOS os ativos
def _init_metrics_buffer():
    """Cria METRICS_BUFFER dinamicamente a partir dos símbolos ativos no config."""
    buf = {}
    try:
        from core.vt_config_loader import CONFIG as _cfg
        symbols = _cfg.get("symbols", ["WIN", "WDO", "BIT", "DOL", "IND", "WSP"])
    except Exception:
        symbols = ["WIN", "WDO", "BIT", "DOL", "IND", "WSP"]
    for sym in symbols:
        buf[sym] = {"volumes": deque(maxlen=40), "atrs": deque(maxlen=40), "spreads": deque(maxlen=40)}
    return buf

METRICS_BUFFER = _init_metrics_buffer()


def log_anomaly(symbol, event_type, data):
    """Grava anomalia para análise posterior."""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol,
        "event": event_type,
        "data": data,
    }
    try:
        with open(ANOMALY_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# Debounce: evita spam de alertas repetidos (só 1 por tipo/símbolo a cada N minutos)
_last_notify: dict[str, float] = {}

DEBOUNCE_MINUTES = {
    "VOLUME_SPIKE": 30,
    "VOLATILITY_SPIKE": 15,
    "DRAWDOWN": 10,
    "STREAK_LOSS": 5,
    "REVERSAL": 30,
    "VWAP_CROSS": 60,   # muito frequente, só 1x por hora
    "BREAKOUT": 10,
}

# Eventos que NÃO valem notificação Telegram (só log interno)
_SILENT_EVENTS = {"VWAP_CROSS"}  # VWAP cross é informação, não alerta


def notify(event_type: str, symbol: str, msg: str, tf: str = ""):
    """Alerta rápido via Telegram (zero LLM). Com debounce anti-spam."""
    # Eventos silenciosos: só log, sem Telegram
    if event_type in _SILENT_EVENTS:
        return

    now = time.time()
    key = f"{event_type}_{symbol}"
    cooldown = DEBOUNCE_MINUTES.get(event_type, 15) * 60

    if key in _last_notify and (now - _last_notify[key]) < cooldown:
        return  # debounce: não repete antes do cooldown

    _last_notify[key] = now

    icons = {
        "VOLUME_SPIKE": "📈",
        "VOLATILITY_SPIKE": "⚡",
        "DRAWDOWN": "⚠️",
        "STREAK_LOSS": "🔻",
        "REVERSAL": "🔄",
        "BREAKOUT": "🚀",
    }
    icon = icons.get(event_type, "🔔")
    symbol_label = f"{symbol} {tf}" if tf else symbol
    try:
        from vt_hermes_helper import hermes_send
        hermes_send("telegram:-1004284773048", f"{icon} *{event_type}* {symbol_label}\n{msg}")
    except Exception:
        pass


def fetch_snapshot(symbol: str, tf: str = "M5") -> dict:
    """Coleta snapshot completo do mercado."""
    bars_result = _run_wine(EXECUTOR_WIN, "bars", symbol, tf, "30")
    bars = bars_result.get("bars", [])
    if not bars:
        return {"error": "sem barras"}

    tick_data = tick(symbol)
    status_data = status()
    positions = status_data.get("positions", [])

    # VWAP e ATR
    vwap = calc_vwap(bars[:20])
    atr = calc_atr(bars[:15])

    # Estatísticas das últimas 20 barras
    recent = bars[:20]
    volumes = [b["volume"] for b in recent]
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    max_vol = max(volumes) if volumes else 0

    # Preço atual vs VWAP
    current = bars[0]["close"]
    vwap_distance_pct = ((current - vwap) / vwap * 100) if vwap else 0

    # Range da sessão
    highs = [b["high"] for b in bars[:10]]
    lows = [b["low"] for b in bars[:10]]
    session_high = max(highs) if highs else 0
    session_low = min(lows) if lows else 0

    # Spread atual
    spread = tick_data.get("ask", 0) - tick_data.get("bid", 0) if tick_data else 0

    # Momentum (comparação últimos 5 closes)
    if len(bars) >= 6:
        mom5 = (bars[0]["close"] - bars[5]["close"]) / bars[5]["close"] * 100
    else:
        mom5 = 0

    # Tendência das últimas 10 barras
    if len(bars) >= 10:
        sma5 = sum(b["close"] for b in bars[:5]) / 5
        sma10 = sum(b["close"] for b in bars[:10]) / 10
        trend = "ALTA" if sma5 > sma10 else "BAIXA"
    else:
        trend = "NEUTRO"

    # Posição ativa neste símbolo
    pos = None
    for p in positions:
        if p["symbol"] == symbol:
            pos = p
            break

    snapshot = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol,
        "timeframe": tf,
        "price": current,
        "bid": tick_data.get("bid", 0) if tick_data else 0,
        "ask": tick_data.get("ask", 0) if tick_data else 0,
        "spread": spread,
        "vwap": round(vwap, 2),
        "vwap_distance_pct": round(vwap_distance_pct, 3),
        "atr": round(atr, 2),
        "avg_volume": int(avg_vol),
        "current_volume": bars[0]["volume"],
        "session_high": session_high,
        "session_low": session_low,
        "momentum_5bar": round(mom5, 4),
        "trend": trend,
        "sma5": round(sma5, 2) if len(bars) >= 10 else 0,
        "sma10": round(sma10, 2) if len(bars) >= 10 else 0,
        "n_positions": len(positions),
        "position": pos,
        "bars_count": len(bars),
    }

    # Buffer de métricas (para comparação histórica)
    root = symbol[:3]  # "WIN" or "WDO"
    if root in METRICS_BUFFER:
        METRICS_BUFFER[root]["volumes"].append(bars[0]["volume"])
        METRICS_BUFFER[root]["atrs"].append(atr)
        METRICS_BUFFER[root]["spreads"].append(spread)

    return snapshot


def calc_vwap(bars):
    sum_pv = sum_v = 0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        vol = max(b["volume"], 1)
        sum_pv += typical * vol
        sum_v += vol
    return sum_pv / sum_v if sum_v > 0 else 0


def calc_atr(bars):
    if len(bars) < 2:
        return 0
    tr_sum = 0
    n = min(14, len(bars) - 1)
    for i in range(n):
        h, l = bars[i]["high"], bars[i]["low"]
        c_prev = bars[i + 1]["close"]
        tr_sum += max(h - l, abs(h - c_prev), abs(l - c_prev))
    return tr_sum / n


def detect_anomalies(snapshot: dict) -> list:
    """Detecta eventos anômalos que merecem atenção."""
    anomalies = []
    symbol = snapshot["symbol"]
    tf = snapshot.get("timeframe", "")
    root = symbol[:3]
    buf = METRICS_BUFFER.get(root)

    if not buf or len(buf["volumes"]) < 5:
        return anomalies

    avg_vol = sum(buf["volumes"]) / len(buf["volumes"])
    avg_atr = sum(buf["atrs"]) / len(buf["atrs"])
    avg_spread = sum(buf["spreads"]) / len(buf["spreads"]) if any(s > 0 for s in buf["spreads"]) else 0

    # Enriquece snapshot com posição REAL do state (se houver).
    # snapshot.position vem de status()["positions"] (agregado MT5 do symbol),
    # o que causa: (a) TF errado (usa tf da iteração, não da trade),
    # (b) volume inflado (soma de múltiplas ordens), (c) atr/atr_ratio
    # calculado com ATR do snapshot, não da posição.
    # state.positions tem chave f"{symbol}_{tf}" — uma chave por trade.
    snap_pos = snapshot.get("position")
    real_pos, tf_real = find_real_position(symbol, tf)
    if real_pos and snap_pos:
        # Substitui o tf da iteração pelo tf da posição real
        tf = tf_real or tf
        # Sobrescreve campos com dados do state
        # volume: state guarda o volume da trade (1), não agregado MT5
        if "volume" in real_pos:
            snap_pos["volume"] = real_pos["volume"]
        # atr: state guarda o ATR capturado na entrada (real, não do snapshot)
        if "atr" in real_pos and real_pos["atr"] > 0:
            snap_pos["atr"] = real_pos["atr"]
        # SL em pontos: state guarda sl_pts (200), em vez do SL_price
        if "sl_pts" in real_pos and "sl" in snap_pos:
            # Mantém sl_price (do MT5), mas exibe o sl_pts nos logs
            snap_pos["sl_pts"] = real_pos["sl_pts"]
    elif real_pos and not snap_pos:
        # State tem posição mas snapshot não (raro — divergence)
        snapshot["position"] = {
            "type": real_pos.get("direction", ""),
            "symbol": symbol,
            "volume": real_pos.get("volume", 1),
            "price_open": real_pos.get("entry_price", 0),
            "sl": real_pos.get("entry_price", 0) - real_pos.get("sl_pts", 0) if real_pos.get("direction") == "BUY" else real_pos.get("entry_price", 0) + real_pos.get("sl_pts", 0),
            "profit": 0,
            "ticket": real_pos.get("entry_ticket", ""),
            "atr": real_pos.get("atr", 0),
        }
        tf = tf_real or tf


    # 1. Volume spike
    if snapshot["current_volume"] > avg_vol * 2 and avg_vol > 0:
        ratio = snapshot["current_volume"] / avg_vol
        price = snapshot.get("price", 0)
        vwap = snapshot.get("vwap", 0)
        trend = snapshot.get("trend", "?")
        pos = snapshot.get("position")
        msg_parts = [
            f"Volume {ratio:.0f}x acima do normal",
            f"• Preço: {price:.2f} | VWAP: {vwap:.2f} ({snapshot.get('vwap_distance_pct', 0):+.2f}%)",
            f"• Tendência: {trend} | ATR: {snapshot.get('atr', 0):.0f}",
        ]
        if pos:
            pdir = "BUY" if str(pos.get("type", "")).endswith("BUY") or pos.get("type") in (0,) else "SELL"
            pnl = pos.get("profit", 0)
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg_parts.append(f"• Posição: {pdir} {pos.get('price_open', 0):.2f} {emoji} R$ {pnl:.2f}")
        anomalies.append({
            "type": "VOLUME_SPIKE",
            "severity": "ALTO" if ratio > 3 else "MÉDIO",
            "msg": "\n".join(msg_parts),
            "tf": tf
        })

    # 2. Volatilidade spike
    if avg_atr > 0 and snapshot["atr"] > avg_atr * 2:
        ratio = snapshot["atr"] / avg_atr
        price = snapshot.get("price", 0)
        pos = snapshot.get("position")
        msg_parts = [
            f"ATR {snapshot['atr']:.0f} = {ratio:.0f}x acima da média ({avg_atr:.0f})",
            f"• Preço: {price:.2f} | Spread: {snapshot.get('spread', 0):.2f}",
        ]
        if pos:
            pdir = "BUY" if str(pos.get("type", "")).endswith("BUY") or pos.get("type") in (0,) else "SELL"
            pnl = pos.get("profit", 0)
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg_parts.append(f"• Posição: {pdir} {pos.get('price_open', 0):.2f} {emoji} R$ {pnl:.2f}")
        anomalies.append({
            "type": "VOLATILITY_SPIKE",
            "severity": "ALTO" if ratio > 2.5 else "MÉDIO",
            "msg": "\n".join(msg_parts),
            "tf": tf
        })

    # 3. Drawdown na posição (só quando pnl < 0 — prejuízo real)
    pos = snapshot.get("position")
    if pos:
        pnl = pos.get("profit", 0)
        entry = pos.get("price_open", 0)
        # Preferir atr da posição real (state) sobre o atr do snapshot,
        # pois o snapshot pode estar usando o atr de outro TF (M5 vs M30)
        atr = pos.get("atr", 0) or snapshot["atr"]
        if atr > 0 and entry > 0 and pnl < 0:
            # Usar get_multiplier para multiplicador correto por ativo
            try:
                from vt_trade_log import get_multiplier
                mult = get_multiplier(symbol)
            except Exception:
                mult = 0.20 if "WIN" in symbol else 10.0 if "WDO" in symbol else 1.0
            drawdown_pts = abs(pnl) / mult
            if drawdown_pts > atr * 0.5:
                # Coletar dados para mensagem rica
                direction = pos.get("type", "")
                if isinstance(direction, int):
                    direction = "BUY" if direction in (0,) else "SELL"
                sl_price = pos.get("sl", 0)
                current = snapshot.get("price", 0)
                ticket = pos.get("ticket", pos.get("id", ""))
                volume = pos.get("volume", 1)
                # Distância até o SL
                if sl_price and entry:
                    if direction == "BUY":
                        sl_dist = abs(current - sl_price) if current else 0
                        sl_total = abs(entry - sl_price)
                    else:
                        sl_dist = abs(sl_price - current) if current else 0
                        sl_total = abs(sl_price - entry)
                    sl_pct = (1 - sl_dist / sl_total * 100) if sl_total > 0 else 0
                else:
                    sl_dist = sl_total = sl_pct = 0
                # Duração
                duration = ""
                try:
                    import json as _json
                    _state = _json.load(open("/tmp/vt_autotrader_state.json"))
                    _pos_key = f"{symbol}_{tf}"
                    _pos_state = _state.get("positions", {}).get(_pos_key, {})
                    _entry_time = _pos_state.get("entry_time", "")
                    if _entry_time:
                        from datetime import datetime as _dt
                        _et = _dt.fromisoformat(_entry_time) if "T" in str(_entry_time) else None
                        if _et:
                            _mins = int((_dt.now() - _et).total_seconds() / 60)
                            duration = f"{_mins}min"
                except Exception:
                    pass
                # PnL dia
                pnl_dia = ""
                try:
                    import json as _json2
                    _state2 = _json2.load(open("/tmp/vt_autotrader_state.json"))
                    _dp = _state2.get("daily_pnl", 0)
                    pnl_dia = f"R$ {_dp:+.0f}"
                except Exception:
                    pass
                # ATR ratio
                atr_ratio = drawdown_pts / atr if atr > 0 else 0
                severity = "ALTO" if drawdown_pts > atr else "MÉDIO"
                msg_parts = [
                    f"{direction} {symbol} {tf}",
                    f"• Prejuízo: R$ {abs(pnl):.2f}",
                    f"• Entrada: {entry:.2f} → Atual: {current:.2f}",
                ]
                if sl_price:
                    msg_parts.append(f"• SL: {sl_price:.2f} ({sl_dist:.0f}pts restantes)")
                msg_parts.append(f"• ATR: {atr:.0f} | Drawdown: {atr_ratio:.1f}x ATR")
                if ticket:
                    msg_parts.append(f"• Ticket: {ticket} | Vol: {volume}")
                if duration:
                    msg_parts.append(f"• Duração: {duration}")
                if pnl_dia:
                    msg_parts.append(f"• PnL Dia: {pnl_dia}")
                anomalies.append({
                    "type": "DRAWDOWN",
                    "severity": severity,
                    "msg": "\n".join(msg_parts),
                    "tf": tf
                })

    # 4. VWAP cruzamento — info only, não alerta
    vwap_dist = snapshot["vwap_distance_pct"]
    if abs(vwap_dist) < 0.05:
        anomalies.append({
            "type": "VWAP_CROSS",
            "severity": "BAIXO",
            "msg": f"Preço no VWAP ({vwap_dist:+.3f}%)",
            "tf": tf
        })

    # 5. Breakout de sessão
    current = snapshot["price"]
    if current > snapshot["session_high"] * 1.001:
        breakout_pct = (current - snapshot["session_high"]) / snapshot["session_high"] * 100
        pos = snapshot.get("position")
        msg_parts = [
            f"⬆ Rompeu MÁXIMA: {current:.2f} (+{breakout_pct:.2f}%)",
            f"• Máxima anterior: {snapshot['session_high']:.2f}",
            f"• VWAP: {snapshot.get('vwap', 0):.2f} ({snapshot.get('vwap_distance_pct', 0):+.2f}%)",
            f"• Tendência: {snapshot.get('trend', '?')} | ATR: {snapshot.get('atr', 0):.0f}",
        ]
        if pos:
            pdir = "BUY" if str(pos.get("type", "")).endswith("BUY") or pos.get("type") in (0,) else "SELL"
            pnl = pos.get("profit", 0)
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg_parts.append(f"• Posição: {pdir} {pos.get('price_open', 0):.2f} {emoji} R$ {pnl:.2f}")
        anomalies.append({
            "type": "BREAKOUT",
            "severity": "ALTO",
            "msg": "\n".join(msg_parts),
            "tf": tf
        })
    elif current < snapshot["session_low"] * 0.999:
        breakout_pct = (snapshot["session_low"] - current) / snapshot["session_low"] * 100
        pos = snapshot.get("position")
        msg_parts = [
            f"⬇ Rompeu MÍNIMA: {current:.2f} (-{breakout_pct:.2f}%)",
            f"• Mínima anterior: {snapshot['session_low']:.2f}",
            f"• VWAP: {snapshot.get('vwap', 0):.2f} ({snapshot.get('vwap_distance_pct', 0):+.2f}%)",
            f"• Tendência: {snapshot.get('trend', '?')} | ATR: {snapshot.get('atr', 0):.0f}",
        ]
        if pos:
            pdir = "BUY" if str(pos.get("type", "")).endswith("BUY") or pos.get("type") in (0,) else "SELL"
            pnl = pos.get("profit", 0)
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg_parts.append(f"• Posição: {pdir} {pos.get('price_open', 0):.2f} {emoji} R$ {pnl:.2f}")
        anomalies.append({
            "type": "BREAKOUT",
            "severity": "ALTO",
            "msg": "\n".join(msg_parts),
            "tf": tf
        })

    # 6. Reversão forte
    mom = snapshot["momentum_5bar"]
    if abs(mom) > 0.5:
        direction = "⬆ subindo" if mom > 0 else "⬇ caindo"
        price = snapshot.get("price", 0)
        pos = snapshot.get("position")
        msg_parts = [
            f"Movimento forte {direction} ({abs(mom):.1f}%)",
            f"• Preço: {price:.2f} | VWAP: {snapshot.get('vwap', 0):.2f}",
            f"• Tendência: {snapshot.get('trend', '?')} | ATR: {snapshot.get('atr', 0):.0f}",
        ]
        if pos:
            pdir = "BUY" if str(pos.get("type", "")).endswith("BUY") or pos.get("type") in (0,) else "SELL"
            pnl = pos.get("profit", 0)
            # Reversão a favor ou contra a posição?
            if (mom > 0 and pdir == "BUY") or (mom < 0 and pdir == "SELL"):
                favor = "✅ a favor da posição"
            else:
                favor = "⚠️ contra a posição"
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg_parts.append(f"• Posição: {pdir} {pos.get('price_open', 0):.2f} {emoji} R$ {pnl:.2f} — {favor}")
        anomalies.append({
            "type": "REVERSAL",
            "severity": "MÉDIO",
            "msg": "\n".join(msg_parts),
            "tf": tf
        })

    # 7. Streak de perdas (lê do state do autotrader)
    try:
        import json as _sj
        _sstate = _sj.load(open("/tmp/vt_autotrader_state.json"))
        _losses = _sstate.get("consecutive_losses", {})
        _max = _sstate.get("max_consecutive_losses", 3)
        root_sym = symbol[:3]
        _sl = _losses.get(symbol, _losses.get(root_sym, 0))
        if _sl >= _max:
            _dp = _sstate.get("daily_pnl", 0)
            _halt = _sstate.get("halt_until", {}).get(symbol, _sstate.get("halt_until", {}).get(root_sym, ""))
            msg_parts = [
                f"{_sl} perdas consecutivas em {symbol}",
                f"• Limite: {_max} | PnL Dia: R$ {_dp:+.0f}",
            ]
            if _halt:
                msg_parts.append(f"• HALT ativo até {_halt}")
            else:
                msg_parts.append("• Próxima perda ativa HALT de 1h")
            anomalies.append({
                "type": "STREAK_LOSS",
                "severity": "ALTO",
                "msg": "\n".join(msg_parts),
                "tf": tf
            })
    except Exception:
        pass

    return anomalies


def save_snapshot(snapshot: dict):
    """Salva snapshot atual + histórico."""
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)
        with open(SNAPSHOT_HISTORY, "a") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except Exception:
        pass


def build_analysis_prompt() -> str:
    """Monta prompt rico com todos os dados coletados."""
    lines = []

    # Snapshot atual
    if SNAPSHOT_FILE.exists():
        snap = json.loads(SNAPSHOT_FILE.read_text())
        lines.append(f"SNAPSHOT ATUAL ({snap['time']}):")
        lines.append(f"  {snap['symbol']}: preço={snap['price']} VWAP={snap['vwap']} ATR={snap['atr']}")
        lines.append(f"  Distância VWAP: {snap['vwap_distance_pct']:+.3f}%")
        lines.append(f"  Volume: {snap['current_volume']} (média: {snap['avg_volume']})")
        lines.append(f"  Spread: {snap['spread']}")
        lines.append(f"  Trend: {snap['trend']} | Momentum 5b: {snap['momentum_5bar']:+.3f}%")
        lines.append(f"  SMA5={snap['sma5']} | SMA10={snap['sma10']}")
        lines.append(f"  Sessão: High={snap['session_high']} Low={snap['session_low']}")
        if snap.get("position"):
            p = snap["position"]
            lines.append(f"  POSIÇÃO ABERTA: {p['type']} {p['volume']} contratos @ {p['price_open']} PnL R${p['profit']:+.2f} SL={p['sl']}")

    # Histórico recente (últimas 20 snapshots)
    if SNAPSHOT_HISTORY.exists():
        history_lines = SNAPSHOT_HISTORY.read_text().strip().split("\n")
        recent = history_lines[-10:]
        if recent:
            lines.append("\nEVOLUÇÃO (últimos 10 snapshots):")
            for h in recent:
                try:
                    d = json.loads(h)
                    lines.append(f"  {d['time']} | {d['symbol']}={d['price']} VWAP={d['vwap']} "
                               f"Vol={d['current_volume']} ATR={d['atr']} Mom={d['momentum_5bar']:+.3f}%")
                except Exception:
                    pass

    # Anomalias detectadas
    if ANOMALY_FILE.exists():
        anomaly_lines = ANOMALY_FILE.read_text().strip().split("\n")
        recent_anomalies = anomaly_lines[-10:]
        if recent_anomalies:
            lines.append("\nANOMALIAS DETECTADAS:")
            for a in recent_anomalies:
                try:
                    d = json.loads(a)
                    lines.append(f"  {d['time']} [{d['event']}] {d['symbol']}: {d['data'].get('msg', '')}")
                except Exception:
                    pass

    # Status da conta
    try:
        s = status()
        a = s.get("account", {})
        lines.append(f"\nCONTA: saldo R$ {a.get('balance', 0):,.2f} | equity R$ {a.get('equity', 0):,.2f} | "
                    f"margem R$ {a.get('margin', 0):,.2f}")
        positions = s.get("positions", [])
        lines.append(f"POSIÇÕES ABERTAS: {len(positions)}")
        for p in positions:
            lines.append(f"  {p['symbol']} {p['type']} {p['volume']} @ {p['price_open']} SL={p['sl']} PnL R${p['profit']:+.2f}")
    except Exception:
        pass

    return "\n".join(lines)


def analyze():
    """Coleta snapshot, detecta anomalias, alerta, e retorna dados."""
    print("=" * 60)
    print(f"Vibe-Trading Analyst | {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    _config = load_config()
    _roots = list(_config.get("resolved_symbols", {}).keys()) or ["WIN", "WDO"]
    for root in _roots:
        symbol = _config.get("resolved_symbols", {}).get(root)
        if not symbol:
            print(f"[WARN] Não resolveu {root}")
            continue

        print(f"\n--- {symbol} M5 ---")

        # Coletar snapshot
        snap = fetch_snapshot(symbol, "M5")
        if "error" in snap:
            print(f"  Erro: {snap['error']}")
            continue

        # Salvar
        save_snapshot(snap)

        # Detectar anomalias
        anomalies = detect_anomalies(snap)

        # Print resumo
        print(f"  Preço: {snap['price']} | VWAP: {snap['vwap']} ({snap['vwap_distance_pct']:+.3f}%)")
        print(f"  ATR: {snap['atr']} | Volume: {snap['current_volume']} (avg: {snap['avg_volume']})")
        print(f"  Trend: {snap['trend']} | Momentum: {snap['momentum_5bar']:+.3f}%")
        print(f"  Spread: {snap['spread']} | Sessão: {snap['session_low']}-{snap['session_high']}")

        if snap.get("position"):
            p = snap["position"]
            print(f"  POSIÇÃO: {p['type']} {p['volume']} @ {p['price_open']} PnL R${p['profit']:+.2f}")

        # Alertas
        for a in anomalies:
            icon = "🔴" if a["severity"] == "ALTO" else "🟡" if a["severity"] == "MÉDIO" else "🟢"
            print(f"  {icon} {a['type']}: {a['msg']}")
            log_anomaly(symbol, a["type"], a["data"] if "data" in a else {"msg": a["msg"], "severity": a["severity"]})
            notify(a["type"], symbol, a["msg"], a.get("tf", ""))

    return build_analysis_prompt()


def watch_loop():
    """Monitora continuamente (a cada 30s)."""
    print("Monitorando mercado (Ctrl+C para parar)...")
    bar_count = 0
    last_bar_time = None

    while True:
        try:
            snap_time = datetime.now().strftime("%H:%M")
            if snap_time != last_bar_time:
                last_bar_time = snap_time
                bar_count += 1

                _config = load_config()
                _roots = list(_config.get("resolved_symbols", {}).keys()) or ["WIN", "WDO"]
                for root in _roots:
                    symbol = _config.get("resolved_symbols", {}).get(root)
                    if not symbol:
                        continue
                    snap = fetch_snapshot(symbol, "M5")
                    if "error" not in snap:
                        save_snapshot(snap)
                        anomalies = detect_anomalies(snap)
                        for a in anomalies:
                            log_anomaly(symbol, a["type"], a)
                            notify(a["type"], symbol, a["msg"], a.get("tf", ""))

                if bar_count % 6 == 0:
                    print(f"[{snap_time}] Barra #{bar_count} — snapshot salvo")
        except Exception as e:
            print(f"[ERRO] {e}")

        time.sleep(30)


def main():
    if "--watch" in sys.argv:
        watch_loop()
    elif "--snapshot" in sys.argv:
        _config = load_config()
        _roots = list(_config.get("resolved_symbols", {}).keys()) or ["WIN", "WDO"]
        for root in _roots:
            symbol = _config.get("resolved_symbols", {}).get(root)
            if symbol:
                snap = fetch_snapshot(symbol)
                save_snapshot(snap)
                print(f"Snapshot {symbol} salvo")
    elif "--prompt" in sys.argv:
        print(build_analysis_prompt())
    else:
        print(analyze())


if __name__ == "__main__":
    main()
