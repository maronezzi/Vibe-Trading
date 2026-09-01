#!/usr/bin/env python3
"""Liga/desliga a trava de lucro (profit lock) via save_full_config.

Uso:
    python3 scripts/toggle_profit_lock.py off    # desativa (experimento demo)
    python3 scripts/toggle_profit_lock.py on     # restaura comportamento padrão
    python3 scripts/toggle_profit_lock.py        # mostra estado atual

Afeta profit_lock_enabled E trailing_profit_lock_enabled (trava de CONTA —
bloqueia novas entradas quando o ratchet arma). O trailing stop por POSIÇÃO
continua sempre. O daemon recarrega o config sozinho na próxima troca de
versão — não precisa reiniciar.

Autorização: Bruno 01/09 ("Como é uma conta demo, vc quer desativar o profit
lock e verificar se alterações que fizemos está funcionando?") — experimento
de calibração do modo conta-real; restaurar ao fim do pregão.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    cfg = load_config()
    atual = (bool(cfg.get("profit_lock_enabled", False)),
             bool(cfg.get("trailing_profit_lock_enabled", True)))
    print(f"Estado atual: profit_lock_enabled={atual[0]} "
          f"trailing_profit_lock_enabled={atual[1]} (v{cfg.get('_version')})")

    if mode not in ("on", "off"):
        if mode != "status":
            print("Uso: toggle_profit_lock.py [on|off|status]")
            return 1
        return 0

    novo = (mode == "on", mode == "on")
    if novo == atual:
        print("Config já está nesse estado.")
    else:
        cfg["profit_lock_enabled"] = novo[0]
        cfg["trailing_profit_lock_enabled"] = novo[1]
        save_full_config(cfg, updated_by="scripts/toggle_profit_lock.py")
        cfg2 = load_config()
        print(f"NOVO: profit_lock_enabled={cfg2.get('profit_lock_enabled')} "
              f"trailing_profit_lock_enabled={cfg2.get('trailing_profit_lock_enabled')} "
              f"(v{cfg2.get('_version')}, by {cfg2.get('_updated_by')})")
    if mode == "off":
        # Desarma TAMBÉM o lock cheio já armado (state /tmp/vt_profit_lock.json) —
        # is_locked() não consulta o config, então sem isso as entradas seguem
        # bloqueadas mesmo com a flag off.
        from core import vt_profit_lock as _pl
        locked, st = _pl.is_locked()
        if locked:
            _pl.release_lock()
            print(f"Lock cheio DESARMADO (estava armado desde "
                  f"{str(st.get('armed_at', '?'))[:16]}, target R$ {st.get('target', 0):.2f})")
        else:
            print("Lock cheio: não estava armado.")
    print("Daemon recarrega o config sozinho no próximo ciclo — /tmp/vt_autotrader.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
