"""
Guinevere 2.0 -- signal engine (Commission 018 core intelligence 31 Jul 2026;
ATTRIBUTION FIX 3 Aug 2026).

Turns raw Alpha Vantage news + calendar context into ONE net directional signal per
instrument, via: event classification -> direction matrix -> decay -> conflict
resolution -> confidence modifier (clamped +/-25). Pure/deterministic and unit-tested;
no network or file I/O here. ALL TIMES UTC.

ATTRIBUTION FIX (3 Aug 2026, Nick-approved) -- stops the wider macro feed converting
NEUTRAL into confidently-wrong:
  1. Keyword scans are WORD-BOUNDARY matched, so "fed" no longer matches "FedEx",
     "easing" no longer matches "increasing", "war" no longer matches "warehouse".
     Central-bank classification needs a NAMED bank OR a rate-ACTION phrase; bare
     "strike" no longer triggers geopolitical (only military-context strikes do).
  2. The eff_rel = max(rel, 0.3) floor is REMOVED for non-global events -- a zero-
     relevance article contributes ZERO.
  3. Instrument-relevance gating: a non-global event drives an instrument ONLY if the
     article is genuinely relevant to it (matching ticker relevance OR an instrument
     keyword). Genuinely GLOBAL scheduled-macro central-bank decisions still route to
     all matrix instruments as designed (they keep the relevance floor).
"""

import re
from datetime import datetime, timezone

from . import config as C


# Base modifier magnitude by event rank tier (before decay + sentiment scaling).
_BASE_BY_RANK = {
    C.RANK_SCHEDULED_MACRO: 22,
    C.RANK_GEOPOLITICAL:    20,
    C.RANK_ECON_DATA:       12,
    C.RANK_MARKET_FLOW:      8,
}

# Relevance floor kept ONLY for genuinely-global scheduled-macro central-bank decisions,
# which move every matrix instrument by design even without an instrument-specific term.
_GLOBAL_MACRO_FLOOR = 0.3

_WORD_RE = {}


def _contains(text, kws):
    """Word-boundary keyword scan (3 Aug 2026): matches whole words/phrases only, so short
    keywords ('fed', 'war', 'easing', 'dow') never false-match inside longer words."""
    for k in kws:
        pat = _WORD_RE.get(k)
        if pat is None:
            pat = re.compile(r'\b' + re.escape(k) + r'\b')
            _WORD_RE[k] = pat
        if pat.search(text):
            return True
    return False


def classify_article(article):
    """Return a list of event_type strings this article represents (may be empty).
    Uses word-boundary title+summary keyword scan; AV overall sentiment disambiguates
    hawkish/dovish and strong/weak when the keywords alone are directionless."""
    text = ((article.get("title") or "") + " " + (article.get("summary") or "")).lower()
    try:
        av_sent = float(article.get("overall_sentiment_score") or 0.0)
    except (TypeError, ValueError):
        av_sent = 0.0
    events = []

    # RANK 1 -- central bank / scheduled macro. Requires a NAMED bank AND a rate-ACTION
    # phrase (true compound context) -- a mere "the Fed" name-drop in an opinion piece is
    # NOT a policy decision and must not create a global macro driver.
    if _contains(text, C.KW_CB_NAMES) and _contains(text, C.KW_CB_ACTION):
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

    # RANK 3 -- oil supply / production (3 Aug 2026). Gated on oil context so only genuine
    # crude-supply stories (OPEC+ output, glut, production cut) classify -- not "Toyota cuts
    # production". OIL_SUPPLY_UP = more supply = bearish; OIL_SUPPLY_DOWN = cut = bullish.
    if _contains(text, C.KW_OIL_CONTEXT):
        if _contains(text, C.KW_SUPPLY_UP):
            events.append("OIL_SUPPLY_UP")
        elif _contains(text, C.KW_SUPPLY_DOWN):
            events.append("OIL_SUPPLY_DOWN")
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
    """0..1 relevance of an article to an instrument: max of AV ticker relevance and a
    WORD-BOUNDARY keyword match. 0.0 when the article names neither the instrument's
    tickers nor any of its keywords -- such articles no longer attribute (unless the event
    is a genuinely-global central-bank decision, handled in build_signal)."""
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
            # ATTRIBUTION GATE (3 Aug 2026): genuinely-global scheduled-macro central-bank
            # decisions route to all matrix instruments (relevance floor kept). Everything
            # else (geopolitical, data) must be genuinely relevant to THIS instrument --
            # zero relevance contributes zero (no floor). Kills confident-noise.
            if is_macro:
                eff_rel = max(rel, _GLOBAL_MACRO_FLOOR)
            else:
                if rel <= 0.0:
                    continue
                eff_rel = rel
            dec = C.decay_factor(age_h, is_macro)
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
    gdir = "BULLISH" if net_dir > 0 else "BEARISH"
    favoured = "LONG" if net_dir > 0 else "SHORT"

    # GUINEVERE BOOSTS AND REDIRECTS -- SHE NEVER BLOCKS (31 Jul 2026, Archie/Nick).
    # `modifier` is a POSITIVE boost (0..25) applied ONLY to a trade in the FAVOURED
    # direction. A trade in the OPPOSITE direction gets ZERO -- never a negative penalty.
    # Instead Arthur is REDIRECTED: look for a favoured-direction setup; if none, trade the
    # opposing setup normally at full indicator confidence.
    return {
        "instrument": instrument,
        "direction": gdir,
        "guinevere_direction": gdir,
        "confidence": conf,
        "confidence_level": conf,
        "modifier": mag,                    # positive-only boost for the FAVOURED direction
        "favoured": favoured,               # LONG / SHORT -- the direction Guinevere boosts
        "boost_direction": favoured,
        "redirect_direction": favoured,     # if the setup opposes Guinevere, look for THIS
        "redirect_modifier": mag,           # boost to apply if a favoured-direction setup appears
        "opposing_modifier": 0,             # explicit: the opposing direction is NEVER penalised
        "mixed": mixed,
        "primary_event": primary["headline"] or primary["event_type"],
        "primary_type": primary["event_type"],
        "secondary_event": (secondary["headline"] if secondary else None),
        "neutral_reason": ("Guinevere favours %s (+%d). Opposing setups get NO penalty -- "
                           "look for a %s setup; if none, trade normally."
                           % (favoured, mag, favoured)),
        "decay_factor": primary["decay"],
        "drivers": ranked,
        "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _neutral(instrument, now_utc, drivers=None):
    return {
        "instrument": instrument, "direction": "NEUTRAL", "guinevere_direction": "NEUTRAL",
        "confidence": "NEUTRAL", "confidence_level": "NEUTRAL",
        "modifier": 0, "favoured": "", "boost_direction": "", "redirect_direction": "",
        "redirect_modifier": 0, "opposing_modifier": 0,
        "mixed": bool(drivers), "primary_event": None, "primary_type": None,
        "secondary_event": None,
        "neutral_reason": "No significant news -- your technical indicators carry full weight.",
        "decay_factor": 0.0, "drivers": drivers or [],
        "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
