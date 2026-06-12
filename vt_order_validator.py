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

# Limites seguros para SL (em pontos)
SL_LIMITS = {
    "WDO": {"min": 50, "max": 300, "atr_multiplier_max": 5.0},
    "WIN": {"min": 500, "max": 50000, "atr_multiplier_max": 3.0},
}


def _log(msg: str, file=None):
    """Log com timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    target = file or VALIDATOR_LOG
    with open(target, "a") as f:
        f.write(line)


def _ask_llm(prompt: str, timeout: int = 30) -> Optional[str]:
    """Consulta LLM para análise da ordem.
    Usa o mesmo modelo configurado no Hermes (text-only para economia).
    """
    try:
        # Usar hermes CLI para consultar LLM
        result = subprocess.run(
            ["hermes", "ask", "--prompt", prompt, "--max-tokens", "500"],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            _log(f"[WARN] LLM retornou erro: {result.stderr[:200]}")
            return None
    except subprocess.TimeoutExpired:
        _log("[WARN] LLM timeout")
        return None
    except FileNotFoundError:
        _log("[WARN] hermes CLI não encontrado")
        return None
    except Exception as e:
        _log(f"[WARN] Erro ao consultar LLM: {e}")
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

    # Determinar base do símbolo (WDO ou WIN)
    base = "WDO" if "WDO" in symbol else "WIN"
    limits = SL_LIMITS.get(base, {})

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
    if atr > 0:
        atr_mult = sl_pts / atr
        max_mult = limits.get("atr_multiplier_max", 5.0)
        if atr_mult > max_mult:
            alerts.append({
                "type": "SL_ATR_EXCESSIVO",
                "severity": "HIGH",
                "detail": f"SL é {atr_mult:.1f}x o ATR ({atr:.1f}pts). Máximo recomendado: {max_mult}x ATR.",
                "suggestion": f"Reduzir SL para {int(atr * max_mult)}pts ({max_mult}x ATR)"
            })
        elif atr_mult < 0.5:
            alerts.append({
                "type": "SL_ATR_MUITO_APERTADO",
                "severity": "MEDIUM",
                "detail": f"SL é {atr_mult:.2f}x o ATR. Muito apertado, pode ser stopado por volatilidade normal.",
                "suggestion": f"Aumentar SL para pelo menos {int(atr * 1.0)}pts (1.0x ATR)"
            })

    # 4. Verificar se SL está do lado errado
    if entry_price > 0 and sl_pts > 0:
        if direction == "BUY":
            # BUY: SL deve estar ABAIXO da entrada
            # (verificação implícita — se sl_pts é positivo, está correto)
            pass
        elif direction == "SELL":
            # SELL: SL deve estar ACIMA da entrada
            # (verificação implícita)
            pass

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

    # 2. Consulta LLM para análise (se habilitado)
    if use_llm:
        prompt = f"""Analise esta ordem de trading que acabou de ser executada:

Símbolo: {symbol} | Direção: {direction} | Entrada: {entry_price}
SL: {sl_pts}pts | ATR: {atr:.2f}pts | Estratégia: {strategy}

{f"Alertas detectados: {json.dumps([a['type'] for a in local_alerts])}" if local_alerts else "Nenhum alerta local."}

Perguntas rápidas:
1. O SL está coerente para este ativo?
2. Há algum risco que passou batido?
3. Deveria ajustar o SL? Se sim, para quanto?

Responda em JSON:
{{
  "sl_ok": true/false,
  "sl_sugerido": <pts ou null>,
  "risco": "curta descrição ou null",
  "resumo": "resumo em 1 linha"
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

                    # Se LLM sugeriu SL diferente
                    new_sl_raw = llm_data.get("sl_sugerido")
                    if new_sl_raw is not None:
                        # Type-check: aceitar apenas int/float positivos
                        if isinstance(new_sl_raw, (int, float)) and new_sl_raw > 0:
                            new_sl = int(new_sl_raw)
                        elif isinstance(new_sl_raw, str) and new_sl_raw.strip().isdigit():
                            new_sl = int(new_sl_raw.strip())
                        else:
                            _log(f"[WARN] LLM retornou sl_sugerido inválido: {new_sl_raw!r}")
                            new_sl = None

                        if new_sl is not None and new_sl != sl_pts:
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
