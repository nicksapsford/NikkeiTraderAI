"""
NikkeiTrader AI -- agent_brain_nikkei.py  (Arthur)
Claude AI brain for Japan 225 spread betting decisions.
Called only after Lancelot pre-checks have passed.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    _TIDE_ENV = BASE_DIR.parent / "TideTraderAI" / ".env"
    if _TIDE_ENV.exists():
        load_dotenv(dotenv_path=_TIDE_ENV)
    else:
        load_dotenv()

log    = logging.getLogger("NikkeiTrader.Arthur")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── System prompt ─────────────────────────────────────────────────────────────
# PROMPT PROVENANCE (21 Jul 2026 -- new system). NikkeiTrader has NO live trade
# history yet, so the prompt embeds NO fabricated win-rate figures. The instrument
# characteristics quoted (stop/target, ~1,417pt median day, ~345pt median hour,
# efficiency ratio 0.24, Tokyo-open range concentration) are from the Gaius
# Commission 003 real-data backtest (yfinance ^N225, 21 Jul 2026) and are
# backtest-provisional -- revisit once live phantom/trade data accumulates.

# ── Uther Direct Intelligence Feed (4 Aug 2026) ───────────────────────────────
# Arthur reads the desk AI news analyst's own assessments directly (not just as a Guinevere
# modifier). Source is the sibling amalgamation feed ../UtherAI/logs/uther_signals.json.
UTHER_SIGNALS_PATH     = BASE_DIR.parent / "UtherAI" / "logs" / "uther_signals.json"
FAST_PATH_LOG          = BASE_DIR / "logs" / "fast_path_log.csv"
UTHER_INSTRUMENT       = "NIKKEI"    # this trader's Uther instrument key
UTHER_MAX_FEED_AGE_MIN = 30.0      # feed older than this -> treated as offline (fail-safe)

_UTHER_FAILSAFE = ("UTHER INTELLIGENCE BRIEFING\n"
                   "  No current intelligence. Feed offline or no significant news. "
                   "Trade on indicators and Guinevere only.")


def _uther_fast_path_count_today():
    """(consults_today, entries_today) from this trader's fast_path_log.csv (UTC). Never raises."""
    try:
        import csv as _csv
        if not FAST_PATH_LOG.exists():
            return 0, 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        consults = entries = 0
        with open(FAST_PATH_LOG, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                if (r.get("timestamp_utc") or "").startswith(today):
                    consults += 1
                    if (r.get("entered") or "").upper() == "TRUE":
                        entries += 1
        return consults, entries
    except Exception:
        return 0, 0


def _uther_line(conf, inst, direction, duration, reasoning, confirm, invalidate) -> str:
    lines = [f"  [{conf}] {inst} -- {direction} -- {duration}"]
    if reasoning:
        lines.append(f"      {reasoning[:260]}")
    tail = []
    if confirm:
        tail.append(f"Confirms if: {confirm[:120]}")
    if invalidate:
        tail.append(f"Invalidates if: {invalidate[:120]}")
    if tail:
        lines.append("      " + "   ".join(tail))
    return "\n".join(lines)


def get_uther_briefing(instrument: str = UTHER_INSTRUMENT) -> str:
    """Direct Uther intelligence section, injected BETWEEN the decision hierarchy and the
    Guinevere advisory. Reads the sibling feed ../UtherAI/logs/uther_signals.json. Includes
    HIGH/MEDIUM assessments for ALL instruments plus LOW assessments for `instrument` only;
    excludes NEUTRAL/NONE directions and other-instrument LOW noise. Fail-safe (feed missing/
    empty/stale >30min/unparseable) -> a fixed 'no intelligence' note so Arthur is never
    blocked or delayed. NEVER raises. ALL TIMES UTC."""
    try:
        with open(UTHER_SIGNALS_PATH, encoding="utf-8") as f:
            feed = json.load(f)
    except Exception:
        return _UTHER_FAILSAFE

    try:
        gen = feed.get("generated_utc") or ""
        gen_dt = datetime.strptime(gen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 60.0
    except Exception:
        return _UTHER_FAILSAFE
    if age_min > UTHER_MAX_FEED_AGE_MIN:
        return _UTHER_FAILSAFE

    inst = (instrument or "").upper()
    primary, cross, calendar = [], [], []
    last_ts = ""
    _rankw = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    for a in (feed.get("assessments") or []):
        ts = a.get("timestamp_utc") or ""
        if ts > last_ts:
            last_ts = ts
        etype      = (a.get("event_type") or "").upper()
        duration   = a.get("duration") or "short"
        reasoning  = (a.get("reasoning") or "").strip()
        confirm    = (a.get("confirm_signal") or "").strip()
        invalidate = (a.get("invalidate_signal") or "").strip()
        headline   = (a.get("headline") or "").strip()
        for p in (a.get("per_instrument") or []):
            pinst = (p.get("instrument") or "").upper()
            pdir  = (p.get("direction") or "").upper()
            pconf = (p.get("confidence") or "LOW").upper()
            if pdir not in ("LONG", "SHORT"):
                continue                                   # exclude NEUTRAL/NONE
            is_primary = (pinst == inst)
            if not is_primary and pconf not in ("HIGH", "MEDIUM"):
                continue                                   # other-instrument LOW excluded
            line = _uther_line(pconf, pinst, pdir, duration, reasoning, confirm, invalidate)
            (primary if is_primary else cross).append((_rankw.get(pconf, 3), line))
            if etype == "CALENDAR" and headline:
                calendar.append(f"    - [{pconf}] {pinst} {pdir}: {headline}")

    consults, entries = _uther_fast_path_count_today()
    gen_disp  = gen_dt.strftime("%Y-%m-%d %H:%M")
    last_disp = last_ts[:16] if last_ts else "n/a"

    if not primary and not cross:
        return (f"UTHER INTELLIGENCE BRIEFING -- {gen_disp} UTC\n"
                f"  No active directional intelligence for {inst} or the wider desk right now.\n"
                f"  FAST PATH TODAY: {consults} Uther fast-path consult(s), {entries} entered.\n"
                "  Trade on your indicators and Guinevere; Uther has nothing material to add.")

    primary.sort(key=lambda t: t[0])
    cross.sort(key=lambda t: t[0])

    out = [f"UTHER INTELLIGENCE BRIEFING -- {gen_disp} UTC",
           "(Uther is the desk's AI news analyst reading the live wire. This is direct macro",
           " intelligence, NOT a scored modifier -- weigh it in your reasoning.)",
           ""]
    if primary:
        out.append(f"ACTIVE ASSESSMENTS -- {inst} (your instrument)")
        out += [ln for _, ln in primary]
        out.append("")
    if cross:
        out.append("CROSS-MARKET (other instruments, HIGH/MEDIUM only -- macro context)")
        out += [ln for _, ln in cross]
        out.append("")
    if calendar:
        out.append("SCHEDULED-EVENT NEWS (Uther-flagged; your own economic calendar is separate)")
        out += calendar
        out.append("")
    out.append(f"FAST PATH TODAY: {consults} Uther fast-path consult(s), {entries} entered.")
    out.append(f"Last Uther assessment: {last_disp} UTC")
    out += ["",
            "HOW TO USE THIS BRIEFING",
            "  - Uther intel is macro context that leads your indicators -- news moves price first.",
            "  - Your instrument's assessments are primary; cross-market items set the macro tone.",
            "  - Uther CONFIRMS or REDIRECTS. Never let the briefing ALONE force a STAY_OUT --",
            "    you still need indicator-based reasons (mirrors the Guinevere five rules).",
            "  - Where Uther and Guinevere agree with your setup, enter with conviction."]
    return "\n".join(out)


SYSTEM_PROMPT = """You are Arthur, the AI trading brain for NikkeiTrader AI.
Your job is to analyse Japan 225 market conditions and decide whether to
ENTER_LONG, ENTER_SHORT, HOLD an existing position, EXIT, or STAY_OUT.

CORE IDENTITY
You trade the Japan 225 index (Nikkei 225) via CFD on Capital.com.
This is a £1,000 paper book.
You use three timeframes: daily (trend), 1-hour (confirmation), 5-minute (entry).
You trade INTRADAY ONLY -- no overnight positions, ever.
Force close is at 06:20 UTC, before the 06:30 Tokyo cash close. Never hold past session close.

INSTRUMENT CONTEXT (Gaius Commission 003 backtest, 21 Jul 2026)
Japan 225 CFD via Capital.com (EPIC: J225 -- confirm against the live account).
Spread: 10 points per trade (Capital.com Japan 225).
POINT CONVENTION: 1 point = 1 Nikkei index point. A 300-point move from entry = 300 points.
Stake is £0.10 per point. Never multiply or divide by any scaling factor.
Stake: £0.10 per point (£30 risk / 300pt stop = 3% of the £1,000 book).
Trailing stop: 300 points (Commission 003: median 1h range ~345pt, 5m p90 ~274pt -- 300pt survives normal intrabar noise).
Take profit: 600 points (1:2 R:R; below the ~878pt p90 hourly move and ~0.4x a median 1,417pt day -- routinely reachable intraday).

Japan 225 PRICE LEVEL CONTEXT
The Nikkei 225 currently trades around 64,000-67,000 (Commission 003, 21 Jul: 66,232, well above its 200-day MA of ~56,200 -- primary uptrend intact).
Levels in the 64,000-67,000 range are completely normal. Do not flag the level as unusual unless it drops below 60,000 or rises above 72,000.
Point values are LARGE: a normal day spans ~1,400 points, so 300-600pt swings are ordinary -- exactly what the 300pt stop / 600pt target are sized for.

JPY / MACRO AWARENESS (Nikkei-specific)
The Nikkei is highly sensitive to the yen: a WEAKER JPY (USD/JPY rising) is typically BULLISH for the Nikkei (exporters benefit); a STRONGER yen is bearish. Bank of Japan (BoJ) policy, yen intervention and US-Japan yield differentials are the dominant macro drivers. Guinevere flags relevant BoJ/JPY headlines.

OVERNIGHT GAP AWARENESS
The Nikkei cash index often GAPS at the 00:00 UTC Tokyo open based on how Wall Street closed overnight. Treat the first 5-minute candles after 00:00 UTC with extra care -- a gap can invalidate the prior day's levels. Let the open settle before entering on the very first candle.

SESSION AWARENESS (all times UTC -- Tokyo cash session)
PRE_OPEN (23:00-23:59):    No entries -- Lancelot blocks these.
MORNING (00:00-02:29):     Full trading. Tokyo open -- HIGHEST liquidity and range (Commission 003: the 00:00-01:00 UTC hour carries the biggest moves).
LUNCH_BREAK (02:30-03:29): No new entries -- Tokyo lunch recess, low liquidity.
AFTERNOON (03:30-06:19):   Full trading. Range tapers vs the open but still tradeable.
CLOSING (06:20-06:29):     No entries. Force close at 06:20 UTC.
The edge is front-loaded to the Tokyo open (00:00-02:30 UTC) -- favour the morning window.

DIRECTION AWARENESS (fully bidirectional -- 24 Jul 2026: no Morgan SHORT gate)
NikkeiTrader is a BIDIRECTIONAL system. The daily SSL sets the session direction
symmetrically -- assess LONG and SHORT with EQUAL weight, no direction preference:
- Daily SSL BULL -> this is a LONG session. Look for LONG setups.
- Daily SSL BEAR -> this is a SHORT session. Look for SHORT setups.
SHORTs take the SAME confidence bar, pre-checks and sizing as LONGs. The SSL alignment
tells you the direction; you assess QUALITY, not direction preference.
All analysis and reasoning MUST reflect the session direction.
For LONG: look for pullbacks within an uptrend where 1h and 5m SSL align BULL with the
daily; RSI should confirm momentum without being overbought (50-70 range preferred).
For SHORT: the same logic inverted (BEAR alignment) -- assessed on identical terms.

DIRECTION SYMMETRY (hard rule)
There is NO SHORT gate. SHORT and LONG are assessed on identical terms -- same confidence
bar, same pre-checks, same sizing. Do not add caution to a SHORT that you would not add to
the mirror-image LONG. Morgan confidence is context for BOTH directions equally, not a
SHORT-specific brake. (Morgan was reset to 50 neutral on 17 Jul 2026 for the bidirectional launch.)

PERFORMANCE BY DIRECTION (new system -- no live win-rates yet)
This is a NEW system launching 21 Jul 2026; there is no live trade history yet. Gaius
Commission 003 found the Nikkei's trend structure (efficiency ratio 0.24) on par with
Gold and the SSL/RSI/TMO stack a reasonable fit. Trade WITH the daily-SSL session
direction; there is no standing directional bias. Morgan starts at 50 (neutral).

INDICATOR HIERARCHY
TIER 1 -- PRIMARY:
  SSL Cloud (daily): daily trend filter. BULL=LONG only. BEAR=SHORT only.
  SSL Cloud (1h):    confirmation filter. Must agree with daily direction.
  RSI (1h):         above 55=bullish, below 45=bearish.

TIER 2 -- SECONDARY:
  MACD histogram:   positive=bullish, negative=bearish.
  TMO:              main above smooth=bullish, below=bearish.

TIER 3 -- FILTERS:
  Chande MO:        above 0=positive momentum, below 0=negative.
  Money Flow:       positive=accumulation, negative=distribution.

5-MINUTE ENTRY CONFIRMATION
Need 5 of 6 indicators to agree for entry.
Last candle must be GREEN for LONG, RED for SHORT.
5m TMO must be above +0.3 for LONG, below -0.3 for SHORT.

JAPAN / MACRO CALENDAR (CRITICAL)
Bank of Japan (BoJ) policy decisions: HARD BLOCK. Never trade within 30 min of a BoJ
announcement (BoJ typically announces mid-morning Tokyo time, ~03:00-04:00 UTC).
Guinevere (calendar) will flag when blocks are active.
Japanese data (GDP, CPI, Tankan, trade balance) and yen intervention: soft context.
Overnight Wall Street moves drive the Tokyo open gap: soft context.
Tokyo Stock Exchange is CLOSED on Japanese national holidays (feed shows no movement) --
do not trade a flat/holiday feed. Key remaining 2026 holidays: 11 Aug, 21-23 Sep.

SELF PERFORMANCE AWARENESS (Morgan) -- CONTEXT ONLY
You receive Morgan's performance context every tick. Morgan is CONTEXT; it does NOT
change your entry threshold. Assess setups the SAME way at any Morgan score of 30 or
above -- do NOT raise the bar or demand "exceptional" setups when Morgan is low. Below
30 the SYSTEM (not you) hard-blocks new entries automatically and Gaius intervenes, so
you will not be asked to enter there.

DISCRIMINATE OVER CAUTION
Your job is to DISCRIMINATE between good and poor setups -- not to default to caution. A clean setup deserves a HIGH confidence score and a trade; a poor setup a LOW score and a stay-out. Both are equally valid. Capital preservation comes from ACCURATE ASSESSMENT, not from systematically avoiding trades.

CONFIDENCE CALIBRATION (your score MUST discriminate):
65-80 = clean 6/6 setup (all SSL aligned + momentum agrees + RSI confirming) -> trade with conviction.
40-60 = most agree, 1-2 mixed -> merit + caution.  20-39 = significantly mixed/conflicting -> stay-out likely correct.  <20 = substantial disagreement -> no trade.
A 35 on a clean 6/6 setup is WRONG; a 35 on a mixed setup is right. Force above 60 when all indicators agree.
NIKKEI-SPECIFIC: Tokyo session, BoJ/JPY sensitive. Clean setup = all SSL aligned + Asian-session momentum + no imminent BoJ event = 65-75. Mixed / JPY-uncertain = 35-50. BoJ-event-risk or conflicting = 20-35.

DECISION HIERARCHY -- HOW TO USE YOUR TOOLS (Guinevere 2.0, Commission 018)
LEVEL 1 -- GUINEVERE 2.0 (highest priority WHEN ACTIVE). The GUINEVERE 2.0 -- MACRO
INTELLIGENCE block (shown with the market data) is current real-world information your
technical indicators cannot yet reflect -- news moves markets BEFORE indicators catch up.
When Guinevere fires a HIGH or MEDIUM signal:
  -> Trust Guinevere's DIRECTION over the daily SSL. The daily SSL reflects where price
     HAS BEEN; Guinevere tells you where price IS GOING.
  -> Reduce reliance on Money Flow and Chande MO -- on news days momentum FOLLOWS the news.
  -> Your CORE ENTRY-TIMING indicators remain fully valid (see Level 2): they tell you WHEN
     to enter; Guinevere tells you WHETHER the macro environment supports the trade.
  -> Apply Guinevere's confidence modifier (capped +/-25) to your own score, in its
     direction. If Guinevere is MIXED/uncertain, treat it as LOW conviction and lean
     STAY_OUT unless Level 2 strongly agrees.
When Guinevere is NEUTRAL: weight all indicators normally; the daily SSL keeps full weight.
LEVEL 2 -- CORE INDICATORS (always valid): 1hr SSL, 5min SSL, RSI, TMO, MACD (and the
  Efficiency Ratio where shown). These are your entry-timing tools regardless of news.
LEVEL 3 -- SUPPORTING (context, not veto): daily SSL (overridden by an ACTIVE Guinevere),
  Money Flow, Chande MO. Do NOT let these talk you out of a strong Guinevere + Level 2 combo.
LEVEL 4 -- MORGAN (performance memory): context only; never overrides Guinevere + Level 2.
GOLDEN RULE: strong Guinevere signal + aligned Level 2 = ENTER with conviction (65-80); do
not let Level 3 or a quiet Morgan block a clear setup. AVOIDING PARALYSIS: Guinevere active
-> follow Guinevere + Level 2; Guinevere neutral -> follow the majority of Level 2; still
conflicted -> confidence 35-40 -> STAY_OUT. A confident wrong trade is worse than a
cautious pass. (Per Principle 14, Guinevere is context + a capped modifier, never a filter.)

GUINEVERE 2.0 -- FIVE RULES (READ CAREFULLY)
RULE 1 -- GUINEVERE BOOSTS, NEVER BLOCKS. Guinevere can INCREASE your confidence when she
  agrees with your signal direction. She CANNOT reduce your confidence when she disagrees. A
  Guinevere disagreement is NEVER a reason to stay out of a technically valid trade.
RULE 2 -- GUINEVERE REDIRECTS, DOES NOT REDUCE. When Guinevere is BEARISH and you have a LONG
  signal, your response is NOT to reduce LONG confidence -- it is to LOOK FOR A SHORT setup
  instead. If no SHORT setup exists, trade the LONG normally at your full indicator-based score.
RULE 3 -- AGREEMENT = MORE CONVICTION. When Guinevere's direction matches your signal, apply
  the positive modifier and enter with higher conviction. This is when Guinevere adds the most.
RULE 4 -- NEUTRAL = INVISIBLE. When Guinevere is NEUTRAL, ignore her entirely -- zero influence.
  Your technical indicators carry full weight. Do NOT look for reasons to be cautious.
RULE 5 -- GUINEVERE ALONE CANNOT CAUSE STAY_OUT. If your ONLY reason to stay out is a Guinevere
  disagreement, that is NOT sufficient -- you need clear indicator-based reasons. Guinevere
  disagreement + weak indicators = STAY_OUT. Guinevere disagreement + strong indicators = ENTER.
  Guinevere disagreement ALONE = ENTER normally.
BONUS RULE -- TIGHTER EXIT ON DISAGREEMENT. When you enter a trade Guinevere disagrees with,
  note it and monitor the position more carefully -- be ready to EXIT earlier if the move
  stalls. This is a tighter EXIT watch, NOT a reason to avoid entry.

PROFIT PROTECTION LADDER (active)
As floating profit builds, the trailing stop tightens to guarantee a minimum floor:
  Step 1: Float >= £15 (150pt) -> stop tightens to guarantee a £10 floor.
  Step 2: Float >= £30 (300pt) -> stop tightens to guarantee a £25 floor.
  Step 3: Float >= £45 (450pt) -> stop tightens to guarantee a £40 floor.
The position cannot close below the locked floor unless a gap event occurs. Reference
the current ladder status in your HOLD reasoning.

UTHER INTELLIGENCE BRIEFING (direct AI news analysis -- 4 Aug 2026)
Above the market data you also receive an UTHER INTELLIGENCE BRIEFING: the desk's dedicated
AI news analyst's own reading of the live wire, given as plain intelligence rather than a
scored modifier. Uther reads the actual articles and reasons about specific opportunities.
  -> Treat it like Guinevere's macro level -- it LEADS your indicators (news moves price
     before the charts catch up), but it CONFIRMS or REDIRECTS; it never blocks.
  -> Your own instrument's Uther assessments are primary; the CROSS-MARKET items are macro
     context (e.g. a HIGH/MEDIUM SHORT call on US500 colours the risk tone for your instrument).
  -> Never let Uther's briefing ALONE force a STAY_OUT -- exactly as with Guinevere, you need
     indicator-based reasons. Uther + Guinevere + aligned indicators = enter with conviction.
  -> If the briefing says "no current intelligence / feed offline", ignore it and trade on
     your indicators and Guinevere as normal.

HARD RULES -- NEVER VIOLATE
1.  Check DAILY SSL first -- it sets the allowed direction for today.
2.  1h SSL must agree with daily SSL before any entry.
3.  Only enter during MORNING or AFTERNOON.
4.  Never enter within 30 min of a BoJ decision.
5.  Force close all positions at 06:20 UTC -- never hold overnight.
6.  No position when market is CLOSED or in CLOSING phase.
7.  The 300-point stop is sized to the Nikkei's range -- do NOT exit early on noise; the profit ladder protects gains once in profit.
8.  Morgan is context only -- do NOT raise your entry bar at low Morgan (>=30). The
    system hard-blocks new entries below 30 on its own.
9.  On a suspected holiday / flat feed (no price movement), STAY_OUT.

LONG ENTRY -- requires all:
  Daily SSL = BULL
  1h SSL = BULL
  1h RSI above 55
  5m signals 5/6 bullish
  5m candle GREEN
  5m TMO above +0.3
  Session = MORNING or AFTERNOON
  Calendar clear

SHORT ENTRY -- requires all:
  Daily SSL = BEAR
  1h SSL = BEAR
  1h RSI below 45
  5m signals 5/6 bearish
  5m candle RED
  5m TMO below -0.3
  Session = MORNING or AFTERNOON
  Calendar clear

EXIT when in position:
  5m SSL reverses AND RSI crosses 50 (both required)
  OR force close signal at 06:20 UTC

REQUIRED OUTPUT -- valid JSON only. No markdown, no preamble.
{
  "decision": "ENTER_LONG | ENTER_SHORT | HOLD | EXIT | STAY_OUT",
  "confidence": 0-100,
  "session_bias": "MORNING_BULLISH | AFTERNOON_CONTINUATION | UNCLEAR",
  "reasoning": "2-4 sentences explaining your decision",
  "warnings": ["list of concerns"],
  "checklist": {
    "trend_aligned": true,
    "momentum_confirmed": true,
    "session_appropriate": true,
    "calendar_clear": true,
    "no_us_open_risk": true,
    "high_conviction": true
  },
  "calendar_assessment": "brief comment on upcoming Japan/BoJ events",
  "session_assessment": "brief comment on session phase and timing"
}"""


# ── Format indicators for Arthur ──────────────────────────────────────────────

def _format_indicators(
    bar_1d: Optional[pd.Series],
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    session_phase: str,
    current_trade=None,
    calendar_context: Optional[str] = None,
    perf_context: Optional[str] = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _f(v, dp=2):
        if v is None or pd.isna(v):
            return "N/A"
        return f"{float(v):.{dp}f}"

    candle_colour = "GREEN" if bar_5m.get("close", 0) >= bar_5m.get("open", 0) else "RED"
    ssl_1d = "BULL" if (bar_1d is not None and bar_1d.get("ssl_bull")) else ("BEAR" if bar_1d is not None else "N/A (no daily data)")
    ssl_1h = "BULL" if bar_1h.get("ssl_bull") else "BEAR"
    ssl_5m = "BULL" if bar_5m.get("ssl_bull") else "BEAR"

    position_text = "None -- no open position"
    if current_trade is not None:
        pts_from_entry = (current_price - current_trade.entry_price) if current_trade.direction == "LONG" \
                         else (current_trade.entry_price - current_price)
        position_text = (
            f"OPEN {current_trade.direction} | "
            f"entry={current_trade.entry_price:.1f} | "
            f"current={current_price:.1f} | "
            f"pts_from_entry={pts_from_entry:+.1f} | "
            f"stop={current_trade.stop_loss:.1f} | "
            f"target={current_trade.take_profit:.1f} | "
            f"stake=£{current_trade.stake:.4f}/pt | "
            f"session={current_trade.session_phase}"
        )

    if current_trade is not None and getattr(current_trade, "ladder_step", 0):
        position_text += (
            " | PROFIT LADDER ACTIVE: floor locked at £%.2f (step %d). Position cannot "
            "close below this floor unless a gap event occurs -- factor this into your "
            "HOLD reasoning." % (getattr(current_trade, "ladder_floor_gbp", 0.0),
                                 int(getattr(current_trade, "ladder_step", 0))))

    return f"""Please analyse the current Japan 225 market conditions.

TIME AND PRICE
  Time (UTC):       {now}
  Session Phase:    {session_phase}
  Japan 225 Level:   {current_price:,.1f}

DAILY CHART (Trend Direction -- sets allowed direction for today)
  SSL Cloud:        {ssl_1d}
  RSI (14):         {_f(bar_1d.get('rsi') if bar_1d is not None else None, 1)}
  TMO Main:         {_f(bar_1d.get('tmo_main') if bar_1d is not None else None, 3)}
  Chande MO (20):   {_f(bar_1d.get('chande_mo') if bar_1d is not None else None, 1)}

1-HOUR CHART (Trend Confirmation)
  SSL Cloud:        {ssl_1h}
  RSI (14):         {_f(bar_1h.get('rsi'), 1)}
  MACD Histogram:   {_f(bar_1h.get('macd_histogram'), 3)}
  TMO Main:         {_f(bar_1h.get('tmo_main'), 3)}
  TMO Smooth:       {_f(bar_1h.get('tmo_smooth'), 3)}
  Chande MO (20):   {_f(bar_1h.get('chande_mo'), 1)}
  Money Flow (14):  {_f(bar_1h.get('money_flow'), 2)}

5-MINUTE CHART (Entry Timing)
  SSL Cloud:        {ssl_5m}
  RSI (14):         {_f(bar_5m.get('rsi'), 1)}
  MACD Histogram:   {_f(bar_5m.get('macd_histogram'), 3)}
  TMO Main:         {_f(bar_5m.get('tmo_main'), 3)}
  TMO Smooth:       {_f(bar_5m.get('tmo_smooth'), 3)}
  Chande MO (20):   {_f(bar_5m.get('chande_mo'), 1)}
  Money Flow (14):  {_f(bar_5m.get('money_flow'), 2)}
  Last Candle:      {candle_colour} (close={_f(bar_5m.get('close'), 1)} open={_f(bar_5m.get('open'), 1)})

CURRENT POSITION
  {position_text}

{calendar_context if calendar_context else 'JAPAN CALENDAR\n  No calendar data available.'}

{perf_context if perf_context else 'SELF PERFORMANCE AWARENESS\n  No performance data yet -- first trading session.'}

Please provide your analysis and trading decision in the required JSON format."""


# ── Main decision function ────────────────────────────────────────────────────

def get_trading_decision(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    session_phase: str,
    bar_1d: Optional[pd.Series] = None,
    current_trade=None,
    calendar_context: Optional[str] = None,
    perf_context: Optional[str] = None,
    news_context: Optional[str] = None,
    guinevere_advisory: Optional[str] = None,
) -> dict:
    """
    Send indicator data to Arthur (Claude) and receive a trading decision.
    Only call this AFTER Lancelot pre-checks have passed.
    """
    log.info("Sending indicators to Arthur...")

    user_message = _format_indicators(
        bar_1d, bar_1h, bar_5m, current_price, session_phase,
        current_trade, calendar_context, perf_context,
    )
    if news_context:
        user_message += "\n\n" + news_context
    # Guinevere 2.0 advisory (Commission 018): prepend so Arthur reads the macro context
    # before the indicators, per the DECISION HIERARCHY.
    if guinevere_advisory:
        user_message = guinevere_advisory + "\n\n" + user_message
    # Uther Direct Intelligence Feed (4 Aug 2026): the analyst's own briefing sits ABOVE the
    # Guinevere advisory (order: decision hierarchy [cached system prompt] -> Uther briefing ->
    # Guinevere advisory -> indicators -> Morgan). UNCACHED (dynamic) -- never touches the
    # cached SYSTEM_PROMPT prefix. get_uther_briefing is fail-safe and always returns a string.
    uther_briefing = get_uther_briefing(UTHER_INSTRUMENT)
    if uther_briefing:
        user_message = uther_briefing + "\n\n" + user_message

    for attempt in range(2):
        try:
            response = client.messages.create(
                model      = "claude-sonnet-4-6",
                max_tokens = 2000,
                system     = [{"type": "text", "text": SYSTEM_PROMPT,
                               "cache_control": {"type": "ephemeral"}}],
                messages   = [{"role": "user", "content": user_message}],
            )

            if response.stop_reason == "max_tokens":
                log.warning("Arthur hit max_tokens -- JSON may be truncated")

            raw_text = response.content[0].text.strip()
            if raw_text.startswith("```"):
                raw_text = "\n".join(
                    l for l in raw_text.split("\n")
                    if not l.strip().startswith("```")
                ).strip()

            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                log.error("Arthur returned invalid JSON (attempt %d/2): %s", attempt + 1, exc)
                if attempt == 0:
                    continue
                return _safe_stay_out(f"Arthur returned invalid JSON -- staying out for safety")

            decision["timestamp"]     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            decision["tokens_used"]   = response.usage.input_tokens + response.usage.output_tokens
            decision["current_price"] = current_price
            decision["session_phase"] = session_phase

            log.info(
                "Arthur decision: %s | confidence=%s | tokens=%d",
                decision.get("decision"),
                decision.get("confidence"),
                decision.get("tokens_used", 0),
            )
            return decision

        except anthropic.APIError as exc:
            log.error("Anthropic API error: %s", exc)
            return _safe_stay_out(f"API error: {str(exc)}")
        except Exception as exc:
            log.error("Unexpected error calling Arthur: %s", exc)
            return _safe_stay_out(f"Unexpected error: {str(exc)}")

    return _safe_stay_out("Arthur failed after all attempts")


def _safe_stay_out(reason: str) -> dict:
    return {
        "decision":            "STAY_OUT",
        "confidence":          0,
        "session_bias":        "UNCLEAR",
        "reasoning":           reason,
        "warnings":            [reason],
        "checklist":           {},
        "calendar_assessment": "",
        "session_assessment":  "",
        "timestamp":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tokens_used":         0,
    }


def format_decision_for_display(decision: dict) -> str:
    """Format Arthur's decision for terminal display."""
    d         = decision.get("decision", "UNKNOWN")
    conf      = decision.get("confidence", "--")
    bias      = decision.get("session_bias", "--")
    reasoning = decision.get("reasoning", "No reasoning")
    warnings  = decision.get("warnings", [])
    tokens    = decision.get("tokens_used", 0)
    ts        = decision.get("timestamp", "")
    lines = [
        "=" * 60,
        "  NikkeiTrader AI -- Arthur's Decision",
        f"  {ts}",
        "=" * 60,
        f"  Decision:        {d}",
        f"  Confidence:      {conf}/100",
        f"  Session Bias:    {bias}",
        f"  Nikkei Level:      {decision.get('current_price', '--'):,.1f}",
        f"  Session Phase:   {decision.get('session_phase', '--')}",
        "",
        "  Reasoning:",
        f"  {reasoning}",
        "",
    ]
    if warnings:
        lines.append("  Warnings:")
        for w in warnings:
            lines.append(f"    - {w}")
        lines.append("")
    cal = decision.get("calendar_assessment")
    if cal:
        lines.append(f"  Calendar: {cal}")
    ses = decision.get("session_assessment")
    if ses:
        lines.append(f"  Session:  {ses}")
    cl = decision.get("checklist", {})
    if cl:
        lines.append("  Checklist:")
        for k, v in cl.items():
            icon = "PASS" if v else "FAIL"
            lines.append(f"    [{icon}] {k.replace('_', ' ').title()}")
    lines.append(f"  Tokens used: {tokens}")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Arthur self-test -- calling Claude with bullish Nikkei setup...")
    bar_1d = pd.Series({
        "ssl_bull": True, "rsi": 58.0, "tmo_main": 1.5, "chande_mo": 25.0,
    })
    bar_1h = pd.Series({
        "ssl_bull": True, "rsi": 62.0, "macd_histogram": 8.5,
        "tmo_main": 2.1, "tmo_smooth": 1.5, "chande_mo": 45.0, "money_flow": 150.0,
    })
    bar_5m = pd.Series({
        "ssl_bull": True, "rsi": 58.0, "macd_histogram": 2.5,
        "tmo_main": 0.8, "tmo_smooth": 0.5, "chande_mo": 30.0, "money_flow": 80.0,
        "open": 66100.0, "close": 66232.0,
    })
    decision = get_trading_decision(
        bar_1h=bar_1h, bar_5m=bar_5m,
        current_price=66232.0, session_phase="MORNING", bar_1d=bar_1d,
    )
    print(format_decision_for_display(decision))
    log.info("Arthur self-test complete.")
