"""
Otimização de meio-dia — corrige: muda os params em params_by_tf.BIT_H1
(que é o único TF que executa VWAP em BIT), não no bloco base 'bit'.
"""
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from vt_config_loader import load_config, save_params, save_full_config  # noqa: E402

cfg = load_config(force=True)

# Reverter mudança anterior no bloco base 'bit' (não tem efeito prático
# porque params_by_tf.BIT_H1 sobrescreve o que VWAP usa)
# Vamos salvar de volta os valores antigos
revert = {
    "vwap_period": 30,
    "vwap_buy_threshold": 1.01,
}
save_params("bit", revert, updated_by="meio_dia_revert")
print("Revertido bloco base 'bit' para vwap_period=30, vwap_buy_threshold=1.01")

# Agora ajustar de verdade em params_by_tf.BIT_H1
# Carrega config fresca
cfg = load_config(force=True)
new_h1 = dict(cfg["params_by_tf"].get("BIT_H1", {}))
new_h1["vwap_period"] = 40          # 30 → 40 (mais suavizado)
new_h1["vwap_buy_threshold"] = 1.015  # 1.01 → 1.015 (entrada mais seletiva)
# Não tocar vwap_sell_threshold (mantido em 0.99)
# Não tocar sl_atr_mult
# Apenas 2 params alterados

cfg["params_by_tf"]["BIT_H1"] = new_h1
ok = save_full_config(cfg, updated_by="meio_dia")
print(f"save_full_config → {ok}")
print(f"Versão: v{cfg.get('_version')} (by {cfg.get('_updated_by')})")
print(f"  BIT_H1.vwap_period: {new_h1['vwap_period']}")
print(f"  BIT_H1.vwap_buy_threshold: {new_h1['vwap_buy_threshold']}")
print(f"  BIT_H1.vwap_sell_threshold (preservado): {new_h1['vwap_sell_threshold']}")
print(f"  BIT_H1.sl_atr_mult (preservado): {new_h1['sl_atr_mult']}")
