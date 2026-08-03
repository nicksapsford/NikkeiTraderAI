"""
NikkeiTrader AI -- main_nikkeitrader.py
Japan 225 spread betting main loop.
Mon-Fri 00:00-06:30 UTC (Tokyo cash). Force close at 06:20 UTC. No overnight positions.

PAPER_TRADING_MODE = True until demo account is verified.
"""

import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytz
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

PAPER_TRADING_MODE = True
_VER_FILE          = Path(__file__).resolve().parent / "VERSION"
VERSION            = _VER_FILE.read_text().strip() if _VER_FILE.exists() else "1.0.0"
CANDLE_INTERVAL    = 300      # 5-minute candle loop (seconds)
POSITION_INTERVAL  = 30       # position monitoring (seconds)
HEARTBEAT_INTERVAL = 240      # emit a liveness log at least this often, even when idle
                              # (LUNCH_BREAK / PRE_OPEN produce no other output; without
                              #  this the watchdog reads the silence as a freeze and kills us)
DASHBOARD_INTERVAL = 15       # push live top-line state to the dashboard this often,
                              # in every session phase (not just the 5-min candle ticks)
BASE_DIR           = Path(__file__).resolve().parent
LOG_DIR            = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG      = LOG_DIR / "shutdown.flag"
LIFT_FLAG          = LOG_DIR / "confidence_lift.json"   # manual Morgan confidence lift (live)

# ── Env / logging setup ───────────────────────────────────────────────────────

_ENV_PATH = BASE_DIR / ".env"
# Capital.com / Anthropic credentials: prefer this system's own .env, then a known-good
# sibling (all Albion systems share the one Capital.com demo account). Fixes the yfinance
# fallback -- TideTraderAI/.env carries only Kraken keys, no CAPITALCOM_.
_ENV_CANDIDATES = [
    _ENV_PATH,
    BASE_DIR.parent / "USTraderAI" / ".env",
    BASE_DIR.parent / "GoldTraderAI" / ".env",
]
for _cand in _ENV_CANDIDATES:
    if _cand.exists():
        load_dotenv(dotenv_path=_cand)
        break
else:
    load_dotenv()

# ─── ALBION STANDING RULE: ALL LOG TIMESTAMPS ARE UTC ────────────────────────
# Force Python's logging to emit %(asctime)s in UTC, not BST/local. Without this
# line, logging defaults to local time and every log line is +1h vs the UTC CSV
# artefacts (phantom_trades.csv etc.) — the exact BST/UTC mismatch that caused a
# misread on 11 Jul 2026. Never interpret an Albion log timestamp as local time;
# confirm UTC before analysing. (Baked in per Nick's directive, 12 Jul 2026.)
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "nikkeitrader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("NikkeiTrader.Main")

# ── Internal imports ──────────────────────────────────────────────────────────

from agent_brain_nikkei   import get_trading_decision, format_decision_for_display
from calendar_nikkei      import check_calendar, is_hard_blocked, get_calendar_context
from data_feed_nikkei     import NikkeiDataFeed, get_session_phase, is_market_open, minutes_until_next_open, CLOSED
from capitalcom_connector import CapitalComConnector, Nikkei_EPIC
from notifier_nikkei      import (
    notify_system_startup, notify_system_shutdown,
    notify_trade_opened, notify_trade_closed_win, notify_trade_closed_loss,
    notify_kill_switch_triggered, notify_kill_switch_reset,
    notify_calendar_block, notify_daily_summary, notify_system_error,
)
from paper_trader_nikkei  import PaperTraderNikkei, TRADES_LOG
import performance_nikkei
import guinevere_news
try:
    import guinevere2                       # Guinevere 2.0 directional news (Commission 018)
except Exception:
    guinevere2 = None
from performance_nikkei   import (
    get_performance_context, get_perf_dashboard_dict, invalidate_cache,
    generate_milestone_review, process_new_phantom_verdicts,
)
from pre_checks_nikkei    import run_all_pre_checks, run_individual_pre_checks
import phantom_tracker
import benchmark_link

SESS_TZ = timezone.utc   # Tokyo cash session == UTC (Japan has no DST)

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_SHUTDOWN = False

def _handle_signal(sig, frame):
    global _SHUTDOWN
    log.info("Shutdown signal received (%s)", sig)
    _SHUTDOWN = True

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Account state ─────────────────────────────────────────────────────────────


