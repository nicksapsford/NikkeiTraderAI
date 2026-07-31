"""
Guinevere 2.0 -- cache manager (Commission 018 build, 31 Jul 2026).
Tiny JSON cache so Arthur consultations never hit the Alpha Vantage API directly.
Keyed by instrument; entries expire after config.CACHE_TTL_SEC (15 min). ALL TIMES UTC.
"""

import json
import os
import time

_CACHE_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "logs", "guinevere2_cache.json")


def _load():
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def get(key, ttl_sec):
    """Return the cached value for key if fresh, else None."""
    entry = _load().get(key)
    if not entry:
        return None
    if (time.time() - entry.get("ts", 0)) > ttl_sec:
        return None
    return entry.get("value")


def put(key, value):
    """Store value under key with the current timestamp. Never raises."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        data = _load()
        data[key] = {"ts": time.time(), "value": value}
        with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass
