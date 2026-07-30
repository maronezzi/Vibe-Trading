#!/usr/bin/env python3
"""Wave 14.5.1 op3 (Bruno 15/07 10:33 BRT): ajusta max_daily_loss do config
para destravar o bot (kill switch tinha travado em -R$ 300 enquanto PnL
do dia era -R$ 537 — saldo R$ 1.001.195,54).

Bruno pediu "opção 3: forçar operação". Limite ajustado para -R$ 1500
(dá espaço pra tentar recuperar sem deixar o bot perder tudo).

USO: AUTOTRADER PAUSADO (Lei 1). Roda uma vez e sai.
"""
import sys
sys.path.insert(0, '.')

from core.vt_config_loader import load_config, save_full_config

NEW_LIMIT = -1500
OPERATOR = "bruno_wave_14_5_1_opcao3_destrava"

cfg = load_config()
old = cfg.get("max_daily_loss")
print(f"Limite atual: R$ {old}")
print(f"PnL dia atual: -R$ 537,60 (MT5) / -R$ 1.212 (DB)")
print(f"Saldo: R$ 1.001.195,54")
print()

if old == NEW_LIMIT:
    print(f"⚠️ Já está em R$ {NEW_LIMIT}. Nada a fazer.")
    sys.exit(0)

cfg["max_daily_loss"] = NEW_LIMIT
save_full_config(cfg, updated_by=OPERATOR)

cfg2 = load_config()
print(f"✅ max_daily_loss = R$ {cfg2.get('max_daily_loss')}")
print(f"   _version = {cfg2.get('_version')}")
print(f"   _updated_by = {cfg2.get('_updated_by')}")