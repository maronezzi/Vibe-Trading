"""
Testes para core/vt_confluence.py — camada de confluencia multi-indicador.

Wave N (Bruno 09/07): cobertura dos 4 checks (candle, fib, htf, adx) + notify.

Estrutura:
    TestConfluenceScore  — combinacao dos 4 checks + threshold de score
    TestCandleCheck      — padroes de candle (hammer / engulfing)
    TestFibonacciCheck   — proximidade de niveis 38.2/50/61.8%
    TestHtfBias          — confirmacao de direcao via H1
    TestAdxCheck         — filtro de tendencia (nao chop)
    TestNotify           — notify_confluence_result (Telegram + cooldown)
"""

from core.vt_confluence import evaluate, notify_confluence_result
from core.vt_autotrader import calculate_ema, calculate_rsi, calculate_adx
from core.vt_notify import reset_cooldown


# ─── Helpers ──────────────────────────────────────────────────────────────


def make_utils():
    """Constroi dict de utils com os indicadores puros do autotrader."""
    return {
        "calculate_ema": calculate_ema,
        "calculate_rsi": calculate_rsi,
        "calculate_adx": calculate_adx,
    }


def make_trending_bars(direction="UP", n=80, start_price=100.0, atr_pct=0.01):
    """Cria n bars com tendencia clara (UP/DOWN). bars[0]=mais recente.

    Cada iteracao adiciona move=start_price*atr_pct ao preco, com candles
    de corpo proporcional e wicks curtos. Resultado: sequencia monotona
    de highs e lows, ideal para gerar ADX alto e +/- DI desbalanceados.
    """
    bars = []
    price = start_price
    for i in range(n):
        move = start_price * atr_pct
        if direction == "UP":
            price += move * 0.3
        else:
            price -= move * 0.3
        o = price - move * 0.2
        c = price + move * 0.2 if direction == "UP" else price - move * 0.2
        h = max(o, c) + move * 0.5
        lo = min(o, c) - move * 0.5
        bars.insert(
            0,
            {
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": 100,
                "time": f"2026-01-01T09:{i:02d}:00",
            },
        )
    return bars


def make_choppy_bars(n=80, start_price=100.0):
    """Cria n bars com oscilacao que zera o ADX (DI balanceado).

    Alterna candles com +DM e -DM de mesma magnitude a cada barra,
    de modo que +DI e -DI suavizados fiquem aproximadamente iguais.
    Sem direcao liquida, DX → 0 e ADX fica bem abaixo de 18.
    """
    bars = []
    for i in range(n):
        if i % 2 == 0:
            o = start_price
            c = start_price + 0.1
            h = start_price + 0.5
            lo = start_price - 0.1
        else:
            o = start_price + 0.1
            c = start_price
            h = start_price + 0.3
            lo = start_price - 0.3
        bars.append(
            {
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": 100,
                "time": f"2026-01-01T09:{i:02d}:00",
            }
        )
    bars.reverse()
    return bars


def make_fib_bars(swing_low=100.0, swing_high=110.0, n=60):
    """Cria n bars com swing_low/swing_high definidos. bars[0]=mais recente."""
    bars = []
    for i in range(n):
        price = swing_low + (i / (n - 1)) * (swing_high - swing_low)
        o = price - 0.05
        c = price + 0.05
        h = c + 0.05
        lo = o - 0.05
        bars.append(
            {
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": 100,
                "time": f"2026-01-01T09:{i:02d}:00",
            }
        )
    bars.reverse()
    return bars


def make_hammer_pair():
    """bars[0]=hammer bullish; bars[1]=candle normal bearish."""
    return [
        {
            "open": 100.3,
            "high": 100.7,
            "low": 95.0,
            "close": 100.5,
            "volume": 100,
            "time": "2026-01-01T09:00:00",
        },
        {
            "open": 100.5,
            "high": 100.7,
            "low": 99.9,
            "close": 100.0,
            "volume": 100,
            "time": "2026-01-01T08:59:00",
        },
    ]


def make_engulfing_bear_pair():
    """bars[0]=bearish engulfing; bars[1]=candle bullish."""
    return [
        {
            "open": 110.0,
            "high": 110.3,
            "low": 95.0,
            "close": 96.0,
            "volume": 100,
            "time": "2026-01-01T09:00:00",
        },
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 100,
            "time": "2026-01-01T08:59:00",
        },
    ]


# ─── TestConfluenceScore ──────────────────────────────────────────────────


