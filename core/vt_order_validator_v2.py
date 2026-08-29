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
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).parent.parent
DB_PATH = PROJECT / "vt_trades.db"
VALIDATOR_LOG = Path("/tmp/vt_order_validator_v2.log")
ALERT_LOG = Path("/tmp/vt_order_alerts_v2.log")

# Cache LLM em memória: key → {response, ts}
_llm_cache: dict = {}
CACHE_TTL_MINUTES = 5

# Limites de SL (em pontos EXECUTOR). Wave 880.B5 fix (Bruno 2026-08-05): os
# valores de `max` foram alinhados com _calc_sl (core/vt_autotrader.py:2117-2123)
# — max_native * point_mult — para o validator NUNCA inflar o SL acima do que o
# _calc_sl permite. Antes, BIT max=500000 (5000 nativos) era 10× o max_native=500
# do _calc_sl; o clamp "pré-envio" pegava uma sugestão de SL_ATR_EXCESSIVO e
# inflava 50.000 → 141.857 pts (1.418 nativos = 2,8× o máximo), invertendo a
# proteção. Agora max = max_native * point_mult de cada símbolo.
SL_LIMITS = {
    "WDO": {"min": 3000, "max": 12000,  "atr_multiplier_max": 5.0},   # 12 nativos × 1000
    "WIN": {"min": 200,  "max": 800,    "atr_multiplier_max": 5.0},    # 800 nativos × 1
    "BIT": {"min": 3000, "max": 50000,  "atr_multiplier_max": 3.0},    # 500 nativos × 100
    "DOL": {"min": 3000, "max": 200000, "atr_multiplier_max": 5.0},    # 200 nativos × 1000
    "IND": {"min": 200,  "max": 350,    "atr_multiplier_max": 5.0},    # 350 nativos × 1
    "WSP": {"min": 500,  "max": 20000,  "atr_multiplier_max": 5.0},    # 200 nativos × 100
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


# Provedores LLM em ordem de prioridade: primário → fallback.
# Nomes de provider conforme ~/.hermes/config.yaml e `hermes fallback list`.
# Wave 880.E (Bruno 07/08): TODOS os callers devem puxar o modelo GLOBAL.
# Wave 880.F (Bruno 07/08): nova ordem de uso LLM definida pelo Bruno:
#   1º zenmux/deepseek-v4-flash-free → 2º zenmux/deepseek-v4-flash →
#   3º alibaba/deepseek-v4-flash-0731 → 4º qwen3.8-max (último recurso).
# (deepseek-v4-pro REMOVIDO da cadeia — Bruno 09/08.)
# Transporte HTTP direto (medido: 3-13s vs 46-54s CLI) — mesmo endpoint/key
# do config do hermes.
_LLM_PROVIDERS = [
    {"provider": "zenmux",             "model": "deepseek/deepseek-v4-flash-free", "timeout": 12},
    {"provider": "zenmux",             "model": "deepseek/deepseek-v4-flash",      "timeout": 12},
    {"provider": "alibaba-token-plan", "model": "deepseek-v4-flash-0731",          "timeout": 15},
    {"provider": "alibaba-token-plan", "model": "qwen3.8-max",                     "timeout": 15},
]
MAX_TOTAL_LLM_TIMEOUT = 60  # hard cap: 12+12+15+15=54 + margem

# Wave 883.B1 (Bruno 29/08): circuit-breaker por modelo. Após N falhas
# consecutivas do mesmo modelo, ele entra em cooldown e a cascata pula direto.
# Sem isso, cada entrada queimava 15-20s na pós-validação tentando provedores
# mortos (provider zenmux inexistente + HTTP 403 — incidente 24-28/08),
# engordando o ciclo do daemon e atrasando a detecção dos próximos sinais.
_LLM_FAIL_STRIKES = 2
_LLM_COOLDOWN_S = 600  # 10 min
_LLM_HEALTH: dict = {}  # model -> {"fails": int, "until": float}

# Wave 883.B1: o hermes CLI devolve rc=0 com o TEXTO DO ERRO no stdout
# (ex.: "HTTP 403: Access to model denied", "agent failed: Unknown provider").
# Sem este gate, o erro virava llm_analysis e o daemon logava
# "[VALIDATOR] LLM OK ... HTTP 403" — máscara de falha (65x em 24-28/08).
_LLM_ERROR_SIGNATURES = (
    "http 4", "http 5", "access to model denied", "agent failed",
    "unknown provider", "rate limit", "insufficient", "unauthorized",
    "invalid api key", "quota exceeded", "credit",
)


def _looks_like_llm_error(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(sig in low for sig in _LLM_ERROR_SIGNATURES)


def _llm_in_cooldown(model: str) -> bool:
    st = _LLM_HEALTH.get(model)
    return bool(st and st.get("until", 0.0) > time.time())


def _llm_note_failure(model: str) -> None:
    st = _LLM_HEALTH.setdefault(model, {"fails": 0, "until": 0.0})
    st["fails"] += 1
    if st["fails"] >= _LLM_FAIL_STRIKES:
        st["until"] = time.time() + _LLM_COOLDOWN_S
        _log(f"[LLM] {model}: {st['fails']} falhas consecutivas → cooldown de "
             f"{_LLM_COOLDOWN_S // 60}min (circuit-breaker Wave 883.B1)")


def _llm_note_success(model: str) -> None:
    _LLM_HEALTH.pop(model, None)

# Wave 880.D (Bruno 06/08): transporte HTTP direto (OpenAI-compatible) como
# caminho primário. Medido 06/08: CLI hermes = 46-54s (overhead de sessão),
# HTTP direto = 3.5-13s. Mesmos endpoints/keys do ~/.hermes/config.yaml
# (providers chat_completions). CLI hermes vira fallback se o HTTP falhar.
_PROVIDER_ENDPOINTS = {
    "alibaba-token-plan": (
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        "ALIBABA_TOKEN_PLAN_API_KEY",
    ),
    "zenmux": (
        "https://zenmux.ai/api/v1/chat/completions",
        "ZENMUX_API_KEY",
    ),
}

_hermes_env_cache: Optional[dict] = None


def _load_hermes_env() -> dict:
    """Parse simples de ~/.hermes/.env (KEY=VALUE). Cache em memória."""
    global _hermes_env_cache
    if _hermes_env_cache is not None:
        return _hermes_env_cache
    env: dict = {}
    try:
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    _hermes_env_cache = env
    return env


def _ask_llm_http(prompt: str, provider: str, model: str, timeout: int) -> Optional[str]:
    """Chamada HTTP direta ao endpoint OpenAI-compatible do provider.

    Retorna resposta ou None. Usa urllib (stdlib, zero deps — o daemon roda
    no python3 do sistema). enable_thinking=False pra modelos de raciocínio
    não gastarem tempo com chain-of-thought.
    """
    import urllib.request

    ep = _PROVIDER_ENDPOINTS.get(provider)
    if not ep:
        return None
    url, key_env = ep
    api_key = _load_hermes_env().get(key_env) or os.environ.get(key_env)
    if not api_key:
        _log(f"[LLM] {model}: API key {key_env} não encontrada (~/.hermes/.env)")
        return None

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "enable_thinking": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        elapsed = time.time() - t0
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if content.strip():
            _log(f"[LLM] {model} OK http ({elapsed:.1f}s, {len(content)} chars)")
            return content.strip()
        _log(f"[LLM] {model} http resposta vazia em {elapsed:.1f}s")
        return None
    except Exception as e:
        _log(f"[LLM] {model} http falhou em {time.time()-t0:.1f}s: {str(e)[:150]}")
        return None


def _ask_llm_cli(prompt: str, provider: str, model: str, timeout: int) -> Optional[str]:
    """Fallback: hermes CLI -z (transporte antigo). Mais lento (~10s overhead)."""
    from core.vt_hermes_helper import find_hermes
    hermes_bin = find_hermes()
    if not hermes_bin:
        _log(f"[LLM] {model}: hermes não encontrado no PATH")
        return None

    t0 = time.time()
    try:
        result = subprocess.run(
            [hermes_bin, "-z", prompt, "-m", model, "--provider", provider],
            capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.time() - t0
        if result.returncode == 0 and result.stdout.strip():
            resp = result.stdout.strip()
            # Wave 883.B1: rc=0 com erro no stdout NÃO é resposta válida.
            if _looks_like_llm_error(resp):
                _log(f"[LLM] {model} cli devolveu mensagem de erro com rc=0 "
                     f"({elapsed:.1f}s): {resp[:150]}")
                return None
            _log(f"[LLM] {model} OK cli ({elapsed:.1f}s, {len(resp)} chars)")
            return resp
        _log(f"[LLM] {model} cli falhou em {elapsed:.1f}s rc={result.returncode} "
             f"stderr={result.stderr[:200]}")
        return None
    except subprocess.TimeoutExpired:
        _log(f"[LLM] {model} cli timeout após {timeout}s")
        return None
    except Exception as e:
        _log(f"[LLM] {model} cli erro: {e}")
        return None


def _ask_llm_provider(prompt: str, provider: str, model: str, timeout: int) -> Optional[str]:
    """
    Tenta um único provedor LLM. Retorna resposta ou None.

    Wave 880.D: HTTP direto primeiro (3.5-13s medidos); se falhar, hermes CLI.
    O budget total por provider é `timeout` (NÃO dobra): HTTP consome até
    `timeout`; se falhar antes do limite, o restante vai pro CLI. Se o HTTP
    já estourou o budget, o CLI é pulado. Logs de timing pra diagnóstico.
    """
    t0 = time.time()
    resp = _ask_llm_http(prompt, provider, model, timeout)
    if resp:
        return resp
    # Fallback CLI só se sobrar budget (HTTP falhou rápido: sem key, DNS, 4xx).
    remaining = timeout - (time.time() - t0)
    if remaining < 5:  # CLI tem ~10s de overhead; menos de 5s é inútil
        return None
    return _ask_llm_cli(prompt, provider, model, int(remaining))


def _ask_llm_with_fallback(prompt: str, timeout: int = 60) -> Optional[str]:
    """Tenta providers em ordem: zenmux-free → zenmux-flash → alibaba-flash-0731
    → qwen3.8-max.

    Timeout total limitado a MAX_TOTAL_LLM_TIMEOUT (72s). Cada provedor tem seu
    próprio timeout; o deadline global garante que a soma nunca ultrapasse o
    limite aceitável para validação de trade.

    Wave 883.B1: modelos em cooldown (falhas consecutivas recentes) são
    pulados sem custo de timeout; se TODOS estiverem em cooldown, retorna
    None imediatamente — a validação local continua sendo a guardiã.
    """
    from core.vt_hermes_helper import find_hermes
    if not find_hermes():
        _log("[LLM] hermes não encontrado no PATH — pulando validação LLM")
        return None

    eligible = [p for p in _LLM_PROVIDERS if not _llm_in_cooldown(p["model"])]
    if not eligible:
        cooling = ", ".join(p["model"] for p in _LLM_PROVIDERS)
        _log(f"[LLM] todos os modelos em cooldown — cascata pulada (0s): {cooling}")
        return None

    deadline = time.time() + min(timeout, MAX_TOTAL_LLM_TIMEOUT)

    for idx, prov in enumerate(eligible):
        remaining = deadline - time.time()
        if remaining <= 2:
            _log(f"[LLM] sem tempo restante para tentar {prov['model']}")
            break
        # timeout do provedor, porém nunca além do deadline global
        per_timeout = min(prov["timeout"], int(remaining))

        resp = _ask_llm_provider(prompt, prov["provider"], prov["model"], per_timeout)
        if resp:
            _llm_note_success(prov["model"])
            return resp
        _llm_note_failure(prov["model"])

        next_prov = eligible[idx + 1] if idx + 1 < len(eligible) else None
        if next_prov:
            _log(f"[LLM] {prov['model']} falhou, tentando {next_prov['model']}...")

    _log("[LLM] Ambos os provedores falharam")
    return None


def _ask_llm(prompt: str, timeout: int = 60) -> Optional[str]:
    """Consulta LLM com fallback entre provedores (zenmux-free → zenmux-flash → alibaba-flash-0731 → qwen3.8-max)."""
    return _ask_llm_with_fallback(prompt, timeout=timeout)


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
        return {"n_trades": 0, "win_rate": 0.0, "source": "exact", "symbol_used": symbol}

    base = _get_base(symbol)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    def _query(sym_pattern: str, exact: bool):
        """Consulta trades fechadas do setup. exact=True → símbolo exato (contrato atual)."""
        op = "=" if exact else "LIKE"
        param = symbol if exact else f"{base}%"
        if direction:
            return conn.execute(f"""
                SELECT net_pnl, exit_reason,
                       (julianday(exit_time) - julianday(entry_time)) * 24 * 60 as dur
                FROM trades
                WHERE symbol {op} ?
                  AND timeframe = ?
                  AND strategy = ?
                  AND direction = ?
                  AND entry_time >= ?
                  AND exit_time IS NOT NULL
            """, (param, tf, strategy, direction, cutoff)).fetchall()
        return conn.execute(f"""
            SELECT net_pnl, exit_reason,
                   (julianday(exit_time) - julianday(entry_time)) * 24 * 60 as dur
            FROM trades
            WHERE symbol {op} ?
              AND timeframe = ?
              AND strategy = ?
              AND entry_time >= ?
              AND exit_time IS NOT NULL
        """, (param, tf, strategy, cutoff)).fetchall()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # 1) SÍMBOLO EXATO primeiro (contrato atual) — corrige 13/08/2026:
        #    histórico por root (WIN%) misturava contrato VENCIDO (ex: WINQ26 com
        #    PnL do dia do vencimento) no setup do contrato novo (WINV26), fazendo
        #    o validator reportar "0% WR, PnL -114" para um contrato sem histórico.
        rows = _query(symbol, exact=True)
        source = "exact"
        symbol_used = symbol
        if not rows:
            # 2) Fallback: root (contratos anteriores) — sinalizado pro LLM
            rows = _query(f"{base}%", exact=False)
            source = "root"
            symbol_used = base

        conn.close()

        n = len(rows)
        if n == 0:
            return {"n_trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
                    "source": source, "symbol_used": symbol_used}

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
            "source": source,
            "symbol_used": symbol_used,
        }
    except Exception as e:
        _log(f"[WARN] historical_setup_stats falhou: {e}")
        return {"n_trades": 0, "win_rate": 0.0, "source": "exact", "symbol_used": symbol}


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
          2. Histórico do setup (DB) — apenas contexto pro LLM, sem bloqueio
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
        order_data.get("sl_pts", 0)
        order_data.get("atr", 0)
        order_data.get("entry_price", 0)
        base = _get_base(symbol)

        # ── NÍVEL 1: Validação local (rápida, sem custo) ──
        local_alerts = self._validate_local(order_data, base)
        result["alerts"] = local_alerts
        if local_alerts:
            result["valid"] = False
            for a in local_alerts:
                _log(f"[LOCAL] {symbol} {direction} — [{a['severity']}] {a['type']}: {a['detail']}")

        # ── NÍVEL 2: Histórico do setup (apenas contexto pro LLM, sem bloqueio) ──
        h_stats = historical_setup_stats(symbol, tf, strategy, direction, days=30)

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
                        "suggestion": "Usar sl_pts POSITIVO"
                    })
            elif direction == "SELL":
                sl_price = entry_price + sl_native_distance
                if sl_price <= entry_price:
                    alerts.append({
                        "type": "SL_LADO_ERRADO",
                        "severity": "CRITICAL",
                        "detail": f"SELL com sl_pts={sl_pts} → SL efetivo {sl_price:.2f} ABAIXO da entrada {entry_price:.2f}.",
                        "suggestion": "Usar sl_pts POSITIVO"
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
        hist_symbol = h.get("symbol_used", symbol)
        hist_note = ""
        if h.get("source") == "root":
            hist_note = ("\n  ⚠️ Histórico do ROOT (contratos ANTERIORES, ex: vencidos) — pode incluir "
                         "período de vencimento anômalo; NÃO reflete o contrato atual. "
                         "Tratar com cautela, não como evidência forte.")
        hist_section = f"""Histórico do setup (30d, símbolo {hist_symbol}):
  - Trades: {h.get('n_trades', 0)} | WR: {h.get('win_rate', 0):.1f}% | PnL médio: R$ {h.get('avg_pnl', 0):.2f}
  - Total: R$ {h.get('total_pnl', 0):.2f} | Duração média: {h.get('avg_duration_min', 0):.0f}min{hist_note}"""

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
6. Seja conciso: NÃO mostre raciocínio passo a passo; responda em no máximo 100 tokens.

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
                    _log("[WARN] parse: JSON irrecuperável")
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


def validate_pre_send(order_data: dict) -> dict:
    """Gate pré-envio: apenas corrige SL quando necessário.

    Roda ANTES de safe_buy/safe_sell. Usa checks determinísticos locais
    (SL_LIMITS, ATR) para sugerir ajuste de SL. Nunca bloqueia a ordem.

    Returns:
        {
            "allowed": True,  # sempre permitido (sem bloqueio)
            "block_reason": None,
            "alerts": list[dict],
            "adjusted_sl": int | None,  # sugestão de SL
        }
    """
    import re as _re

    v = ValidatorV2()
    symbol = order_data.get("symbol", "UNKNOWN")
    direction = order_data.get("direction", "UNKNOWN")
    tf = order_data.get("tf", order_data.get("timeframe", "?"))
    base = _get_base(symbol)

    result = {
        "allowed": True,
        "block_reason": None,
        "alerts": [],
        "adjusted_sl": None,
    }

    # ── Checks locais (SL_LIMITS, ATR, lado) ──
    local_alerts = v._validate_local(order_data, base)
    result["alerts"] = local_alerts
    if local_alerts:
        for a in local_alerts:
            _log(f"[PRE-SEND] {symbol} {direction} {tf}: "
                 f"[{a['severity']}/{a['type']}] {a['detail']}")

    # ── Extrair sugestão de SL dos alerts ──
    # Wave 880.B5 fix (Bruno 2026-08-05): o clamp só pode APERTAR o SL (reduzir
    # distância), nunca INFLAR (aumentar). Antes, uma sugestão de alerta era
    # aplicada cegamente mesmo que aumentasse o SL — no BIT isto inflou
    # 50.000 → 141.857 pts (2,8× o max_native), invertendo a proteção. Agora:
    # só aplica adjusted_sl se ele for MENOR que o sl_pts atual (aperta) ou se
    # o SL atual está claramente fora do range (abaixo do min → sobe pro min,
    # que é legítimo; acima do max → desce pro max). Nunca infla dentro do range.
    for a in local_alerts:
        suggestion = a.get("suggestion", "")
        match = _re.search(r"(\d+)\s*pts", suggestion)
        if match:
            suggested = int(match.group(1))
            limits = SL_LIMITS.get(base, {"min": 200, "max": 50000})
            suggested = max(limits["min"], min(limits["max"], suggested))
            current_sl = order_data.get("sl_pts", 0)
            # Só ajusta se: (a) SL atual abaixo do mínimo (sobe pro min, legítimo),
            # ou (b) sugestão APERTA o SL (suggested < current). Nunca infla.
            if current_sl < limits["min"]:
                result["adjusted_sl"] = limits["min"]
                _log(f"[PRE-SEND] ADJUST {symbol} {direction} {tf}: "
                     f"SL {current_sl} abaixo do mínimo → {limits['min']} "
                     f"([{a['type']}] {a['detail']})")
                break
            elif suggested < current_sl:
                result["adjusted_sl"] = suggested
                _log(f"[PRE-SEND] ADJUST {symbol} {direction} {tf}: "
                     f"SL {current_sl} → {suggested} (apertar, [{a['type']}] {a['detail']})")
                break
            # suggested >= current e current >= min → não infla, mantém atual.

    return result


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
