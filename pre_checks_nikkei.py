"""
NikkeiTrader AI -- pre_checks_nikkei.py  (Lancelot)
Hard filter guardian. Runs before Arthur is ever called.
Japan 225 spread betting specific: session-phase aware, UK kill limits.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd

from data_feed_nikkei import (
    get_session_phase,
    MORNING, AFTERNOON,
    PRE_OPEN, LUNCH_BREAK, CLOSING, CLOSED,
)

log = logging.getLogger("NikkeiTrader.Lancelot")

# ── Thresholds ────────────────────────────────────────────────────────────────

DAILY_LOSS_LIMIT_GBP    = 30.0   # 6% of £500 -- hard stop for the day
MAX_CONSECUTIVE_LOSSES  = 5      # kill switch after this many in a row
COOLDOWN_MINUTES        = 30     # wait after a loss before next entry
MIN_TMO_FOR_ENTRY       = 0.3    # 5m TMO must exceed this magnitude
CHOPPY_RSI_THRESHOLD    = 5.0    # RSI within 5 of 50 = choppy
CHOPPY_TMO_THRESHOLD    = 0.5    # TMO within 0.5 of zero = choppy
CHOPPY_SIGNALS_REQUIRED = 2      # block if this many choppy signals


# ── Result builders ───────────────────────────────────────────────────────────

def _pass() -> dict:
    return {"passed": True, "reason": None}


def _fail(reason: str, block_direction: str = "BOTH") -> dict:
    log.info("  PRE-CHECK FAILED: %s", reason)
    return {"passed": False, "reason": reason, "block_direction": block_direction, "decision": "STAY_OUT"}


def _trigger_kill_switch(account, reason: str) -> dict:
    """Tiered kill switch. 1st trigger = 6h wait; 2nd = 12h; 3rd+ = 24h."""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    history = [
        t for t in account.kill_history
        if datetime.fromisoformat(t).replace(tzinfo=timezone.utc) > cutoff
    ]
    history.append(now.isoformat())
    count = len(history)
    if count == 1:
        tier, wait_hours = 1, 6
    elif count == 2:
        tier, wait_hours = 2, 12
    else:
        tier, wait_hours = 3, 24
    account.kill_history        = history
    account.kill_switch_active  = True
    account.kill_switch_reason  = reason
    account.kill_switch_tier    = tier
    account.kill_switch_until   = (now + timedelta(hours=wait_hours)).isoformat()
    log.warning(
        "KILL SWITCH (Tier %d) -- %s | auto-resume in %dh",
        tier, reason, wait_hours,
    )
    result = _fail(reason)
    result["kill_switch_triggered"] = True
    result["kill_tier"] = tier
    return result


# ── Individual checks ─────────────────────────────────────────────────────────

def check_kill_switch(account) -> dict:
    if account.kill_switch_active:
        return _fail(f"KILL SWITCH ACTIVE -- {account.kill_switch_reason or 'triggered'}")
    return _pass()


def check_daily_loss_limit(account) -> dict:
    daily_pnl = account.daily_pnl_gbp
    if daily_pnl <= -DAILY_LOSS_LIMIT_GBP:
        reason = (
            f"Daily loss limit hit (GBP {daily_pnl:.2f} / "
            f"limit GBP -{DAILY_LOSS_LIMIT_GBP:.2f})"
        )
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_consecutive_losses(account) -> dict:
    n = account.consecutive_losses
    if n >= MAX_CONSECUTIVE_LOSSES:
        return _trigger_kill_switch(account, f"{n} consecutive losses")
    return _pass()


def check_kill_switch_reset(account) -> bool:
    """Auto-reset kill switch after the tier wait period. Returns True if reset."""
    if not account.kill_switch_active:
        return False
    if not account.kill_switch_until:
        return False
    until = datetime.fromisoformat(account.kill_switch_until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < until:
        return False
    tier = account.kill_switch_tier
    account.kill_switch_active = False
    account.kill_switch_reason = ""
    account.kill_switch_until  = None
    account.consecutive_losses = 0
    msg = f"Kill switch reset (Tier {tier}) -- resuming"
    if tier >= 3:
        msg += ". Manual review recommended."
    log.info(msg)
    return True


def check_cooldown(account) -> dict:
    """Block entries for COOLDOWN_MINUTES after a losing trade."""
    last_loss_time = account.last_loss_time
    if not last_loss_time:
        return _pass()
    try:
        if isinstance(last_loss_time, str):
            last_loss = datetime.fromisoformat(last_loss_time)
        else:
            last_loss = last_loss_time
        if last_loss.tzinfo is None:
            last_loss = last_loss.replace(tzinfo=timezone.utc)
        minutes_since = (datetime.now(timezone.utc) - last_loss).total_seconds() / 60
        if minutes_since < COOLDOWN_MINUTES:
            remaining = int(COOLDOWN_MINUTES - minutes_since)
            return _fail(
                f"Cooldown active -- {remaining} min remaining after last loss."
            )
    except Exception as exc:
        log.warning("Cooldown check error: %s", exc)
    return _pass()


def check_session_phase() -> dict:
    """
    Only allow new entries during MORNING or AFTERNOON.
    LUNCH_BREAK, CLOSING, PRE_OPEN, CLOSED all block new entries.
    """
    phase = get_session_phase()
    if phase in (MORNING, AFTERNOON):
        return _pass()
    messages = {
        PRE_OPEN:  "Pre-open (before 08:15 UK) -- no entries until Morning Prime",
        LUNCH_BREAK:"Lunch lull (12:00-13:30 UK) -- no new entries during lunch",
        CLOSING:   "Closing session (15:30-16:30 UK) -- no new entries",
        CLOSED:    "Market closed -- no entries outside trading hours",
    }
    return _fail(messages.get(phase, f"Session phase {phase} -- no entries"))


def check_daily_trend_filter(bar_1d: Optional[pd.Series], direction: str) -> dict:
    """
    Daily SSL sets the bias for the day.
    Daily BULL: LONG only
    Daily BEAR: SHORT only
    Daily NEUTRAL (NaN ssl_bull): both allowed
    """
    if bar_1d is None:
        return _pass()
    ssl_1d = bar_1d.get("ssl_bull")
    if pd.isna(ssl_1d):
        return _pass()
    if ssl_1d and direction == "SHORT":
        return _fail(
            "Daily SSL is BULL -- only LONG entries allowed today. "
            f"Proposed direction {direction} blocked.",
            block_direction="SHORT",
        )
    if not ssl_1d and direction == "LONG":
        return _fail(
            "Daily SSL is BEAR -- only SHORT entries allowed today. "
            f"Proposed direction {direction} blocked.",
            block_direction="LONG",
        )
    return _pass()


def check_ssl_agreement(bar_1h: pd.Series, bar_5m: pd.Series, direction: str = "BOTH") -> dict:
    """1h and 5m SSL must both match the intended direction (bidirectional,
    System 1 Review 17 Jul): BULL for a LONG session, BEAR for a SHORT session.
    Falls back to mutual 1h/5m agreement when direction is unknown."""
    ssl_1h = bar_1h.get("ssl_bull")
    ssl_5m = bar_5m.get("ssl_bull")
    if pd.isna(ssl_1h) or pd.isna(ssl_5m):
        return _fail("SSL Cloud data unavailable")
    d1h = "BULL" if ssl_1h else "BEAR"
    d5m = "BULL" if ssl_5m else "BEAR"
    if direction in ("LONG", "SHORT"):
        want_bull = (direction == "LONG")
        if bool(ssl_1h) != want_bull or bool(ssl_5m) != want_bull:
            need = "BULL" if want_bull else "BEAR"
            return _fail(f"SSL not aligned {need} for {direction} -- 1h={d1h}, 5m={d5m}.")
        return _pass()
    # Direction unknown: require 1h and 5m to at least agree with each other.
    if ssl_1h != ssl_5m:
        return _fail(f"SSL conflict -- 1h={d1h} but 5m={d5m}. Market in transition.")
    return _pass()


def check_1h_rsi_confirms(bar_1h: pd.Series, direction: str) -> dict:
    """1h RSI at least 52 for LONG, at most 48 for SHORT."""
    rsi_1h = bar_1h.get("rsi")
    if pd.isna(rsi_1h):
        return _pass()
    if direction == "LONG" and rsi_1h < 52:
        return _fail(
            f"1h RSI is {rsi_1h:.1f} -- need at least 52 for LONG entry.",
            block_direction="LONG",
        )
    if direction == "SHORT" and rsi_1h > 48:
        return _fail(
            f"1h RSI is {rsi_1h:.1f} -- need at most 48 for SHORT entry.",
            block_direction="SHORT",
        )
    return _pass()


def check_5m_tmo_momentum(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """5m TMO must show meaningful momentum: > +0.3 for LONG, < -0.3 for SHORT."""
    ssl_bull = bar_1h.get("ssl_bull")
    tmo_5m   = bar_5m.get("tmo_main")
    if pd.isna(ssl_bull) or pd.isna(tmo_5m):
        return _pass()
    if ssl_bull and tmo_5m < MIN_TMO_FOR_ENTRY:
        return _fail(
            f"Bullish setup but 5m TMO only {tmo_5m:.3f} -- "
            f"need >{MIN_TMO_FOR_ENTRY} for momentum.",
            block_direction="LONG",
        )
    if not ssl_bull and tmo_5m > -MIN_TMO_FOR_ENTRY:
        return _fail(
            f"Bearish setup but 5m TMO only {tmo_5m:.3f} -- "
            f"need <-{MIN_TMO_FOR_ENTRY} for momentum.",
            block_direction="SHORT",
        )
    return _pass()


def check_choppy_market(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """Block if RSI and TMO both near zero -- market is directionless."""
    choppy = []
    rsi_5m = bar_5m.get("rsi")
    if pd.notna(rsi_5m) and abs(rsi_5m - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy.append(f"5m RSI near 50 ({rsi_5m:.1f})")
    tmo_5m = bar_5m.get("tmo_main")
    if pd.notna(tmo_5m) and abs(tmo_5m) <= CHOPPY_TMO_THRESHOLD:
        choppy.append(f"5m TMO near zero ({tmo_5m:.3f})")
    rsi_1h = bar_1h.get("rsi")
    if pd.notna(rsi_1h) and abs(rsi_1h - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy.append(f"1h RSI near 50 ({rsi_1h:.1f})")
    if len(choppy) >= CHOPPY_SIGNALS_REQUIRED:
        return _fail(
            f"Choppy market: {', '.join(choppy)}. No clear direction -- best trade is no trade."
        )
    return _pass()


def check_candle_confirmed(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """Last 5m candle must be green for LONG, red for SHORT."""
    ssl_bull    = bar_1h.get("ssl_bull")
    open_price  = bar_5m.get("open")
    close_price = bar_5m.get("close")
    if pd.isna(ssl_bull) or pd.isna(open_price) or pd.isna(close_price):
        return _pass()
    candle_green = close_price >= open_price
    if ssl_bull and not candle_green:
        return _fail(
            "Bullish setup but last 5m candle is RED -- waiting for green confirmation.",
            block_direction="LONG",
        )
    if not ssl_bull and candle_green:
        return _fail(
            "Bearish setup but last 5m candle is GREEN -- waiting for red confirmation.",
            block_direction="SHORT",
        )
    return _pass()


# ── Master runner ─────────────────────────────────────────────────────────────

def run_all_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    proposed_direction: str = "BOTH",
    relax_soft: bool = False,
) -> dict:
    """
    Run all pre-checks in order. Returns on first failure.
    Claude is only called if this returns passed=True.

    relax_soft (Guinevere 2.0 HIGH-ALERT active consultation, 31 Jul 2026): when True,
    SKIP only the three SOFT quality checks -- 1h SSL agreement, 5m TMO momentum, Candle
    confirmed -- so a HIGH-confidence Guinevere signal can force an Arthur consult on an
    otherwise soft-blocked tick. The SAFETY checks (kill switch / daily loss / consecutive
    losses / cooldown / session phase) run FIRST and are NEVER relaxed, and the Daily trend
    filter, 1h RSI and Not-choppy (volatility) quality checks are ALSO still enforced -- so no
    risk control can ever be bypassed. Arthur still needs >= 60 confidence to act (enforced in main).
    """
    log.info("--- Lancelot running pre-checks ---")
    _RELAXABLE = {"1h SSL agreement", "5m TMO momentum", "Candle confirmed"}

    safety_checks = [
        ("Kill switch",        lambda: check_kill_switch(account)),
        ("Daily loss limit",   lambda: check_daily_loss_limit(account)),
        ("Consecutive losses", lambda: check_consecutive_losses(account)),
        ("Cooldown period",    lambda: check_cooldown(account)),
        ("Session phase",      lambda: check_session_phase()),
    ]
    for name, fn in safety_checks:
        result = fn()
        if not result["passed"]:
            log.info("  [FAIL] %s -- %s", name, result["reason"])
            return result
        log.info("  [PASS] %s", name)

    if current_trade is None:
        direction = proposed_direction
        ssl_1h    = bar_1h.get("ssl_bull")
        if direction == "BOTH":
            direction = "LONG" if ssl_1h else "SHORT"

        quality_checks = [
            ("Daily trend filter",  lambda: check_daily_trend_filter(bar_1d, direction)),
            ("1h SSL agreement",    lambda: check_ssl_agreement(bar_1h, bar_5m, direction)),
            ("1h RSI confirming",   lambda: check_1h_rsi_confirms(bar_1h, direction)),
            ("5m TMO momentum",     lambda: check_5m_tmo_momentum(bar_1h, bar_5m)),
            ("Not choppy",          lambda: check_choppy_market(bar_1h, bar_5m)),
            ("Candle confirmed",    lambda: check_candle_confirmed(bar_1h, bar_5m)),
        ]
        for name, fn in quality_checks:
            if relax_soft and name in _RELAXABLE:
                log.info("  [RELAXED] %s -- Guinevere HIGH alert (soft check skipped)", name)
                continue
            result = fn()
            if not result["passed"]:
                log.info("  [FAIL] %s -- %s", name, result["reason"])
                return result
            log.info("  [PASS] %s", name)

    log.info("  All pre-checks passed -- ready for Arthur%s",
             " (Guinevere HIGH-alert relaxed)" if relax_soft else "")
    return _pass()


def run_individual_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    proposed_direction: str = "BOTH",
) -> dict:
    """Run each check individually for dashboard display. Returns dict of name -> bool."""
    ssl_1h    = bar_1h.get("ssl_bull")
    direction = proposed_direction if proposed_direction in ("LONG", "SHORT") else ("LONG" if ssl_1h else "SHORT")
    checks    = {}
    checks["Kill Switch OK"]       = check_kill_switch(account)["passed"]
    checks["Daily Loss OK"]        = check_daily_loss_limit(account)["passed"]
    checks["Consecutive Losses OK"]= check_consecutive_losses(account)["passed"]
    checks["Cooldown OK"]          = check_cooldown(account)["passed"]
    checks["Session Phase OK"]     = check_session_phase()["passed"]
    if current_trade is None:
        checks["Daily Trend OK"]   = check_daily_trend_filter(bar_1d, direction)["passed"]
        checks["SSL Aligned"]      = check_ssl_agreement(bar_1h, bar_5m, direction)["passed"]
        checks["1h RSI Confirming"]= check_1h_rsi_confirms(bar_1h, direction)["passed"]
        checks["Momentum Strong"]  = check_5m_tmo_momentum(bar_1h, bar_5m)["passed"]
        checks["Not Choppy"]       = check_choppy_market(bar_1h, bar_5m)["passed"]
        checks["Candle Confirmed"] = check_candle_confirmed(bar_1h, bar_5m)["passed"]
    else:
        for k in ["Daily Trend OK", "SSL Aligned", "1h RSI Confirming",
                  "Momentum Strong", "Not Choppy", "Candle Confirmed"]:
            checks[k] = None
    return checks


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    import types
    log.info("Lancelot self-test")
    account_ok = types.SimpleNamespace(
        kill_switch_active=False, kill_switch_reason="",
        kill_switch_tier=0, kill_switch_until=None,
        kill_history=[], daily_pnl_gbp=-5.0,
        consecutive_losses=1, last_loss_time=None,
    )
    bar_1h = pd.Series({
        "ssl_bull": True, "rsi": 62.0, "macd_histogram": 120.0,
        "tmo_main": 2.1, "tmo_smooth": 1.5, "chande_mo": 45.0,
        "money_flow": 100.0, "open": 8200.0, "close": 8250.0,
    })
    bar_5m = pd.Series({
        "ssl_bull": True, "rsi": 58.0, "macd_histogram": 5.0,
        "tmo_main": 0.8, "tmo_smooth": 0.5, "chande_mo": 30.0,
        "money_flow": 80.0, "open": 8230.0, "close": 8250.0,
    })
    result = run_all_pre_checks(bar_1h, bar_5m, account_ok, proposed_direction="LONG")
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")
    log.info("Lancelot self-test complete.")
