"""
Guinevere 2.0 -- calendar checker (Commission 018 build, 31 Jul 2026).
Adds the NEW directional calendar intelligence Guinevere 1.0 lacked: month-end,
quarter-end and options-expiry detection, surfaced to the signal engine as Rank-4
(RANK_MARKET_FLOW) context. The per-system calendar_<x>.py HARD BLOCKS (FOMC/BoJ/BoE/NFP
windows) are UNCHANGED and remain authoritative -- this only adds soft directional context.
ALL TIMES UTC.
"""

import calendar as _cal
from datetime import datetime, timezone, timedelta


def _last_trading_day(year, month):
    """Last weekday (Mon-Fri) of the month, UTC."""
    last = _cal.monthrange(year, month)[1]
    d = datetime(year, month, last, tzinfo=timezone.utc).date()
    while d.weekday() >= 5:             # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _third_friday(year, month):
    d = datetime(year, month, 1, tzinfo=timezone.utc).date()
    fridays = [d.replace(day=day) for day in range(1, _cal.monthrange(year, month)[1] + 1)
               if datetime(year, month, day).weekday() == 4]
    return fridays[2] if len(fridays) >= 3 else None


def get_calendar_events(now_utc=None):
    """Return a list of active Rank-4 calendar events for today (UTC)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_utc.date()
    events = []

    ltd = _last_trading_day(today.year, today.month)
    if today == ltd:
        if today.month in (3, 6, 9, 12):
            events.append({"event_type": "QUARTER_END",
                           "headline": "Last trading day of the quarter -- rebalancing / profit-taking risk HIGH"})
        else:
            events.append({"event_type": "MONTH_END",
                           "headline": "Last trading day of %s -- profit-taking risk HIGH" % today.strftime("%B")})

    tf = _third_friday(today.year, today.month)
    if tf and today == tf:
        events.append({"event_type": "OPTIONS_EXPIRY",
                       "headline": "Monthly options expiry (3rd Friday) -- pinning / elevated intraday vol"})
    return events


def calendar_note(now_utc=None):
    """One-line human summary of today's calendar context (for the advisory / dashboard)."""
    ev = get_calendar_events(now_utc)
    if not ev:
        return "no major calendar events today"
    return "; ".join(e["headline"] for e in ev)
