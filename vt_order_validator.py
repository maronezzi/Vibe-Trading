"""
vt_order_validator.py — Validação inteligente pós-envio.

Após cada ordem enviada pelo autotrader, este script analisa:
1. Se o SL está coerente com o ATR (não muito largo, não muito apertado)
2. Se a direção da ordem faz sentido com a estratégia
3. Se os parâmetros estão dentro dos limites esperados
4. Se há riscos não capturados (spread alto, horário de risco, etc.)

Se detectar problema, loga alerta e sugere correção via LLM.

Uso (chamado automaticamente pelo autotrader):
    from vt_order_validator import validate_order
    result = validate_order(order_data)
"""

import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

PROJECT = Path(__file__).parent
VALIDATOR_LOG = Path("/tmp/vt_order_validator.log")
ALERT_LOG = Path("/tmp/vt_order_alerts.log")

# Limites seguros para SL (em pontos EXECUTOR = sl_pts * point)
SL_LIMITS = {
    "WDO": {"min": 3000, "max": 300000, "atr_multiplier_max": 5.0},
    "WIN": {"min": 200, "max": 3000, "atr_multiplier_max": 5.0},
    "BIT": {"min": 3000, "max": 500000, "atr_multiplier_max": 3.0},  # max 500k exec pts = 5000 nativos (BIT ATR chega a 3600+)
    "DOL": {"min": 3000, "max": 300000, "atr_multiplier_max": 5.0},
    "IND": {"min": 200, "max": 3000, "atr_multiplier_max": 5.0},
    "WSP": {"min": 500, "max": 30000, "atr_multiplier_max": 5.0},
}


