"""
Pytest config — injeta o root do projeto e o diretório core/ no sys.path
para que os testes consigam fazer `from core.vt_autotrader import ...`,
`from agi_tuning_17h import ...`, etc.

Mesmo padrão aplicado em monitoring/vt_daily_report.py (17/06/2026)
para corrigir ModuleNotFoundError: No module named 'vt_hermes_helper'.

Runs before any test collection.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _PROJECT_ROOT / "core"
_AGI_DIR = _PROJECT_ROOT / "optimization"  # agi_tuning_17h.py lives here
_MT5_DIR = _PROJECT_ROOT / "mt5"  # mt5_error_recovery.py lives here
_MONITORING_DIR = _PROJECT_ROOT / "monitoring"  # vt_analyst.py lives here

# Idempotente: não duplica entradas
for p in (str(_PROJECT_ROOT), str(_CORE_DIR), str(_AGI_DIR), str(_MT5_DIR), str(_MONITORING_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
