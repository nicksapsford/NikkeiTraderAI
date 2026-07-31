"""
Guinevere 2.0 -- directional news intelligence for Arthur (Commission 018, 31 Jul 2026).

Vendored identically into each of the 6 Arthur systems. One call per Arthur consultation:

    import guinevere2
    advisory = guinevere2.get_advisory("FTSE")      # -> string for Arthur's prompt
    sig      = guinevere2.get_signal("FTSE")        # -> net signal dict (for dashboard/log)
    guinevere2.log_decision(sig, arthur_decision="ENTER_LONG", arthur_confidence_after=68)

Instruments: FTSE, GOLD, OIL, US500, NIKKEI (ES uses "US500"). Directional CONTEXT only
(Principle 14): a +/-25-capped confidence modifier, never a filter. FAIL SAFE: any failure
(missing API key, HTTP error) yields a NEUTRAL advisory -- Arthur just trades on indicators.
Requires ALPHA_VANTAGE_API_KEY in .env. ALL TIMES UTC.
"""

from .guinevere2_main import (
    get_signal, get_advisory, format_dashboard, format_archie, log_decision, is_high_alert,
)

__all__ = ["get_signal", "get_advisory", "format_dashboard", "format_archie",
           "log_decision", "is_high_alert"]
__version__ = "2.1.0"   # BOOSTS AND REDIRECTS, NEVER BLOCKS (31 Jul 2026)
