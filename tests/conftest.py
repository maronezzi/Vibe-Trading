"""
Pytest config — injeta o root do projeto e o diretório core/ no sys.path
para que os testes consigam fazer `from core.vt_autotrader import ...`,
`from agi_tuning_17h import ...`, etc.

Mesmo padrão aplicado em monitoring/vt_daily_report.py (17/06/2026)
para corrigir ModuleNotFoundError: No module named 'vt_hermes_helper'.

Runs before any test collection.
"""
import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _PROJECT_ROOT / "core"
_AGI_DIR = _PROJECT_ROOT / "optimization"  # agi_tuning_17h.py lives here
_MT5_DIR = _PROJECT_ROOT / "mt5"  # mt5_error_recovery.py lives here
_MONITORING_DIR = _PROJECT_ROOT / "monitoring"  # vt_analyst.py lives here

# Idempotente: não duplica entradas
for p in (str(_PROJECT_ROOT), str(_CORE_DIR), str(_AGI_DIR), str(_MT5_DIR), str(_MONITORING_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ─── Isolamento do config de produção (2026-06-23) ─────────────────────────
# Por padrão, TODO teste roda com CONFIG_PATH redirecionado para uma cópia
# temporária do vt_config.json, e o cache do loader (_config/_mtime) é zerado.
# Assim qualquer save_full_config/save_params durante os testes vai para o tmp,
# e o config de PRODUÇÃO nunca pode ser corrompido pelo pytest (bug histórico:
# o test_agi_memo fazia backup/restaura do config real, e quando outro teste
# corrompia o config entre setUp e tearDown, o estrago era propagado).
#
# O cache é zerado ANTES e DEPOIS de cada teste para evitar snapshot stale.
# Testes que PRECISAM do config real de fato (caso raro) podem optar out:
#     @pytest.mark.uses_real_config


@pytest.fixture(autouse=True)
def _isolate_vt_config(request, monkeypatch, tmp_path):
    """Redireciona vt_config_loader.CONFIG_PATH para tmp por padrão (fail-safe)."""
    if request.node.get_closest_marker("uses_real_config"):
        return

    import vt_config_loader

    real_path = vt_config_loader.CONFIG_PATH
    tmp_cfg = tmp_path / "vt_config_test.json"

    # Snapshot do config real (se existir) para o tmp.
    if real_path.exists():
        try:
            data = json.loads(real_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    # Garantir as chaves mínimas exigidas pela validação de load_config()
    # (symbols/strategy/wdo/win) para que o tmp sempre carregue, independente
    # do estado — eventualmente incompleto — do config real.
    data.setdefault("symbols", ["WIN", "WDO", "BIT", "WSP"])
    data.setdefault("strategy", {})
    data.setdefault("wdo", {})
    data.setdefault("win", {})

    tmp_cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # Redireciona path + zera cache do loader ANTES do teste.
    monkeypatch.setattr(vt_config_loader, "CONFIG_PATH", tmp_cfg)
    monkeypatch.setattr(vt_config_loader, "_config", None)
    monkeypatch.setattr(vt_config_loader, "_mtime", 0)

    yield

    # Zera cache DEPOIS para o próximo teste reler do path real (revertido
    # automaticamente pelo monkeypatch).
    vt_config_loader._config = None
    vt_config_loader._mtime = 0
