"""
NikkeiTrader AI -- calendar_nikkei.py  (Guinevere)
Japan calendar with hard blocks around BoJ decisions + Japanese holidays.
Hard block: 30 min either side of BoJ decisions; full-day on Japanese holidays.
Soft context: Fed decisions (overnight US -> Tokyo gap) passed to Arthur.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("NikkeiTrader.Guinevere")

HARD_BLOCK_MINUTES = 30

# ── Hard-block calendar (BoJ decisions + Japanese holidays) ───────────────────
# BoJ dates load below (currently empty -- see note). Fed/NFP kept as US soft
# context (overnight Wall Street -> Tokyo open gap). Time-conversion helpers below
# are retained for the US soft events.

# BoJ Monetary Policy Meetings (2026, confirmed by Nick 21 Jul 2026). BoJ has no
# fixed announcement time and the decision can land anywhere in the short Tokyo
# session (00:00-06:30 UTC), so a 30-min window is unsafe -- we hard-block the
# WHOLE of both meeting days (anticipation day + decision day). Handled below in
# check_calendar alongside the holidays (full-day block), not as timed events.
BOJ_MEETING_DAYS = {
    "2026-07-30", "2026-07-31",   # July MPM
    "2026-09-17", "2026-09-18",   # September MPM
    "2026-10-29", "2026-10-30",   # October MPM
    "2026-12-17", "2026-12-18",   # December MPM
}
# (BoJ timed-event lists + the 30-min timed window they fed were REMOVED 25 Jul 2026 --
#  dormant dead code. The full-day BOJ_MEETING_DAYS block above is the active protection.)

# Fed decisions drive the overnight Wall Street move -> Tokyo open gap (soft context).
FED_DATES_2026 = [
    "2026-01-29", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-07-30", "2026-09-17", "2026-11-05", "2026-12-10",
]

# Japanese national holidays -- Tokyo Stock Exchange CLOSED (full-day hard block).
# Remaining 2026 (from 21 Jul): brief-confirmed 11 Aug + 21-23 Sep, plus the standard
# autumn holidays. The Capital.com feed will also be flat on these days.
JAPAN_HOLIDAYS = {
    "2026-08-11",                               # Mountain Day
    "2026-09-21", "2026-09-22", "2026-09-23",   # Respect-for-Aged / bridge / Autumnal Equinox
    "2026-10-12",                               # Sports Day
    "2026-11-03", "2026-11-23",                 # Culture Day / Labour Thanksgiving
}


def _uk_noon_to_utc(date_str: str) -> datetime:
    """Convert a date string to UTC datetime for 12:00 UK time (BST/GMT aware)."""
    try:
        import pytz
        uk_tz = pytz.timezone("Europe/London")
        naive = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, minute=0)
        uk_dt = uk_tz.localize(naive)
        return uk_dt.astimezone(timezone.utc)
    except ImportError:
        # Approximate: use 12:00 UTC (1h off in summer but acceptable)
        base = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
        return base


def _us_1330_uk_to_utc(date_str: str) -> datetime:
    """13:30 UK time to UTC (NFP, US CPI etc)."""
    try:
        import pytz
        uk_tz = pytz.timezone("Europe/London")
        naive = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=13, minute=30)
        uk_dt = uk_tz.localize(naive)
        return uk_dt.astimezone(timezone.utc)
    except ImportError:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=13, minute=30, tzinfo=timezone.utc)


def _uk_0700_to_utc(date_str: str) -> datetime:
    """07:00 UK time to UTC (CPI, GDP, employment)."""
    try:
        import pytz
        uk_tz = pytz.timezone("Europe/London")
        naive = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=7, minute=0)
        uk_dt = uk_tz.localize(naive)
        return uk_dt.astimezone(timezone.utc)
    except ImportError:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=7, tzinfo=timezone.utc)


def _build_boe_events(year_dates: list) -> list:
    events = []
    for d in year_dates:
        try:
            events.append({
                "name":      "Bank of Japan Policy Decision",
                "datetime":  _uk_noon_to_utc(d),
                "impact":    "HARD_BLOCK",
                "source":    "BoJ",
                "date_str":  d,
            })
        except Exception:
            pass
    return events


def _get_fed_events() -> list:
    events = []
    for d in FED_DATES_2026:
        try:
            events.append({
                "name":      "Federal Reserve Decision",
                "datetime":  _us_1330_uk_to_utc(d),
                "impact":    "SOFT",
                "source":    "Fed",
                "date_str":  d,
            })
        except Exception:
            pass
    return events


def _nfp_dates_2026() -> list:
    """First Friday of each month in 2026."""
    dates = []
    for month in range(1, 13):
        d = datetime(2026, month, 1)
        while d.weekday() != 4:
            d += timedelta(days=1)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates


def _get_nfp_events() -> list:
    events = []
    for d in _nfp_dates_2026():
        events.append({
            "name":      "US Non-Farm Payrolls",
            "datetime":  _us_1330_uk_to_utc(d),
            "impact":    "SOFT",
            "source":    "US",
            "date_str":  d,
        })
    return events


# ── Public API ────────────────────────────────────────────────────────────────

def check_calendar(now_utc: Optional[datetime] = None) -> dict:
    """
    Main calendar check. Returns:
      hard_block      : bool -- True means DO NOT TRADE
      block_reason    : str  -- why if hard_block
      upcoming_events : list -- events in next 24h
      calendar_summary: str  -- plain English for Arthur
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Full-day hard blocks: Japanese national holidays (exchange closed) and BoJ
    # policy-meeting days (unpredictable announcement time within the session).
    today_str = now_utc.strftime("%Y-%m-%d")
    if today_str in JAPAN_HOLIDAYS:
        reason = "Japanese national holiday (%s) -- Tokyo Stock Exchange closed" % today_str
        return {
            "hard_block":       True,
            "block_reason":     reason,
            "upcoming_events":  [],
            "calendar_summary": "HOLIDAY: %s. No trading." % reason,
            "next_boe":         "",
        }
    if today_str in BOJ_MEETING_DAYS:
        reason = "Bank of Japan policy meeting (%s) -- announcement timing unpredictable, full-day block" % today_str
        return {
            "hard_block":       True,
            "block_reason":     reason,
            "upcoming_events":  [],
            "calendar_summary": "BoJ MEETING: %s. No trading." % reason,
            "next_boe":         "",
        }

    soft_events = _get_fed_events() + _get_nfp_events()

    # BoJ 30-minute timed-entry window REMOVED 25 Jul 2026 (filter-audit tidy-up):
    # it was dormant dead code -- the BoJ timed-date list was empty so this never fired.
    # The FULL-DAY BoJ block above (BOJ_MEETING_DAYS early-return) is the active BoJ
    # protection and is unchanged.
    hard_block   = False
    block_reason = ""

    # Upcoming events in next 24 hours
    upcoming = []
    window_24h = timedelta(hours=24)
    for ev in soft_events:
        delta = ev["datetime"] - now_utc
        if timedelta(0) <= delta <= window_24h:
            mins_away = int(delta.total_seconds() / 60)
            upcoming.append({
                "name":      ev["name"],
                "time_utc":  ev["datetime"].strftime("%H:%M UTC"),
                "date":      ev["date_str"],
                "impact":    ev["impact"],
                "mins_away": mins_away,
            })
    upcoming.sort(key=lambda x: x["mins_away"])

    # Next BoJ decision: the timed BoJ list is gone; the full-day BOJ_MEETING_DAYS block
    # is the active protection, so there is no "next timed BoJ" string to compute.
    next_boe_str = ""

    # Calendar summary for Arthur
    if hard_block:
        summary = f"HARD BLOCK ACTIVE: {block_reason}"
    elif upcoming:
        ev_list = "; ".join(
            f"{u['name']} in {u['mins_away']} min" for u in upcoming[:3]
        )
        summary = f"Upcoming: {ev_list}. {next_boe_str}"
    else:
        summary = f"Calendar clear -- no events in next 24h. {next_boe_str}"

    return {
        "hard_block":       hard_block,
        "block_reason":     block_reason,
        "upcoming_events":  upcoming,
        "calendar_summary": summary,
        "next_boe":         next_boe_str,
    }


