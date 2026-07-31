"""
Guinevere 2.0 -- signal engine (Commission 018 core intelligence, 31 Jul 2026).

Turns raw Alpha Vantage news + calendar context into ONE net directional signal per
instrument, via: event classification -> direction matrix -> decay -> conflict
resolution -> confidence modifier (clamped +/-25). Pure/deterministic and unit-tested;
no network or file I/O here. ALL TIMES UTC.
"""

from datetime import datetime, timezone

from . import config as C


# Base modifier magnitude by event rank tier (before decay + sentiment scaling).
_BASE_BY_RANK = {
    C.RANK_SCHEDULED_MACRO: 22,
    C.RANK_GEOPOLITICAL:    20,
    C.RANK_ECON_DATA:       12,
    C.RANK_MARKET_FLOW:      8,
}


def _contains(text, kws):
    return any(k in text for k in kws)


def classify_article(article):
    """Return a list of event_type strings this article represents (may be empty).
    Uses title+summary keyword scan; AV overall sentiment disambiguates hawkish/dovish
    and strong/weak when the keywords alone are directionless."""
    text = ((article.get("title") or "") + " " + (article.get("summary") or "")).lower()
    try:
        av_sent = float(article.get("overall_sentiment_score") or 0.0)
    except (TypeError, ValueError):
        av_sent = 0.0
    events = []

    # RANK 1 -- central bank / scheduled macro
    if _contains(text, C.KW_CENTRAL_BANK):
        if _contains(text, ["boj", "bank of japan"]):
            events.append("BOJ_HAWKISH" if _contains(text, C.KW_HAWKISH) else
                          ("BOJ_DOVISH" if _contains(text, C.KW_DOVISH) else
                           ("BOJ_HAWKISH" if av_sent < -0.05 else "BOJ_DOVISH")))
        elif _contains(text, ["boe", "bank of england"]):
            events.append("BOE_HAWKISH" if _contains(text, C.KW_HAWKISH) else
                          ("BOE_DOVISH" if _contains(text, C.KW_DOVISH) else
                           ("BOE_HAWKISH" if av_sent < -0.05 else "BOE_DOVISH")))
        else:  # Fed / generic monetary
            if _contains(text, C.KW_HAWKISH):
                events.append("FED_HAWKISH")
            elif _contains(text, C.KW_DOVISH):
                events.append("FED_DOVISH")
            else:
                events.append("FED_HAWKISH" if av_sent < -0.05 else "FED_DOVISH")

    # RANK 2 -- geopolitical
    if _contains(text, C.KW_GEOPOLITICAL):
        events.append("GEO_DEESCALATION" if _contains(text, C.KW_DEESCALATION)
                      else "GEO_ESCALATION")

    # RANK 3 -- economic data
    if _contains(text, C.KW_ECON_DATA):
        if _contains(text, C.KW_STRONG_DATA):
            events.append("DATA_STRONG")
        elif _contains(text, C.KW_WEAK_DATA):
            events.append("DATA_WEAK")
        else:
            events.append("DATA_STRONG" if av_sent > 0.05 else
                          ("DATA_WEAK" if av_sent < -0.05 else None))
    return [e for e in events if e]


def _article_age_hours(article, now_utc):
    """Age of an AV article. time_published is 'YYYYMMDDTHHMMSS' (UTC)."""
    tp = article.get("time_published") or ""
    try:
        dt = datetime.strptime(tp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc - dt).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return 0.5  # unknown -> treat as recent-ish


def _instrument_relevance(article, cfg):
    """0..1 relevance of an article to an instrument (max of AV ticker relevance and a
    keyword-match flag). Lets us attribute topic/keyword articles that lack a ticker tag."""
    rel = 0.0
    for ts in (article.get("ticker_sentiment") or []):
        if ts.get("ticker") in cfg["tickers"]:
            try:
                rel = max(rel, float(ts.get("relevance_score") or 0.0))
            except (TypeError, ValueError):
                pass
    text = ((article.get("title") or "") + " " + (article.get("summary") or "")).lower()
    if _contains(text, cfg["keywords"]):
        rel = max(rel, 0.4)
    return rel


