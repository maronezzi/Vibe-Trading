#!/usr/bin/env python3
"""W14.7 (2026-07-20, seg, Bruno) — Corrige contract_specs.mult no vt_config.json.

Bug histórico: o config tem multipliers que divergem dos valores broker-truth
validados (fallback hardcoded em core/vt_trade_log.py:182 get_multiplier).
Ex.: WIN$.mult=1.0 no config, mas o real (mini WINQ26) é 0.20 R$/ponto.

Origem do erro: scripts/w873_recovery_20260707.py gravou 1.0 achando que era
broker-truth — mas esse valor vale para WIN cheio, não mini. O fallback
hardcoded em get_multiplier() sempre sobrescreveu o config em runtime (com
warning de divergência a cada log_entry), então o PnL vivo nunca foi afetado.
Esta correção alinha o config ao fallback para:
  - silenciar o warning spam ("contract_specs.WIN$.mult diverge...");
  - deixar config e runtime consistentes (auditabilidade).

Validação empírica: o `net_pnl` do DB (que usa get_multiplier → 0.20) casa
com a queda real do saldo broker. Ex.: trade SELL 174585→175100 = -515 pts
→ -R$103 (DB) e não -R$515.

Muda APENAS `mult` em cada spec. Mantém slip_r/margin/tick inalterados (sem
evidência empírica para mudá-los agora; evita efeito colateral no backtest).

Uso (autotrador PAUSADO — data/autotrader.paused presente):
    python3 scripts/w14_7_fix_contract_specs_mult_20260720.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

# Valores broker-truth validados (mesma fonte do fallback em
# core/vt_trade_log.py:182 get_multiplier). Confirmed por casamento
# DB net_pnl × saldo broker em 20/07.
CORRECT_MULT = {
    "WIN$": 0.20,    # era 1.0  (mini WINQ26 = R$0.20/ponto)
    "WDO$": 10.00,   # era 0.0015
    "BIT$": 0.01,    # Wave 880.K: mantém 0.01 (R$0,01/pt — validado broker-truth)
    "WSP$": 0.01,    # já correto (mantém)
    "DOL$": 1.00,    # era 0.0018
    "IND$": 1.0,     # já correto (mantém)
}


def main():
    # Guarda de convenção: autotrador deve estar pausado. Não enforce em
    # código (o gate real do pause entra em core/vt_autotrader.py Wave 880.F,
    # mas só vive após reinício do daemon — ver AGENTS.md).
    pause_file = Path(__file__).resolve().parent.parent / "data" / "autotrader.paused"
    if not pause_file.exists():
        print("⚠️  AVISO: data/autotrader.paused AUSENTE.")
        print("   Regra Bruno: rodar scripts de config com autotrador pausado.")
        resp = input("   Continuar mesmo assim? [digite SIM]: ").strip()
        if resp != "SIM":
            print("Abortado.")
            sys.exit(1)

    cfg = load_config()
    specs = cfg.get("contract_specs", {})

    print("=== ANTES ===")
    for k in CORRECT_MULT:
        cur = specs.get(k, {}).get("mult", "?")
        flag = "" if cur == CORRECT_MULT[k] else f"  ← mudar para {CORRECT_MULT[k]}"
        print(f"  {k}.mult = {cur}{flag}")

    # Aplica só `mult`; preserva slip_r/margin/tick existentes.
    changed = 0
    for k, mult in CORRECT_MULT.items():
        if k not in specs:
            print(f"  ⚠️  {k} ausente em contract_specs — pulando")
            continue
        if specs[k].get("mult") != mult:
            specs[k]["mult"] = mult
            changed += 1
    cfg["contract_specs"] = specs

    print(f"\n=== DEPOIS ({changed} spec(s) alterada(s)) ===")
    for k in CORRECT_MULT:
        print(f"  {k}.mult = {specs[k]['mult']}")

    if changed == 0:
        print("\n✓ Nada a fazer — config já estava correto.")
        return

    save_full_config(cfg, updated_by="w14_7_fix_contract_specs_20260720")
    print(f"\n✓ vt_config.json salvo ({changed} mult corrigido, by=w14_7_fix_contract_specs_20260720)")


if __name__ == "__main__":
    main()
