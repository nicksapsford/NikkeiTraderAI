"""
NikkeiTrader AI -- performance_nikkei.py  (Morgan)
Performance tracker and confidence engine for Arthur.
Analyses trade history to calibrate how aggressively Arthur should trade.
"""

import csv
import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

import phantom_tracker

# ─── ALBION STANDING RULE: ALL TIMESTAMPS ARE UTC ────────────────────────────
# Every timestamp this module reads or writes (trades.csv, morgan_confidence.csv,
# phantom verdicts, log lines) is UTC — written via datetime.now(timezone.utc)
# and read back as UTC. NEVER interpret any Albion timestamp as BST/local.
# Confirm UTC before analysing. (Nick's standing rule, baked in 12 Jul 2026.)

log = logging.getLogger("NikkeiTrader.Morgan")

# ── Persistent Morgan confidence store ────────────────────────────────────────
# Individual phantom-verdict feedback accumulates here, centred on 50.0. This is
# separate from get_stay_out_adjustment() (aggregate quality nudge) and the delta
# (get_confidence() - 50.0) is folded into the reported confidence once, below.
_MORGAN_STATE_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'morgan_confidence.json')
_morgan_lock = threading.Lock()
_morgan_confidence = None

# ── CSV audit trail / restore-on-startup ──────────────────────────────────────
# Every confidence change also appends a row here so the trajectory survives a
# restart and can be inspected/graphed. load_confidence() reseeds the in-memory
# store from the last row on startup. Separate from the JSON store above (which
# holds only the latest value) and from the trade CSVs.
CONFIDENCE_LOG = os.path.join(os.path.dirname(__file__), 'logs', 'morgan_confidence.csv')
CONFIDENCE_FIELDNAMES = ['timestamp', 'confidence', 'level', 'reason']


def save_confidence(confidence, reason='tick'):
    """Append one confidence observation to CONFIDENCE_LOG. level is derived as
    HIGH (>=65) / LOW (<=35) / MEDIUM otherwise. Writes the header if the file
    is new. Best-effort — never raises."""
    try:
        conf = float(confidence)
        if conf >= 65:
            level = 'HIGH'
        elif conf <= 35:
            level = 'LOW'
        else:
            level = 'MEDIUM'
        os.makedirs(os.path.dirname(CONFIDENCE_LOG), exist_ok=True)
        new_file = not os.path.exists(CONFIDENCE_LOG) or os.path.getsize(CONFIDENCE_LOG) == 0
        with open(CONFIDENCE_LOG, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CONFIDENCE_FIELDNAMES)
            if new_file:
                writer.writeheader()
            writer.writerow({
                'timestamp':  datetime.now(timezone.utc).isoformat(),
                'confidence': round(conf, 2),
                'level':      level,
                'reason':     reason,
            })
    except Exception as e:
        log.warning("Morgan: could not append confidence to CSV: %s", e)