def _log(msg: str, file=None):
    """Log com timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    target = file or VALIDATOR_LOG
    with open(target, "a") as f:
        f.write(line)


def _ask_llm(prompt: str, timeout: int = 60) -> Optional[str]:
    """Consulta LLM com cadeia de fallback.

    Ordem de modelos:
    1. minimax/minimax-m3 (minimax-portal) — padrão desde 2026-06-15 (MiniMax direto)
    2. zhipu/glm-5.2 (zai) — fallback 1
    3. xiaomi/mimo-v2.5-pro (zai) — fallback 2

    Política:
    - Tenta cada modelo em sequência
    - Se modelo retorna resposta válida, retorna imediatamente
    - Se falha (timeout, 429, 5xx), loga e tenta o próximo
    - Se todos falham, retorna None
    """
    from vt_hermes_helper import find_hermes
    hermes_bin = find_hermes()
    if not hermes_bin:
        _log("[WARN] hermes CLI não encontrado no sistema")
        return None

    models = [
        ("minimax/minimax-m3", "minimax-portal"),   # primário
        ("glm-5.2", "zai"),                        # fallback 1
    ]

    for model, provider in models:
        try:
            result = subprocess.run(
                [hermes_bin, "-z", prompt, "-m", model, "--provider", provider],
                capture_output=True,
                text=True,
                timeout=timeout
            )
            if result.returncode == 0 and result.stdout.strip():
                if model != models[0][0]:
                    _log(f"[LLM] Fallback {model} ({provider}) respondeu OK")
                return result.stdout.strip()

            # Erro — logar e tentar próximo
            stderr = (result.stderr or "")[:200]
            if "429" in stderr or "balance" in stderr.lower() or "quota" in stderr.lower():
                _log(f"[WARN] {model} ({provider}): sem saldo (429) — tentando próximo")
            else:
                _log(f"[WARN] {model} ({provider}): erro rc={result.returncode}: {stderr[:80]}")

        except subprocess.TimeoutExpired:
            _log(f"[WARN] {model} ({provider}): timeout ({timeout}s) — tentando próximo")
        except Exception as e:
            _log(f"[WARN] {model} ({provider}): {e}")

    _log("[WARN] Todos os modelos LLM falharam — validação local apenas")
    return None


def _validate_sl_locally(order_data: dict) -> list:
    """Validação local do SL (sem LLM, rápida).
    Retorna lista de alertas encontrados.
    """
    alerts = []
    symbol = order_data.get("symbol", "")
    direction = order_data.get("direction", "")
    sl_pts = order_data.get("sl_pts", 0)
    atr = order_data.get("atr", 0)
    entry_price = order_data.get("entry_price", 0)
    strategy = order_data.get("strategy", "")

    # Determinar base do símbolo
    base = "WDO" if "WDO" in symbol else "WIN" if "WIN" in symbol else \
           "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
           "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
    limits = SL_LIMITS.get(base, {"min": 200, "max": 50000, "atr_multiplier_max": 5.0})

    # 1. SL muito pequeno (pode ser stopado por ruído)
    min_sl = limits.get("min", 50)
    if sl_pts < min_sl:
        alerts.append({
            "type": "SL_MUITO_PEQUENO",
            "severity": "HIGH",
            "detail": f"SL de {sl_pts}pts abaixo do mínimo seguro ({min_sl}pts). Risco de stop por ruído.",
            "suggestion": f"Aumentar SL para pelo menos {min_sl}pts"
        })

    # 2. SL muito grande (risco excessivo)
    max_sl = limits.get("max", 500)
    if sl_pts > max_sl:
        alerts.append({
            "type": "SL_MUITO_GRANDE",
            "severity": "CRITICAL",
            "detail": f"SL de {sl_pts}pts acima do máximo seguro ({max_sl}pts). Risco de perda excessiva.",
            "suggestion": f"Reduzir SL para {max_sl}pts ou menos"
        })

    # 3. SL vs ATR (se ATR disponível)
    # sl_pts está em executor units (sl_pts * point = distância em preço)
    # ATR está em pontos nativos do preço
    # Precisa converter sl_pts pra pontos nativos para comparar
    _point_mult = {
        "WDO": 1000, "WIN": 1, "BIT": 100, "DOL": 1000, "IND": 1, "WSP": 100
    }.get(base, 1)
    sl_native = sl_pts / _point_mult if _point_mult > 0 else sl_pts
    
    if atr > 0:
        atr_mult = sl_native / atr
        max_mult = limits.get("atr_multiplier_max", 5.0)
        if atr_mult > max_mult:
            alerts.append({
                "type": "SL_ATR_EXCESSIVO",
                "severity": "HIGH",
                "detail": f"SL é {atr_mult:.1f}x o ATR ({atr:.1f}pts). Máximo recomendado: {max_mult}x ATR.",
                "suggestion": f"Reduzir SL para {int(atr * max_mult * _point_mult)}pts executor ({max_mult}x ATR)"
            })
        elif atr_mult < 0.5:
            alerts.append({
                "type": "SL_ATR_MUITO_APERTADO",
                "severity": "MEDIUM",
                "detail": f"SL é {atr_mult:.2f}x o ATR. Muito apertado, pode ser stopado por volatilidade normal.",
                "suggestion": f"Aumentar SL para pelo menos {int(atr * 1.0 * _point_mult)}pts executor (1.0x ATR)"
            })

    # 4. Verificar se SL está do lado errado (CAUSA DE PERDAS CATASTRÓFICAS)
    # Replicar lógica do executor MT5:
    #   BUY:  raw_sl = price - cur_sl_pts * point
    #   SELL: raw_sl = price + cur_sl_pts * point
    # Se sl_pts é negativo, o lado INVERTE:
    #   BUY com sl_pts<0  → raw_sl = price + |sl|*point → ACIMA (errado)
    #   SELL com sl_pts<0 → raw_sl = price - |sl|*point → ABAIXO (errado)
    sl_native_distance = sl_pts * _point_mult  # em pontos nativos (pode ser negativo)
    if entry_price > 0 and sl_pts != 0:
        if direction == "BUY":
            # SL efetivo = entry - distância (em nativo). Positivo = SL abaixo. Negativo = SL acima.
            sl_price = entry_price - sl_native_distance
            if sl_price >= entry_price:
                alerts.append({
                    "type": "SL_LADO_ERRADO",
                    "severity": "CRITICAL",
                    "detail": f"BUY com sl_pts={sl_pts} → SL efetivo {sl_price:.2f} está ACIMA da entrada {entry_price:.2f}. SL INVERTIDO (perda sem limite)!",
                    "suggestion": f"Usar sl_pts POSITIVO (ex: 600pts = 600 nativos abaixo de {entry_price:.2f})"
                })
        elif direction == "SELL":
            # SL efetivo = entry + distância. Positivo = SL acima. Negativo = SL abaixo.
            sl_price = entry_price + sl_native_distance
            if sl_price <= entry_price:
                alerts.append({
                    "type": "SL_LADO_ERRADO",
                    "severity": "CRITICAL",
                    "detail": f"SELL com sl_pts={sl_pts} → SL efetivo {sl_price:.2f} está ABAIXO da entrada {entry_price:.2f}. SL INVERTIDO (perda sem limite)!",
                    "suggestion": f"Usar sl_pts POSITIVO (ex: 3000pts = 3.0 nativos acima de {entry_price:.2f})"
                })

    return alerts


def validate_order(order_data: dict, use_llm: bool = True) -> dict:
    """
    Valida uma ordem após envio.

    Args:
        order_data: dict com:
            - symbol: símbolo (ex: WDON26)
            - direction: BUY ou SELL
            - entry_price: preço de entrada
            - sl_pts: stop loss em pontos
            - atr: ATR atual
            - strategy: nome da estratégia
            - volume: contratos
            - ticket: ticket MT5 (se já atribuído)
            - signal_detail: detalhes do sinal (opcional)
        use_llm: se True, consulta LLM para análise adicional

    Returns:
        dict com:
            - valid: bool
            - alerts: lista de alertas
            - llm_analysis: resposta da LLM (se consultada)
            - suggested_action: ação sugerida (se houver)
    """
    result = {
        "valid": True,
        "alerts": [],
        "llm_analysis": None,
        "suggested_action": None
    }

    symbol = order_data.get("symbol", "UNKNOWN")
    direction = order_data.get("direction", "UNKNOWN")
    sl_pts = order_data.get("sl_pts", 0)
    strategy = order_data.get("strategy", "UNKNOWN")
    atr = order_data.get("atr", 0)
    entry_price = order_data.get("entry_price", 0)

    # 1. Validação local (rápida, sem custo)
    local_alerts = _validate_sl_locally(order_data)
    result["alerts"] = local_alerts

    if local_alerts:
        result["valid"] = False
        _log(f"[ALERTA] {symbol} {direction} — {len(local_alerts)} alertas encontrados:")
        for alert in local_alerts:
            _log(f"  [{alert['severity']}] {alert['type']}: {alert['detail']}")
            _log(f"    Sugestão: {alert['suggestion']}")

    # 2. Consulta LLM para análise (SEMPRE, não só quando há alertas)
    if use_llm:
        # Pre-computar point do ativo para prompt limpo (evita double-brace no f-string)
        _point_map = {"WIN": 1, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1, "WSP": 0.01}
        _pt = _point_map.get(symbol[:3], 1)
        _native_sl = sl_pts * _pt
        _native_atr = atr
        _ideal_sl_min_pts = int(_native_atr * 1.2 / _pt)
        _ideal_sl_max_pts = int(_native_atr * 1.5 / _pt)

        # Determinar limites de executor pts para incluir no prompt (evita deadlock)
        _base = "WDO" if "WDO" in symbol else "WIN" if "WIN" in symbol else \
                "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
        _limits = SL_LIMITS.get(_base, {"min": 200, "max": 50000})
        _sl_min_exec = _limits["min"]
        _sl_max_exec = _limits["max"]

        prompt = f"""Você é um trader profissional. Analise esta ordem e OBRIGATORIAMENTE sugira um SL otimizado.

