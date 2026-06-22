import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vt_hermes_helper import hermes_send

msg = """🔧 *Otimização Meio-Dia v339*

📊 Diagnóstico (até 12h):
• BIT M15 EMA_PULLBACK: 3 trades, WR 0%, -R$ 1.823 ⚠️
• BIT M5 VWAP: 1 trade, WR 100%, +R$ 399 ✅
• DOL M5/M15: 4 trades, WR 50%, ~0
• IND M30: 2 trades, WR 50%, -R$ 147
• WSP M5: 3 trades, WR 100%, +R$ 22
• WIN: 0 trades (amostra insuficiente)

🎯 Ajuste aplicado (1 ativo, 2 params):
• BIT_H1 (VWAP ativo):
  - vwap_period: 30 → 40 (mais suavizado)
  - vwap_buy_threshold: 1.01 → 1.015 (entrada mais seletiva)

🚫 NÃO alterado:
• Estratégias (BIT continua VWAP no H1)
• sl_atr_mult (mantido em 0.8)
• vwap_sell_threshold (mantido em 0.99)

📌 Ativos sem mudança:
• WIN: 0 trades = amostra < 5 (insuficiente)
• DOL: WR 50% (acima do limiar 40%)
• IND: 2 trades (amostra < 5)
• WSP: WR 100% (sem necessidade)

Config salva: vt_config.json v339
By: meio_dia"""

ok = hermes_send("telegram:-1004284773048", msg, timeout=15)
print(f"Telegram send → {ok}")
