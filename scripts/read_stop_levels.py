#!/usr/bin/env python3
"""Lê stop level / freeze level / volume_step dos contratos ativos no MT5 conectado.

Uso (manual, fora do cron):
    python3 scripts/read_stop_levels.py

Read-only via orchestrator (Wine) — ZERO ordens. Rode com a conta ALVO
conectada no MT5: hoje (demo) serve para contrastar; na migração, rode com a
conta REAL conectada e cole o bloco sugerido no vt_config.json via
save_params() (nunca editar o JSON direto).

Contexto: docs/lesson_learning_2026-08-05.md — a demo reporta stops_level=0
e aceita trailing de 5pts; a real rejeitou ×155 (INVALID_STOPS). Os valores
reais são config do SERVIDOR do broker (não publicados — buscar na internet
não resolve; a leitura abaixo É a resposta autoritativa, e perguntar à XP é
o caminho secundário). Podem mudar intradiário em picos de volatilidade —
recomendo re-ler no dia da migração.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mt5 import mt5_orchestrator as orch  # noqa: E402

def main() -> None:
    cfg = json.loads((ROOT / "vt_config.json").read_text(encoding="utf-8"))
    resolved = cfg.get("resolved_symbols", {}) or {}
    if not resolved:
        raise SystemExit("resolved_symbols vazio no vt_config.json")

    sugerido = {}
    print(f"{'raiz':<5} {'contrato':<9} {'stops_level':>11} {'freeze':>7} "
          f"{'vol_step':>8} {'spread':>7}")
    for root in sorted(resolved):
        contrato = resolved[root]
        info = orch.info(contrato)
        if not isinstance(info, dict) or "error" in info:
            print(f"{root:<5} {contrato:<9} ERRO: {info}")
            continue
        stops = info.get("trade_stops_level")
        freeze = info.get("trade_freeze_level", "?")
        step = info.get("volume_step", "?")
        spread = info.get("spread", "?")
        point = info.get("point", 1.0)
        print(f"{root:<5} {contrato:<9} {stops!s:>11} {freeze!s:>7} "
              f"{step!s:>8} {spread!s:>7}")
        # stops_level vem em POINTS do símbolo (point); converter p/ pts de preço
        try:
            sugerido[root] = float(stops or 0) * float(point or 1.0)
        except (TypeError, ValueError):
            sugerido[root] = 0.0

    print("\nBloco sugerido p/ o walker (modo conta-real, pts de PREÇO):")
    print(json.dumps({"stop_level_sim_pts": sugerido}, ensure_ascii=False, indent=2))
    print("\n⚠️  Aplicar SOMENTE com os valores da CONTA REAL (via save_params).")
    print("⚠️  trade_freeze_level ausente no payload do executor — se precisar dele")
    print("   (trailing muito próximo do preço), estender mt5_executor.cmd_info.")


if __name__ == "__main__":
    main()
