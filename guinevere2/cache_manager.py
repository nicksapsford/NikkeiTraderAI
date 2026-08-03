"""
Guinevere 2.0 -- cache manager (Commission 018 31 Jul 2026; FEED FIX 3 Aug 2026).
Tiny JSON cache. Entries: {"ts": fetch_time, "value": [...articles], "status": "OK"|"FAILED"}.
get() keeps the 15-min TTL contract; get_entry() returns the RAW entry (any age) so the
fetcher can keep last-good articles across a failed refetch instead of blanking a populated
feed. Old entries (pre-fix, no "status") are treated as OK. ALL TIMES UTC.
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
    """Back-compat: cached value if fresh AND status OK, else None."""
    entry = _load().get(key)
    if not entry:
        return None
    if entry.get("status", "OK") != "OK":
        return None
    if (time.time() - entry.get("ts", 0)) > ttl_sec:
        return None
    return entry.get("value")


def get_entry(key):
    """Full raw entry {ts, value, status} regardless of TTL, or None. Lets the fetcher
    keep last-good articles across a failed refetch."""
    return _load().get(key)


def put(key, value, status="OK"):
    """Store value under key with the current timestamp + status. Never raises."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        data = _load()
        data[key] = {"ts": time.time(), "value": value, "status": status}
        with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass
