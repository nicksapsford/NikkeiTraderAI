"""
Guinevere 2.0 -- configuration (Commission 018 build, 31 Jul 2026).

Instrument -> Alpha Vantage query config, the event-type -> per-instrument DIRECTION
matrix, conflict-resolution RANKS, signal DECAY curve, and the confidence-modifier scale.
Vendored identically into each of the 6 Arthur systems; behaviour is driven entirely by
data here so tuning never touches logic. ALL TIMES UTC.
"""

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
KW_GEOPOLITICAL = ["attack", "strike", "war", "invasion", "sanction", "conflict",
                   "military", "troops", "missile", "drone", "airstrike", "escalat"]
KW_DEESCALATION = ["ceasefire", "truce", "peace deal", "de-escalat", "diplomacy", "withdraw"]
KW_CENTRAL_BANK = ["fed", "fomc", "rate hike", "rate cut", "boe", "bank of england",
                   "boj", "bank of japan", "hawkish", "dovish", "powell", "tightening",
                   "easing", "interest rate", "monetary policy"]
KW_HAWKISH = ["hawkish", "rate hike", "hike", "tightening", "higher for longer", "inflation concern"]
KW_DOVISH  = ["dovish", "rate cut", "cut", "easing", "stimulus", "pause"]
KW_ECON_DATA = ["gdp", "pmi", "inflation", "cpi", "unemployment", "jobs", "payroll",
                "nfp", "retail sales", "manufacturing", "consumer confidence"]
KW_STRONG_DATA = ["beat", "stronger", "higher than expected", "rose", "surge", "robust", "tops"]
KW_WEAK_DATA   = ["miss", "weaker", "lower than expected", "fell", "slump", "disappoint", "contract"]

# --- Per-instrument Alpha Vantage query config --------------------------------
# tickers: AV NEWS_SENTIMENT tickers proven to return tagged articles (tested 31 Jul).
# topics:  AV topic filters. keywords: title/summary scan to attribute events to us.
INSTRUMENTS = {
    "FTSE":   {"tickers": ["FOREX:GBP"], "topics": ["financial_markets", "economy_macro"],
               "keywords": ["ftse", "uk ", "u.k.", "london", "britain", "sterling", "gilt", "boe"]},
    "GOLD":   {"tickers": ["GLD"], "topics": ["economy_macro", "economy_monetary"],
               "keywords": ["gold", "xau", "safe haven", "safe-haven", "bullion", "precious metal"]},
    "OIL":    {"tickers": ["BNO", "USO"], "topics": ["energy_transportation"],
               "keywords": ["oil", "crude", "brent", "opec", "wti", "barrel", "refinery", "petroleum"]},
    "US500":  {"tickers": ["SPY"], "topics": ["financial_markets", "economy_macro", "economy_monetary"],
               "keywords": ["s&p", "sp500", "s&p 500", "wall street", "nasdaq", "dow", "us stock"]},
    "NIKKEI": {"tickers": ["EWJ", "FOREX:JPY"], "topics": ["financial_markets"],
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
    "MONTH_END": RANK_MARKET_FLOW, "QUARTER_END": RANK_MARKET_FLOW,
}
SCHEDULED_MACRO_EVENTS = {"FED_HAWKISH", "FED_DOVISH", "BOE_HAWKISH", "BOE_DOVISH",
                          "BOJ_HAWKISH", "BOJ_DOVISH"}

# Polling cadence (seconds) + cache TTL.
POLL_IN_SESSION_SEC = 300       # 5 min in-session
POLL_OFF_SESSION_SEC = 1800     # 30 min off-session
CACHE_TTL_SEC = 900             # 15-min cache (never hit API on every Arthur tick)
