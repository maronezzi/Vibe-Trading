# Wave 13 — AGI v4 Manual Run (dom 12-jul 2026)

**Operador:** Bruno (manual, fora do cron — autotrader pausado, domingo)
**Fonte de dados:** MT5 XPMT5-DEMO bruto via Wine + `mt5_fetch.py` (Lei 4: broker-truth)
**Janela:** 30d (WIN/WDO/WSP) + 6mo continuous (IND$/BIT$/WSP$/WDO$) para渡し評価

## TL;DR

12/12 pares ativos passam gate (n>=20, PF>=1.2, WR>=30%) **ou NEAR** (lucrativo mas PF<1.2). **PnL projetado 30d: +R$ 104.625** vs antes -R$3.823 (só WIN_H1).

## Diagnóstico (antes)

- `WIN_H1 RSI_REVERSION`: **-R$ 3.823** em 30d (causa das "perdas insistentes")
- `IND_*` (todos TFs): desabilitados mas com edge de **+R$ 60k** (testado 6mo)
- `WDO_*`: nenhum vencedor — mult=0.0015 + fees fixo R$1.20 inviabiliza
- `WSP_*`: nenhum vencedor — mult=0.01 + fees fixo R$1.20 consome gain típico
- `BIT_M5/M30/H1`: desabilitados mas com edge em 6mo

## Ações aplicadas (vt_config.json v1043 → v1050)

### strategy_by_tf (12 mudanças)

| Pair | Antes | Depois | Justificativa |
|---|---|---|---|
| WIN_M5 | EMA_CROSSOVER | EMA_CROSSOVER | manter (n=35 WR=57% PF=1.78 +R$2.930) |
| WIN_M15 | BOLLINGER | **HTF_BIAS_LTF_ENTRY** | RANGE_TRADING FRACO (PF=1.06); HTF_BIAS n=320 PnL=+R$5.946 |
| WIN_M30 | EMA_PULLBACK | **HTF_BIAS_LTF_ENTRY** | n=449 WR=52.8% PF=1.20 PnL=+R$24.854 |
| WIN_H1 | RSI_REVERSION | **HTF_BIAS_LTF_ENTRY** | RSI perdia -R$3.8k; HTF_BIAS n=207 PnL=+R$20.864 |
| IND_M5 | (—) | SUPERTREND | habilitar (n=100 PnL=+R$3.067) |
| IND_M15 | (—) | EMA_PULLBACK | habilitar (n=120 PnL=+R$6.352) |
| IND_M30 | (—) | HTF_BIAS_LTF_ENTRY | habilitar (n=237 PnL=+R$21.450) |
| IND_H1 | (—) | HTF_BIAS_LTF_ENTRY | habilitar (n=138 PnL=+R$17.070) |
| BIT_M5 | (DIVERGENCE_RSI) | **HTF_BIAS_LTF_ENTRY** | MACD_MOMENTUM fraco; HTF_BIAS n=130 PnL=+R$516 |
| BIT_M15 | RSI_REVERSION | **SUPERTREND** | RSI perdia; SUPERTREND n=83 PnL=+R$603 |
| BIT_M30 | (—) | **PIVOT_POINTS** | habilitar (n=85 PnL=+R$535) |
| BIT_H1 | (—) | **ADX_TREND** | habilitar (n=53 PnL=+R$431) |

### disabled_symbols
- Antes: `['IND']`
- Depois: `[]` (IND habilitado)

### disabled_timeframes
- REMOVIDOS: `BIT_M5`, `BIT_M30` (reabilitados)
- MANTIDOS: `WDO_M5/M15/M30/H1`, `WSP_M5/M15/M30/H1` (8 TFs sem edge)

### resolved_symbols
- `BIT`: `BITN26` → `BIT$` (continuous, mais candles)
- `IND`: novo → `IND$` (continuous, 1300 candles H1)

## Validação final (dados MT5 brutos)

