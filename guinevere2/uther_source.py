"""
Guinevere 2.0 -- Uther assessment source (Commission 019 amalgamation, 3 Aug 2026).

Reads Uther's amalgamation feed (logs/uther_signals.json in the sibling UtherAI repo) and
returns recent actionable assessments for one instrument as event drivers. CRASH-SAFE: any
error (missing file, bad JSON, Uther down/stale) -> [] so Guinevere falls to a NEUTRAL
advisory and Arthur trades on indicators. It NEVER falls back to the keyword classifier --
a degraded signal presented as normal is worse than an honest NEUTRAL. ALL TIMES UTC.
"""

import json
from datetime import datetime, timezone

from . import config as C


def _parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_feed():
    """Parsed uther_signals.json dict, or {} on any failure."""
    try:
        with open(C.UTHER_SIGNALS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def feed_status(now=None):
    """(status, age_min) for dashboards/brief: OK / STALE / DOWN / NO_KEY / MISSING."""
    now = now or datetime.now(timezone.utc)
    d = load_feed()
    if not d:
        return "MISSING", None
    gen = _parse_ts(d.get("generated_utc", ""))
    age_min = ((now - gen).total_seconds() / 60.0) if gen else None
    api = d.get("api_status", "OK")
    if api in ("DOWN", "NO_KEY"):
        return api, age_min
    if age_min is not None and age_min > C.UTHER_STALE_MIN:
        return "STALE", age_min
    return "OK", age_min


def get_assessments(instrument, now=None):
    """Recent actionable Uther assessments for `instrument` as raw driver dicts:
    {event_type, rank, direction(+1/-1), confidence, age_h, timestamp, reasoning, headline}.
    Assessments older than UTHER_MAX_AGE_H are dropped (decay has zeroed them)."""
    now = now or datetime.now(timezone.utc)
    d = load_feed()
    out = []
    for a in (d.get("assessments") or []):
        ts = _parse_ts(a.get("timestamp_utc", ""))
        if ts is None:
            continue
        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        if age_h > C.UTHER_MAX_AGE_H:
            continue
        try:
            rank = int(a.get("rank") or 4)
        except (TypeError, ValueError):
            rank = 4
        for p in (a.get("per_instrument") or []):
            if p.get("instrument") != instrument:
                continue
            drc = p.get("direction")
            if drc not in ("LONG", "SHORT"):
                continue
            out.append({
                "event_type": a.get("event_type", "NONE"),
                "rank": rank if rank in (1, 2, 3, 4) else 4,
                "direction": 1 if drc == "LONG" else -1,
                "confidence": (p.get("confidence") or "LOW").upper(),
                "age_h": age_h,
                "timestamp": a.get("timestamp_utc", ""),
                "reasoning": a.get("reasoning", ""),
                "headline": a.get("headline", ""),
            })
    return out
