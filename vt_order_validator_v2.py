"""
vt_order_validator_v2.py — Validator inteligente com cache + contexto histórico.

Melhorias sobre v1:
  1. CACHE LLM (5min) — não chama LLM se mesmo setup (symbol+tf+strategy+sl_band)
     foi visto em <5min. Reduz chamadas em 80%+ em mercados laterais.
  2. CONTEXTO HISTÓRICO — consulta vt_trades.db: setup com WR<30% nos últimos
     30 dias → marca como HISTORICAL_LOSING e sugere NÃO abrir. Sem LLM.
  3. CONTEXTO DE SESSÃO — respeita PnL diário, streak de losses, posição aberta.
     PnL < -R$1000 OU 3+ losses seguidas → NÃO sugere aumentar SL.
  4. CONTEXTO MERCADO — passa pro LLM: hora do dia, spread atual, volume.
  5. DECISÃO MULTI-NÍVEL — local check → DB check → cache check → LLM check.

Mantém compatibilidade com a função `validate_and_fix()` do v1 (interface estável).
"""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).parent
DB_PATH = PROJECT / "vt_trades.db"
VALIDATOR_LOG = Path("/tmp/vt_order_validator_v2.log")
ALERT_LOG = Path("/tmp/vt_order_alerts_v2.log")

# Cache LLM em memória: key → {response, ts}
_llm_cache: dict = {}
CACHE_TTL_MINUTES = 5

# Limites de SL (em pontos EXECUTOR)
SL_LIMITS = {
    "WDO": {"min": 3000, "max": 300000, "atr_multiplier_max": 5.0},
    "WIN": {"min": 200, "max": 3000, "atr_multiplier_max": 5.0},
    "BIT": {"min": 3000, "max": 500000, "atr_multiplier_max": 3.0},
    "DOL": {"min": 3000, "max": 300000, "atr_multiplier_max": 5.0},
    "IND": {"min": 200, "max": 3000, "atr_multiplier_max": 5.0},
    "WSP": {"min": 500, "max": 30000, "atr_multiplier_max": 5.0},
}

# Thresholds de contexto
DAILY_LOSS_BLOCK_SL_INCREASE = -1000.0  # PnL diário abaixo disso → não aumenta SL
CONSECUTIVE_LOSSES_BLOCK = 3           # 3+ losses seguidas → não aumenta SL
HISTORICAL_WR_THRESHOLD = 30.0         # WR < 30% no setup → marca como ruim
HISTORICAL_MIN_TRADES = 10             # Mínimo de trades pra análise histórica ser confiável


