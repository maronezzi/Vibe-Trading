"""
Aplica a mudança de WSP_M5 no config via save_full_config (mesmo método do Stage 5).

Mudança: BOLLINGER → EMA_CROSSOVER (ema_fast=8, ema_slow=20, sl_atr_mult=1.8, cooldown=60)
Motivo: backtest 30d aprovado nos gates do AGI (PF=2.22, WR=81%, PnL=+R$874).
Antes: BOLLINGER estava R$-72 (PF=0.96).

Preserva params de gestão de risco existentes (max_daily_trades, trail_activate,
profit_lock_r, max_consecutive_losses, halt_duration_minutes) — só atualiza
os da estratégia + sl_atr_mult + cooldown_seconds.
"""
import sys
sys.path.insert(0, '/home/bruno/Projects/Vibe-Trading')

from core.vt_config_loader import load_config, save_full_config

# Recarrega do disco (mesma prática do Stage 5)
cfg = load_config(force=True)

pair = "WSP_M5"
print(f"Config ANTES (v{cfg.get('_version')}):")
print(f"  strategy_by_tf[{pair}] = {cfg.get('strategy_by_tf', {}).get(pair)}")
print(f"  params_by_tf[{pair}] = {cfg.get('params_by_tf', {}).get(pair, {})}")

# 1. Trocar estratégia
cfg.setdefault("strategy_by_tf", {})[pair] = "EMA_CROSSOVER"

# 2. Atualizar params_by_tf — merge: preserva gestão de risco, troca estratégia + SL + cooldown
pbt = cfg.setdefault("params_by_tf", {}).setdefault(pair, {})
# Params da nova estratégia (do candidato aprovado)
pbt["ema_fast"] = 8
pbt["ema_slow"] = 20
# Params universais otimizados
pbt["sl_atr_mult"] = 1.8
pbt["cooldown_seconds"] = 60
# Remove params do BOLLINGER que não fazem sentido para EMA_CROSSOVER
for stale in ("bb_period", "bb_std", "rsi_period", "rsi_overbought", "rsi_oversold",
              "adx_period", "adx_threshold", "min_confluence_score"):
    pbt.pop(stale, None)

print(f"\nConfig DEPOIS:")
print(f"  strategy_by_tf[{pair}] = {cfg['strategy_by_tf'][pair]}")
print(f"  params_by_tf[{pair}] = {pbt}")

# 3. Salvar via save_full_config (respeita ALLOWED_WRITERS, lock, _version, atomic)
save_full_config(cfg, updated_by="bruno_wsp_m5_ema_crossover")
print(f"\n✅ Config salvo (v{load_config(force=True).get('_version')})")
print(f"   _updated_by: {load_config(force=True).get('_updated_by')}")
