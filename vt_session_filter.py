#!/usr/bin/env python3
"""
vt_session_filter.py — Smart session filter.

Blocks trading during:
  - First 5 minutes after market open (whipsaw zone)
  - Last 15 minutes before close (unpredictable)
  - Low liquidity periods (lunch hour 12:00-13:30 BRT)
  - High-impact news events (via vt_calendar.py)

Enhances vt_autotrader.py's _is_safe_time_window() with finer control.
"""

from datetime import datetime, timedelta


# BRT time windows to avoid trading (hour, minute)
BLOCKED_WINDOWS = [
    # Opening chaos: first 5 minutes
    {"start": (9, 0), "end": (9, 5), "reason": "market_open_whipsaw", "severity": "hard"},
    # Pre-close volatility: last 15 minutes
    {"start": (16, 45), "end": (17, 0), "reason": "pre_close_volatility", "severity": "hard"},
    # Lunch hour: low liquidity
    {"start": (12, 0), "end": (13, 30), "reason": "lunch_hour_liquidity", "severity": "soft"},
    # Opening auction aftermath
    {"start": (9, 5), "end": (9, 10), "reason": "post_auction_settle", "severity": "soft"},
]

# High-impact news times (manually maintained or from calendar)
# Format: (month, day, hour, minute, reason)
HIGH_IMPACT_EVENTS = [
    # COPOM decisions (typically 14:00 BRT on meeting days)
    # IPCA releases (typically 9:00 BRT)
    # Selic decisions
    # These would be populated from vt_calendar.py or manually
]


def check_session_filter(
    symbol: str = None,
    tf: str = None,
    now: datetime = None,
) -> dict:
    """
    Check if current time is safe for trading.

    Returns:
        {
            "allowed": bool,
            "reason": str,
            "severity": "hard" | "soft" | "none",
            "wait_minutes": int (minutes until window opens),
        }
    """
    try:
        if now is None:
            now = datetime.now()

        h, m = now.hour, now.minute
        current_min = h * 60 + m

        # Check each blocked window
        for window in BLOCKED_WINDOWS:
            start_h, start_m = window["start"]
            end_h, end_m = window["end"]
            start_min = start_h * 60 + start_m
            end_min = end_h * 60 + end_m

            if start_min <= current_min < end_min:
                wait = end_min - current_min
                return {
                    "allowed": False,
                    "reason": window["reason"],
                    "severity": window["severity"],
                    "wait_minutes": wait,
                }

        # Check high-impact news events (within 5 min window)
        for event in HIGH_IMPACT_EVENTS:
            event_month, event_day, event_h, event_m, event_reason = event
            if now.month == event_month and now.day == event_day:
                event_min = event_h * 60 + event_m
                if abs(current_min - event_min) <= 5:
                    return {
                        "allowed": False,
                        "reason": f"news_{event_reason}",
                        "severity": "hard",
                        "wait_minutes": abs(current_min - event_min),
                    }

        # Check via calendar if available
        try:
            from vt_calendar import is_trading_day

            ok, motivo = is_trading_day(now.date())
            if not ok:
                return {
                    "allowed": False,
                    "reason": f"calendar_{motivo}",
                    "severity": "hard",
                    "wait_minutes": 0,
                }
        except Exception:
            pass  # Calendar check optional

        # All clear
        return {
            "allowed": True,
            "reason": "clear",
            "severity": "none",
            "wait_minutes": 0,
        }

    except Exception as e:
        # Graceful degradation: allow on error
        return {
            "allowed": True,
            "reason": f"error_fallback: {e}",
            "severity": "none",
            "wait_minutes": 0,
        }


def is_lunch_hour(now: datetime = None) -> bool:
    """Quick check if current time is lunch hour."""
    if now is None:
        now = datetime.now()
    h, m = now.hour, now.minute
    return 12 <= h < 13 or (h == 13 and m < 30)


def is_opening_window(now: datetime = None) -> bool:
    """Quick check if in opening window (first 5 min)."""
    if now is None:
        now = datetime.now()
    h, m = now.hour, now.minute
    return h == 9 and m < 5


def is_closing_window(now: datetime = None) -> bool:
    """Quick check if in closing window (last 15 min)."""
    if now is None:
        now = datetime.now()
    h, m = now.hour, now.minute
    return (h == 16 and m >= 45) or h >= 17


def get_next_safe_window(now: datetime = None) -> tuple:
    """Get the next safe trading window start time."""
    if now is None:
        now = datetime.now()

    h, m = now.hour, now.minute
    current_min = h * 60 + m

    # Sort windows by start time
    sorted_windows = sorted(BLOCKED_WINDOWS, key=lambda w: w["start"][0] * 60 + w["start"][1])

    for window in sorted_windows:
        start_h, start_m = window["start"]
        end_h, end_m = window["end"]
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m

        # If we're before this window starts, it's safe until then
        if current_min < start_min:
            return (start_h, start_m), f"safe_until_{start_h}:{start_m:02d}"

        # If we're in this window, next safe time is when it ends
        if start_min <= current_min < end_min:
            return (end_h, end_m), f"blocked_until_{end_h}:{end_m:02d}"

    # After all windows - safe until tomorrow
    return (9, 0), "safe_until_tomorrow"


def format_session_result(result: dict) -> str:
    """Format session filter result for logging."""
    if result["allowed"]:
        return "[SESSION] CLEAR"
    severity = result["severity"].upper()
    reason = result["reason"]
    wait = result["wait_minutes"]
    return f"[SESSION] BLOCKED ({severity}) | {reason} | wait={wait}min"