class TestConfluenceScore:
    """Testes da funcao principal evaluate() — combinacao dos 4 checks."""

    def test_all_pass_with_perfect_setup(self):
        """Setup ideal: candle bullish engulfing, fib match, H1 confirma, ADX trending."""
        bars = make_trending_bars(direction="UP", n=80, start_price=100.0, atr_pct=0.02)
        bars[1] = {
            "open": 148.0,
            "high": 148.2,
            "low": 147.0,
            "close": 147.5,
            "volume": 100,
            "time": "2026-01-01T09:77:00",
        }
        bars[0] = {
            "open": 147.0,
            "high": 148.5,
            "low": 146.5,
            "close": 148.5,
            "volume": 100,
            "time": "2026-01-01T09:78:00",
        }
        swing_high = max(b["high"] for b in bars[:50])
        swing_low = min(b["low"] for b in bars[:50])
        fib_50 = swing_high - (swing_high - swing_low) * 0.5
        bars_h1 = make_trending_bars(direction="UP", n=80, start_price=100.0, atr_pct=0.02)
        result = evaluate("WINQ26", "M5", "BUY", fib_50, 1.0, bars, bars_h1, {}, make_utils())
        assert result["score"] == 4, (
            f"Esperado score=4, obtido {result['score']}: "
            f"candle={result['checks']['candle']} "
            f"fib={result['checks']['fibonacci']} "
            f"htf={result['checks']['htf_bias']} "
            f"adx={result['checks']['adx']}"
        )
        assert result["pass"] is True
        assert result["blocked_reason"] is None
        assert result["min_score"] == 2

    def test_zero_score_blocks(self):
        """bars vazio e bars_h1=None → todos checks falham → score=0."""
        result = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, [], None, {}, make_utils())
        assert result["score"] == 0
        assert result["pass"] is False
        assert result["blocked_reason"] is not None
        assert "checks falharam" in result["blocked_reason"]
        assert result["min_score"] == 2

    def test_min_score_threshold(self):
        """score=1 com min_score=2 → bloqueado; score=2 com min_score=2 → passa."""
        bars = make_trending_bars(n=2)
        params_blocked = {
            "candle_required": False,
            "fib_required": True,
            "htf_required": True,
            "adx_required": True,
            "min_confluence_score": 2,
        }
        result_a = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, bars, None, params_blocked, make_utils())
        assert result_a["score"] == 1
        assert result_a["pass"] is False
        assert "checks falharam" in result_a["blocked_reason"]
        assert "minimo 2" in result_a["blocked_reason"]

        params_pass = {
            "candle_required": False,
            "fib_required": False,
            "htf_required": True,
            "adx_required": True,
            "min_confluence_score": 2,
        }
        result_b = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, bars, None, params_pass, make_utils())
        assert result_b["score"] == 2
        assert result_b["pass"] is True
        assert result_b["blocked_reason"] is None

    def test_default_min_score_is_2(self):
        """Sem min_confluence_score no params → usa DEFAULT=2."""
        result = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, [], None, {}, make_utils())
        assert result["min_score"] == 2


# ─── TestCandleCheck ──────────────────────────────────────────────────────


class TestCandleCheck:
    """Testes do check de candle pattern (hammer/engulfing)."""

    def test_hammer_bullish_passes(self):
        """bars[0]=hammer bullish, direction=BUY → candle.pass=True, pattern=HAMMER."""
        bars = make_hammer_pair()
        params = {
            "candle_required": True,
            "fib_required": False,
            "htf_required": False,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, bars, None, params, make_utils())
        assert result["checks"]["candle"]["pass"] is True
        assert result["checks"]["candle"]["pattern"] == "HAMMER"
        assert "HAMMER" in result["checks"]["candle"]["reason"]

    def test_engulfing_bearish_passes(self):
        """bars[0]=bearish engulfing, direction=SELL → candle.pass=True, pattern=ENGULFING_BEAR."""
        bars = make_engulfing_bear_pair()
        params = {
            "candle_required": True,
            "fib_required": False,
            "htf_required": False,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "SELL", 100.0, 1.0, bars, None, params, make_utils())
        assert result["checks"]["candle"]["pass"] is True
        assert result["checks"]["candle"]["pattern"] == "ENGULFING_BEAR"
        assert "ENGULFING_BEAR" in result["checks"]["candle"]["reason"]

    def test_wrong_direction_blocks(self):
        """bars[0]=hammer bullish, direction=SELL → candle.pass=False."""
        bars = make_hammer_pair()
        params = {
            "candle_required": True,
            "fib_required": False,
            "htf_required": False,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "SELL", 100.0, 1.0, bars, None, params, make_utils())
        assert result["checks"]["candle"]["pass"] is False
        assert result["checks"]["candle"]["pattern"] is None
        assert "SELL" in result["checks"]["candle"]["reason"]


# ─── TestFibonacciCheck ───────────────────────────────────────────────────