def _today_realised_pnl(csv_path) -> float:
    """Sum today's (UTC) realised P&L from the trade-log CSV (Today P&L Persist Fix,
    30 Jul 2026). Lets a mid-day restart keep the 'today' counter accurate instead of
    resetting to zero. Only CLOSED trades are in this CSV, so open positions are excluded.
    Robust: missing file / no trades today / bad rows -> 0.0. All timestamps are UTC."""
    import csv as _csv
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path
    try:
        p = _Path(csv_path)
        if not p.exists():
            return 0.0
        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        total = 0.0
        with p.open(newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                d = (row.get("date") or "").strip()
                if not d:
                    d = (row.get("entry_time") or "").strip()[:10]
                if d != today:
                    continue
                try:
                    total += float(row.get("pnl_gbp") or 0.0)
                except (TypeError, ValueError):
                    continue
        return round(total, 2)
    except Exception:
        return 0.0


class AccountState:
    """Holds live trading account state passed to pre-checks."""

    def __init__(self, capital: float) -> None:
        self.capital_gbp        = capital
        self.daily_pnl_gbp      = 0.0
        self.consecutive_losses = 0
        self.last_loss_time     = None
        self.kill_switch_active = False
        self.kill_switch_tier   = 0
        self.kill_switch_until  = None   # ISO timestamp when kill expires
        self.kill_switch_reason = ""
        self.kill_history       = []     # ISO timestamps of triggers in last 48h

    def record_trade(self, pnl_gbp: float) -> None:
        self.daily_pnl_gbp += pnl_gbp
        self.capital_gbp = round(self.capital_gbp + pnl_gbp, 2)
        if pnl_gbp < 0:
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now(timezone.utc)
        else:
            self.consecutive_losses = 0

    def reset_daily(self) -> None:
        self.daily_pnl_gbp = 0.0


# ── Dashboard push (best-effort) ──────────────────────────────────────────────

DASHBOARD_URL = "http://localhost:5008/api/update"

_last_dashboard_dict: dict = {}
_dash_first_ok:  bool  = False   # log the first successful push at INFO for confirmation
_dash_fail_count: int  = 0
_dash_last_warn: float = 0.0


def _dashboard_push_ok(kind: str, phase: str, price: float, status: str, http) -> None:
    """Confirm a push landed. First one logs at INFO; the rest at DEBUG."""
    global _dash_first_ok
    if not _dash_first_ok:
        _dash_first_ok = True
        log.info("Dashboard connected -- first %s push OK | phase=%s nikkei=%.1f status=%s HTTP %s",
                 kind, phase, price, status, http)
    else:
        log.debug("Dashboard %s push | phase=%s nikkei=%.1f status=%s HTTP %s",
                  kind, phase, price, status, http)


def _dashboard_push_warn(exc: Exception) -> None:
    """Surface push failures (throttled) instead of swallowing them silently."""
    global _dash_fail_count, _dash_last_warn
    _dash_fail_count += 1
    now = time.monotonic()
    if now - _dash_last_warn > 60:
        log.debug("Dashboard push failing (%d so far): %s -- is dashboard_nikkei.py running on :5008?",
                  _dash_fail_count, exc)
        _dash_last_warn = now


def _serialise_trade(trade):
    if trade is None:
        return None
    if hasattr(trade, "__dict__"):
        return {k: str(v) for k, v in trade.__dict__.items()}
    return trade


def _safe_float(v):
    try:
        f = float(v)
        return None if f != f else f  # NaN check (NaN != NaN)
    except (TypeError, ValueError):
        return None


def _indicator_snapshot(bar) -> dict:
    if bar is None:
        return {}
    return {
        "ssl_bull":   bool(bar.get("ssl_bull", False)),
        "rsi":        _safe_float(bar.get("rsi")),
        "macd":       _safe_float(bar.get("macd")),
        "tmo_main":   _safe_float(bar.get("tmo_main")),
        "chande_mo":  _safe_float(bar.get("chande_mo")),
        "money_flow": _safe_float(bar.get("money_flow")),
    }


def _ssl_label(ind: dict) -> str:
    """BULL / BEAR / -- from an indicator snapshot's ssl_bull flag."""
    if not ind or "ssl_bull" not in ind:
        return "--"
    return "BULL" if ind["ssl_bull"] else "BEAR"


def _fmt_ind(v) -> str:
    """2dp string for a numeric indicator, or '' if unavailable."""
    return "" if v is None else f"{float(v):.2f}"


def _build_exit_meta(ind_1d: dict, ind_1h: dict, ind_5m: dict, decision: dict) -> dict:
    """Indicator snapshot + Arthur's exit-decision confidence for an ARTHUR_EXIT
    (Gaius Commission 012). Scalars are taken from the 1h snapshot (the confirmation
    timeframe). Best-effort -- returns None on any error so a logging failure never
    blocks a trade close."""
    try:
        ind_1h = ind_1h or {}
        conf = decision.get("confidence") if decision else None
        return {
            "exit_daily_ssl":  _ssl_label(ind_1d),
            "exit_1h_ssl":     _ssl_label(ind_1h),
            "exit_5m_ssl":     _ssl_label(ind_5m),
            "exit_tmo":        _fmt_ind(ind_1h.get("tmo_main")),
            "exit_money_flow": _fmt_ind(ind_1h.get("money_flow")),
            "exit_rsi":        _fmt_ind(ind_1h.get("rsi")),
            "exit_chande_mo":  _fmt_ind(ind_1h.get("chande_mo")),
            "exit_confidence": "" if conf is None else str(conf),
        }
    except Exception:
        return None


def _push_dashboard(
    stanley:    PaperTraderNikkei,
    account:    AccountState,
    decision:   dict = None,
    pre_checks: dict = None,
    phase:      str  = "",
    nikkei_level: float = 0.0,
    calendar_summary: str = "",
    connector_status: str = "yahoo",
    panel_mode: str = "pre_checks",
    trend_1d:   str = "NEUTRAL",
    trend_1h:   str = "NEUTRAL",
    signal_5m:  str = "NEUTRAL",
    indicators_1d: dict = None,
    indicators_1h: dict = None,
    indicators_5m: dict = None,
) -> None:
    """Push latest state to dashboard via HTTP POST (separate process)."""
    try:
        import requests
        perf = get_perf_dashboard_dict()
        payload = {
            "mode":          "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "version":       VERSION,
            "phase":         phase,
            "nikkei_level":    nikkei_level,
            "connector_status": connector_status,
            "capital":       stanley.capital_gbp,
            "daily_pnl":     account.daily_pnl_gbp,
            "total_trades":  stanley.total_trades,
            "win_rate":      stanley.win_rate,
            "in_trade":      stanley.in_trade,
            "current_trade": _serialise_trade(stanley.current_trade),
            "decision":      decision,
            "panel_mode":    panel_mode,
            "checklist":     (decision or {}).get("checklist", {}),
            "pre_checks":    pre_checks,
            "trend_1d":      trend_1d,
            "trend_1h":      trend_1h,
            "signal_5m":     signal_5m,
            "indicators_1d": indicators_1d or {},
            "indicators_1h": indicators_1h or {},
            "indicators_5m": indicators_5m or {},
            "perf":          perf,
            "calendar":      calendar_summary,
            "kill_switch":   account.kill_switch_active,
            "kill_tier":     account.kill_switch_tier,
            "updated_at":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        resp = requests.post(
            DASHBOARD_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        _dashboard_push_ok("full", phase, nikkei_level, connector_status, resp.status_code)
    except Exception as exc:
        _dashboard_push_warn(exc)


def _push_dashboard_live(
    stanley: PaperTraderNikkei,
    account: AccountState,
    ig:      CapitalComConnector,
    feed:    NikkeiDataFeed,
    now_utc: datetime,
) -> None:
    """
    Lightweight, frequent push of the always-known top-line fields (live price,
    phase, connector status, capital, P&L, open position). Runs every loop tick
    in ALL session phases -- including PRE_OPEN / LUNCH_BREAK / CLOSING and the
    gaps between 5-minute candle ticks -- so the dashboard never sits on its
    0.0 / -- defaults.

    Deliberately omits decision / pre_checks / indicators so that this frequent
    merge does NOT overwrite the richer panel data from the last candle tick.
    """
    try:
        import requests
        phase = get_session_phase(now_utc)
        price = _get_price(ig, feed)
        connector_status = "capitalcom" if (ig is not None and ig.connected) else "yahoo"
        payload = {
            "mode":             "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "version":          VERSION,
            "phase":            phase,
            "nikkei_level":       price,
            "connector_status": connector_status,
            "capital":          stanley.capital_gbp,
            "daily_pnl":        account.daily_pnl_gbp,
            "total_trades":     stanley.total_trades,
            "win_rate":         stanley.win_rate,
            "in_trade":         stanley.in_trade,
            "current_trade":    _serialise_trade(stanley.current_trade),
            "kill_switch":      account.kill_switch_active,
            "kill_tier":        account.kill_switch_tier,
            "perf":             get_perf_dashboard_dict(),   # keep confidence exposed in ALL market states
            "updated_at":       now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        resp = requests.post(
            DASHBOARD_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        _dashboard_push_ok("live", phase, price, connector_status, resp.status_code)
    except Exception as exc:
        _dashboard_push_warn(exc)


# ── Core candle tick ──────────────────────────────────────────────────────────

def run_candle_tick(
    feed:    NikkeiDataFeed,
    stanley: PaperTraderNikkei,
    account: AccountState,
    ig:      CapitalComConnector,
) -> None:
    """
    Called once every 5 minutes during a trading session.
    Gathers indicators, runs pre-checks, calls Arthur, acts on decision.
    """
    now_utc      = datetime.now(timezone.utc)
    phase        = get_session_phase(now_utc)
    nikkei_price   = _get_price(ig, feed)
    connector_status = "capitalcom" if (ig is not None and ig.connected) else "yahoo"

    log.info("--- CANDLE TICK | %s | phase=%s | Nikkei=%.1f ---",
             now_utc.strftime("%H:%M:%S UTC"), phase, nikkei_price)

    # Gaius Commission 012: fill any ARTHUR_EXIT row's post-exit prices once 30/60 min
    # have elapsed (runs every tick, survives restarts, never raises).
    stanley.fill_post_exit_prices(nikkei_price, now_utc)

    # Calendar check
    hard_blocked, block_reason, event_name, mins_remain = is_hard_blocked(now_utc)
    cal_context = get_calendar_context(now_utc)
    cal_summary = check_calendar(now_utc).get("calendar_summary", "")

    if hard_blocked:
        log.warning("CALENDAR HARD BLOCK: %s (%d min remaining)", block_reason, mins_remain)
        if not stanley.in_trade:
            _push_dashboard(stanley, account, phase=phase, nikkei_level=nikkei_price,
                            calendar_summary=cal_summary, connector_status=connector_status)
            return

    # Refresh data
    try:
        feed.refresh()
    except Exception as exc:
        log.error("Data refresh failed: %s", exc)
        return

    bar_1d = feed.latest_bar("1d")
    bar_1h = feed.latest_bar("1h")
    bar_5m = feed.latest_bar("5m")

    if bar_1h is None or bar_5m is None:
        log.warning("Insufficient indicator data -- skipping tick")
        return

    # Performance context
    perf_context = get_performance_context()

    # Determine proposed direction from composite signals
    sig_1h = feed.composite_signal("1h")
    sig_5m = feed.composite_signal("5m")
    trend_1d = "LONG" if bar_1d.get("ssl_bull") else "SHORT"
    # Bidirectional (System 1 Review 17 Jul): the DAILY SSL sets the session
    # direction -- BULL -> LONG session, BEAR -> SHORT session (Morgan-gated below).
    # NEUTRAL only when the daily SSL is unavailable (None/NaN).
    _ssl_1d = bar_1d.get("ssl_bull")
    proposed_direction = "NEUTRAL" if (_ssl_1d is None or _ssl_1d != _ssl_1d) else trend_1d

    ind_1d = _indicator_snapshot(bar_1d)
    ind_1h = _indicator_snapshot(bar_1h)
    ind_5m = _indicator_snapshot(bar_5m)

    # Pre-checks
    checks = run_all_pre_checks(
        bar_1h=bar_1h,
        bar_5m=bar_5m,
        account=account,
        current_trade=stanley.current_trade,
        bar_1d=bar_1d,
        proposed_direction=proposed_direction,
    )
    individual_checks = run_individual_pre_checks(
        bar_1h=bar_1h,
        bar_5m=bar_5m,
        account=account,
        current_trade=stanley.current_trade,
        bar_1d=bar_1d,
        proposed_direction=proposed_direction,
    )

    # GUINEVERE 2.0 HIGH-ALERT active consultation (31 Jul 2026): a HIGH-confidence Guinevere
    # signal may force an Arthur consult even when ONLY soft Lancelot checks block. The
    # re-run relaxes ONLY the soft quality checks (SSL alignment / momentum / candle);
    # safety checks + daily-trend + RSI + choppy are always enforced, so no risk control is
    # bypassed. Arthur still needs >= 60 confidence to act (enforced after the decision).
    guin_high_alert = False
    _guin_alert_note = None
    if (not checks["passed"]) and guinevere2 is not None and stanley.current_trade is None:
        try:
            _al, _favdir, _note = guinevere2.is_high_alert("NIKKEI")
            if _al:
                _relaxed = run_all_pre_checks(
                    bar_1h=bar_1h, bar_5m=bar_5m, account=account,
                    current_trade=stanley.current_trade, bar_1d=bar_1d,
                    proposed_direction=_favdir, relax_soft=True)
                if _relaxed["passed"]:
                    checks = _relaxed
                    guin_high_alert = True
                    _guin_alert_note = _note
                    proposed_direction = _favdir
                    log.warning("GUINEVERE HIGH ALERT: soft checks relaxed (%s). %s", _favdir, _note)
        except Exception as _e:
            log.warning("Guinevere high-alert relaxation failed: %s", _e)

    if not checks["passed"]:
        log.info("Pre-checks FAILED: %s", checks.get("reason"))
        _push_dashboard(stanley, account, pre_checks=individual_checks,
                        phase=phase, nikkei_level=nikkei_price, calendar_summary=cal_summary,
                        connector_status=connector_status, panel_mode="pre_checks",
                        trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                        indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)

        # Kill switch notifications
        if checks.get("kill_switch_triggered"):
            account.kill_switch_active = True
            tier = checks.get("kill_tier", 1)
            account.kill_switch_tier   = tier
            wait_hours = {1: 6, 2: 12}.get(tier, 24)
            account.kill_switch_until  = None
            notify_kill_switch_triggered(
                tier=tier, reason=checks.get("reason", ""),
                wait_hours=wait_hours,
                daily_pnl=account.daily_pnl_gbp,
                capital=stanley.capital_gbp,
            )
        elif checks.get("kill_switch_reset"):
            account.kill_switch_active = False
            notify_kill_switch_reset(
                tier=account.kill_switch_tier,
                wait_hours=0,
                capital=stanley.capital_gbp,
            )
            account.kill_switch_tier = 0
        return

    # ── Zone-3 MORGAN HARD BLOCK (three-zone model, 24 Jul 2026, Nick's direct order) ──
    # Below 30, suspend NEW entries and let Gaius intervene. Existing open positions are
    # unaffected -- when in a trade we fall through so Arthur still manages HOLD/EXIT.
    # (Zone 2, 30-49, is WARNING only: trading continues, no code restriction here.)
    if not stanley.in_trade:
        _morgan_now = get_perf_dashboard_dict().get("confidence_score")
        if performance_nikkei.morgan_hard_block(50 if _morgan_now is None else _morgan_now):
            log.warning("MORGAN HARD BLOCK: confidence %s < 30 -- new entries suspended "
                        "(Gaius intervention active)", _morgan_now)
            _push_dashboard(stanley, account, pre_checks=individual_checks,
                            phase=phase, nikkei_level=nikkei_price, calendar_summary=cal_summary,
                            connector_status=connector_status, panel_mode="pre_checks",
                            trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                            indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)
            return

    # Guinevere 2.0 -- directional macro intelligence for Arthur (Commission 018).
    # Replaces the old +/-8 news adjustment: Guinevere 2.0 advises Arthur IN THE PROMPT
    # (DECISION HIERARCHY) and Arthur sets his own confidence (Principle 14). Fail-safe:
    # any failure yields a NEUTRAL advisory and never blocks the consult.
    guin_advisory, guin_sig = None, None
    if guinevere2 is not None:
        try:
            guin_sig = guinevere2.get_signal("NIKKEI")
            guin_advisory = guinevere2.get_advisory("NIKKEI")
        except Exception as _e:
            log.warning("Guinevere 2.0 failed: %s", _e)
    # On a HIGH-alert relaxed consult, tell Arthur it is a Guinevere-forced consultation
    # (soft checks relaxed, direction supplied, 60+ required).
    if guin_high_alert and _guin_alert_note and guin_advisory:
        guin_advisory = _guin_alert_note + "\n\n" + guin_advisory

    # Call Arthur
    decision = get_trading_decision(
        bar_1h=bar_1h,
        bar_5m=bar_5m,
        current_price=nikkei_price,
        session_phase=phase,
        bar_1d=bar_1d,
        current_trade=stanley.current_trade,
        calendar_context=cal_context,
        perf_context=perf_context,
        guinevere_advisory=guin_advisory,
    )

    # Guinevere 2.0 signal logging for ongoing Gaius assessment (Commission 018): log the
    # signal + Arthur's response so Gaius can score Guinevere 2.0's value over time.
    if guinevere2 is not None and guin_sig is not None:
        try:
            guinevere2.log_decision(guin_sig,
                                    arthur_decision=decision.get("decision", ""),
                                    arthur_confidence_after=decision.get("confidence", ""))
        except Exception as _e:
            log.warning("Guinevere 2.0 logging failed: %s", _e)

    # NEWS FAST PATH floor (Architecture B, 3 Aug 2026): on a relaxed (soft-check-bypassed)
    # consult, require Arthur >= 65 confidence to enter -- a higher bar (was 60) precisely
    # because soft confirmation was relaxed. Extra 5 points compensates for the relaxed checks.
    if guin_high_alert and decision.get("decision") in ("ENTER_LONG", "ENTER_SHORT"):
        try:
            if float(decision.get("confidence") or 0) < 65:
                log.warning("UTHER HIGH ALERT: Arthur confidence %.0f < 65 on relaxed "
                            "consult -- STAY_OUT.", float(decision.get("confidence") or 0))
                decision["decision"] = "STAY_OUT"
                decision["guinevere_high_alert_floor"] = True
        except (TypeError, ValueError):
            decision["decision"] = "STAY_OUT"

    log.info(format_decision_for_display(decision))
    _push_dashboard(stanley, account, decision=decision, pre_checks=individual_checks,
                    phase=phase, nikkei_level=nikkei_price, calendar_summary=cal_summary,
                    connector_status=connector_status, panel_mode="claude",
                    trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                    indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)

    action = decision.get("decision", "STAY_OUT")

    # News fast path (Architecture B): tag entries taken via the Uther HIGH-alert relaxed
    # consult + log every fast-path consult (entry or stay-out) for the Uther dashboard/Gaius.
    _fp_trigger = ""
    if guin_high_alert:
        _fp_trigger = ((guin_sig or {}).get("uther_reasoning")
                       or (guin_sig or {}).get("primary_event") or "")[:160]
        try:
            _log_fast_path("NIKKEI", proposed_direction, action, decision.get("confidence"), _fp_trigger)
        except Exception as _fe:
            log.warning("fast-path log failed: %s", _fe)

    # --- Act on decision ---
    if action == "ENTER_LONG" and not stanley.in_trade:
        _open_trade(stanley, account, ig, "LONG", nikkei_price, phase,
                    fast_path=guin_high_alert, fast_path_trigger=_fp_trigger)

    elif action == "ENTER_SHORT" and not stanley.in_trade:
        _open_trade(stanley, account, ig, "SHORT", nikkei_price, phase,
                    fast_path=guin_high_alert, fast_path_trigger=_fp_trigger)

    elif action == "EXIT" and stanley.in_trade:
        # Gaius Commission 012: capture the indicator snapshot + Arthur's exit confidence
        # so we can later judge whether the early exit was skill or premature.
        _emeta = _build_exit_meta(ind_1d, ind_1h, ind_5m, decision)
        _close_trade(stanley, account, ig, nikkei_price, "ARTHUR_EXIT", exit_meta=_emeta)

    elif action == "HOLD" and stanley.in_trade:
        log.info("Arthur says HOLD -- maintaining position")

    elif action == "STAY_OUT":
        log.info("Arthur says STAY_OUT -- no action")
        try:
            _dir = proposed_direction if proposed_direction in ("LONG", "SHORT") else ("LONG" if bar_1d.get("ssl_bull") else "SHORT")
            try:
                # Guinevere sentiment score at signal time (desk-wide logging sweep,
                # 18 Jul 2026): was blank on Nikkei phantom rows. Cached fetch -- no extra call.
                try:
                    _guin_score = guinevere_news.fetch_nikkei_sentiment().get("score")
                except Exception:
                    _guin_score = None
                _snap = phantom_tracker.build_snapshot(
                    ind_1d, ind_1h, ind_5m,
                    morgan_score=performance_nikkei.get_confidence(),
                    session=phase,
                    guinevere_score=_guin_score,
                )
            except Exception as _se:
                log.warning("phantom indicator snapshot failed: %s", _se)
                _snap = None
            try:
                _bstate, _bavail = benchmark_link.read_availability()
            except Exception:
                _bstate, _bavail = ("UNKNOWN", None)
            phantom_tracker.record_decision(
                market="Nikkei",
                direction_blocked=_dir,
                price_at_decision=nikkei_price,
                confidence=decision.get("confidence"),
                reason_for_stay_out="ARTHUR_STAY_OUT",
                get_price_fn=lambda m: _get_price(ig, feed),
                indicators=_snap,
                benchmark_state=_bstate,
                benchmark_available=_bavail,
            )
        except Exception as _exc:
            log.warning("phantom_tracker record failed: %s", _exc)

    # Milestone review every 50 trades
    if stanley.total_trades > 0 and stanley.total_trades % 50 == 0:
        from paper_trader_nikkei import TRADES_LOG
        milestone = stanley.total_trades // 50
        generate_milestone_review(TRADES_LOG, milestone)


# ── Position monitoring ───────────────────────────────────────────────────────

def monitor_open_position(
    stanley:  PaperTraderNikkei,
    account:  AccountState,
    ig:       CapitalComConnector,
    feed:     NikkeiDataFeed,
) -> None:
    """
    Called every 30 seconds while in a position.
    Checks trailing stop, force close at 06:20 UTC.
    """
    if not stanley.in_trade:
        return

    now_utc    = datetime.now(timezone.utc)
    nikkei_price = _get_price(ig, feed)

    # Force close at 06:20 UTC
    from strategy_nikkei import should_force_close
    if should_force_close(now_utc):
        log.warning("Force close at 06:20 UTC -- closing all positions")
        _close_trade(stanley, account, ig, nikkei_price, "FORCE_CLOSE_0620")
        return

    # Trailing stop + take profit check
    reason = stanley.monitor_trade(nikkei_price)
    if reason:
        trade = stanley.trade_history[-1] if stanley.trade_history else None
        _handle_closed_trade(account, trade)
        log.info("Position auto-closed: %s | price=%.1f", reason, nikkei_price)
        invalidate_cache()


# ── Open / close helpers ──────────────────────────────────────────────────────

def _log_fast_path(market, direction, arthur_decision, arthur_confidence, trigger) -> None:
    """Append a row to logs/fast_path_log.csv for every Uther HIGH-alert (fast-path) consult --
    the source for the Uther dashboard fast-path panel + Gaius separate assessment. UTC."""
    import csv as _csv
    path = LOG_DIR / "fast_path_log.csv"
    hdr = ["timestamp_utc", "market", "direction", "arthur_decision",
           "arthur_confidence", "entered", "uther_confidence", "trigger"]
    entered = "TRUE" if arthur_decision in ("ENTER_LONG", "ENTER_SHORT") else "FALSE"
    row = {"timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
           "market": market, "direction": direction or "",
           "arthur_decision": arthur_decision or "STAY_OUT",
           "arthur_confidence": arthur_confidence if arthur_confidence is not None else "",
           "entered": entered, "uther_confidence": "HIGH", "trigger": (trigger or "")[:160]}
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=hdr)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _open_trade(
    stanley:  PaperTraderNikkei,
    account:  AccountState,
    ig:       CapitalComConnector,
    direction: str,
    price:    float,
    phase:    str,
    fast_path: bool = False,
    fast_path_trigger: str = "",
) -> None:
    trade = stanley.open_trade(direction, price, phase,
                               fast_path=fast_path, fast_path_trigger=fast_path_trigger,
                               fast_path_uther_confidence=("HIGH" if fast_path else ""))
    if PAPER_TRADING_MODE:
        log.info("[PAPER] OPEN %s | entry=%.1f | stop=%.1f | target=%.1f | stake=£%.4f/pt",
                 direction, price, trade.stop_loss, trade.take_profit, trade.stake)
    else:
        try:
            ig.open_position(
                epic         = Nikkei_EPIC,
                direction    = direction,
                size         = trade.stake,
                stop_distance= trade.stop_pts,
            )
            log.info("[LIVE] OPEN %s via Capital.com | entry=%.1f", direction, price)
        except Exception as exc:
            log.error("Capital.com open_position failed: %s -- position tracked paper only", exc)
            notify_system_error(f"Capital.com open failed: {exc}")

    notify_trade_opened(
        direction=direction, entry_price=price,
        stop_loss=trade.stop_loss, take_profit=trade.take_profit,
        stake=trade.stake, session_phase=phase,
    )
    log.info("Trade opened: %s", trade.summary())


def _close_trade(
    stanley:  PaperTraderNikkei,
    account:  AccountState,
    ig:       CapitalComConnector,
    price:    float,
    reason:   str,
    exit_meta: dict = None,
) -> None:
    trade = stanley.close_trade(price, reason, exit_meta=exit_meta)
    if trade is None:
        return
    _handle_closed_trade(account, trade)
    invalidate_cache()

    if not PAPER_TRADING_MODE:
        try:
            positions = ig.get_open_positions()
            for pos in positions:
                ig.close_position(
                    deal_id   = pos.get("dealId"),
                    direction = "SELL" if trade.direction == "LONG" else "BUY",
                    size      = trade.stake,
                )
            log.info("[LIVE] Position closed via Capital.com | reason=%s", reason)
        except Exception as exc:
            log.error("Capital.com close_position failed: %s", exc)
            notify_system_error(f"Capital.com close failed: {exc}")

    if trade.pnl_gbp >= 0:
        notify_trade_closed_win(
            direction=trade.direction, exit_price=price,
            pnl_pts=trade.pnl_pts, pnl_gbp=trade.pnl_gbp,
            capital=account.capital_gbp, reason=reason,
        )
    else:
        notify_trade_closed_loss(
            direction=trade.direction, exit_price=price,
            pnl_pts=trade.pnl_pts, pnl_gbp=trade.pnl_gbp,
            capital=account.capital_gbp, reason=reason,
        )


def _handle_closed_trade(account: AccountState, trade) -> None:
    if trade is None:
        return
    account.record_trade(trade.pnl_gbp)
    log.info("Trade result: %s%+.2f GBP | capital=£%.2f",
             "+" if trade.pnl_gbp >= 0 else "", trade.pnl_gbp, account.capital_gbp)


# ── Price getter ──────────────────────────────────────────────────────────────

def _get_price(ig: CapitalComConnector, feed: NikkeiDataFeed) -> float:
    """Get current Nikkei price -- Capital.com first, yfinance fallback."""
    try:
        if ig is not None and ig.connected:
            price_data = ig.get_price(Nikkei_EPIC)
            return price_data.get("mid", 0.0)
    except Exception:
        pass
    try:
        df = feed.get("5m")
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return 0.0


# ── Daily summary ─────────────────────────────────────────────────────────────

_last_summary_date: str = ""


def _maybe_send_daily_summary(stanley: PaperTraderNikkei, account: AccountState) -> None:
    global _last_summary_date
    today = datetime.now(SESS_TZ).strftime("%Y-%m-%d")
    if today == _last_summary_date:
        return
    now_sess = datetime.now(SESS_TZ)
    if now_sess.hour == 6 and now_sess.minute >= 30:
        notify_daily_summary(
            date_str=today,
            trades=stanley.total_trades,
            pnl_gbp=account.daily_pnl_gbp,
            capital=stanley.capital_gbp,
            win_rate=stanley.win_rate,
        )
        account.reset_daily()
        _last_summary_date = today
        log.info("Daily summary sent for %s", today)


# ── Main loop ─────────────────────────────────────────────────────────────────

def _apply_confidence_lift() -> None:
    """Apply a pending manual confidence lift (logs/confidence_lift.json) in-process
    so a Gaius/dashboard lift takes effect LIVE -- Morgan's persisted baseline is
    otherwise cached in this process until restart. Written by the dashboard
    /api/lift-confidence endpoint (or Gaius --lift); consumed here via the existing
    set_confidence() and the flag deleted. Does not change the confidence algorithm."""
    import json
    try:
        if not LIFT_FLAG.exists():
            return
        data = json.loads(LIFT_FLAG.read_text(encoding="utf-8"))
        reason = data.get("reason") or "CONFIDENCE LIFT -- manual override"
        if data.get("reset_gating"):
            # Nick's manual reset (signed off 25 Jul 2026): clamp the LIVE GATING
            # score to 50, not just the phantom-delta baseline. "Morgan is now 50."
            prior = performance_nikkei.get_perf_dashboard_dict().get("confidence_score")
            performance_nikkei.reset_to_50(reason=reason)
            LIFT_FLAG.unlink(missing_ok=True)
            log.warning("Morgan MANUAL RESET applied live: gating %s -> 50.0 (%s)", prior, reason)
            return
        val = max(0.0, min(100.0, float(data.get("confidence", 50.0))))
        prior = performance_nikkei.get_confidence()
        performance_nikkei.set_confidence(val, reason=reason)
        LIFT_FLAG.unlink(missing_ok=True)
        log.warning("Morgan CONFIDENCE LIFT applied live: %.1f -> %.1f (%s)", prior, val, reason)
    except Exception as _exc:
        log.warning("Confidence lift apply failed: %s", _exc)
        try:
            LIFT_FLAG.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    global _SHUTDOWN
    log.info("=" * 70)
    log.info("  NikkeiTrader AI v%s", VERSION)
    log.info("  Japan 225 Spread Betting -- Capital.com")
    log.info("  Mode: %s", "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING")
    log.info("=" * 70)

    # Capital.com connector -- always connect for live price data, even in
    # paper trading mode. PAPER_TRADING_MODE only controls whether trades
    # are sent to Excalibur (live) or tracked by Stanley (paper) below.
    ig = CapitalComConnector()
    try:
        ig.connect()
        ig_connected = True
        log.info("Capital.com connected")
    except Exception as exc:
        log.error("Capital.com connection failed: %s -- yfinance fallback", exc)
        ig_connected = False

    # Data feed
    feed = NikkeiDataFeed(ig_connector=ig if ig_connected else None)
    try:
        feed.initialise()
    except Exception as exc:
        log.warning("Initial data load partial: %s -- will retry", exc)

    # Contrarian phantom log (Job 2, Gaius Commission 001): NikkeiTrader records the
    # opposite-direction (LONG) mirror of every blocked SHORT to build an evidence
    # base. DATA COLLECTION ONLY -- no change to live trading or Arthur's logic.
    try:
        phantom_tracker.enable_contrarian("NikkeiTrader")
    except Exception as _exc:
        log.warning("contrarian log enable failed: %s", _exc)

    # Resolve any phantom PENDING verdicts that survived a restart, then start
    # a continuous watchdog so new stale rows resolve every 15 min (no restart).
    try:
        phantom_tracker.resolve_stale_pending(get_historical_price_fn=feed.get_historical_price)
        phantom_tracker.start_watchdog(get_historical_price_fn=feed.get_historical_price, interval_minutes=15)
    except Exception as _exc:
        log.warning("phantom resolve/watchdog startup failed: %s", _exc)

    # Morgan individual phantom-verdict feedback poller (daemon, every 300s).
    try:
        process_new_phantom_verdicts()
    except Exception as _exc:
        log.warning("Morgan phantom feedback poller startup failed: %s", _exc)

    # Restore Morgan's confidence trajectory from the CSV audit trail so a
    # restart resumes where it left off instead of resetting to baseline 50.
    try:
        _saved_conf = performance_nikkei.load_confidence()
        if _saved_conf is not None:
            performance_nikkei.set_confidence(_saved_conf, reason='restore')
            log.info("Morgan: confidence restored from CSV -> %.1f", _saved_conf)
        else:
            log.info("Morgan: no persisted confidence found -- baseline 50")
    except Exception as _exc:
        log.warning("Morgan confidence restore failed: %s", _exc)

    # Apply any confidence lift requested while the engine was down (Step 4).
    _apply_confidence_lift()

    # Paper trader + account
    stanley = PaperTraderNikkei()
    account = AccountState(capital=stanley.capital_gbp)
    # Today P&L Persist Fix (30 Jul 2026): seed the in-memory daily tally from today's
    # closed trades so a mid-day restart keeps the 'today' figure instead of resetting to 0.
    account.daily_pnl_gbp = _today_realised_pnl(TRADES_LOG)
    if account.daily_pnl_gbp:
        log.info("Restored today's realised P&L from trade log: GBP %.2f", account.daily_pnl_gbp)
    stanley.print_status()

    notify_system_startup(
        capital=stanley.capital_gbp,
        mode="PAPER" if PAPER_TRADING_MODE else "LIVE",
    )

    # Clear any stale shutdown flag left over from a previous session so we
    # don't immediately exit. During this run the flag is only ever *written*
    # by the dashboard and *consumed* (deleted) by the watchdog -- see below.
    SHUTDOWN_FLAG.unlink(missing_ok=True)

    log.info("NikkeiTrader AI is running. Ctrl+C to stop.")
    log.info("Dashboard: http://localhost:5008  (start dashboard_nikkei.py separately)")

    last_candle_tick    = 0.0
    last_position_check = 0.0
    last_heartbeat      = 0.0
    last_dashboard_push = 0.0
    _force_close_done   = False

    while not _SHUTDOWN:
        try:
            now     = time.monotonic()
            now_utc = datetime.now(timezone.utc)
            now_sess  = datetime.now(SESS_TZ)

            # ── Dashboard shutdown check ──────────────────────────────────────
            # NOTE: do NOT delete the flag here. We exit cleanly and leave the
            # flag on disk so the watchdog (Galahad) sees it, stops itself, and
            # does not relaunch us. The watchdog removes the flag on its way out.
            if SHUTDOWN_FLAG.exists():
                log.info("Shutdown requested via dashboard -- stopping (flag left for watchdog)")
                break

            # Apply a pending manual confidence lift live (Gaius intervention Step 4).
            _apply_confidence_lift()

            # ── Live dashboard push (all phases, every ~15s) ──────────────────
            # Keeps the dashboard's price/phase/status tiles current outside the
            # 5-minute candle ticks and outside the active-trading windows.
            if (now - last_dashboard_push) >= DASHBOARD_INTERVAL:
                _push_dashboard_live(stanley, account, ig, feed, now_utc)
                last_dashboard_push = now

            # ── Liveness heartbeat ────────────────────────────────────────────
            # Guarantees regular output during quiet phases (LUNCH_BREAK, PRE_OPEN,
            # market-closed) so Galahad's freeze detector never mistakes a healthy
            # idle bot for a hang. Idle sleeps below are capped to stay under it.
            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                log.info("Heartbeat -- alive | %s UTC | phase=%s | in_trade=%s",
                         now_sess.strftime("%H:%M"), get_session_phase(now_utc),
                         stanley.in_trade)
                last_heartbeat = now

            # Skip weekends entirely
            if now_sess.weekday() >= 5:
                log.debug("Weekend -- idle")
                _interruptible_sleep(HEARTBEAT_INTERVAL)
                continue

            # Outside the Tokyo cash session entirely.
            # BUG FIX (25 Jul 2026): the old guard idled whenever the UTC hour was
            # < 8 or >= 17 -- a London daytime-session window copied from the other
            # desks. The Nikkei cash session is 00:00-06:30 UTC, so that gate matched
            # the ENTIRE session (hour 0-6 < 8) every night and `continue`d before the
            # candle tick ever ran: no feed.refresh(), no indicator computation, blank
            # SSL/RSI/MACD/TMO, Lancelot permanently blocked. Gate on the real Tokyo
            # phase instead (get_session_phase already computes 00:00-06:30 UTC).
            hour = now_sess.hour
            if get_session_phase(now_utc) == CLOSED:
                mins = minutes_until_next_open()
                sleep_sec = max(60, min(mins * 60, HEARTBEAT_INTERVAL)) if mins else HEARTBEAT_INTERVAL
                log.info("Market closed (UTC %s) -- next open in %s min",
                         now_sess.strftime("%H:%M"), mins if mins else "?")
                _interruptible_sleep(sleep_sec)
                _force_close_done = False
                continue

            # Force close at 06:20 UTC
            if hour == 6 and now_sess.minute >= 20:
                if stanley.in_trade and not _force_close_done:
                    price = _get_price(ig, feed)
                    log.warning("06:20 UTC force close triggered")
                    _close_trade(stanley, account, ig, price, "FORCE_CLOSE_0620")
                    _force_close_done = True
                _maybe_send_daily_summary(stanley, account)
                _interruptible_sleep(60)
                continue

            if hour == 6 and now_sess.minute >= 30:
                _maybe_send_daily_summary(stanley, account)
                _interruptible_sleep(60)
                continue

            # Position monitoring every 30 seconds
            if stanley.in_trade and (now - last_position_check) >= POSITION_INTERVAL:
                monitor_open_position(stanley, account, ig, feed)
                last_position_check = now

            # Candle tick every 5 minutes (only during trading sessions)
            if is_market_open() and (now - last_candle_tick) >= CANDLE_INTERVAL:
                run_candle_tick(feed, stanley, account, ig)
                last_candle_tick = now

            _interruptible_sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error("Main loop error: %s", exc, exc_info=True)
            notify_system_error(str(exc)[:200])
            time.sleep(30)

    # Shutdown
    log.info("")
    log.info("=" * 70)
    log.info("  NikkeiTrader AI -- Shutdown")
    log.info("=" * 70)
    if stanley.in_trade:
        log.warning("Position still open at shutdown -- closing paper record")
        price = _get_price(ig, feed)
        _close_trade(stanley, account, ig, price, "SHUTDOWN")
    stanley.print_status()
    notify_system_shutdown(stanley.capital_gbp)
    log.info("NikkeiTrader AI stopped cleanly.")


def _interruptible_sleep(seconds: float) -> None:
    """Sleep that responds to _SHUTDOWN flag."""
    end = time.monotonic() + seconds
    while not _SHUTDOWN and time.monotonic() < end:
        time.sleep(min(1, end - time.monotonic()))


if __name__ == "__main__":
    main()
