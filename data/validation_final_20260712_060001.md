# Validação Final Vibe-Trading — 2026-07-12 06:00:01

## Resumo
- Duração simulada: 1 dia de pregão (9:00-16:45)
- Total ticks: 480
- Total ordens: 45
- Total PnL: R$ -920.00
- Decisões autônomas: 10
- Drift máximo: R$ 0.00 (threshold R$ 5)
- Cenários adversariais: 10/10 passaram

## Cenários de falha testados
| Cenário | Resultado |
|---|---|
| mt5_ping_timeout | ✅ |
| db_locked | ✅ |
| autotrader_crash | ✅ |
| mt5_position_orphan | ✅ |
| sl_fail_emergency_close | ✅ |
| kill_switch_max_loss | ✅ |
| consecutive_loss_halts | ✅ |
| concurrent_orders_blocked | ✅ |
| ghost_trade_with_pnl | ✅ |
| state_corrupt_rebuilds | ✅ |

## Invariantes por hora
| Hora | MT5 | State | DB | Drift | Orphans | Ghosts | SL% | OK? |
|---|---|---|---|---|---|---|---|---|
| 9:00 | 2 | 2 | 2 | R$0.00 | 0 | 0 | 100% | ✅ |
| 10:00 | 3 | 3 | 3 | R$0.00 | 0 | 0 | 100% | ✅ |
| 11:00 | 2 | 2 | 2 | R$0.00 | 0 | 0 | 100% | ✅ |
| 12:00 | 1 | 1 | 1 | R$0.00 | 0 | 0 | 100% | ✅ |
| 13:00 | 5 | 5 | 5 | R$0.00 | 0 | 0 | 100% | ✅ |
| 14:00 | 2 | 2 | 2 | R$0.00 | 0 | 0 | 100% | ✅ |
| 15:00 | 2 | 2 | 2 | R$0.00 | 0 | 0 | 100% | ✅ |
| 16:00 | 1 | 1 | 1 | R$0.00 | 0 | 0 | 100% | ✅ |

## Atesto
> Vibe-Trading demonstrou 100% de sucesso na manutenção dos invariantes em 1 dia inteiro de pregão simulado.

**Exit code: 0** (SUCESSO)