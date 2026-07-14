# Backtest vs Real Trading Gap Analysis — 2026-06-22

## Summary
- **Backtest**: 115 trades, 69% WR, +R$3,974
- **Real Bot**: 26 trades, low WR, -R$716
- **Gap**: 89 missing trades (~77% filtered out)

## Root Causes Identified

### 1. CONFIG VERSION MISMATCH (PRIMARY CAUSE)
The v783 config was generated at 17:21 (5:21 PM), but market closes at 16:45 (4:45 PM).
This means the bot ran the ENTIRE trading day with an OLDER, more restrictive config.
The AGI backtested against v783 for the whole day, but the bot never used v783 during market hours.

Evidence: Log shows "máximo diário atingido (5/1)" for BIT — the v783 config has max_daily_trades=999.
An older config with max_daily_trades=1 was in effect during trading hours.

**Impact**: Massive — most of the 89 missing trades.

### 2. PER-SYMBOL DAILY TRADE LIMITS (BLOCKING)
The real bot accumulates daily trade counts PER SYMBOL across ALL timeframes.
If BIT takes 3 trades on M5, 2 on M15, 1 on H1 = 6 total for BIT symbol.
When the per-symbol limit (e.g., 3) is hit, ALL timeframes for that symbol are blocked.

The backtest has NO such limits — it takes every signal across all timeframes.

**Impact**: After early trades, entire symbols get blocked for the rest of the day.

### 3. COOLDOWN MISMATCH (SUPPORTING)
- Backtest: 5-bar cooldown (25 minutes on M5)
- Real bot: 180-300s cooldown (3-5 minutes) = ~1 bar on M5

The real bot cooldown is SHORTER, which means it should allow MORE trades, not fewer.
But combined with per-symbol limits, the bot takes a few quick trades then hits the daily cap.

### 4. WARMUP/WINDOW FILTERS
- Real bot: 15-minute warmup + 15-minute winddown (loses 30 min of 7.5h session = 6.7%)
- Backtest: No such filter

**Impact**: Minor (~8 trades out of 115, not the main issue).

### 5. CONSECUTIVE LOSS / HALT (DISABLED)
max_consecutive_losses = 999 (effectively disabled in v783).
Not a factor with current config.

### 6. POSITION-LEVEL FILTERS (REAL BOT ONLY)
- `_defenses_ok()`: Checks MT5 for existing positions (prevents duplicates)
- Signal deduplication (same bar_ts + direction)
- Per-direction cooldown (adds 3s check after symbol cooldown)

**Impact**: Minor — these are safety guards, not heavy filters.

## Recommendations

### A. Add Realistic Limits to Backtest
1. Track per-symbol daily trade count
2. Apply max_daily_trades limits (from config)
3. Add warmup/winddown window (15 min each)
4. Use actual bot cooldown values from config

### B. Relax Bot Limits (Make More Like Backtest)
1. Increase per-TF max_daily_trades (bot accumulates across TFs, so per-TF limits are redundant)
2. The per-symbol limit (base max_daily_trades) is the real gate — keep it reasonable

### C. Fix Config Deployment Timing
1. Deploy AGI configs BEFORE market open (not after close)
2. Or: Make bot hot-reload configs immediately (already does this via load_config())
3. The real issue is the AGI runs at 17h (after close), so optimized config only takes effect next day
