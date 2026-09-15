#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wave 894C (Bruno 15/09/2026) — meio termo dos gatilhos de lucro.

Motivação (dados ago→set/2026): o piso estrutural do alvo (1.5× perda média)
é pró-cíclico — dia ruim → piso sobe → alvo inalcançável → trava nunca arma.
O alvo escalou 100→400 e setembro teve 2 travas de lucro vs 20 em agosto
(alvo ~150, o único mês positivo). Bruno: "lucro acima de R$60 satisfatório;
o prejuízo estava na casa dos 300 — achar meio termo".

Mecânica resultante (alvo 200):
- Ratchet acorda em +R$60 (trailing_activation_pct 0.3 × trailing_target_per_lot 200):
  bloqueia novas entradas e garante ≥50% do pico (floor sobe até 100% no alvo);
- Lock full fecha o dia em +R$200 (profit_lock_min_target 200 = per_lot, alinhados);
- Soft stop para novas entradas em −R$150 (soft_daily_loss; Wave 894).
Perda de referência do piso do calibrador passa a ser a LIMITADA (código).

One-shot. Rodar com autotrader PAUSADO (fora do pregão).
"""
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

UPDATED_BY = "bruno_human_decision_wave894c_retrigger_profits"

ALVOS = {
    # 400 → 200: faixa que funcionou em agosto (único mês positivo; 20 travas)
    "profit_lock_min_target": 200.0,
    # 250 → 200: ALINHA o alvo do ratchet com o lock full (estavam 250 vs 400)
    "trailing_target_per_lot": 200.0,
    # 0.5 → 0.3: ratchet acorda em 0.3×200 = R$60 (o gatilho pedido)
    "trailing_activation_pct": 0.3,
    # Explícito (Wave 894 no daemon já usava −150): input do piso anti-pró-ciclo
    "soft_daily_loss": -150.0,
}


def main() -> int:
    try:
        r = subprocess.run(["pgrep", "-f", "core/vt_autotrader.py"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            print("🚫 autotrader rodando — rode fora do pregão/daemon parado")
            return 2
    except FileNotFoundError:
        pass

    from core.vt_config_loader import load_config, save_full_config

    cfg = load_config(force=True)
    print(f"config base: _version={cfg.get('_version')} "
          f"_updated_by={cfg.get('_updated_by')}")
    for k, novo in ALVOS.items():
        antigo = cfg.get(k, "(ausente)")
        cfg[k] = novo
        print(f"{k}: {antigo} → {novo}")
    if "trailing_floor_pct" not in cfg:
        cfg["trailing_floor_pct"] = 0.5

    notas = cfg.get("_notes") or ""
    cfg["_notes"] = (notas + " | " if notas else "") + (
        "Wave 894C 15/09: gatilhos de lucro no meio termo — alvo 400→200, "
        "per_lot 250→200 (ratchet alinhado ao lock full), ativação 0.5→0.3 "
        "(ratchet acorda em R$60), soft_daily_loss −150 explícito. Piso do "
        "calibrador agora referencia a perda LIMITADA (anti-pró-ciclo)."
    )

    save_full_config(cfg, updated_by=UPDATED_BY)
    print(f"✅ gravado por {UPDATED_BY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