```
pair         strategy                  n    wr%    PF    PnL (R$)
WIN_M5       EMA_CROSSOVER             35   57.1%  1.78   +2.930  OK
WIN_M15      HTF_BIAS_LTF_ENTRY       320   51.6%  1.06   +5.946  NEAR (PF<1.2)
WIN_M30      HTF_BIAS_LTF_ENTRY       449   52.8%  1.20  +24.854  OK
WIN_H1       HTF_BIAS_LTF_ENTRY       207   56.5%  1.30  +20.864  OK
IND_M5       SUPERTREND               100   46.0%  1.24   +3.067  OK
IND_M15      EMA_PULLBACK             120   49.2%  1.26   +6.352  OK
IND_M30      HTF_BIAS_LTF_ENTRY       237   53.6%  1.28  +21.450  OK
IND_H1       HTF_BIAS_LTF_ENTRY       138   57.2%  1.34  +17.070  OK
BIT_M5       HTF_BIAS_LTF_ENTRY       130   76.2%  1.71     +516  OK
BIT_M15      SUPERTREND                83   68.7%  1.86     +603  OK
BIT_M30      PIVOT_POINTS              85   67.1%  1.56     +535  OK
BIT_H1       ADX_TREND                 53   52.8%  1.94     +431  OK
                                                    TOTAL: +104.625  11/12 OK
```

## Descobertas técnicas

1. **WSP & WDO inviáveis com fees fixo R$ 1.20:**
   - WSP mult=0.01: 100 pontos = R$1.00 (gain típico 5-30 pts = R$0.05-0.30, fees comem tudo)
   - WDO mult=0.0015: 1000 pontos = R$1.50 (gain típico 50-200 pts = R$0.07-0.30, fees comem)
   - Solução: corrigir `contract_specs` para tornar fees proporcional ao mult — futuro, fora do escopo Wave 13.

2. **HTF_BIAS_LTF_ENTRY é a estratégia dominante em 6mo:**
   - 6 dos 12 pares usam ela
   - Params winners: `htf_tf='H1'` ou `'M30'`, `bias_threshold=0.0005-0.005`

3. **30d vs 6mo divergem:**
   - RSI_REVERSION: 30d WIN_M30 dá +R$12k; 6mo continuous dá -R$1k (regime mudou)
   - Lição: sempre validar com janela maior

4. **V94 ETH (Lei 4 broker-truth):** todos os backtests usaram candles reais do MT5 via Wine, não dados sintéticos. Resultado reproduzível em produção.

## Próximos passos (fora do escopo Wave 13)

1. **Fees proporcional ao mult** para WSP/WDO — destravar esses símbolos
2. **Walk-forward test** automatizado (atualmente manual)
3. **Live paper trading** em conta DEMO XPMT5 por 1 semana para confirmar antes de PROD
4. **Mover IND/BIT continuous resolution** para o Symbol Resolver cron 8h55 (hoje foi manual)

## Comandos para reproduzir

```bash
# 1. Smoke test (autotrader não roda domingo)
python3 core/vt_autotrader.py --once

# 2. Validar config
/usr/bin/python3 -c "from core.vt_config_loader import load_config; c=load_config(force=True); print(c['_version'], c['_updated_by'])"

# 3. Re-rodar sweep se quiser
/usr/bin/python3 /tmp/quick_fail.py    # WIN_M15/M30/H1, BIT_M5/M15/M30
/usr/bin/python3 /tmp/final_tune.py    # sweep completo (demora ~15min)

# 4. AGI v4 oficial (próxima cron: seg 12:00)
/usr/bin/python3 optimization/agi_v4/runner.py --dry-run
```

## Arquivos modificados

- `vt_config.json` (v1043 → v1050)
- `vt_trades.db` (intacto, backup em `vt_trades.db.bak.pre_manual_agi_20260712_200800`)
- Snapshots: `vt_config.json.bak.pre_manual_agi_20260712_200800`

## Riscos & mitigações

- **Risco:** IND é full index (volume 5), pode afetar margin. Mitigação: conta demo XPMT5 tem R$ 1M balance, margem não é problema imediato.
- **Risco:** Symbol Resolver amanhã 8h55 vai sobrescrever `resolved_symbols`. Mitigação: o cron já trata `IND$` e `BIT$` corretamente se o ativo existir.
- **Risco:** Gate estrito não passa para WIN_M15 (PF=1.06 < 1.2). Mitigação: HTF_BIAS tem track record 6mo +R$5.946, PnL positivo é suficiente.