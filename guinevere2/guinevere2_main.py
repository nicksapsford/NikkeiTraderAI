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
                "No significant news. Rely on your technical indicators fully.\n" +
                "Calendar: %s\n" % now_note +
                "Your technical analysis carries FULL weight today. Do not look for reasons\n"
                "to be cautious -- Guinevere is NEUTRAL and has zero influence.\n" + line)

    mod = sig["modifier"]                         # POSITIVE boost for the favoured direction
    favoured = sig.get("favoured") or ("LONG" if sig["direction"] == "BULLISH" else "SHORT")
    opposite = "SHORT" if favoured == "LONG" else "LONG"
    drivers = sig.get("drivers", [])[:3]
    lines = [head, "ACTIVE NEWS EVENTS:"]
    for d in drivers:
        dfav = "LONG" if d["direction"] > 0 else "SHORT"
        dm = int(round(min(25, d["strength"])))
        lines.append("  [%s] %s -- %s (%.0fh old, decay %.0f%%)"
                     % (_tier(d["strength"]), d["event_type"], d["headline"] or "-",
                        d["age_h"], d["decay"] * 100))
        lines.append("     -> %s: favours %s (+%d if you trade %s)"
                     % (instrument, dfav, dm, dfav))
    lines.append("")
    lines.append("CALENDAR TODAY: %s" % now_note)
    net_word = sig["direction"] + (" (MIXED)" if sig["mixed"] else " (%s)" % sig["confidence"])
    lines.append("")
    lines.append("NET GUINEVERE SIGNAL FOR %s: %s -- FAVOURS %s" % (instrument, net_word, favoured))
    lines.append("  -> If your setup is %s (agrees): ADD +%d confidence -- enter with conviction."
                 % (favoured, mod))
    lines.append("  -> If your setup is %s (disagrees): NO penalty. Look for a %s opportunity"
                 % (opposite, favoured))
    lines.append("     instead; if none, trade your %s setup NORMALLY at full indicator confidence."
                 % opposite)
    lines.append("     (Guinevere ALONE is never a reason to STAY_OUT -- tighter EXIT watch only.)")
    lines.append("")
    lines.append("REMEMBER (FIVE RULES): Guinevere BOOSTS and REDIRECTS -- she NEVER BLOCKS. She")
    lines.append("only ADDS confidence in her favoured direction; she NEVER subtracts. See the")
    lines.append("GUINEVERE 2.0 -- FIVE RULES section in your standing instructions.")
    lines.append(line.rstrip("\n"))
    return "\n".join(lines)


def _tier(strength):
    return "HIGH" if strength >= 15 else ("MED" if strength >= 9 else "LOW")


def format_dashboard(sig):
    """Compact dict for the dashboard Guinevere 2.0 panel. `modifier` is a POSITIVE boost
    for the `favoured` direction (never negative)."""
    return {
        "signal": sig.get("direction", "NEUTRAL"),
        "modifier": sig.get("modifier", 0),          # positive boost only
        "favoured": sig.get("favoured", ""),         # LONG/SHORT it boosts
        "confidence": sig.get("confidence", "NEUTRAL"),
        "primary_event": sig.get("primary_event") or "-",
        "mixed": sig.get("mixed", False),
        "as_of": sig.get("as_of", ""),
    }


def format_archie(sig):
    """One-line Archie-brief string (positive boost + favoured direction; never negative)."""
    if sig.get("direction") == "NEUTRAL" or not sig.get("modifier"):
        return "GUINEVERE 2.0: NEUTRAL (%s) | %s" % (
            (sig.get("as_of", "")[11:16] or "--"), calendar_checker.calendar_note())
    return "GUINEVERE 2.0: %s -> favours %s (+%d) | %s | cal: %s" % (
        sig.get("direction"), sig.get("favoured", ""), sig.get("modifier", 0),
        (sig.get("primary_event") or "-")[:60], calendar_checker.calendar_note())


def is_high_alert(instrument, sig=None):
    """Guinevere active-consultation trigger (31 Jul 2026 update). Returns
    (is_alert: bool, favoured_direction: str, note: str). True only on a HIGH-confidence
    directional signal -- the main loop may then consult Arthur even when ONLY soft Lancelot
    checks (SSL alignment / momentum / candle) block. RISK controls (kill switch, cooldown,
    consecutive losses, daily loss, session CLOSED, volatility floor, daily-trend SSL) are
    ALWAYS enforced by Lancelot and are never bypassed."""
    try:
        sig = sig or get_signal(instrument)
        if sig.get("confidence") == "HIGH" and sig.get("modifier", 0) >= 20 and sig.get("favoured"):
            note = ("GUINEVERE HIGH ALERT -- Active consultation. %s favours %s (+%d). SSL/"
                    "momentum/candle alignment RELAXED -- Guinevere provides the direction. All "
                    "RISK controls remain active. Score 60+ required to enter."
                    % (instrument, sig["favoured"], sig["modifier"]))
            return True, sig["favoured"], note
    except Exception:
        pass
    return False, "", ""


def log_decision(sig, arthur_decision="", arthur_confidence_after="",
                 arthur_confidence_before="", trade_outcome=""):
    signal_logger.log_signal(sig, arthur_decision, arthur_confidence_after,
                             arthur_confidence_before, trade_outcome)
