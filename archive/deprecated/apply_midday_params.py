"""
Aplica otimização de meio-dia baseada nos trades < 12h de hoje.
Regras:
- NÃO troca estratégia
- NÃO mexe em sl_atr_mult
- Máximo 2 parâmetros por ativo
- Salva via save_params com updated_by="meio_dia"
"""
import sys
sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")
from vt_config_loader import load_config, save_params, save_full_config

# Mudanças decididas pela análise (apenas params por par symbol_TF + bloco do símbolo):
# 2026-06-19: DOL removido — Bruno tirou contratos cheios de circulação.
# - WDO_M30 (EMA_PULLBACK, 33%WR n=3): adx_threshold 18->22 + pullback_pct 0.10->0.15
# - WDO_M5  (RSI_REVERSION, 0%WR n=3): rsi_period 10->14 + rsi_oversold 25->20
# - WIN_M30 (RSI_REVERSION, 0%WR n=2): rsi_period 14->21 + rsi_overbought 75->70

changes = {
    # SYMBOL-LEVEL params (não sobrescreve nada em params_by_tf; é o default do bloco do símbolo)
    "wdo": {"adx_threshold": 22, "pullback_pct": 0.15},
    "win": {"rsi_period": 21, "rsi_overbought": 70},

    # PAR-LEVEL override (params_by_tf) — tem precedência
    # WDO M5 — RSI_REVERSION: RSI curto demais, muito ruído
    "WDO_M5": {"rsi_period": 14, "rsi_oversold": 20},
}

cfg = load_config(force=True)
print("=== CONFIG ANTES ===")
print(f"  version: {cfg.get('_version')}")
print(f"  updated_by: {cfg.get('_updated_by')}")
print()
print("Mudanças a aplicar:")
for k, v in changes.items():
    print(f"  {k}: {v}")

print()
print("=== Aplicando via save_params ===")
for key, params in changes.items():
    # save_params aceita tanto symbol root (e.g. 'wdo') quanto chave arbitrária
    # mas update do config[key]. Vamos garantir que chaves params_by_tf vão pra params_by_tf
    if "_" in key and key not in cfg:
        # é uma chave params_by_tf, força salvar em params_by_tf
        cfg = load_config(force=True)
        cfg.setdefault("params_by_tf", {})
        # IMPORTANTE: a config carregada é a do disco; precisa garantir merge
        cfg["params_by_tf"][key] = {**(cfg["params_by_tf"].get(key, {})), **params}
        # Atualiza metadados
        from datetime import datetime
        cfg["_version"] = cfg.get("_version", 0) + 1
        cfg["_updated_at"] = datetime.now().isoformat()
        cfg["_updated_by"] = "meio_dia"
        from vt_config_loader import _atomic_write
        ok = _atomic_write(cfg)
        print(f"  ✅ params_by_tf[{key}] = {params}  saved={ok}")
        # recarrega
        cfg = load_config(force=True)
    else:
        ok = save_params(key, params, updated_by="meio_dia")
        print(f"  ✅ {key} = {params}  saved={ok}")

print()
print("=== CONFIG DEPOIS ===")
cfg2 = load_config(force=True)
print(f"  version: {cfg2.get('_version')}  updated_by: {cfg2.get('_updated_by')}")
print()
print("Valores finais por chave alterada:")
for k in changes:
    if "_" in k:
        # params_by_tf
        print(f"  params_by_tf[{k}]: {cfg2.get('params_by_tf', {}).get(k)}")
    else:
        print(f"  {k}: {cfg2.get(k)}")

# Confirma sl_atr_mult inalterado
print()
print("=== Sanity check: sl_atr_mult ===")
for k in ["wdo", "win", "WDO_M5"]:
    if "_" in k:
        v = cfg2.get("params_by_tf", {}).get(k, {}).get("sl_atr_mult")
    else:
        v = cfg2.get(k, {}).get("sl_atr_mult")
    print(f"  {k}.sl_atr_mult = {v}")