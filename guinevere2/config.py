"""
Guinevere 2.0 -- configuration (Commission 018 build, 31 Jul 2026;
Uther amalgamation, Commission 019, 3 Aug 2026).

Instrument -> Alpha Vantage query config, the event-type -> per-instrument DIRECTION
matrix, conflict-resolution RANKS, signal DECAY curve, and the confidence-modifier scale.
Vendored identically into each of the 6 Arthur systems; behaviour is driven entirely by
data here so tuning never touches logic. ALL TIMES UTC.
"""

import os

# --- Signal source (Commission 019 amalgamation, 3 Aug 2026) ------------------
# "uther"   = consume Uther's AI assessments as event drivers (DEFAULT; retires keywords).
# "keyword" = legacy in-package keyword classifier (dormant fallback, kept ~2 weeks then cut).
# Uther reads the world in context; this engine still applies decay / conflict resolution /
# caps / the five rules -- the discipline layer is unchanged, only the event SOURCE differs.
SIGNAL_SOURCE = (os.getenv("GUINEVERE_SIGNAL_SOURCE", "uther") or "uther").strip().lower()

# Confidence-weighted driver strength in Uther mode (before decay/conflict). Safety valve
# while Uther's track record builds: a wrong LOW moves Arthur +5; a wrong HIGH stays <= +25
# and still cannot block or force anything (Arthur needs 60+, all Lancelot controls enforced).
UTHER_CONF_BASE = {"HIGH": 22, "MEDIUM": 13, "LOW": 5}   # brief bands: HIGH 15-25/MED 10-15/LOW<=5
UTHER_MAX_AGE_H = 8.0     # ignore assessments older than this (decay has zeroed them)
UTHER_STALE_MIN = 45      # signals file older than this (minutes) -> feed shown STALE (still decays)

# Path to Uther's amalgamation feed (logs/uther_signals.json). Default: sibling UtherAI repo
# (all Albion repos are siblings on the desk -- same pattern RoundTable uses). Override with
# GUINEVERE_UTHER_SIGNALS. Fail-safe: unreadable -> no drivers -> NEUTRAL (NEVER keyword).
_HERE = os.path.dirname(os.path.abspath(__file__))
UTHER_SIGNALS_PATH = os.getenv("GUINEVERE_UTHER_SIGNALS") or os.path.normpath(
    os.path.join(_HERE, os.pardir, os.pardir, "UtherAI", "logs", "uther_signals.json"))

# --- Confidence-modifier scale (Principle 14: context, never a filter) --------
MODIFIER_CAP = 25          # hard cap +/- in either direction (indicators stay primary)
PER_DRIVER_CAP = 15        # any single driver caps here before summing
MODIFIER = {               # (aligned_low, aligned_high) -- magnitude by confidence tier
    "HIGH":   (20, 25),
    "MEDIUM": (10, 15),
    "LOW":    (5, 10),
    "NEUTRAL": (0, 0),
}

# --- Conflict-resolution priority ranks (1 = highest, wins on conflict) --------
RANK_SCHEDULED_MACRO = 1   # Fed/BoE/BoJ/NFP/CPI -- reshape the regime, override the rest
RANK_GEOPOLITICAL    = 2   # war/strike/sanctions -- override minor news + calendar
RANK_ECON_DATA       = 3   # GDP/PMI/inflation prints
RANK_MARKET_FLOW     = 4   # month/quarter end, OpEx -- lowest, still useful context

# --- Signal decay by age in hours (Section 2.6 of Commission 018) -------------
# Fraction of full strength retained. Scheduled macro is exempt (see DECAY_MACRO).
def decay_factor(age_hours, is_scheduled_macro=False):
    if is_scheduled_macro:
        return 1.0 if age_hours <= 24 else 0.25
    if age_hours <= 1:   return 1.0
    if age_hours <= 3:   return 0.75
    if age_hours <= 6:   return 0.5
    return 0.25

# --- Keyword sets for event classification ------------------------------------
# NOTE (3 Aug 2026 feed fix): keyword scans are now WORD-BOUNDARY matched (signal_engine
# ._contains), so "fed" no longer matches "FedEx", "easing" no longer matches "increasing",
# "war" no longer matches "warehouse". Bare "strike" removed (matched "strike a deal");
# only military-context strikes trigger geopolitical now.
KW_GEOPOLITICAL = ["war", "invasion", "sanction", "conflict", "military", "troops",
                   "missile", "drone", "airstrike", "air strike", "military strike",
                   "missile strike", "drone strike", "escalat", "attack"]
KW_DEESCALATION = ["ceasefire", "truce", "peace deal", "peace talks", "de-escalat",
                   "diplomacy", "withdraw"]
# Central-bank classification now needs a NAMED bank OR a rate-ACTION phrase (compound
# context) -- a passing mention of "interest rate" no longer flags an equity story as Fed.
KW_CB_NAMES  = ["fed", "fomc", "federal reserve", "boe", "bank of england", "boj",
                "bank of japan", "ecb", "powell", "central bank"]
KW_CB_ACTION = ["rate hike", "rate hikes", "rate cut", "rate cuts", "rate rise", "rate rises",
                "rate decision", "rate hold", "hold rates", "holds rates", "held rates",
                "raise rates", "raises rates", "raised rates", "cut rates", "cuts rates",
                "lower rates", "lowered rates", "hike rates", "hikes rates", "hiked rates",
                "basis point", "basis points", "hawkish", "dovish", "tightening",
                "monetary easing", "quantitative", "rates steady", "rates unchanged",
                "rate unchanged"]
KW_CENTRAL_BANK = KW_CB_NAMES + KW_CB_ACTION   # kept for back-compat / external refs
KW_HAWKISH = ["hawkish", "rate hike", "rate rise", "hike rates", "tightening",
              "higher for longer", "inflation concern"]
KW_DOVISH  = ["dovish", "rate cut", "cut rates", "monetary easing", "stimulus", "rate hold"]
KW_ECON_DATA = ["gdp", "pmi", "inflation", "cpi", "unemployment", "jobs", "payroll",
                "nfp", "retail sales", "manufacturing", "consumer confidence"]
KW_STRONG_DATA = ["beat", "stronger", "higher than expected", "rose", "surge", "robust", "tops"]
KW_WEAK_DATA   = ["miss", "weaker", "lower than expected", "fell", "slump", "disappoint", "contract"]

# Oil supply / production (3 Aug 2026 addition, Nick-approved). OPEC+/output news the wider
# ticker+topic feed now surfaces but which no event type previously caught (the OPEC half of
# the 3 Aug incident). GATED on oil context (KW_OIL_CONTEXT) so a non-oil "cuts production"
# story never triggers; the build_signal relevance gate is a second guard.
KW_OIL_CONTEXT = ["oil", "crude", "opec", "brent", "wti", "barrel", "petroleum", "refinery"]
KW_SUPPLY_UP = ["boost production", "boost output", "raise output", "raise production",
                "increase production", "increase output", "production increase",
                "output increase", "more barrels", "supply glut", "oversupply",
                "ramp up production", "output hike", "higher output", "increase supply",
                "boost oil production", "raise oil production", "adds barrels"]
KW_SUPPLY_DOWN = ["production cut", "output cut", "cut production", "cut output",
                  "reduce output", "reduce production", "supply cut", "curb output",
                  "curb production", "slash production", "production curb", "lower output",
                  "cut oil production", "cut supply", "reduce supply", "output reduction"]

# --- Per-instrument Alpha Vantage query config --------------------------------
# tickers: AV NEWS_SENTIMENT tickers proven to return tagged articles (tested 31 Jul).
# topics:  AV topic filters. keywords: title/summary scan to attribute events to us.
INSTRUMENTS = {
    "FTSE":   {"tickers": ["FOREX:GBP"], "topics": ["financial_markets", "economy_macro"],
               "keywords": ["ftse", "footsie", "uk", "london", "britain", "british", "sterling", "gilt", "boe"]},
    "GOLD":   {"tickers": ["GLD"], "topics": ["economy_macro", "economy_monetary"],
               "keywords": ["gold", "xau", "safe haven", "safe-haven", "bullion", "precious metal"]},
    "OIL":    {"tickers": ["BNO", "USO"], "topics": ["energy_transportation"],
               "keywords": ["oil", "crude", "brent", "opec", "wti", "barrel", "refinery", "petroleum", "hormuz"]},
    "US500":  {"tickers": ["SPY"], "topics": ["financial_markets", "economy_macro", "economy_monetary"],
               "keywords": ["s&p", "sp500", "s&p 500", "wall street", "nasdaq", "dow", "us stock", "us stocks"]},
    # NIKKEI (3 Aug 2026 feed fix): the combined tickers=EWJ,FOREX:JPY was REJECTED by AV
    # ("Invalid inputs" -- mixing an equity ETF with a FOREX ticker). Dropped to the single
    # valid + FRESH FOREX:JPY; Japan macro now comes via topics=economy_macro/financial_markets
    # + the keyword attribution below (EWJ alone returned only week-old articles).
    "NIKKEI": {"tickers": ["FOREX:JPY"], "topics": ["economy_macro", "financial_markets"],
               "keywords": ["nikkei", "japan", "tokyo", "boj", "yen", "japanese"]},
}

# --- Event-type -> per-instrument DIRECTION matrix (Section 2 / Part 2B step 4) -
# Each maps to +1 (BULLISH), -1 (BEARISH), 0 (NEUTRAL) per instrument.
BULL, BEAR, FLAT = 1, -1, 0
DIRECTION_MATRIX = {
    "GEO_ESCALATION":  {"OIL": BULL, "GOLD": BULL, "FTSE": BEAR, "US500": BEAR, "NIKKEI": BEAR},
    "GEO_DEESCALATION":{"OIL": BEAR, "GOLD": BEAR, "FTSE": BULL, "US500": BULL, "NIKKEI": BULL},
    "FED_HAWKISH":     {"GOLD": BEAR, "US500": BEAR, "FTSE": FLAT, "OIL": BEAR, "NIKKEI": FLAT},
    "FED_DOVISH":      {"GOLD": BULL, "US500": BULL, "FTSE": BULL, "OIL": BULL, "NIKKEI": BULL},
    "BOE_HAWKISH":     {"FTSE": BEAR},
    "BOE_DOVISH":      {"FTSE": BULL},
    "BOJ_HAWKISH":     {"NIKKEI": BEAR},
    "BOJ_DOVISH":      {"NIKKEI": BULL},
    "DATA_STRONG":     {"US500": BULL, "FTSE": BULL, "OIL": BULL, "GOLD": BEAR, "NIKKEI": BULL},
    "DATA_WEAK":       {"US500": BEAR, "FTSE": BEAR, "OIL": BEAR, "GOLD": BULL, "NIKKEI": BEAR},
    "OIL_SUPPLY_UP":   {"OIL": BEAR},   # more supply (OPEC+ boost / glut) -> bearish crude
    "OIL_SUPPLY_DOWN": {"OIL": BULL},   # supply cut (OPEC+ curb)          -> bullish crude
    "MONTH_END":       {},   # applied to the strongest trender at runtime (BEARISH bias)
    "QUARTER_END":     {},
}

# Which rank each event type carries.
EVENT_RANK = {
    "FED_HAWKISH": RANK_SCHEDULED_MACRO, "FED_DOVISH": RANK_SCHEDULED_MACRO,
    "BOE_HAWKISH": RANK_SCHEDULED_MACRO, "BOE_DOVISH": RANK_SCHEDULED_MACRO,
    "BOJ_HAWKISH": RANK_SCHEDULED_MACRO, "BOJ_DOVISH": RANK_SCHEDULED_MACRO,
    "GEO_ESCALATION": RANK_GEOPOLITICAL, "GEO_DEESCALATION": RANK_GEOPOLITICAL,
    "DATA_STRONG": RANK_ECON_DATA, "DATA_WEAK": RANK_ECON_DATA,
    "OIL_SUPPLY_UP": RANK_ECON_DATA, "OIL_SUPPLY_DOWN": RANK_ECON_DATA,
    "MONTH_END": RANK_MARKET_FLOW, "QUARTER_END": RANK_MARKET_FLOW,
}
SCHEDULED_MACRO_EVENTS = {"FED_HAWKISH", "FED_DOVISH", "BOE_HAWKISH", "BOE_DOVISH",
                          "BOJ_HAWKISH", "BOJ_DOVISH"}

# Polling cadence (seconds) + cache TTL.
POLL_IN_SESSION_SEC = 300       # 5 min in-session
POLL_OFF_SESSION_SEC = 1800     # 30 min off-session
CACHE_TTL_SEC = 900             # 15-min cache (never hit API on every Arthur tick)

# --- Universal macro sweep (Commission 018 FEED FIX, 3 Aug 2026) --------------
# ONE ticker-less macro query per poll cycle, merged into EVERY instrument's feed and routed
# via DIRECTION_MATRIX. Catches geopolitical/macro stories (Iran, OPEC, Fed) that carry no
# instrument-specific ticker tag -- the exact gap that missed the 3 Aug Oil catalysts. The
# per-instrument `topics` above are now sent too (they were configured but never queried).
# Shared across instruments via MACRO_CACHE_KEY so it runs once per TTL. See news_fetcher.
MACRO_TOPICS = "economy_macro"
MACRO_TIME_FROM_HOURS = 6       # time_from = now - 6h on the MACRO query ONLY (Nick-approved)
MACRO_LIMIT = 50
MACRO_CACHE_KEY = "news:__MACRO__"

# --- Feed-health thresholds (dashboard indicator, by newest ARTICLE age) ------
FEED_GREEN_MAX_H = 6            # GREEN: newest article < 6h
FEED_AMBER_MAX_H = 24          # AMBER: 6-24h  |  RED: > 24h or empty/failed
