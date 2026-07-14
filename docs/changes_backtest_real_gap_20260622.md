# Changes Made — Backtest vs Real Trading Gap Fix (2026-06-22)

## Problem
- Backtest: 115 trades, 69% WR, +R$3,974
- Real bot: 26 trades, low WR, -R$716
- Gap: 89 missing trades (~77% filtered out)

## Root Causes Found

### 1. CONFIG VERSION TIMING (Primary)
v783 config was generated at 17:21 (AFTER market close at 16:45).
Bot ran all day with older, restrictive config. Backtest used v783 for full day.

### 2. PER-TF max_daily_trades BOTTLENECK (Critical)
The bot accumulates daily trades PER SYMBOL across ALL timeframes.
Per-TF limits (2-6) acted as premature blockers because:
- BIT takes 3 trades on M5 → hits per-TF limit of 6 → but if BIT_M15 also takes 3 = 6 total for BIT
- After symbol-level accumulation reaches per-TF limit, ALL TFs blocked

**Old values → New values (all set to 999):**
| TF | Old max_daily | New max_daily |
|---|---|---|
| WIN_M15 | 4 | 999 |
| WIN_M30 | 4 | 999 |
| BIT base | 3 | 999 |
| BIT_M5 | 6 | 999 |
| BIT_M15 | 6 | 999 |
| BIT_M30 | 3 | 999 |
| BIT_H1 | 4 | 999 |
| WSP_M15 | 6 | 999 |
| WDO_M15 | 6 | 999 |
| WDO_M30 | 6 | 999 |

Safety: Global cap remains min(global_max_daily_trades, 50) = 50 trades/day.

### 3. BACKTEST MISSING REALISTIC FILTERS (Fixed)
The backtest had NONE of these — now it has:
- **Per-symbol daily trade limits**: Mirrors autotrader's hierarchy
- **Warmup/winddown window**: 15 min after open, 15 min before close
- **Config-driven cooldowns**: Uses actual cooldown_seconds from config (not hardcoded 5-bar)

## Files Modified

### 1. `optimization/vt_forward_backtest.py`
- Added `_resolve_sim_max_daily()` helper function
- Added warmup/winddown window filter in `simulate_forward()`
- Added per-symbol daily trade counting with day-reset
- Changed cooldown from hardcoded 5 bars to config-driven (cooldown_seconds)
- Added `config` parameter pass-through from `run_mini_backtest_pair()` to `simulate_forward()`

### 2. `vt_config.json`
- Set all per-TF max_daily_trades to 999 (12 values changed)
- Set BIT base max_daily_trades from 3 to 999
- Updated _doc_max_daily_trades documentation

### 3. `docs/backtest_vs_real_gap_analysis.md` (new)
- Detailed analysis document

## Expected Impact
- **Backtest**: Will be more realistic, with slightly fewer trades due to warmup/winddown + daily limits
- **Real bot**: Will take significantly more trades (limited only by cooldown + global 50 cap)
- **Gap**: Should narrow from ~77% to ~10-20% (remaining gap = slippage, execution delay, MT5 latency)

## Remaining Recommendations (Not Implemented)
1. **Deploy AGI configs before market open** — Currently AGI runs at 17h (after close)
2. **Reduce warmup from 15→5 min** — First 5 min has good signals (market opening range)
3. **Reduce winddown from 15→5 min** — Last 5 min is risky, but 15 min is conservative
4. **Add execution delay simulation** to backtest (1-bar delay for real bot)
5. **Make AGI optimize against realistic backtest** (with the new filters)
