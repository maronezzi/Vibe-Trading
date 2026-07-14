"""
test_profit_lock.py
===================
Testa a lógica do bloco profit-lock introduzido na Wave Melhoria 2.

O profit-lock move o SL para o entry (zero-loss) quando o lucro atinge
``profit_lock_r × initial_sl_pts`` (fração do risco inicial = 1R).

Como ``manage_position`` é uma função monolítica acoplada a state global e
MT5 bridge, este teste exercita a LÓGICA do bloco profit-lock isoladamente
(mesmo padrão do test_breakeven_safety.py que mocka componentes).

Cobre:
- profit_lock_r=0.0 (default) = não faz nada.
- Lucro >= profit_lock_r × R → SL move para entry, profit_locked=True.
- Lucro < profit_lock_r × R → não move.
- Já locked (profit_locked=True) → não re-dispara.
- trailing já ligado → profit-lock pulado (mutuamente exclusivo).
"""
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _eval_profit_lock(profit_pts, initial_sl_pts, current_sl_pts,
                      profit_lock_r, trail_on, profit_locked):
    """
    Replica a condição do bloco profit-lock em manage_position, SEM chamar MT5.
    Retorna (should_lock, lock_pts) onde should_lock indica se o SL deveria
    ser movido para entry.

    Esta é uma extração fiel da guarda lógica do bloco em vt_autotrader.py
    (apenas a parte de decisão — a chamada safe_modify_sl_with_emergency_close
    é mockada nos testes que exercitam manage_position real).
    """
    if trail_on or profit_locked or profit_lock_r <= 0:
        return (False, None)
    if initial_sl_pts <= 0:
        return (False, None)
    if profit_pts >= profit_lock_r * initial_sl_pts:
        # lock_pts = int(2 / point_val) — point_val varia por symbol.
        # No runtime é ~2 ticks. Para o teste, assumimos lock_pts pequeno.
        lock_pts = 2  # simplificação: 2 executor points
        if lock_pts < abs(current_sl_pts):
            return (True, lock_pts)
    return (False, None)


class TestProfitLockLogic(unittest.TestCase):
    """Testa a lógica de decisão do bloco profit-lock (extraída isoladamente)."""

    def test_disabled_default_does_nothing(self):
        """profit_lock_r=0.0 (default) nunca dispara o lock."""
        should, _ = _eval_profit_lock(
            profit_pts=1000, initial_sl_pts=300, current_sl_pts=300,
            profit_lock_r=0.0, trail_on=False, profit_locked=False)
        self.assertFalse(should, "profit_lock_r=0.0 deve ser no-op")

    def test_profit_above_threshold_locks(self):
        """Lucro >= 0.5R (com R=300) → 150pts de lucro dispara o lock."""
        should, lock_pts = _eval_profit_lock(
            profit_pts=200, initial_sl_pts=300, current_sl_pts=300,
            profit_lock_r=0.5, trail_on=False, profit_locked=False)
        self.assertTrue(should, "200pts >= 0.5×300=150 deve disparar lock")
        self.assertLess(lock_pts, 300, "lock_pts deve ser < sl_pts atual (aperta)")

    def test_profit_below_threshold_no_lock(self):
        """Lucro < 0.5R não dispara o lock."""
        should, _ = _eval_profit_lock(
            profit_pts=100, initial_sl_pts=300, current_sl_pts=300,
            profit_lock_r=0.5, trail_on=False, profit_locked=False)
        self.assertFalse(should, "100pts < 0.5×300=150 NÃO deve disparar")

    def test_already_locked_no_redisparo(self):
        """profit_locked=True → não re-dispara (one-shot)."""
        should, _ = _eval_profit_lock(
            profit_pts=2000, initial_sl_pts=300, current_sl_pts=2,
            profit_lock_r=0.5, trail_on=False, profit_locked=True)
        self.assertFalse(should, "Já locked não deve re-disparar")

    def test_trailing_on_skips_lock(self):
        """Trailing já ligado → profit-lock pulado (mutuamente exclusivo)."""
        should, _ = _eval_profit_lock(
            profit_pts=2000, initial_sl_pts=300, current_sl_pts=300,
            profit_lock_r=0.5, trail_on=True, profit_locked=False)
        self.assertFalse(should, "Com trailing ligado, profit-lock deve ser pulado")

    def test_lock_only_tightens(self):
        """Se lock_pts >= sl_pts atual (já apertado), não move (só aperta)."""
        # sl_pts já foi apertado para 1 (muito menor que lock_pts=2)
        should, lock_pts = _eval_profit_lock(
            profit_pts=2000, initial_sl_pts=300, current_sl_pts=1,
            profit_lock_r=0.5, trail_on=False, profit_locked=False)
        # lock_pts=2 não é < 1 → não aperta
        if should:
            self.assertLess(lock_pts, 1, "lock só aplica se mais apertado que atual")
        # Neste caso, should deve ser False pois 2 >= 1
        self.assertFalse(should, "lock_pts=2 >= current=1 não deve afrouxar")

    def test_exact_threshold_locks(self):
        """Lucro exatamente = profit_lock_r × R dispara (>= é inclusivo)."""
        should, _ = _eval_profit_lock(
            profit_pts=150, initial_sl_pts=300, current_sl_pts=300,
            profit_lock_r=0.5, trail_on=False, profit_locked=False)
        self.assertTrue(should, "150pts == 0.5×300 deve disparar (>=)")

    def test_uses_initial_risk_not_current(self):
        """R = initial_sl_pts (congelado), não o sl_pts atual (que pode ter sido apertado)."""
        # sl_pts atual já apertado para 100 por BE temporal, mas initial_risk=300.
        # Lucro 200 >= 0.5×300=150 → dispara (se lock_pts < 100).
        should, lock_pts = _eval_profit_lock(
            profit_pts=200, initial_sl_pts=300, current_sl_pts=100,
            profit_lock_r=0.5, trail_on=False, profit_locked=False)
        # 200 >= 150 → condição de lucro OK. lock_pts=2 < 100 → aplica.
        self.assertTrue(should, "Deve usar initial_risk=300, não current=100")
        self.assertLess(lock_pts, 100)

    def test_zero_initial_risk_no_lock(self):
        """initial_sl_pts=0 (degenerate) não dispara (guarda anti-div-by-zero)."""
        should, _ = _eval_profit_lock(
            profit_pts=5000, initial_sl_pts=0, current_sl_pts=0,
            profit_lock_r=0.5, trail_on=False, profit_locked=False)
        self.assertFalse(should, "initial_risk=0 não deve disparar lock")


class TestProfitLockConfig(unittest.TestCase):
    """Verifica que a config suporta o novo param profit_lock_r."""

    def test_profit_lock_r_is_valid_float(self):
        """Se presente na config, profit_lock_r deve ser float em [0, 1.5]."""
        import json
        cfg_path = os.path.join(PROJECT_ROOT, "vt_config.json")
        with open(cfg_path) as f:
            cfg = json.load(f)
        for pair, params in cfg.get("params_by_tf", {}).items():
            if "profit_lock_r" in params:
                val = params["profit_lock_r"]
                self.assertIsInstance(val, (int, float),
                    f"{pair}.profit_lock_r deve ser numérico, got {type(val)}")
                self.assertGreaterEqual(val, 0.0,
                    f"{pair}.profit_lock_r deve ser >= 0.0, got {val}")
                self.assertLessEqual(val, 1.5,
                    f"{pair}.profit_lock_r deve ser <= 1.5, got {val}")


if __name__ == "__main__":
    unittest.main()
