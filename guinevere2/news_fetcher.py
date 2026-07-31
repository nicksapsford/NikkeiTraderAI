"""
Guinevere 2.0 -- news fetcher (Commission 018 build, 31 Jul 2026).
Fetches Alpha Vantage NEWS_SENTIMENT for one instrument, cached 15 min. FAIL SAFE:
any error (missing key, HTTP failure, bad JSON) returns [] so Arthur's consult proceeds
with a NEUTRAL signal -- never blocks or crashes a trading tick. ALL TIMES UTC.
"""

import logging
import os

try:
    import requests
except Exception:                       # requests missing -> fail safe
    requests = None

from . import config as C
from . import cache_manager

log = logging.getLogger("Guinevere2.Fetch")
_BASE = "https://www.alphavantage.co/query"
_ERR_LOG = os.path.join(os.path.dirname(__file__), os.pardir, "logs", "guinevere2_errors.log")


def _log_error(msg):
    try:
        os.makedirs(os.path.dirname(_ERR_LOG), exist_ok=True)
        from datetime import datetime, timezone
        with open(_ERR_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(), msg))
    except Exception:
        pass


def fetch(instrument, limit=50):
    """Return a list of AV feed article dicts for `instrument` (cached). [] on any failure."""
    cfg = C.INSTRUMENTS.get(instrument)
    if cfg is None:
        return []
    cached = cache_manager.get("news:" + instrument, C.CACHE_TTL_SEC)
    if cached is not None:
        return cached

    key = (os.getenv("ALPHA_VANTAGE_API_KEY") or "").strip()
    if not key or requests is None:
        _log_error("no ALPHA_VANTAGE_API_KEY / requests unavailable -> NEUTRAL for %s" % instrument)
        cache_manager.put("news:" + instrument, [])   # cache the empty so we don't retry every tick
        return []

    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ",".join(cfg["tickers"]),
        "limit": str(limit),
        "sort": "LATEST",
        "apikey": key,
    }
    try:
        r = requests.get(_BASE, params=params, timeout=8)
        data = r.json()
        feed = data.get("feed")
        if feed is None:
            # AV returns {"Information": ...} on rate-limit / bad key
            _log_error("AV no feed for %s: %s" % (instrument, str(data)[:200]))
            cache_manager.put("news:" + instrument, [])
            return []
        cache_manager.put("news:" + instrument, feed)
        return feed
    except Exception as exc:
        _log_error("AV fetch failed for %s: %s" % (instrument, exc))
        cache_manager.put("news:" + instrument, [])
        return []
