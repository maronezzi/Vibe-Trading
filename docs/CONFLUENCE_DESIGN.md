"""
core/vt_confluence.py — Camada de confluência multi-indicador.

Wave Per-TF+ (Bruno 09/07): filtro que envolve qualquer estrategia.
Combina 4 checks independentes. Score 0-4. Se score < min_score, bloqueia.

Checks:
  1. CANDLE_PATTERN: padrao de reversao (hammer/pin bar/engulfing) na direcao
  2. FIBONACCI:      preco perto (touch_pct) de nivel 38.2/50/61.8% de swing
  3. HTF_BIAS:       H1 confirma direcao (BULL/BEAR via EMA+ADX)
  4. ADX_TRENDING:   ADX >= adx_min (mercado em tendencia, nao chop)

API publica:
    evaluate(symbol, tf, direction, price, atr, bars, bars_h1, params, utils) -> dict

    Returns:
        {
          "score": int (0-4),
          "max_score": 4,
          "pass": bool,                    # score >= min_score
          "checks": {
            "candle":    {"pass": bool, "pattern": str|None, "reason": str},
            "fibonacci": {"pass": bool, "level": float|None, "distance_pct": float},
            "htf_bias":  {"pass": bool, "bias": str|None, "reason": str},
            "adx":       {"pass": bool, "adx": float, "threshold": float},
          },
          "min_score": int,                # resolvido por params_by_tf > default
          "blocked_reason": str|None,      # PT-BR, None se passou
        }

Parametros (lidos de params_by_tf[symbol_tf]):
    min_confluence_score  (int, default 2)
    candle_required       (bool, default True)   # se False, sempre passa
    fib_required          (bool, default True)
    fib_lookback          (int, default 50)
    fib_touch_pct         (float, default 0.003)
    htf_required          (bool, default True)
    htf_ema_fast          (int, default 9)
    htf_ema_slow          (int, default 21)
    htf_adx_min           (float, default 18.0)
    adx_required          (bool, default True)
    adx_min               (float, default 18.0)

Telegram:
    notify_confluence(symbol, tf, direction, score, checks, blocked) em core/vt_confluence.py
    - se blocked: cooldown 5min por (symbol, tf) — evita spam
    - se passou: cooldown 0 (sempre envia junto com a mensagem de abertura)

Wiring:
    - Adicionar "confluence" ao _strategy_utils
    - Em check_and_trade (core/vt_autotrader.py ~L1610), apos strategy_func retornar
      result e antes do _defenses_ok, chamar evaluate():
        if not confluence_passed: continue
    - Log: "[SINAL] WINQ26 M15 SELL @ 173635.00 | RSI_REVERSION | score=3/4
             (candle✓ fib✗ htf✓ adx✓)"
    - Telegram blocked: "🚫 CONFLUÊNCIA BLOQUEADA WIN M15 SELL
             | score 1/4 (candle✓ fib✗ htf✗ adx✗)
             | Motivo: 3 checks falharam
             | Esperado >=2"
"""