def load_confidence():
    """Return the confidence from the last row of CONFIDENCE_LOG as a float, or
    None if the file is missing/empty/unreadable. Logs on a successful restore."""
    try:
        if not os.path.exists(CONFIDENCE_LOG) or os.path.getsize(CONFIDENCE_LOG) == 0:
            return None
        last = None
        with open(CONFIDENCE_LOG, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                last = row
        if last is None:
            return None
        conf = float(last['confidence'])
        log.info("Morgan: restored confidence %.1f from CSV (reason=%s, ts=%s)",
                 conf, last.get('reason'), last.get('timestamp'))
        return conf
    except Exception as e:
        log.warning("Morgan: could not load confidence from CSV: %s", e)
        return None


def _load_morgan_confidence():
    global _morgan_confidence
    if _morgan_confidence is None:
        try:
            with open(_MORGAN_STATE_PATH) as f:
                _morgan_confidence = float(json.load(f).get('confidence', 50.0))
        except Exception:
            _morgan_confidence = 50.0
    return _morgan_confidence


def get_confidence():
    with _morgan_lock:
        return _load_morgan_confidence()


def set_confidence(value, reason='update'):
    global _morgan_confidence
    with _morgan_lock:
        _morgan_confidence = max(0.0, min(100.0, float(value)))
        try:
            os.makedirs(os.path.dirname(_MORGAN_STATE_PATH), exist_ok=True)
            with open(_MORGAN_STATE_PATH, 'w') as f:
                json.dump({'confidence': _morgan_confidence}, f)
        except Exception as e:
            log.warning("Morgan: could not persist confidence: %s", e)
        save_confidence(_morgan_confidence, reason)
        log.info("Morgan: confidence set to %.1f", _morgan_confidence)
        return _morgan_confidence


def apply_phantom_verdict_feedback(verdict, pnl_1hr, current_confidence):
    """Apply one phantom verdict to Morgan's running confidence.

    NEUTRAL -> no change. Otherwise raw = clamp(abs(pnl_1hr)/50, 0.5, 2.0);
    CORRECT -> +raw (we were right to stay out), WRONG -> -raw (we missed a
    winner). Logs the reason and the confidence transition. Returns
    (adjustment, reason)."""
    if verdict == 'NEUTRAL':
        log.info("Morgan: phantom NEUTRAL -> confidence unchanged")
        return 0.0, "NEUTRAL: no change"
    try:
        pnl = abs(float(pnl_1hr))
    except (TypeError, ValueError):
        pnl = 0.0
    raw = max(0.5, min(2.0, pnl / 50.0))
    if verdict == 'CORRECT':
        adjustment = raw
        reason = "CORRECT: right to stay out (+%.2f)" % raw
    elif verdict == 'WRONG':
        adjustment = -raw
        reason = "WRONG: missed a winner (-%.2f)" % raw
    else:
        log.info("Morgan: phantom unknown verdict '%s' -> confidence unchanged", verdict)
        return 0.0, "UNKNOWN: no change"
    log.info("Morgan: phantom %s pnl_1hr=%s -> confidence %+.2f (from %.1f to %.1f)",
             verdict, pnl_1hr, adjustment, current_confidence,
             max(0.0, min(100.0, current_confidence + adjustment)))
    return adjustment, reason


# Guard so the poller is only ever started once per process.
_phantom_poller_thread = None


def process_new_phantom_verdicts(get_confidence_fn=None, set_confidence_fn=None):
    """Daemon poller: every 300s apply any unprocessed phantom verdicts to
    Morgan's running confidence (clamped 0-100) and mark them processed.
    Idempotent — a second call is a no-op if the poller is already alive."""
    global _phantom_poller_thread
    if get_confidence_fn is None:
        get_confidence_fn = get_confidence
    if set_confidence_fn is None:
        set_confidence_fn = set_confidence

    if _phantom_poller_thread is not None and _phantom_poller_thread.is_alive():
        log.info("Morgan: phantom poller already running -- not starting a second.")
        return _phantom_poller_thread

    def _loop():
        log.info("Morgan: phantom verdict poller started -- scanning every 300s.")
        while True:
            try:
                rows = phantom_tracker.get_unprocessed_verdicts()
                if rows:
                    confidence = get_confidence_fn()
                    processed = []
                    for r in rows:
                        adjustment, _reason = apply_phantom_verdict_feedback(
                            r.get('verdict'), r.get('pnl_1hr'), confidence
                        )
                        confidence = max(0.0, min(100.0, confidence + adjustment))
                        ts = r.get('timestamp')
                        if ts:
                            processed.append(ts)
                    set_confidence_fn(confidence)
                    if processed:
                        phantom_tracker.mark_processed(processed)
                    log.info("Morgan: processed %d phantom verdict(s) -> confidence %.1f",
                             len(processed), confidence)
            except Exception as e:
                log.error("Morgan: phantom poller error: %s", e)
            time.sleep(300)

    _phantom_poller_thread = threading.Thread(
        target=_loop, daemon=True, name="MorganPhantomPoller"
    )
    _phantom_poller_thread.start()
    log.info("Morgan: phantom poller thread started.")
    return _phantom_poller_thread


def _apply_phantom_delta(score):
    """Fold the persisted individual-phantom confidence delta into a base score
    exactly once. Centred on 50 so a neutral store leaves the score unchanged."""
    return int(max(0, min(100, score + (get_confidence() - 50.0))))


def get_stay_out_adjustment():
    """Morgan self-improvement: nudge confidence by STAY OUT decision quality.
    >70% correct -> +5 ; <40% correct -> -5 ; 40-70% or <8 judged -> 0."""
    summary = phantom_tracker.get_summary(last_n=10)
    if summary.get('judged', 0) < 8:
        return 0.0
    quality = summary['quality_score']
    if quality is None:
        return 0.0
    if quality > 70:
        log.info("Morgan: STAY OUT quality %s%% -> confidence +5", quality)
        return 5.0
    if quality < 40:
        log.info("Morgan: STAY OUT quality %s%% -> confidence -5", quality)
        return -5.0
    return 0.0

LOG_DIR    = Path(__file__).parent / "logs"
TRADES_LOG = LOG_DIR / "nikkei_trades.csv"
REVIEW_DIR = LOG_DIR

_cache: dict = {}
_cache_valid = False


def invalidate_cache() -> None:
    global _cache_valid
    _cache_valid = False


def _load_trades(trades_log: Path = TRADES_LOG) -> pd.DataFrame:
    if not trades_log.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(trades_log)
        if df.empty:
            return df
        df["pnl_gbp"] = pd.to_numeric(df["pnl_gbp"], errors="coerce").fillna(0)
        df["_dt"]     = pd.to_datetime(df["entry_time"], errors="coerce")
        return df
    except Exception as exc:
        log.warning("Could not load trades: %s", exc)
        return pd.DataFrame()


def _compute_confidence(df: pd.DataFrame) -> dict:
    """
    Confidence score 0-100 based on recent performance.
    Conservative mode activates below 25.
    """
    if df.empty or len(df) < 5:
        return {
            "confidence_score":  _apply_phantom_delta(max(0, min(100, 50 + get_stay_out_adjustment()))),
            "confidence_level":  "MEDIUM",
            "conservative":      False,
            "total_trades":      0,
            "win_rate":          0.0,
            "recent_5":          [],
            "streak_type":       "",
            "streak_count":      0,
            "strongest_conditions": [],
            "weakest_conditions":   [],
        }

    pnls    = df["pnl_gbp"].values
    wins    = sum(1 for p in pnls if p >= 0)
    total   = len(pnls)
    win_rate = wins / total * 100

    recent_20 = df.tail(20)["pnl_gbp"].values
    recent_5  = ["WIN" if p >= 0 else "LOSS" for p in df.tail(5)["pnl_gbp"].values]
    r20_wins  = sum(1 for p in recent_20 if p >= 0)
    r20_wr    = r20_wins / len(recent_20) * 100 if recent_20.size > 0 else 50.0

    avg_win  = sum(p for p in pnls if p > 0) / max(1, wins)
    avg_loss = abs(sum(p for p in pnls if p < 0)) / max(1, total - wins)
    rr       = avg_win / avg_loss if avg_loss > 0 else 1.0

    streak_type  = ""
    streak_count = 0
    for p in reversed(pnls):
        is_win = p >= 0
        if streak_count == 0:
            streak_type  = "WIN" if is_win else "LOSS"
            streak_count = 1
        elif (streak_type == "WIN" and is_win) or (streak_type == "LOSS" and not is_win):
            streak_count += 1
        else:
            break

    score = 50.0
    score += (r20_wr - 50) * 0.6
    score += (rr - 1.0)    * 5.0
    if streak_type == "WIN"  and streak_count >= 3: score += 10
    if streak_type == "LOSS" and streak_count >= 3: score -= 15
    base_score = max(0, min(100, round(score)))
    score = int(max(0, min(100, base_score + get_stay_out_adjustment())))
    score = _apply_phantom_delta(score)

    if score >= 75:   level = "HIGH"
    elif score >= 50: level = "MEDIUM"
    elif score >= 25: level = "LOW"
    else:             level = "VERY_LOW"

    # Direction breakdown
    strongest = []
    weakest   = []
    if total >= 10:
        for direction in ["LONG", "SHORT"]:
            sub = df[df["direction"] == direction]
            if len(sub) >= 5:
                sub_wins = sum(1 for p in sub["pnl_gbp"] if p >= 0)
                wr_dir   = sub_wins / len(sub) * 100
                label    = f"{direction}: {wr_dir:.0f}% WR ({len(sub)} trades)"
                if wr_dir >= 60:
                    strongest.append(label)
                elif wr_dir < 45:
                    weakest.append(label)

        if "session_phase" in df.columns:
            for phase in ["MORNING", "AFTERNOON"]:
                sub = df[df["session_phase"] == phase]
                if len(sub) >= 5:
                    sub_wins = sum(1 for p in sub["pnl_gbp"] if p >= 0)
                    wr_p     = sub_wins / len(sub) * 100
                    label    = f"{phase}: {wr_p:.0f}% WR ({len(sub)} trades)"
                    if wr_p >= 60:
                        strongest.append(label)
                    elif wr_p < 45:
                        weakest.append(label)

    return {
        "confidence_score":     score,
        "confidence_level":     level,
        "conservative":         score < 25,
        "total_trades":         total,
        "win_rate":             round(win_rate, 1),
        "recent_5":             list(reversed(recent_5)),
        "streak_type":          streak_type,
        "streak_count":         streak_count,
        "strongest_conditions": strongest,
        "weakest_conditions":   weakest,
    }


def _compute_direction_session_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"direction": {}, "session": {}}
    direction_stats = {}
    for d in ["LONG", "SHORT"]:
        sub  = df[df["direction"] == d]
        if len(sub) == 0:
            continue
        wins = int(sum(1 for p in sub["pnl_gbp"] if p >= 0))
        direction_stats[d] = {
            "trades":   int(len(sub)),
            "wins":     wins,
            "win_rate": round(wins / len(sub) * 100, 1),
            "net_pnl":  round(float(sub["pnl_gbp"].sum()), 2),
        }
    session_stats = {}
    if "session_phase" in df.columns:
        for s in ["MORNING", "AFTERNOON", "LUNCH_BREAK"]:
            sub = df[df["session_phase"] == s]
            if len(sub) == 0:
                continue
            wins = int(sum(1 for p in sub["pnl_gbp"] if p >= 0))
            session_stats[s] = {
                "trades":   int(len(sub)),
                "wins":     wins,
                "win_rate": round(wins / len(sub) * 100, 1),
                "net_pnl":  round(float(sub["pnl_gbp"].sum()), 2),
            }
    return {"direction": direction_stats, "session": session_stats}


