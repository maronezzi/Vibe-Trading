"""
Otimização de meio-dia — ajusta parâmetros de indicadores sem mudar estratégia.
Regras:
  - Não troca estratégia
  - Não muda sl_atr_mult
  - Ajusta no máximo 2 parâmetros por ativo
"""
import sys
import json
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from vt_config_loader import load_config, save_params  # noqa: E402

cfg = load_config(force=True)
print("=" * 70)
print("OTIMIZAÇÃO DE PARÂMETROS — MEIO-DIA")
print("=" * 70)
print(f"Config v{cfg.get('_version')} carregado.")

# Diagnóstico: trades do dia até 12h
import sqlite3
conn = sqlite3.connect(str(PROJECT_ROOT / "vt_trades.db"))
cur = conn.cursor()
cur.execute("""
    SELECT symbol, strategy, COUNT(*) as trades,
           ROUND(SUM(net_pnl), 2) as pnl,
           ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) as wr
    FROM trades
    WHERE date(entry_time) = date('now')
      AND strftime('%H', entry_time) < '12'
    GROUP BY symbol, strategy
    ORDER BY symbol
""")
print("\n=== Trades hoje (até 12h) ===")
for row in cur.fetchall():
    print(f"  {row[0]:8s} {row[1]:18s} trades={row[2]:3d} pnl={row[3]:+10.2f} wr={row[4]:5.1f}%")
conn.close()

# Ajustes — apenas 1 ativo precisa de mudança (BIT)
# BIT WR 33% hoje, M15 EMA_PULLBACK perdeu -R$ 1.4k.
# M5 VWAP (única per-TF VWAP ativa) deu +R$ 398 WR 100% — bom.
# Apertar VWAP global: vwap_period maior (mais suavizado) e threshold mais seletivo.
# Máximo 2 params, sem mexer em sl_atr_mult.
print("\n=== Ajustes aplicados ===")

changes_bit = {
    "vwap_period": 40,         # de 30 → 40 (mais suavizado, menos ruído)
    "vwap_buy_threshold": 1.015,  # de 1.01 → 1.015 (entrada mais seletiva)
}

# Não mexer em vwap_sell_threshold (assimetria OK, e limite de 2 params).
# Manter coerência: ajustando só buy mantém o sinal de venda no nível atual.
# Mas para manter simetria razoável, vamos espelhar a mudança no sell:
# 1.015 - 1.0 = 0.015; aplicar 1.0 - 0.015 = 0.985 no sell.
# Hmm, isso ainda é 1 param extra (sell). Vou manter só buy + period = 2 params.
# A assimetria buy_threshold 1.015 (era 1.01) e sell_threshold 0.99 (mantido)
# significa que compras exigem +1.5% acima da VWAP, vendas só -1% abaixo.
# Isso é levemente conservador para compras. OK como experimento conservador.

print(f"BIT:")
print(f"  vwap_period: {cfg['bit'].get('vwap_period')} → {changes_bit['vwap_period']}")
print(f"  vwap_buy_threshold: {cfg['bit'].get('vwap_buy_threshold')} → {changes_bit['vwap_buy_threshold']}")
print(f"  (sl_atr_mult mantido em {cfg['bit'].get('sl_atr_mult')})")

ok = save_params("bit", changes_bit, updated_by="meio_dia")
print(f"\n  save_params('bit', ...) → {ok}")

# Reload para confirmar
cfg2 = load_config(force=True)
print(f"  Versão após save: v{cfg2.get('_version')} (by {cfg2.get('_updated_by')})")
print(f"  vwap_period novo: {cfg2['bit']['vwap_period']}")
print(f"  vwap_buy_threshold novo: {cfg2['bit']['vwap_buy_threshold']}")

# Resumo
print("\n=== Resumo ===")
print("Mudanças aplicadas: 1 ativo, 2 parâmetros")
print("  - BIT: vwap_period 30→40, vwap_buy_threshold 1.01→1.015")
print("Nenhuma estratégia foi alterada.")
print("Nenhum sl_atr_mult foi alterado.")