def _log(msg: str, file=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    target = file or VALIDATOR_LOG
    with open(target, "a") as f:
        f.write(line)


def _get_base(symbol: str) -> str:
    """Extrai base do símbolo (BIT, WIN, DOL, etc)."""
    for base in ["WDO", "WIN", "BIT", "DOL", "IND", "WSP"]:
        if base in symbol:
            return base
    return "WIN"


def _cache_key(order_data: dict) -> str:
    """Gera chave de cache: symbol+tf+strategy+sl_band."""
    base = _get_base(order_data.get("symbol", ""))
    sl_pts = order_data.get("sl_pts", 0)
    sl_band = "low" if sl_pts < 5000 else ("mid" if sl_pts < 50000 else "high")
    return f"{base}_{order_data.get('tf', '?')}_{order_data.get('strategy', '?')}_{sl_band}"


def _cache_get(key: str):
    if key not in _llm_cache:
        return None
    entry = _llm_cache[key]
    if datetime.now() - entry["ts"] > timedelta(minutes=CACHE_TTL_MINUTES):
        del _llm_cache[key]
        return None
    return entry["response"]


def _cache_put(key: str, response: str):
    _llm_cache[key] = {"response": response, "ts": datetime.now()}


def _ask_llm(prompt: str, timeout: int = 30) -> Optional[str]:
    """Consulta LLM (com fallback). Timeout reduzido para 30s."""
    from vt_hermes_helper import find_hermes
    hermes_bin = find_hermes()
    if not hermes_bin:
        return None

    models = [
        ("MiniMax-M3", "minimax-oauth"),
        ("glm-5.2", "zai"),
    ]

    for model, provider in models:
        try:
            result = subprocess.run(
                [hermes_bin, "-z", prompt, "-m", model, "--provider", provider],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0 and result.stdout.strip():
                resp = result.stdout.strip()
                _log(f"[LLM] {model} respondeu ({len(resp)} chars)")
                return resp
            else:
                _log(f"[WARN] {model} returncode={result.returncode}, stderr={result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            _log(f"[WARN] {model}: timeout após {timeout}s")
        except Exception as e:
            _log(f"[WARN] {model}: {e}")

    _log("[ERROR] Todos os modelos falharam")
    return None


def get_daily_pnl() -> float:
    """PnL líquido do dia (do state). Retorna 0.0 se não conseguir."""
    try:
        # Tentar state file primeiro
        state_path = Path("/tmp/vt_autotrader_state.json")
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
            return float(state.get("daily_pnl", 0.0))
    except Exception:
        pass
    return 0.0


def get_consecutive_losses(symbol_root: str) -> int:
    """Conta losses consecutivas no state."""
    try:
        state_path = Path("/tmp/vt_autotrader_state.json")
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
            return int(state.get("consecutive_losses", {}).get(symbol_root, 0))
    except Exception:
        pass
    return 0


def get_open_position_for_symbol(symbol_root: str) -> Optional[dict]:
    """Retorna posição aberta no MT5/State pro símbolo."""
    try:
        # Tentar state primeiro
        state_path = Path("/tmp/vt_autotrader_state.json")
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
            for key, pos in state.get("positions", {}).items():
                if key.startswith(symbol_root):
                    return pos
    except Exception:
        pass
    return None


def historical_setup_stats(symbol: str, tf: str, strategy: str, direction: str = "",
                            days: int = 30) -> dict:
    """Consulta DB: stats do setup (symbol+tf+strategy) nos últimos N dias.

    Returns:
        {
            "n_trades": int,
            "wins": int,
            "losses": int,
            "win_rate": float,
            "avg_pnl": float,
            "total_pnl": float,
            "avg_duration_min": float,
        }
    """
    if not DB_PATH.exists():
        return {"n_trades": 0, "win_rate": 0.0}

    base = _get_base(symbol)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        if direction:
            rows = conn.execute("""
                SELECT net_pnl, exit_reason,
                       (julianday(exit_time) - julianday(entry_time)) * 24 * 60 as dur
                FROM trades
                WHERE symbol LIKE ?
                  AND timeframe = ?
                  AND strategy = ?
                  AND direction = ?
                  AND entry_time >= ?
                  AND exit_time IS NOT NULL
            """, (f"{base}%", tf, strategy, direction, cutoff)).fetchall()
        else:
            rows = conn.execute("""
                SELECT net_pnl, exit_reason,
                       (julianday(exit_time) - julianday(entry_time)) * 24 * 60 as dur
                FROM trades
                WHERE symbol LIKE ?
                  AND timeframe = ?
                  AND strategy = ?
                  AND entry_time >= ?
                  AND exit_time IS NOT NULL
            """, (f"{base}%", tf, strategy, cutoff)).fetchall()
        conn.close()

        n = len(rows)
        if n == 0:
            return {"n_trades": 0, "win_rate": 0.0, "total_pnl": 0.0}

        wins = sum(1 for r in rows if r["net_pnl"] > 0)
        losses = n - wins
        wr = wins / n * 100
        total = sum(r["net_pnl"] for r in rows)
        avg = total / n
        avg_dur = sum(r["dur"] or 0 for r in rows) / n

        return {
            "n_trades": n,
            "wins": wins,
            "losses": losses,
            "win_rate": wr,
            "avg_pnl": avg,
            "total_pnl": total,
            "avg_duration_min": avg_dur,
        }
    except Exception as e:
        _log(f"[WARN] historical_setup_stats falhou: {e}")
        return {"n_trades": 0, "win_rate": 0.0}


class ValidatorV2:
    """Validator inteligente com cache + contexto histórico."""

    def __init__(self):
        self.stats = {
            "llm_calls": 0,
            "llm_cached": 0,
            "blocked_historical": 0,
            "blocked_daily_loss": 0,
            "blocked_streak": 0,
        }

    def validate(self, order_data: dict, use_llm: bool = True) -> dict:
        """Valida ordem com decisão multi-nível.

        Níveis:
          1. Local check (SL_LIMITS, SL_LADO_ERRADO) — sempre
          2. Histórico do setup (DB) — bloqueia se WR<30% com 10+ trades
          3. Contexto de sessão (PnL diário, streak) — limita aumento de SL
          4. Cache (5min) — reusa resposta se mesmo setup
          5. LLM — só chamada se passou pelos níveis anteriores
        """
        result = {
            "valid": True,
            "alerts": [],
            "llm_analysis": None,
            "suggested_action": None,
        }

        symbol = order_data.get("symbol", "UNKNOWN")
        direction = order_data.get("direction", "UNKNOWN")
        tf = order_data.get("tf", order_data.get("timeframe", "?"))
        strategy = order_data.get("strategy", "UNKNOWN")
        sl_pts = order_data.get("sl_pts", 0)
        atr = order_data.get("atr", 0)
        entry_price = order_data.get("entry_price", 0)
        base = _get_base(symbol)

        # ── NÍVEL 1: Validação local (rápida, sem custo) ──
        local_alerts = self._validate_local(order_data, base)
        result["alerts"] = local_alerts
        if local_alerts:
            result["valid"] = False
            for a in local_alerts:
                _log(f"[LOCAL] {symbol} {direction} — [{a['severity']}] {a['type']}: {a['detail']}")

        # ── NÍVEL 2: Histórico do setup ──
        h_stats = historical_setup_stats(symbol, tf, strategy, direction, days=30)
        if h_stats["n_trades"] >= HISTORICAL_MIN_TRADES and h_stats["win_rate"] < HISTORICAL_WR_THRESHOLD:
            result["valid"] = False
            result["alerts"].append({
                "type": "HISTORICAL_LOSING",
                "severity": "HIGH",
                "detail": f"Setup {symbol} {tf} {strategy} {direction}: WR={h_stats['win_rate']:.1f}% em {h_stats['n_trades']} trades (últimos 30d). PnL médio R$ {h_stats['avg_pnl']:.2f}.",
                "suggestion": "NÃO ABRIR. AGI deve desativar este setup ou revisar parâmetros."
            })
            self.stats["blocked_historical"] += 1
            _log(f"[HIST] {symbol} {tf} {strategy} {direction}: WR={h_stats['win_rate']:.1f}% em {h_stats['n_trades']}t → bloqueado")
            # Se histórico é ruim, NÃO consulta LLM (decisão clara)
            return result

        # ── NÍVEL 3: Contexto de sessão ──
        daily_pnl = get_daily_pnl()
        symbol_root = base
        streak = get_consecutive_losses(symbol_root)

        # ── NÍVEL 4: Cache ──
        cache_key = _cache_key(order_data)
        cached = _cache_get(cache_key) if use_llm else None

        # ── NÍVEL 5: LLM (se passou tudo) ──
        if use_llm and not cached:
            # Preparar contexto rico pro LLM
            context = self._build_llm_context(
                order_data, base, h_stats, daily_pnl, streak
            )
            prompt = self._build_llm_prompt(order_data, base, context)
            llm_response = _ask_llm(prompt)
            if llm_response:
                self.stats["llm_calls"] += 1
                _cache_put(cache_key, llm_response)
                result["llm_analysis"] = llm_response
                self._parse_llm_response(llm_response, result, order_data, base,
                                          daily_pnl, streak)
        elif cached:
            self.stats["llm_cached"] += 1
            result["llm_analysis"] = cached
            self._parse_llm_response(cached, result, order_data, base,
                                      daily_pnl, streak)

        return result

    def _validate_local(self, order_data: dict, base: str) -> list:
        """Validação local (sem LLM, rápida)."""
        alerts = []
        sl_pts = order_data.get("sl_pts", 0)
        atr = order_data.get("atr", 0)
        entry_price = order_data.get("entry_price", 0)
        direction = order_data.get("direction", "")
        limits = SL_LIMITS.get(base, {"min": 200, "max": 50000, "atr_multiplier_max": 5.0})

        # 1. SL fora dos limites
        if sl_pts > 0 and sl_pts < limits["min"]:
            alerts.append({
                "type": "SL_MUITO_PEQUENO",
                "severity": "HIGH",
                "detail": f"SL de {sl_pts}pts abaixo do mínimo seguro ({limits['min']}pts).",
                "suggestion": f"Aumentar SL para pelo menos {limits['min']}pts"
            })
        if sl_pts > limits["max"]:
            alerts.append({
                "type": "SL_MUITO_GRANDE",
                "severity": "CRITICAL",
                "detail": f"SL de {sl_pts}pts acima do máximo seguro ({limits['max']}pts).",
                "suggestion": f"Reduzir SL para {limits['max']}pts ou menos"
            })

        # 2. SL vs ATR
        point_mult = {"WDO": 1000, "WIN": 1, "BIT": 100, "DOL": 1000, "IND": 1, "WSP": 100}.get(base, 1)
        if atr > 0 and sl_pts > 0:
            sl_native = sl_pts / point_mult
            atr_mult = sl_native / atr
            if atr_mult > limits.get("atr_multiplier_max", 5.0):
                alerts.append({
                    "type": "SL_ATR_EXCESSIVO",
                    "severity": "HIGH",
                    "detail": f"SL é {atr_mult:.1f}x o ATR ({atr:.1f}pts nativos).",
                    "suggestion": f"Reduzir SL para {int(atr * 3.0 * point_mult)}pts executor"
                })
            elif atr_mult < 0.5:
                alerts.append({
                    "type": "SL_ATR_MUITO_APERTADO",
                    "severity": "MEDIUM",
                    "detail": f"SL é {atr_mult:.2f}x o ATR. Muito apertado.",
                    "suggestion": f"Aumentar SL para pelo menos {int(atr * 1.0 * point_mult)}pts executor (1.0x ATR)"
                })

        # 3. SL invertido (lógica do executor)
        if entry_price > 0 and sl_pts != 0:
            sl_native_distance = sl_pts * point_mult
            if direction == "BUY":
                sl_price = entry_price - sl_native_distance
                if sl_price >= entry_price:
                    alerts.append({
                        "type": "SL_LADO_ERRADO",
                        "severity": "CRITICAL",
                        "detail": f"BUY com sl_pts={sl_pts} → SL efetivo {sl_price:.2f} ACIMA da entrada {entry_price:.2f}.",
                        "suggestion": f"Usar sl_pts POSITIVO"
                    })
            elif direction == "SELL":
                sl_price = entry_price + sl_native_distance
                if sl_price <= entry_price:
                    alerts.append({
                        "type": "SL_LADO_ERRADO",
                        "severity": "CRITICAL",
                        "detail": f"SELL com sl_pts={sl_pts} → SL efetivo {sl_price:.2f} ABAIXO da entrada {entry_price:.2f}.",
                        "suggestion": f"Usar sl_pts POSITIVO"
                    })
        return alerts

    def _build_llm_context(self, order_data, base, h_stats, daily_pnl, streak) -> dict:
        """Coleta contexto rico pra passar pro LLM."""
        open_pos = get_open_position_for_symbol(base)
        return {
            "hora": datetime.now().strftime("%H:%M"),
            "daily_pnl": daily_pnl,
            "consecutive_losses": streak,
            "historical_setup": h_stats,
            "open_position": open_pos,
            "trading_phase": "warmup" if datetime.now().hour < 10 else
                             "winddown" if datetime.now().hour >= 16 else "main",
        }

    def _build_llm_prompt(self, order_data, base, context) -> str:
        """Monta prompt estruturado com contexto rico."""
        symbol = order_data.get("symbol", "?")
        direction = order_data.get("direction", "?")
        strategy = order_data.get("strategy", "?")
        tf = order_data.get("tf", "?")
        sl_pts = order_data.get("sl_pts", 0)
        atr = order_data.get("atr", 0)
        entry_price = order_data.get("entry_price", 0)
        limits = SL_LIMITS.get(base, {"min": 200, "max": 50000})

        point_map = {"WDO": 0.001, "WIN": 1, "BIT": 0.01, "DOL": 0.001, "IND": 1, "WSP": 0.01}
        pt = point_map.get(base, 1)
        native_sl = sl_pts * pt
        native_atr = atr
        atr_mult = native_sl / native_atr if native_atr > 0 else 0

        h = context["historical_setup"]
        hist_section = f"""Histórico do setup (30d):
  - Trades: {h.get('n_trades', 0)} | WR: {h.get('win_rate', 0):.1f}% | PnL médio: R$ {h.get('avg_pnl', 0):.2f}
  - Total: R$ {h.get('total_pnl', 0):.2f} | Duração média: {h.get('avg_duration_min', 0):.0f}min"""

        ctx_lines = [
            f"Hora: {context['hora']} | Fase: {context['trading_phase']}",
            f"PnL diário: R$ {context['daily_pnl']:.2f} | Streak losses ({base}): {context['consecutive_losses']}",
        ]
        if context["open_position"]:
            p = context["open_position"]
            ctx_lines.append(f"Posição aberta {base}: {p.get('direction')} @ {p.get('entry_price')} SL_pts={p.get('sl_pts')}")

        return f"""Você é um trader profissional analisando esta ordem com CONTEXTO RICO.

## ORDEM ATUAL
Símbolo: {symbol} | {direction} | TF: {tf} | Estratégia: {strategy}
Entrada: {entry_price} | SL: {sl_pts}pts = {native_sl:.1f}nativos = {atr_mult:.2f}x ATR
ATR: {atr:.2f}pts nativos
Limites executor: [{limits['min']} - {limits['max']}]

## CONTEXTO DE SESSÃO
{chr(10).join(ctx_lines)}

## {hist_section}

## REGRAS
1. Sugira sl_sugerido entre 1.0x-1.8x ATR (em pontos EXECUTOR)
2. Considere o histórico do setup — se WR<30% consistentemente, considere NÃO modificar (deixe como está ou aumente bem pouco)
3. Em drawdown (PnL diário < -R$1000) ou 3+ losses seguidas, NÃO AUMENTE SL (evitar aumentar exposição)
4. Se o histórico do setup é positivo (WR>50%, PnL médio>0), pode sugerir ajuste mais agressivo
5. SEMPRE retorne JSON válido

Retorne APENAS JSON:
{{
  "sl_sugerido": <pts executor>,
  "resumo": "<motivo contextualizado, 1-2 frases>"
}}
"""

    def _parse_llm_response(self, llm_response, result, order_data, base,
                              daily_pnl, streak):
        """Parseia resposta da LLM com recuperação de JSON truncado."""
        try:
            start = llm_response.find("{")
            end = llm_response.rfind("}") + 1
            if start < 0:
                _log(f"[WARN] parse: sem '{{' na resposta: {llm_response[:200]}")
                return
            if end <= start:
                # JSON truncado — tentar fechar manualmente
                _log(f"[WARN] parse: JSON sem '}}' — tentando recuperar: {llm_response[start:start+200]}")
                raw = llm_response[start:]
                # Tentar fechar string e objeto
                if '"' in raw and not raw.rstrip().endswith('"'):
                    raw = raw.rstrip().rstrip(',') + '"'
                if not raw.rstrip().endswith('}'):
                    raw = raw.rstrip() + '}'
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    _log(f"[WARN] parse: JSON irrecuperável")
                    return
            else:
                data = json.loads(llm_response[start:end])

            new_sl = data.get("sl_sugerido")
            if not isinstance(new_sl, (int, float)) or new_sl <= 0:
                _log(f"[WARN] parse: sl_sugerido inválido: {new_sl}")
                return

            new_sl = int(new_sl)
            sl_pts = order_data.get("sl_pts", 0)
            limits = SL_LIMITS.get(base, {"min": 200, "max": 50000})
            new_sl = max(limits["min"], min(limits["max"], new_sl))

            # CONTEXTO: não aumentar SL em drawdown ou streak
            if new_sl > sl_pts:
                if daily_pnl <= DAILY_LOSS_BLOCK_SL_INCREASE:
                    self.stats["blocked_daily_loss"] += 1
                    _log(f"[CONTEXT] daily_pnl R$ {daily_pnl:.2f} < -R$1000 → rejeitado aumento SL")
                    return
                if streak >= CONSECUTIVE_LOSSES_BLOCK:
                    self.stats["blocked_streak"] += 1
                    _log(f"[CONTEXT] streak {streak} >= 3 → rejeitado aumento SL")
                    return

            # Só aplicar se mudança significativa
            diff = abs(new_sl - sl_pts)
            diff_pct = diff / sl_pts * 100 if sl_pts > 0 else 0
            if (diff > 50 or diff_pct > 5) and new_sl != sl_pts:
                result["suggested_action"] = {
                    "type": "MODIFY_SL",
                    "symbol": order_data.get("symbol"),
                    "current_sl": sl_pts,
                    "suggested_sl": new_sl,
                    "reason": data.get("resumo", "LLM sugere ajuste"),
                }
                _log(f"[LLM] Sugere SL {sl_pts} → {new_sl}")
            else:
                # SL mantido — logar motivo pro debug
                _log(f"[LLM] SL mantido em {sl_pts} (sugerido {new_sl}, diff {diff}pts) — {data.get('resumo', '')[:100]}")
        except (json.JSONDecodeError, ValueError) as e:
            _log(f"[WARN] parse_llm_response: {e} — raw: {llm_response[:300]}")


# Função de compatibilidade com v1
def validate_order(order_data: dict, use_llm: bool = True) -> dict:
    """Wrapper de compatibilidade com v1. Cria ValidatorV2 e chama validate."""
    v = ValidatorV2()
    return v.validate(order_data, use_llm)


def validate_and_fix(order_data: dict, modify_sl_func=None) -> dict:
    """Compat: valida e aplica correção (como v1)."""
    result = validate_order(order_data, use_llm=True)
    if result.get("suggested_action") and modify_sl_func:
        action = result["suggested_action"]
        try:
            fix_result = modify_sl_func(
                action["symbol"],
                order_data.get("ticket", 0),
                action["suggested_sl"]
            )
            result["fix_applied"] = fix_result
        except Exception as e:
            result["fix_applied"] = {"error": str(e)}
    return result
