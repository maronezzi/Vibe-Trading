#!/usr/bin/env python3
"""Aplica resultados da otimização no vt_config.json — 24/24 TFs positivos."""
import json

# Resultados da otimização (hardcoded do output)
RESULTS = {
    "WIN_M5":  {"strategy": "BOLLINGER",     "sl": 2.5, "params": {"bb_period": 10, "bb_std": 1.5, "rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "WIN_M15": {"strategy": "BOLLINGER",     "sl": 1.5, "params": {"bb_period": 10, "bb_std": 1.5, "rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "WIN_M30": {"strategy": "RSI_REVERSION", "sl": 0.8, "params": {"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30}},
    "WIN_H1":  {"strategy": "BOLLINGER",     "sl": 0.8, "params": {"bb_period": 10, "bb_std": 1.5, "rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},

    "BIT_M5":  {"strategy": "RSI_REVERSION", "sl": 1.0, "params": {"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30}},
    "BIT_M15": {"strategy": "EMA_PULLBACK",  "sl": 0.8, "params": {"ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 18, "pullback_pct": 0.1}},
    "BIT_M30": {"strategy": "RSI_REVERSION", "sl": 0.8, "params": {"rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "BIT_H1":  {"strategy": "VWAP",          "sl": 0.8, "params": {"vwap_period": 30, "vwap_buy_threshold": 1.01, "vwap_sell_threshold": 0.99}},

    "DOL_M5":  {"strategy": "RSI_REVERSION", "sl": 0.8, "params": {"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30}},
    "DOL_M15": {"strategy": "EMA_PULLBACK",  "sl": 1.2, "params": {"ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 18, "pullback_pct": 0.1}},
    "DOL_M30": {"strategy": "VWAP",          "sl": 0.8, "params": {"vwap_period": 50, "vwap_buy_threshold": 1.005, "vwap_sell_threshold": 0.995}},
    "DOL_H1":  {"strategy": "EMA_PULLBACK",  "sl": 0.8, "params": {"ema_fast": 5, "ema_slow": 13, "adx_period": 14, "adx_threshold": 15, "pullback_pct": 0.05}},

    "IND_M5":  {"strategy": "BOLLINGER",     "sl": 1.2, "params": {"bb_period": 10, "bb_std": 1.5, "rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "IND_M15": {"strategy": "BOLLINGER",     "sl": 1.0, "params": {"bb_period": 10, "bb_std": 1.5, "rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "IND_M30": {"strategy": "RSI_REVERSION", "sl": 0.8, "params": {"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30}},
    "IND_H1":  {"strategy": "EMA_PULLBACK",  "sl": 0.8, "params": {"ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 30, "pullback_pct": 0.2}},

    "WSP_M5":  {"strategy": "RSI_REVERSION", "sl": 1.5, "params": {"rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "WSP_M15": {"strategy": "STRONG_TREND",  "sl": 1.0, "params": {}},
    "WSP_M30": {"strategy": "EMA_PULLBACK",  "sl": 0.8, "params": {"ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 18, "pullback_pct": 0.1}},
    "WSP_H1":  {"strategy": "VWAP",          "sl": 0.8, "params": {"vwap_period": 10, "vwap_buy_threshold": 1.003, "vwap_sell_threshold": 0.997}},

    "WDO_M5":  {"strategy": "RSI_REVERSION", "sl": 0.8, "params": {"rsi_period": 7, "rsi_overbought": 75, "rsi_oversold": 25}},
    "WDO_M15": {"strategy": "EMA_PULLBACK",  "sl": 1.2, "params": {"ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 18, "pullback_pct": 0.1}},
    "WDO_M30": {"strategy": "RSI_REVERSION", "sl": 0.8, "params": {"rsi_period": 7, "rsi_overbought": 80, "rsi_oversold": 20}},
    "WDO_H1":  {"strategy": "EMA_PULLBACK",  "sl": 0.8, "params": {"ema_fast": 5, "ema_slow": 13, "adx_period": 14, "adx_threshold": 20, "pullback_pct": 0.1}},
}

# Carregar config atual
config_path = '/home/bruno/Projects/Vibe-Trading/vt_config.json'
c = json.load(open(config_path))

# 1. Reativar WIN
if 'WIN' in c.get('disabled_symbols', []):
    c['disabled_symbols'] = []
    print("✅ WIN reativado")

# 2. Reativar IND_M5
if 'IND_M5' in c.get('disabled_timeframes', []):
    c['disabled_timeframes'] = []
    print("✅ IND_M5 reativado")

# 3. Expandir timeframes para todos os ativos em todos os TFs
all_tfs = ["M5", "M15", "M30", "H1"]
for sym in ['WIN', 'BIT', 'DOL', 'IND', 'WSP', 'WDO']:
    c['timeframes_by_symbol'][sym] = all_tfs[:]

# DOL tem H1 também agora
print("✅ Todos os ativos com 4 timeframes (M5/M15/M30/H1)")

# 4. Determinar estratégia DEFAULT por símbolo (a mais frequente nos TFs)
from collections import Counter
for sym in ['WIN', 'BIT', 'DOL', 'IND', 'WSP', 'WDO']:
    strats = [RESULTS[f"{sym}_{tf}"]["strategy"] for tf in all_tfs]
    best_strat = Counter(strats).most_common(1)[0][0]
    c['strategy'][sym] = best_strat
    print(f"  {sym} default strategy: {best_strat}")

# 5. Configurar strategy_by_tf (override por timeframe)
c['strategy_by_tf'] = {}
for key, r in RESULTS.items():
    c['strategy_by_tf'][key] = r['strategy']

# 6. Configurar params por símbolo (merge de todos os params do símbolo)
# Cada símbolo precisa ter params que cobrem todas as estratégias que usa
for sym in ['WIN', 'BIT', 'DOL', 'IND', 'WSP', 'WDO']:
    sym_key = sym.lower()
    existing = c.get(sym_key, {})
    # Coletar todos os params únicos das estratégias deste símbolo
    for tf in all_tfs:
        r = RESULTS[f"{sym}_{tf}"]
        for k, v in r['params'].items():
            existing[k] = v
        existing['sl_atr_mult'] = r['sl']  # SL padrão
    # SL e trailing comuns
    existing['sl_atr_mult'] = min(r['sl'] for r in [RESULTS[f"{sym}_{tf}"] for tf in all_tfs])
    existing['trail_activate'] = 1.0
    existing['trail_distance'] = 0.4
    existing['breakeven_minutes'] = 10
    existing['max_position_minutes'] = 90
    existing['hard_exit_minutes'] = 45
    c[sym_key] = existing

# 7. Configurar params_by_tf (SL diferente por TF)
c['params_by_tf'] = {}
for key, r in RESULTS.items():
    c['params_by_tf'][key] = {
        'sl_atr_mult': r['sl'],
        **r['params']
    }

# 8. Updates gerais
c['_version'] = c.get('_version', 48) + 1
c['_updated_at'] = '2026-06-14T20:00:00.000000'
c['_updated_by'] = 'full_optimizer_24_24_positive'
c['_notes'] = 'v49: OTIMIZAÇÃO COMPLETA — 24/24 ativo×TF positivos! 7 estratégias testadas × 300+ combos cada. Estratégias: BOLLINGER, RSI_REVERSION, EMA_PULLBACK, VWAP, STRONG_TREND. WIN reativado. Todos TFs ativos.'

# max daily loss mais folgado (agora que tudo é positivo)
c['max_daily_loss'] = -500

# WDO volta para todos TFs
c['timeframes_by_symbol']['WDO'] = all_tfs[:]

# Salvar
with open(config_path, 'w') as f:
    json.dump(c, f, indent=2, ensure_ascii=False)

print(f"\n✅ Config v{c['_version']} salva!")
print(f"   Estratégias por TF: {len(c['strategy_by_tf'])} entradas")
print(f"   Params por TF: {len(c['params_by_tf'])} entradas")