class TestFibonacciCheck:
    """Testes do check de fibonacci (38.2/50/61.8% de swing)."""

    def test_price_at_fib_50_passes(self):
        """Preco exatamente no nivel 50% → fib.pass=True com distance_pct≈0."""
        bars = make_fib_bars(swing_low=100.0, swing_high=110.0, n=60)
        swing_high = max(b["high"] for b in bars[:50])
        swing_low = min(b["low"] for b in bars[:50])
        fib_50 = swing_high - (swing_high - swing_low) * 0.5

        params = {
            "candle_required": False,
            "fib_required": True,
            "htf_required": False,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "BUY", fib_50, 1.0, bars, None, params, make_utils())
        assert result["checks"]["fibonacci"]["pass"] is True
        assert result["checks"]["fibonacci"]["distance_pct"] < 0.003
        assert result["checks"]["fibonacci"]["level"] is not None

    def test_price_far_from_fib_blocks(self):
        """Preco 20% acima do swing → fib.pass=False com distance_pct alto."""
        bars = make_fib_bars(swing_low=100.0, swing_high=110.0, n=60)
        params = {
            "candle_required": False,
            "fib_required": True,
            "htf_required": False,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "BUY", 120.0, 1.0, bars, None, params, make_utils())
        assert result["checks"]["fibonacci"]["pass"] is False
        assert result["checks"]["fibonacci"]["distance_pct"] > 0.01
        assert result["checks"]["fibonacci"]["level"] is not None


# ─── TestHtfBias ──────────────────────────────────────────────────────────


class TestHtfBias:
    """Testes do check de HTF bias (H1 confirma direcao)."""

    def test_h1_bull_matches_buy(self):
        """bars_h1 em uptrend forte → BULL bias → direction=BUY passa."""
        bars = make_trending_bars(direction="UP", n=80, start_price=100.0, atr_pct=0.02)
        bars_h1 = make_trending_bars(direction="UP", n=80, start_price=100.0, atr_pct=0.02)
        params = {
            "candle_required": False,
            "fib_required": False,
            "htf_required": True,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, bars, bars_h1, params, make_utils())
        assert result["checks"]["htf_bias"]["pass"] is True
        assert result["checks"]["htf_bias"]["bias"] == "BULL"
        assert "BULL" in result["checks"]["htf_bias"]["reason"]

    def test_no_h1_bars_skips_check(self):
        """bars_h1=None → htf_bias.pass=False com motivo de ausencia."""
        bars = make_trending_bars(direction="UP", n=80, start_price=100.0, atr_pct=0.02)
        params = {
            "candle_required": False,
            "fib_required": False,
            "htf_required": True,
            "adx_required": False,
        }
        result = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, bars, None, params, make_utils())
        assert result["checks"]["htf_bias"]["pass"] is False
        assert result["checks"]["htf_bias"]["bias"] is None
        reason = result["checks"]["htf_bias"]["reason"].lower()
        assert "indisponivel" in reason or "ausente" in reason or "insuficiente" in reason


# ─── TestAdxCheck ─────────────────────────────────────────────────────────


class TestAdxCheck:
    """Testes do check de ADX (mercado em tendencia, nao chop)."""

    def test_low_adx_blocks(self):
        """Bars laterais (chop) → ADX baixo (<18) → adx.pass=False."""
        bars = make_choppy_bars(n=80, start_price=100.0)
        params = {
            "candle_required": False,
            "fib_required": False,
            "htf_required": False,
            "adx_required": True,
        }
        result = evaluate("WINQ26", "M5", "BUY", 100.0, 1.0, bars, None, params, make_utils())
        assert result["checks"]["adx"]["pass"] is False
        assert result["checks"]["adx"]["adx"] < 18.0
        assert result["checks"]["adx"]["threshold"] == 18.0


# ─── TestNotify ───────────────────────────────────────────────────────────


class TestNotify:
    """Testes do notify_confluence_result (Telegram com cooldown 5min)."""

    def test_returns_false_when_not_blocked(self):
        """Se blocked=False, nao chama send_fn → retorna False."""
        sent = []

        def fake_send(msg):
            sent.append(msg)

        result = notify_confluence_result(
            "WIN",
            "M5",
            "BUY",
            4,
            4,
            {
                "candle": {"pass": True, "pattern": "HAMMER", "reason": "ok"},
                "fibonacci": {"pass": True, "level": 105.0, "distance_pct": 0.0},
                "htf_bias": {"pass": True, "bias": "BULL", "reason": "ok"},
                "adx": {"pass": True, "adx": 25.0, "threshold": 18.0},
            },
            blocked=False,
            min_score=2,
            send_fn=fake_send,
        )
        assert result is False
        assert sent == []

    def test_calls_send_fn_when_blocked(self):
        """Se blocked=True, chama send_fn com a mensagem formatada."""
        reset_cooldown()
        sent = []

        def fake_send(msg):
            sent.append(msg)

        result = notify_confluence_result(
            "WIN",
            "M5",
            "SELL",
            1,
            4,
            {
                "candle": {"pass": False, "pattern": None, "reason": "no pattern"},
                "fibonacci": {"pass": False, "level": None, "distance_pct": float("inf")},
                "htf_bias": {"pass": True, "bias": "BEAR", "reason": "ok"},
                "adx": {"pass": False, "adx": 10.0, "threshold": 18.0},
            },
            blocked=True,
            min_score=2,
            send_fn=fake_send,
        )
        assert result is True
        assert len(sent) == 1
        msg = sent[0]
        assert "CONFLUÊNCIA BLOQUEADA" in msg
        assert "WIN" in msg
        assert "SELL" in msg
        assert "1/4" in msg
