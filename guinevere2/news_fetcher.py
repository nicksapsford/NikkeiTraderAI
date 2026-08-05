"""
Guinevere 2.0 -- news fetcher (Commission 018 31 Jul 2026; FEED FIX 3 Aug 2026).

Builds one instrument's article set from THREE Alpha Vantage NEWS_SENTIMENT queries,
merged + de-duplicated by URL:
  1. per-instrument tickers    (cfg["tickers"])
  2. per-instrument topics      (cfg["topics"])        <- was configured but NEVER sent
  3. ONE universal macro query  (topics=economy_macro, time_from=now-6h)  <- shared, cached

FAIL SAFE: a failed fetch (DNS / rate-limit / invalid inputs / missing key) NEVER blanks a
populated feed -- the last good articles are KEPT (with their real age). Only a genuinely
VALID-EMPTY response caches []. The API key is scrubbed from all error logs. Every fetch
writes one row to guinevere2_cache_debug.csv. ALL TIMES UTC.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except Exception:                       # requests missing -> fail safe
    requests = None

from . import config as C
from . import cache_manager

log = logging.getLogger("Guinevere2.Fetch")
_BASE = "https://www.alphavantage.co/query"
_LOGDIR = os.path.join(os.path.dirname(__file__), os.pardir, "logs")
_ERR_LOG = os.path.join(_LOGDIR, "guinevere2_errors.log")
_DEBUG_LOG = os.path.join(_LOGDIR, "guinevere2_cache_debug.csv")
_DEBUG_HEADER = "poll_time_utc,instrument,cache_age_s,served_from,num_articles,newest_article_utc\n"

_KEY_RE = re.compile(r'(apikey=)[^&\s\'"]+', re.IGNORECASE)


def _key():
    return (os.getenv("ALPHA_VANTAGE_API_KEY") or "").strip()


def _scrub(msg):
    """Fix 7: strip the API key from anything we log (it leaks via requests' exception URLs)."""
    s = _KEY_RE.sub(r'\1***', str(msg))
    key = _key()
    if key:
        s = s.replace(key, "***")
    return s


def _log_error(msg):
    try:
        os.makedirs(_LOGDIR, exist_ok=True)
        with open(_ERR_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(), _scrub(msg)))
    except Exception:
        pass


def _newest_pub(articles):
    newest = ""
    for a in articles or []:
        tp = a.get("time_published") or ""
        if tp > newest:
            newest = tp
    return newest


def _cache_debug(instrument, cache_age_s, served_from, articles):
    """Fix 6 (pre-authorised): one row per fetch so feed issues are diagnosable at a glance."""
    try:
        os.makedirs(_LOGDIR, exist_ok=True)
        new = not os.path.exists(_DEBUG_LOG)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as fh:
            if new:
                fh.write(_DEBUG_HEADER)
            fh.write("%s,%s,%s,%s,%d,%s\n" % (
                datetime.now(timezone.utc).isoformat(), instrument,
                ("" if cache_age_s is None else int(cache_age_s)),
                served_from, len(articles or []), _newest_pub(articles) or "-"))
    except Exception:
        pass


def _query(params, ctx):
    """One AV NEWS_SENTIMENT call. Returns (feed_list, ok). ok=False on ANY failure
    (exception / no-feed / rate-limit / invalid inputs / missing key). Never raises."""
    key = _key()
    if not key or requests is None:
        _log_error("no ALPHA_VANTAGE_API_KEY / requests unavailable (%s)" % ctx)
        return [], False
    p = dict(params)
    p.update({"function": "NEWS_SENTIMENT", "sort": "LATEST", "apikey": key})
    try:
        r = requests.get(_BASE, params=p, timeout=8)
        data = r.json()
        feed = data.get("feed")
        if feed is None:
            # AV returns {"Information": ...} on rate-limit / bad key / invalid inputs
            _log_error("AV no feed (%s): %s" % (ctx, str(data)[:180]))
            return [], False
        return feed, True
    except Exception as exc:
        _log_error("AV fetch failed (%s): %s" % (ctx, exc))
        return [], False


def _macro_feed():
    """Fix 1: the ONE universal macro query per poll cycle (topics=economy_macro,
    time_from=now-6h), shared across instruments via a dedicated cache key so it runs
    once per TTL. Returns (feed, contacted). Keeps last-good macro on failure."""
    mkey = C.MACRO_CACHE_KEY
    entry = cache_manager.get_entry(mkey)
    if entry and entry.get("status", "OK") == "OK" and (time.time() - entry.get("ts", 0)) <= C.CACHE_TTL_SEC:
        return entry.get("value") or [], True
    tf = (datetime.now(timezone.utc) - timedelta(hours=C.MACRO_TIME_FROM_HOURS)).strftime("%Y%m%dT%H%M")
    feed, ok = _query({"topics": C.MACRO_TOPICS, "limit": str(C.MACRO_LIMIT), "time_from": tf}, "MACRO")
    if ok:
        cache_manager.put(mkey, feed, status="OK")
        return feed, True
    if entry and entry.get("value"):
        return entry.get("value"), True          # keep last-good macro on failure
    cache_manager.put(mkey, [], status="FAILED")
    return [], False


