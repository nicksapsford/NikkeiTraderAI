"""
Guinevere 2.0 -- orchestrator (Commission 018 build, 31 Jul 2026).

Public surface used by each Arthur system (via the package __init__):
    get_signal(instrument)   -> net signal dict (fetch [cached] -> calendar -> engine)
    get_advisory(instrument) -> the GUINEVERE 2.0 advisory string for Arthur's prompt
    format_dashboard(sig)    -> compact dict for the dashboard panel
    format_archie(sig)       -> one-line string for the Archie brief
    log_decision(...)        -> append to guinevere2_log.csv

FAIL SAFE throughout: any failure yields a NEUTRAL advisory, never an exception.
ALL TIMES UTC.
"""

import logging
from datetime import datetime, timezone

from . import news_fetcher, calendar_checker, signal_engine, signal_logger

log = logging.getLogger("Guinevere2.Main")


def get_signal(instrument):
    """Net directional signal for one instrument. NEUTRAL on any failure."""
    try:
        now = datetime.now(timezone.utc)
        articles = news_fetcher.fetch(instrument)
        cal = calendar_checker.get_calendar_events(now)
        return signal_engine.build_signal(instrument, articles, cal, now_utc=now)
    except Exception as exc:                       # fail safe
        log.warning("Guinevere2 get_signal(%s) failed: %s", instrument, exc)
        return signal_engine._neutral(instrument, datetime.now(timezone.utc))


def get_advisory(instrument):
    """The GUINEVERE 2.0 -- MACRO INTELLIGENCE block for Arthur's prompt (string)."""
    sig = get_signal(instrument)
    now_note = calendar_checker.calendar_note()
    ts = sig.get("as_of", "")[11:16] or datetime.now(timezone.utc).strftime("%H:%M")
    line = "================================================================\n"
    head = line + "GUINEVERE 2.0 -- MACRO INTELLIGENCE -- %s UTC\n" % ts + line

    if sig["direction"] == "NEUTRAL" or sig["modifier"] == 0:
        return (head +
                "No significant directional news events active for %s.\n" % instrument +
                "Calendar: %s\n" % now_note +
                "NET SIGNAL: NEUTRAL -- rely on your technical indicators fully.\n" + line)

    mod = sig["modifier"]                         # + supports LONG, - supports SHORT
    long_mod = mod
    short_mod = -mod
    drivers = sig.get("drivers", [])[:3]
    lines = [head, "ACTIVE NEWS EVENTS:"]
    for d in drivers:
        dir_word = "BULLISH" if d["direction"] > 0 else "BEARISH"
        dm = int(round(min(25, d["strength"]))) * (1 if d["direction"] > 0 else -1)
        lines.append("  [%s] %s -- %s (%.0fh old, decay %.0f%%)"
                     % (_tier(d["strength"]), d["event_type"], d["headline"] or "-",
                        d["age_h"], d["decay"] * 100))
        lines.append("     -> %s: %s (%+d conf on LONGs / %+d on SHORTs)"
                     % (instrument, dir_word, dm, -dm))
    lines.append("")
    lines.append("CALENDAR TODAY: %s" % now_note)
    net_word = sig["direction"] + ((" (MIXED/uncertain)" if sig["mixed"] else
                                    " (%s)" % sig["confidence"]))
    lines.append("")
    lines.append("NET GUINEVERE SIGNAL FOR %s: %s" % (instrument, net_word))
    lines.append("  -> %+d points on LONGs, %+d points on SHORTs (cap +/-25 applied)."
                 % (long_mod, short_mod))
    if sig["mixed"]:
        lines.append("  NOTE: signals conflict -- treat as LOW conviction; lean STAY_OUT unless"
                     " your Level-2 indicators strongly agree.")
    lines.append("")
    lines.append("HOW TO USE: news moves markets before indicators catch up. When this signal")
    lines.append("is HIGH/MEDIUM, weight its direction per the DECISION HIERARCHY. Cap +/-25 --")
    lines.append("indicators stay primary; Guinevere calibrates, never overrides a strong setup.")
    lines.append(line.rstrip("\n"))
    return "\n".join(lines)


def _tier(strength):
    return "HIGH" if strength >= 15 else ("MED" if strength >= 9 else "LOW")


def format_dashboard(sig):
    """Compact dict for the dashboard Guinevere 2.0 panel."""
    return {
        "signal": sig.get("direction", "NEUTRAL"),
        "modifier": sig.get("modifier", 0),
        "confidence": sig.get("confidence", "NEUTRAL"),
        "primary_event": sig.get("primary_event") or "-",
        "mixed": sig.get("mixed", False),
        "as_of": sig.get("as_of", ""),
    }


def format_archie(sig):
    """One-line Archie-brief string."""
    if sig.get("direction") == "NEUTRAL" or not sig.get("modifier"):
        return "GUINEVERE 2.0: NEUTRAL (%s) | %s" % (
            (sig.get("as_of", "")[11:16] or "--"), calendar_checker.calendar_note())
    return "GUINEVERE 2.0: %s (%+d) | %s | cal: %s" % (
        sig.get("direction"), sig.get("modifier", 0),
        (sig.get("primary_event") or "-")[:60], calendar_checker.calendar_note())


def log_decision(sig, arthur_decision="", arthur_confidence_after="",
                 arthur_confidence_before="", trade_outcome=""):
    signal_logger.log_signal(sig, arthur_decision, arthur_confidence_after,
                             arthur_confidence_before, trade_outcome)