def get_performance_context(trades_log: Path = TRADES_LOG) -> str:
    """Return formatted performance context string for Arthur."""
    df   = _load_trades(trades_log)
    perf = _compute_confidence(df)
    lines = [
        "SELF PERFORMANCE AWARENESS (Morgan)",
        f"  Confidence:     {perf['confidence_score']}/100 {perf['confidence_level']}",
        f"  Conservative:   {'YES -- STAY_OUT mode' if perf['conservative'] else 'No'}",
        f"  Total trades:   {perf['total_trades']}",
        f"  Win rate:       {perf['win_rate']}%",
        f"  Current streak: {perf['streak_count']} {perf['streak_type']}",
        f"  Recent (last 5): {' | '.join(perf['recent_5']) if perf['recent_5'] else 'no trades yet'}",
    ]
    if perf["strongest_conditions"]:
        lines.append("  Strongest: " + ", ".join(perf["strongest_conditions"]))
    if perf["weakest_conditions"]:
        lines.append("  Weakest:   " + ", ".join(perf["weakest_conditions"]))
    lines.append(
        "\n  Confidence guide: HIGH(75+)=normal, MED(50-74)=raise bar, "
        "LOW(25-49)=exceptional only, VERY_LOW(<25)=STAY OUT hard rule"
    )
    return "\n".join(lines)


