"""
Guinevere 2.0 -- signal logger (Commission 018 build, 31 Jul 2026).
Appends every signal + Arthur's response to logs/guinevere2_log.csv so Gaius can assess
Guinevere 2.0's value over time (exactly as phantom logs assess STAY_OUT decisions).
Never raises. ALL TIMES UTC.
"""

import csv
import os
from datetime import datetime, timezone

_LOG_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "logs", "guinevere2_log.csv")
_FIELDS = ["timestamp", "instrument", "direction", "confidence_level", "modifier_applied",
           "primary_event", "decay_factor", "arthur_confidence_before",
           "arthur_confidence_after", "arthur_decision", "trade_outcome"]


def log_signal(signal, arthur_decision="", arthur_confidence_after="",
               arthur_confidence_before="", trade_outcome=""):
    """Append one row. Call at consult time (decision/confidence optional, filled when known)."""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "instrument": signal.get("instrument", ""),
            "direction": signal.get("direction", ""),
            "confidence_level": signal.get("confidence", ""),
            "modifier_applied": signal.get("modifier", 0),
            "primary_event": (signal.get("primary_event") or "")[:160],
            "decay_factor": signal.get("decay_factor", 0.0),
            "arthur_confidence_before": arthur_confidence_before,
            "arthur_confidence_after": arthur_confidence_after,
            "arthur_decision": arthur_decision,
            "trade_outcome": trade_outcome,
        }
        exists = os.path.exists(_LOG_PATH)
        with open(_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDS)
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception:
        pass