# Single-stock noise pre-filter (5 Aug 2026, ticker-based) -- PARITY with Uther's
# news_fetcher_uther per CODY_STANDING Rule 18. guinevere2 makes no Claude calls (rule-based;
# dormant behind the amalgamation flag), so this is a consistency measure only. A bracketed
# ticker in the HEADLINE that is NOT a market-moving heavyweight -> single-stock noise, dropped
# from the assembled set. No bracketed ticker -> kept (macro/index news never blocked). Keep
# _MARKET_MOVING_TICKERS in sync with UtherAI/config_uther.py MARKET_MOVING_TICKERS.
_TICKER_RE = re.compile(r"[\[(]\s*([A-Z]{2,5})\s*[\])]")
# CONFIRMED by Nick/Archie 5 Aug 2026 -- MUST mirror UtherAI/config_uther.py MARKET_MOVING_TICKERS.
_MARKET_MOVING_TICKERS = {
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "LLY", "BRK", "BRKB",
    "AMD", "MU", "INTC", "QCOM", "TSM", "ASML", "MRVL", "ARM",
    "JPM", "BAC", "GS", "MS",
    "XOM", "CVX", "COP",
    "UNH", "ORCL", "CRM", "NFLX", "V", "MA",
    "HSBC", "AZN", "SHEL", "BP", "RIO", "UL", "BHP", "GSK", "DEO", "NGG", "BCS", "LYG",
    "GLEN", "GLNCY", "AAL", "ANTO", "BAESY", "RYCEY", "BATS", "BTI",
    "TM", "SONY", "NTDOY", "SFTBY", "HMC", "MUFG", "SMFG", "NMR", "FANUY", "KYOCY",
}


def _is_single_stock_noise(a):
    try:
        title = a.get("title")
        title = title if isinstance(title, str) else ("" if title is None else str(title))
        tickers = _TICKER_RE.findall(title.upper())
        if not tickers:
            return False
        return not any(t in _MARKET_MOVING_TICKERS for t in tickers)
    except Exception:
        return False


def _live_fetch(cfg, instrument, limit):
    """Merge ticker + topics + universal-macro queries, deduped by url.
    Returns (articles, contacted) -- contacted True if ANY sub-query reached AV."""
    by_url, contacted = {}, False
    if cfg.get("tickers"):
        feed, ok = _query({"tickers": ",".join(cfg["tickers"]), "limit": str(limit)},
                          "%s/tickers" % instrument)
        contacted = contacted or ok
        for a in feed:
            if a.get("url"):
                by_url[a["url"]] = a
    if cfg.get("topics"):
        feed, ok = _query({"topics": ",".join(cfg["topics"]), "limit": str(limit)},
                          "%s/topics" % instrument)
        contacted = contacted or ok
        for a in feed:
            if a.get("url"):
                by_url[a["url"]] = a
    macro, mok = _macro_feed()
    contacted = contacted or mok
    for a in macro:
        if a.get("url"):
            by_url[a["url"]] = a
    return [a for a in by_url.values() if not _is_single_stock_noise(a)], contacted   # Fix 1 parity


def fetch(instrument, limit=50):
    """Return merged AV articles for `instrument` (cached CACHE_TTL_SEC). Fix 3: keeps
    last-good articles across a failed fetch -- a transient outage NEVER blanks a populated
    feed; only a genuinely valid-empty response caches []. [] only when there is no good
    prior feed AND the fetch failed."""
    cfg = C.INSTRUMENTS.get(instrument)
    if cfg is None:
        return []
    key = "news:" + instrument
    entry = cache_manager.get_entry(key)
    if entry and entry.get("status", "OK") == "OK" and (time.time() - entry.get("ts", 0)) <= C.CACHE_TTL_SEC:
        _cache_debug(instrument, time.time() - entry.get("ts", 0), "cache", entry.get("value"))
        return entry.get("value") or []

    articles, contacted = _live_fetch(cfg, instrument, limit)
    if contacted:
        cache_manager.put(key, articles, status="OK")      # genuine result (may be valid-empty)
        _cache_debug(instrument, 0, "live", articles)
        return articles
    # total failure -> keep last good (its real age preserved), else cache a FAILED empty
    if entry and entry.get("value"):
        age = time.time() - entry.get("ts", 0)
        _cache_debug(instrument, age, "last_good_on_failure", entry.get("value"))
        return entry.get("value")
    cache_manager.put(key, [], status="FAILED")
    _cache_debug(instrument, None, "failed_empty", [])
    return []


def feed_health(instrument):
    """Fix 4: newest-ARTICLE age + status for the dashboard feed-health indicator.
    Colour is by newest time_published (NOT fetch time) so a blinded-but-'fresh' cache
    can never read green. GREEN < 6h / AMBER 6-24h / RED > 24h or empty/failed."""
    entry = cache_manager.get_entry("news:" + instrument) or {}
    articles = entry.get("value") or []
    newest = _newest_pub(articles)
    now = datetime.now(timezone.utc)
    age_h, newest_iso = None, ""
    if newest:
        try:
            dt = datetime.strptime(newest, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
            newest_iso = dt.isoformat()
        except (ValueError, TypeError):
            age_h = None
    if not articles or age_h is None:
        color = "RED"
    elif age_h <= C.FEED_GREEN_MAX_H:
        color = "GREEN"
    elif age_h <= C.FEED_AMBER_MAX_H:
        color = "AMBER"
    else:
        color = "RED"
    return {
        "instrument": instrument,
        "num_articles": len(articles),
        "newest_article_utc": newest_iso,
        "age_hours": (round(age_h, 1) if age_h is not None else None),
        "color": color,
        "cache_status": entry.get("status", "NONE"),
    }