def get_perf_dashboard_dict(trades_log: Path = TRADES_LOG) -> dict:
    """Return performance data dict for dashboard rendering."""
    global _cache, _cache_valid
    if _cache_valid:
        return _cache
    df   = _load_trades(trades_log)
    perf = _compute_confidence(df)
    breakdown = _compute_direction_session_stats(df)
    _cache = {**perf, "breakdown": breakdown}
    _cache_valid = True
    return _cache


def generate_milestone_review(trades_log: Path, milestone_num: int) -> None:
    """Save a milestone review to logs/arthur_review_XX.txt every 50 trades."""
    df = _load_trades(trades_log)
    if df.empty:
        return
    perf      = _compute_confidence(df)
    breakdown = _compute_direction_session_stats(df)
    review_file = REVIEW_DIR / f"arthur_review_{milestone_num:02d}.txt"
    lines = [
        "=" * 60,
        f"NikkeiTrader AI -- Arthur Milestone Review #{milestone_num}",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Trades completed: {perf['total_trades']}",
        "=" * 60,
        "",
        "PERFORMANCE SUMMARY",
        f"  Win rate:       {perf['win_rate']}%",
        f"  Confidence:     {perf['confidence_score']}/100 {perf['confidence_level']}",
        f"  Current streak: {perf['streak_count']} {perf['streak_type']}",
        "",
        "DIRECTION BREAKDOWN",
    ]
    for d, stats in breakdown["direction"].items():
        lines.append(
            f"  {d}: {stats['trades']} trades | {stats['win_rate']}% WR | "
            f"net GBP {stats['net_pnl']:+.2f}"
        )
    lines.append("\nSESSION BREAKDOWN")
    for s, stats in breakdown["session"].items():
        lines.append(
            f"  {s}: {stats['trades']} trades | {stats['win_rate']}% WR | "
            f"net GBP {stats['net_pnl']:+.2f}"
        )
    if perf["strongest_conditions"]:
        lines.append("\nSTRONGEST CONDITIONS")
        for c in perf["strongest_conditions"]:
            lines.append(f"  + {c}")
    if perf["weakest_conditions"]:
        lines.append("\nWEAKEST CONDITIONS (consider avoiding)")
        for c in perf["weakest_conditions"]:
            lines.append(f"  - {c}")
    lines.append("\n" + "=" * 60)
    with open(review_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("Milestone review saved: %s", review_file)


if __name__ == "__main__":
    logging.Formatter.converter = time.gmtime  # ALBION RULE: emit log timestamps in UTC
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    log.info("Morgan self-test")
    context = get_performance_context()
    log.info("Performance context:\n%s", context)
    log.info("Morgan self-test complete.")