def build_signal(instrument, articles, calendar_events, now_utc=None, month_trenders=None):
    """Produce the net signal dict for one instrument.
    articles: list of AV feed dicts. calendar_events: list of dicts from calendar_checker
    ({'event_type','headline'}). month_trenders: optional set of instruments that trended
    strongly this month (month-end bearish bias applies to them)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    cfg = C.INSTRUMENTS.get(instrument)
    if cfg is None:
        return _neutral(instrument, now_utc)

    drivers = []  # each: {event_type, rank, direction, strength, decay, age_h, headline}

    for a in articles:
        events = classify_article(a)
        if not events:
            continue
        rel = _instrument_relevance(a, cfg)
        age_h = _article_age_hours(a, now_utc)
        try:
            mag = abs(float(a.get("overall_sentiment_score") or 0.0))
        except (TypeError, ValueError):
            mag = 0.0
        for etype in events:
            direction = C.DIRECTION_MATRIX.get(etype, {}).get(instrument)
            if not direction:
                continue          # matrix already scopes BOE->FTSE, BOJ->NIKKEI etc.
            rank = C.EVENT_RANK[etype]
            is_macro = etype in C.SCHEDULED_MACRO_EVENTS
            dec = C.decay_factor(age_h, is_macro)
            # Global macro events (geopolitical/Fed/data) move ALL matrix instruments even
            # when the article carries no instrument-specific ticker tag, so floor relevance.
            eff_rel = max(rel, 0.3)
            conf_scale = 0.6 + 0.4 * min(1.0, eff_rel + mag)   # 0.6..1.0
            strength = min(C.PER_DRIVER_CAP, _BASE_BY_RANK[rank] * dec * conf_scale)
            drivers.append({"event_type": etype, "rank": rank, "direction": direction,
                            "strength": strength, "decay": round(dec, 2), "age_h": round(age_h, 1),
                            "headline": (a.get("title") or "")[:120]})

    # Calendar (rank 4): month/quarter end -> mild BEARISH on strong trenders (equities default).
    for ce in (calendar_events or []):
        etype = ce.get("event_type")
        if etype in ("MONTH_END", "QUARTER_END"):
            trenders = month_trenders or {"FTSE", "US500", "NIKKEI"}
            if instrument in trenders:
                dec = 1.0
                drivers.append({"event_type": etype, "rank": C.RANK_MARKET_FLOW,
                                "direction": C.BEAR, "strength": _BASE_BY_RANK[C.RANK_MARKET_FLOW] * dec,
                                "decay": 1.0, "age_h": 0.0, "headline": ce.get("headline", etype)})

    if not drivers:
        return _neutral(instrument, now_utc)

    # --- Conflict resolution by rank (brief: the HIGHEST rank WINS the direction) --
    top_rank = min(d["rank"] for d in drivers)
    top_drivers = [d for d in drivers if d["rank"] == top_rank]
    # same-rank conflict -> signed AVERAGE of the winning tier (brief step 2 rule).
    tier_signed = sum(d["direction"] * d["strength"] for d in top_drivers) / len(top_drivers)
    net_dir = 1 if tier_signed > 0 else (-1 if tier_signed < 0 else 0)
    if net_dir == 0:
        return _neutral(instrument, now_utc, drivers)   # top tier internally cancels

    # Modifier = winning tier's net strength + lower-rank drivers that AGREE with it.
    # A lower-rank OPPOSING driver never cancels a higher rank -- Rank 1 wins.
    modifier = abs(tier_signed)
    for d in drivers:
        if d["rank"] > top_rank and d["direction"] == net_dir:
            modifier += d["strength"]

    # Uncertainty discount (brief step: any conflicting signal -> -50%). The Gold lesson.
    mixed = any(d["direction"] != net_dir for d in drivers)
    if mixed:
        modifier *= 0.5

    modifier = min(C.MODIFIER_CAP, modifier)
    signed = round(net_dir * modifier)   # + => supports LONG
    if abs(signed) < 3:
        return _neutral(instrument, now_utc, drivers)

    mag = abs(signed)
    conf = "HIGH" if mag >= 20 else ("MEDIUM" if mag >= 10 else "LOW")
    if mixed and conf == "HIGH":
        conf = "MEDIUM"
    ranked = sorted(drivers, key=lambda d: (d["rank"], -d["strength"]))
    primary = ranked[0]
    secondary = next((d for d in ranked[1:] if d["event_type"] != primary["event_type"]), None)

    return {
        "instrument": instrument,
        "direction": "BULLISH" if net_dir > 0 else "BEARISH",
        "confidence": conf,
        "modifier": signed,                       # signed: + supports LONG, - supports SHORT
        "mixed": mixed,
        "primary_event": primary["headline"] or primary["event_type"],
        "primary_type": primary["event_type"],
        "secondary_event": (secondary["headline"] if secondary else None),
        "decay_factor": primary["decay"],
        "drivers": ranked,
        "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _neutral(instrument, now_utc, drivers=None):
    return {
        "instrument": instrument, "direction": "NEUTRAL", "confidence": "NEUTRAL",
        "modifier": 0, "mixed": bool(drivers), "primary_event": None, "primary_type": None,
        "secondary_event": None, "decay_factor": 0.0, "drivers": drivers or [],
        "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