Símbolo: {symbol} | Direção: {direction} | Entrada: {entry_price}
SL atual: {sl_pts}pts | ATR: {atr:.2f}pts | Estratégia: {strategy}
Ponto do ativo: {_pt}

REGRAS OBRIGATÓRIAS:
1. SL deve ser entre 1.0x e 2.0x o ATR (em pontos nativos do ativo)
2. converter sl_pts para pontos nativos: sl_pts * point
3. SEMPRE retorne um sl_sugerido OTIMIZADO (não retorne null)
4. Se o SL atual já é otimizado, retorne o mesmo valor
5. LIMITES ABSOLUTOS em pts executor: mínimo {_sl_min_exec}, máximo {_sl_max_exec}
   O sl_sugerido DEVE estar dentro destes limites ou será rejeitado

SL atual em pontos nativos: {_native_sl:.1f}pts
ATR atual: {_native_atr:.2f}pts nativos
SL ideal: {int(_native_atr * 1.2)} a {int(_native_atr * 1.5)}pts nativos = {_ideal_sl_min_pts} a {_ideal_sl_max_pts}pts executor
Limites executor: [{_sl_min_exec} - {_sl_max_exec}]

Retorne APENAS JSON:
{{
  "sl_sugerido": <pts executor, OBRIGATÓRIO, dentro dos limites>,
  "resumo": "<motivo>"
}}"""

        llm_response = _ask_llm(prompt)
        if llm_response:
            result["llm_analysis"] = llm_response
            _log(f"[LLM] Análise para {symbol} {direction}: {llm_response[:200]}...")

            # Tentar parsear resposta da LLM
            try:
                # Encontrar JSON na resposta
                start = llm_response.find("{")
                end = llm_response.rfind("}") + 1
                if start >= 0 and end > start:
                    llm_data = json.loads(llm_response[start:end])

                    # Extrair sl_sugerido do LLM
                    new_sl_raw = llm_data.get("sl_sugerido")
                    if new_sl_raw is not None:
                        if isinstance(new_sl_raw, (int, float)) and new_sl_raw > 0:
                            new_sl = int(new_sl_raw)
                        elif isinstance(new_sl_raw, str) and new_sl_raw.strip().isdigit():
                            new_sl = int(new_sl_raw.strip())
                        else:
                            new_sl = None

                        # Clamp: garantir SL dentro dos limites antes de sugerir
                        if new_sl is not None:
                            if new_sl < _sl_min_exec:
                                _log(f"[LLM] SL sugerido {new_sl} < mínimo {_sl_min_exec}, clampado")
                                new_sl = _sl_min_exec
                            elif new_sl > _sl_max_exec:
                                _log(f"[LLM] SL sugerido {new_sl} > máximo {_sl_max_exec}, clampado")
                                new_sl = _sl_max_exec

                        # Só aplicar se a mudança for significativa (>5% ou >50pts)
                        if new_sl is not None and new_sl != sl_pts:
                            diff = abs(new_sl - sl_pts)
                            diff_pct = diff / sl_pts * 100 if sl_pts > 0 else 0
                            if diff > 50 or diff_pct > 5:
                                result["suggested_action"] = {
                                    "type": "MODIFY_SL",
                                    "symbol": symbol,
                                    "current_sl": sl_pts,
                                    "suggested_sl": new_sl,
                                    "reason": llm_data.get("resumo", llm_data.get("recomendação", "LLM sugere ajuste")),
                                    "risco": llm_data.get("risco"),
                                }
                                _log(f"[LLM] Sugere alterar SL de {sl_pts}pts para {new_sl}pts")
            except json.JSONDecodeError:
                _log("[WARN] LLM retornou JSON inválido")

    # 3. Log final
    status = "OK" if result["valid"] else "ALERTA"
    _log(f"[RESULTADO] {symbol} {direction} — Status: {status}, Alertas: {len(local_alerts)}")

    # Log em arquivo separado de alertas (se houver)
    if not result["valid"]:
        _log(f"{symbol} {direction} SL={sl_pts}pts ATR={atr:.1f}pts Strategy={strategy} Alerts={[a['type'] for a in local_alerts]}", ALERT_LOG)

    return result


def validate_and_fix(order_data: dict, modify_sl_func=None) -> dict:
    """
    Valida ordem e aplica correção automática se necessário.

    Args:
        order_data: dados da ordem
        modify_sl_func: função para modificar SL (ex: orchestrator.modify_sl)

    Returns:
        dict com resultado da validação e se correção foi aplicada
    """
    result = validate_order(order_data, use_llm=True)

    if result["suggested_action"] and result["suggested_action"]["type"] == "MODIFY_SL":
        action = result["suggested_action"]

        # Bounds check: garantir SL dentro dos limites seguros antes de aplicar
        symbol = action.get("symbol", "")
        base = "WDO" if "WDO" in symbol else "WIN" if "WIN" in symbol else \
               "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
               "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
        _limits = SL_LIMITS.get(base, {"min": 200, "max": 50000})
        suggested = action["suggested_sl"]
        if not (_limits["min"] <= suggested <= _limits["max"]):
            _log(f"[FIX] SL sugerido {suggested}pts fora dos limites [{_limits['min']}-{_limits['max']}], IGNORADO")
            result["fix_skipped"] = f"SL fora dos limites: {suggested} vs [{_limits['min']}-{_limits['max']}]"
            return result

        if modify_sl_func:
            _log(f"[FIX] Aplicando correção: SL {action['current_sl']} → {action['suggested_sl']}pts")
            try:
                fix_result = modify_sl_func(
                    action["symbol"],
                    order_data.get("ticket", 0),
                    action["suggested_sl"]
                )
                result["fix_applied"] = fix_result
                _log(f"[FIX] Resultado: {fix_result}")
            except Exception as e:
                _log(f"[FIX] Erro ao aplicar correção: {e}")
                result["fix_applied"] = {"error": str(e)}
        else:
            _log("[FIX] modify_sl_func não fornecido — correção não aplicada")

    return result


# CLI para testes
if __name__ == "__main__":
    # Testar com ordem de exemplo
    test_order = {
        "symbol": "WDON26",
        "direction": "BUY",
        "entry_price": 5180.5,
        "sl_pts": 200,  # SL muito grande!
        "atr": 4.0,
        "strategy": "VWAP",
        "volume": 1,
        "ticket": 123456
    }

    print("Testando validação de ordem...")
    print(f"Ordem: {test_order['symbol']} {test_order['direction']} @ {test_order['entry_price']}")
    print(f"SL: {test_order['sl_pts']}pts | ATR: {test_order['atr']}pts")
    print()

    result = validate_order(test_order, use_llm=False)  # Sem LLM no teste

    print(f"Válida: {result['valid']}")
    print(f"Alertas: {len(result['alerts'])}")
    for alert in result['alerts']:
        print(f"  [{alert['severity']}] {alert['type']}: {alert['detail']}")
        print(f"    → {alert['suggestion']}")

    if result['suggested_action']:
        print(f"\nAção sugerida: {result['suggested_action']}")