def is_hard_blocked(now_utc: Optional[datetime] = None) -> tuple:
    """
    Quick check. Returns (hard_block: bool, reason: str, event_name: str, mins_remaining: int).
    """
    result = check_calendar(now_utc)
    if result["hard_block"]:
        return True, result["block_reason"], "BoJ Decision", HARD_BLOCK_MINUTES
    return False, "", "", 0


def get_calendar_context(now_utc: Optional[datetime] = None) -> str:
    """Return formatted calendar context string for Arthur."""
    cal = check_calendar(now_utc)
    lines = [
        "JAPAN CALENDAR",
        f"  Status: {'HARD BLOCK ACTIVE' if cal['hard_block'] else 'Clear'}",
    ]
    if cal["block_reason"]:
        lines.append(f"  Block reason: {cal['block_reason']}")
    if cal["upcoming_events"]:
        lines.append("  Upcoming (next 24h):")
        for ev in cal["upcoming_events"][:5]:
            impact_str = "[HARD BLOCK]" if ev["impact"] == "HARD_BLOCK" else "[soft]"
            lines.append(
                f"    {ev['name']} -- {ev['time_utc']} "
                f"(in {ev['mins_away']} min) {impact_str}"
            )
    if cal["next_boe"]:
        lines.append(f"  {cal['next_boe']}")
    lines.append(
        "  NOTE: overnight Wall Street close drives the 00:00 UTC Tokyo open gap."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Guinevere self-test")
    now = datetime.now(timezone.utc)
    log.info("Current UTC: %s", now.strftime("%Y-%m-%d %H:%M"))
    cal = check_calendar(now)
    log.info("Hard block: %s", cal["hard_block"])
    log.info("Summary: %s", cal["calendar_summary"])
    if cal["upcoming_events"]:
        log.info("Upcoming events:")
        for ev in cal["upcoming_events"]:
            log.info("  %s in %d min", ev["name"], ev["mins_away"])
    log.info("Guinevere self-test complete.")
