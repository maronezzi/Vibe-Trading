"""
vt_config_loader — Hot-reload config do Vibe-Trading.

Uso no autotrader:
    from vt_config_loader import load_config
    CONFIG = load_config()  # no início
    CONFIG = load_config()  # a cada ciclo (hot reload se mudou)

Uso nos scripts de otimização:
    from vt_config_loader import save_params, save_full_config
    save_params("wdo", params, updated_by="agi_17h")
    save_full_config(config, updated_by="optimizer")
"""

import json
import os
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("vt_config")

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"

# Cache
_config = None
_mtime = 0


def load_config(force: bool = False) -> dict:
    """Carrega config do JSON. Hot-reload se arquivo mudou (mtime)."""
    global _config, _mtime

    try:
        current_mtime = os.path.getmtime(CONFIG_PATH)
    except FileNotFoundError:
        log.error(f"Config não encontrado: {CONFIG_PATH}")
        return _config or {}

    if not force and _config is not None and current_mtime == _mtime:
        return _config  # sem mudança

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            new_config = json.load(f)

        # Validação mínima
        for key in ["symbols", "strategy", "wdo", "win"]:
            if key not in new_config:
                log.error(f"Config inválido: falta chave '{key}'")
                return _config or {}

        # Detectar mudança de versão
        old_ver = _config.get("_version", 0) if _config else 0
        new_ver = new_config.get("_version", 0)

        _config = new_config
        _mtime = current_mtime

        if old_ver != new_ver or force:
            by = new_config.get("_updated_by", "?")
            log.info(f"🔄 Config recarregada! v{old_ver}→v{new_ver} (by {by})")

        return _config

    except (json.JSONDecodeError, IOError) as e:
        log.error(f"Erro ao ler config: {e}")
        return _config or {}


def save_params(symbol_root: str, params: dict, updated_by: str = "optimizer"):
    """Salva parâmetros de um símbolo no JSON (usado por scripts de otimização)."""
    cfg = load_config(force=True)

    key = symbol_root.lower()
    # Merge: mantém chaves existentes, atualiza as novas
    if key in cfg:
        cfg[key].update(params)
    else:
        cfg[key] = params

    cfg["_version"] = cfg.get("_version", 0) + 1
    cfg["_updated_at"] = datetime.now().isoformat()
    cfg["_updated_by"] = updated_by

    return _atomic_write(cfg)


def save_full_config(cfg: dict, updated_by: str = "optimizer"):
    """Salva config completa no JSON (usado pelo AGI)."""
    cfg["_version"] = cfg.get("_version", 0) + 1
    cfg["_updated_at"] = datetime.now().isoformat()
    cfg["_updated_by"] = updated_by

    return _atomic_write(cfg)


def _atomic_write(cfg: dict) -> bool:
    """Escrita atômica: tmp + rename (evita corrupção)."""
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_PATH)
        log.info(f"✅ Config salva v{cfg.get('_version', '?')} (by {cfg.get('_updated_by', '?')})")
        return True
    except IOError as e:
        log.error(f"Erro ao salvar config: {e}")
        return False
