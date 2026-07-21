"""
NikkeiTrader AI -- dashboard_nikkei.py
Two-page browser dashboard at http://localhost:5008
Page 1: Live trading view -- Daily/1h/5m trend cards, Arthur's full-width
        decision panel, performance, open position, pre-checks, calendar.
Page 2: P&L, performance detail, monthly breakdown, full trade history.
Uses Response() to avoid Jinja2 template conflicts.
All JS uses string concatenation -- no template literals.
"""

import csv
import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, Response, jsonify, request

import guinevere_news   # Part 5: Guinevere news sentiment + keyword editor

log = logging.getLogger("NikkeiTrader.Dashboard")
# ALBION STANDING RULE: all log timestamps are UTC (never BST/local). See main_nikkeitrader.py.
logging.Formatter.converter = time.gmtime
logging.basicConfig(level=logging.WARNING)

BASE_DIR         = Path(__file__).resolve().parent

# Semantic version -- read from the VERSION file so the header always shows the
# current version without a code change.
_VER = BASE_DIR / "VERSION"
APP_VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"


def get_git_hash():
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        return result.stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


VERSION_STRING = "v" + str(APP_VERSION) + " (" + get_git_hash() + ")"


def get_stay_out_quality():
    # ALBION RULE: phantom_trades.csv timestamps are UTC — never BST/local.
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'phantom_trades.csv')
    if not os.path.exists(csv_path):
        return {'status': 'No data yet', 'decisions': [], 'quality_score': None,
                'net_saved': None, 'correct': 0, 'wrong': 0, 'neutral': 0}
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        last_20 = rows[-50:]
        correct = sum(1 for r in last_20 if r.get('verdict') == 'CORRECT')
        wrong   = sum(1 for r in last_20 if r.get('verdict') == 'WRONG')
        neutral = sum(1 for r in last_20 if r.get('verdict') == 'NEUTRAL')
        total   = (correct + wrong + neutral)
        quality_score = round((correct/total)*100) if total else 0
        net_saved  = sum(float(r.get('pnl_1hr', 0) or 0) for r in last_20 if r.get('verdict') == 'CORRECT')
        net_missed = sum(float(r.get('pnl_1hr', 0) or 0) for r in last_20 if r.get('verdict') == 'WRONG')
        return {'status': 'ok', 'decisions': last_20, 'quality_score': quality_score,
                'net_saved': net_saved, 'net_missed': net_missed, 'correct': correct, 'wrong': wrong, 'neutral': neutral}
    except Exception as e:
        return {'status': 'Error: ' + str(e), 'decisions': []}


PORT             = 5008
LOG_DIR          = BASE_DIR / "logs"
TRADES_LOG       = LOG_DIR / "nikkei_trades.csv"
SHUTDOWN_FLAG    = LOG_DIR / "shutdown.flag"
STARTING_CAPITAL = 1000.0

app = Flask(__name__)

_state_lock = threading.Lock()
_state: dict = {
    "mode":         "PAPER",
    "version":      APP_VERSION,
    "phase":        "--",
    "nikkei_level":   0.0,
    "connector_status": "yahoo",
    "capital":      1000.0,
    "daily_pnl":    0.0,
    "total_trades": 0,
    "win_rate":     0.0,
    "in_trade":     False,
    "current_trade": None,
    "decision":     None,
    "panel_mode":   "pre_checks",
    "pre_checks":   None,
    "checklist":    {},
    "trend_1d":     "NEUTRAL",
    "trend_1h":     "NEUTRAL",
    "signal_5m":    "NEUTRAL",
    "indicators_1d": {},
    "indicators_1h": {},
    "indicators_5m": {},
    "perf":         None,
    "calendar":     "",
    "kill_switch":  False,
    "kill_tier":    0,
    "updated_at":   "--",
}


def push_state(new_state: dict) -> None:
    """Called from the main loop to update dashboard state."""
    with _state_lock:
        _state.update(new_state)


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


def _flat_field(obj, name, default=None):
    """Read an attribute (object) or key (dict) uniformly."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def compute_flat_fields(s: dict) -> dict:
    """
    Derive flat Lancelot/Arthur/locked-P&L fields from the raw dashboard state
    and return them for merging into /api/state. Never raises — on any error
    it falls back to safe defaults so /api/state cannot 500.
    """
    flat = {
        "lancelot_status":       "BLOCKED",
        "lancelot_fails":        0,
        "lancelot_fail_reasons": [],
        "arthur_decision":       "---",
        "arthur_confidence":     None,
        "arthur_consulted":      False,
        "locked_pnl":            None,
    }
    try:
        decision   = s.get("decision") or {}
        pre_checks = s.get("pre_checks") or {}
        panel_mode = s.get("panel_mode", "pre_checks")
        trade      = s.get("current_trade")
        in_trade   = bool(s.get("in_trade")) or trade is not None
        kill       = bool(s.get("kill_switch"))

        # Did Arthur get reached? pre-checks passed => "claude" panel, or a real
        # decision object exists. A kill/hard block forces not-consulted.
        real_decision = bool(isinstance(decision, dict) and decision.get("decision"))
        consulted = (panel_mode == "claude" or real_decision) and not kill
        flat["arthur_consulted"] = consulted

        # ── Lancelot pre-checks ──────────────────────────────────────────
        fail_reasons = [k for k, v in pre_checks.items() if v is False]
        flat["lancelot_fails"]        = len(fail_reasons)
        flat["lancelot_fail_reasons"] = fail_reasons
        if consulted:
            flat["lancelot_status"] = "CLEAR"
        elif fail_reasons:
            flat["lancelot_status"] = "%d FAILS" % len(fail_reasons)
        else:
            flat["lancelot_status"] = "BLOCKED"

        # ── Arthur decision ──────────────────────────────────────────────
        if not consulted:
            flat["arthur_decision"] = "---"
        elif in_trade:
            flat["arthur_decision"] = "HOLD"
        else:
            raw = (decision.get("decision") or "").upper() if isinstance(decision, dict) else ""
            dec_map = {
                "ENTER_LONG":  "LONG",
                "ENTER_SHORT": "SHORT",
                "STAY_OUT":    "STAY OUT",
                "HOLD":        "HOLD",
            }
            if raw.startswith("EXIT"):
                flat["arthur_decision"] = "STAY OUT"
            else:
                flat["arthur_decision"] = dec_map.get(raw, "STAY OUT" if raw else "---")

        # ── Arthur confidence ────────────────────────────────────────────
        if consulted and isinstance(decision, dict) and decision.get("confidence") is not None:
            try:
                flat["arthur_confidence"] = int(float(decision.get("confidence")))
            except (TypeError, ValueError):
                flat["arthur_confidence"] = None

        # ── Locked-in P&L (P&L if stop is hit at current levels) ──────────
        if trade is not None:
            direction  = (_flat_field(trade, "direction") or "").upper()
            entry      = _flat_field(trade, "entry_price")
            stop       = _flat_field(trade, "stop_loss")
            stake      = _flat_field(trade, "stake")
            if entry is not None and stop is not None and stake is not None:
                entry = float(entry); stop = float(stop); stake = float(stake)
                # Bug C: only surface a Locked figure once the trailing stop has
                # trailed to break-even (genuine secured profit); until then None -> "---".
                if direction == "LONG" and stop >= entry:
                    flat["locked_pnl"] = round((stop - entry) * stake, 2)
                elif direction == "SHORT" and stop <= entry:
                    flat["locked_pnl"] = round((entry - stop) * stake, 2)
    except Exception:
        pass
    return flat


# ---------------------------------------------------------------------------
# Trade log readers (Page 2)
# ---------------------------------------------------------------------------

def load_trades() -> list:
    """Load all Nikkei trades from CSV, most recent first."""
    if not TRADES_LOG.exists():
        return []
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return []
        trades = []
        for _, row in df.iterrows():
            pnl = float(row["pnl_gbp"])
            trades.append({
                "direction":   row["direction"],
                "entry_time":  row["entry_time"],
                "exit_time":   row["exit_time"],
                "entry_price": f"{float(row['entry_price']):,.1f}",
                "exit_price":  f"{float(row['exit_price']):,.1f}",
                "points":      f"{float(row['points_gained']):+.1f}",
                "pnl":         f"{pnl:+.2f}",
                "pnl_class":   "win" if pnl >= 0 else "loss",
                "reason":      row["exit_reason"],
            })
        return list(reversed(trades))
    except Exception:
        return []


def load_account_stats() -> dict:
    empty = {
        "capital": STARTING_CAPITAL, "total_pnl": 0.0,
        "total_return": 0.0, "total_trades": 0,
        "winners": 0, "losers": 0, "win_rate": 0.0,
        "daily_pnl": 0.0,
    }
    if not TRADES_LOG.exists():
        return empty
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return empty
        capital      = float(df["capital_after"].iloc[-1])
        pnls         = df["pnl_gbp"].astype(float)
        total_pnl    = capital - STARTING_CAPITAL
        total_return = (capital / STARTING_CAPITAL - 1) * 100
        winners      = int(len(pnls[pnls > 0]))
        losers       = int(len(pnls[pnls < 0]))
        total        = int(len(pnls))
        win_rate     = (winners / total * 100) if total > 0 else 0.0
        today        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_df     = df[df["date"] == today] if "date" in df.columns else df
        daily_pnl    = today_df["pnl_gbp"].astype(float).sum() if not today_df.empty else 0.0
        return {
            "capital": capital, "total_pnl": total_pnl,
            "total_return": total_return, "total_trades": total,
            "winners": winners, "losers": losers, "win_rate": win_rate,
            "daily_pnl": daily_pnl,
        }
    except Exception:
        return empty


def load_monthly_stats() -> list:
    """Group trades by calendar month for the Page 2 breakdown table."""
    if not TRADES_LOG.exists():
        return []
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return []
        df["pnl_gbp"] = df["pnl_gbp"].astype(float)
        df["_dt"]     = pd.to_datetime(df["entry_time"], errors="coerce")
        df["_mk"]     = df["_dt"].dt.strftime("%Y-%m")
        df["_ml"]     = df["_dt"].dt.strftime("%b %Y")
        monthly = []
        for mk, grp in df.groupby("_mk"):
            pnls  = grp["pnl_gbp"]
            wins  = int(len(pnls[pnls > 0]))
            total = int(len(pnls))
            gross = round(float(pnls.sum()), 2)
            monthly.append({
                "month":    grp["_ml"].iloc[0],
                "trades":   total,
                "wins":     wins,
                "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
                "pnl":      gross,
            })
        monthly.sort(key=lambda x: x["month"])
        return monthly
    except Exception:
        return []


# ---------------------------------------------------------------------------
# HTML -- two-page dashboard
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NikkeiTrader A.I. &mdash; Japan 225</title>
<style>
:root{
  --bg:#0d0d0d;--bg2:#141414;--bg3:#1e1e1e;--border:#2a2a2a;
  --gold:#C8102E;--green:#2ecc71;--red:#e74c3c;--amber:#f39c12;
  --text:#e0e0e0;--muted:#888;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;display:flex;flex-direction:column;}

/* HEADER */
.header{background:var(--bg2);border-bottom:2px solid var(--gold);padding:7px 14px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;height:46px;}
.header-brand{display:flex;align-items:center;gap:8px;}
.logo{font-size:17px;font-weight:700;color:var(--gold);letter-spacing:1px;}
.logo span{color:var(--text);}
.version-tag{font-size:11px;font-weight:600;color:var(--muted);font-family:monospace;margin-left:10px;letter-spacing:0.5px;}
.subtitle{color:var(--muted);font-size:10px;margin-top:1px;}
.header-right{display:flex;align-items:center;gap:10px;}
.clock{font-size:15px;font-weight:600;color:var(--gold);font-family:monospace;}
.excalibur-status{font-size:10px;color:var(--amber);white-space:nowrap;}
.header-price{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:0 20px;border-left:1px solid var(--border);border-right:1px solid var(--border);}
.hdr-price-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:1px;}
.hdr-price-val{font-size:22px;font-weight:700;color:var(--gold);font-family:monospace;letter-spacing:1px;}

/* BUTTONS */
.shutdown-btn{background:rgba(231,76,60,0.08);border:1px solid var(--red);color:var(--red);padding:4px 9px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:0.5px;text-transform:uppercase;transition:background 0.15s;}
.shutdown-btn:hover{background:rgba(231,76,60,0.25);}
.nav-btn{background:rgba(200,16,46,0.15);border:1px solid var(--gold);color:var(--gold);padding:4px 12px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;letter-spacing:0.3px;transition:background 0.15s;}
.nav-btn:hover{background:rgba(200,16,46,0.32);}
/* Phantom Trades page (rollout 19 Jul) */
.phantom-page{flex:1;overflow-y:auto;max-width:900px;width:100%;margin:0 auto;padding:16px 20px;display:flex;flex-direction:column;gap:14px;}
.phantom-head{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.phantom-summary{background:rgba(255,255,255,0.03);border:1px solid var(--border,#333);border-radius:8px;padding:12px 16px;font-size:13px;line-height:1.9;}
.phantom-summary .ps-q{font-weight:700;}
.phantom-scroll{max-height:600px;overflow:auto;}
.phantom-table td.ph-na{color:var(--muted,#888);}
.phantom-table{width:100%;border-collapse:collapse;font-size:12px;}
.phantom-table th{text-align:left;color:var(--muted,#888);font-weight:600;padding:6px 10px;border-bottom:1px solid var(--border,#333);white-space:nowrap;}
.phantom-table td{padding:6px 10px;border-bottom:1px solid rgba(255,255,255,0.05);white-space:nowrap;}
.phantom-table tr:hover td{background:rgba(255,255,255,0.02);}
.v-correct{color:#3fb950;font-weight:700;}
.v-wrong{color:#f85149;font-weight:700;}
.v-neutral{color:#8b949e;font-weight:700;}
.v-pending{color:#d29922;font-weight:700;}
#soqCompact{cursor:pointer;transition:background 0.15s;}
#soqCompact:hover{background:rgba(255,255,255,0.03);}

  /* Guinevere dedicated page (page 3) */
  .guin-page{flex:1;overflow-y:auto;max-width:660px;width:100%;margin:0 auto;padding:16px 18px;display:flex;flex-direction:column;gap:10px;}
  .guin-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:2px;}
  #newsCardCompact{cursor:pointer;transition:background 0.15s;}
  #newsCardCompact:hover{background:rgba(255,255,255,0.03);}


/* SHUTDOWN MODAL */
.modal-overlay{display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.78);justify-content:center;align-items:center;}
.modal-overlay.open{display:flex;}
.modal{background:var(--bg2);border:2px solid var(--red);border-radius:10px;padding:22px 28px;max-width:380px;text-align:center;}
.modal h3{color:var(--red);font-size:15px;margin-bottom:10px;}
.modal p{color:var(--muted);font-size:12px;line-height:1.5;margin-bottom:6px;}
.modal-trade-warn{background:rgba(231,76,60,0.1);border:1px solid var(--red);border-radius:5px;padding:8px;margin:10px 0;color:var(--red);font-size:11px;font-weight:600;}
.modal-btns{display:flex;gap:10px;justify-content:center;margin-top:14px;}
.btn-cancel {background:var(--bg3);border:1px solid var(--border);color:var(--gold);padding:6px 16px;border-radius:4px;cursor:pointer;font-size:11px;}
.btn-confirm{background:rgba(231,76,60,0.1);border:1px solid var(--red);color:var(--red);padding:6px 16px;border-radius:4px;cursor:pointer;font-size:11px;}
.btn-cancel:hover {background:rgba(200,16,46,0.15);}
.btn-confirm:hover{background:rgba(231,76,60,0.25);}

/* PAGE WRAPPERS */
.page-wrap{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;}
#page2{overflow-y:auto;}

/* PAGE 1 GRID -- left indicators | centre (Arthur, full width) | right status */
.main{flex:1;display:grid;grid-template-columns:200px 1fr 260px;gap:7px;padding:7px 7px 5px;overflow:hidden;min-height:0;}
.col{display:flex;flex-direction:column;gap:7px;overflow:hidden;min-height:0;}

/* CARDS */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:7px 9px;overflow:hidden;min-height:0;}
.card-title{font-size:9px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:5px;padding-bottom:4px;border-bottom:1px solid var(--border);flex-shrink:0;}
.card-title.gold{color:var(--gold);border-color:var(--gold);}

/* TREND BADGES */
.trend-badge{font-size:16px;font-weight:700;text-align:center;padding:4px 8px;border-radius:5px;margin-bottom:4px;letter-spacing:1px;}
.trend-long   {background:rgba(46,204,113,0.12);color:var(--green);border:1px solid var(--green);}
.trend-short  {background:rgba(231,76,60,0.12); color:var(--red);  border:1px solid var(--red);}
.trend-neutral{background:rgba(243,156,18,0.12);color:var(--amber);border:1px solid var(--amber);}

/* INDICATOR ROWS */
.ind-row{display:flex;justify-content:space-between;align-items:center;padding:2px 0;border-bottom:1px solid var(--bg3);font-size:11px;}
.ind-row:last-child{border-bottom:none;}
.ind-label{color:var(--muted);}
.ind-val{font-weight:600;}
.bull{color:var(--green);}.bear{color:var(--red);}.neut{color:var(--amber);}.gold{color:var(--gold);}

/* SESSION PHASE */
.phase-badge{display:inline-block;padding:3px 9px;border-radius:3px;font-size:12px;font-weight:700;letter-spacing:0.5px;}
.phase-MORNING{background:rgba(46,204,113,0.12);color:var(--green);}
.phase-AFTERNOON    {background:rgba(52,152,219,0.15);color:#3498db;}
.phase-LUNCH_BREAK   {background:rgba(243,156,18,0.12);color:var(--amber);}
.phase-CLOSING      {background:rgba(230,126,34,0.15);color:#e67e22;}
.phase-PRE_OPEN     {background:rgba(136,136,136,0.12);color:var(--muted);}
.phase-CLOSED       {background:rgba(85,85,85,0.15);color:#666;}
.countdown{font-family:monospace;font-size:20px;font-weight:700;color:var(--gold);}
.countdown.amber{color:var(--amber);}
.countdown.green{color:var(--green);}
.last-updated{color:var(--muted);font-size:10px;margin-top:4px;}

/* DECISION -- full width centre card */
.decision-big{font-size:30px;font-weight:800;text-align:center;padding:10px;border-radius:7px;letter-spacing:3px;margin-bottom:8px;}
.dec-long {background:rgba(46,204,113,0.1);color:var(--green);border:2px solid var(--green);}
.dec-short{background:rgba(231,76,60,0.1); color:var(--red);  border:2px solid var(--red);}
.dec-hold {background:rgba(200,16,46,0.1);color:var(--gold); border:2px solid var(--gold);}
.dec-stay {background:rgba(136,136,136,0.1);color:var(--muted);border:2px solid var(--border);}
.dec-meta{text-align:center;color:var(--muted);font-size:12px;margin-bottom:9px;}
.dec-meta span{color:var(--text);font-weight:600;}
.reasoning{background:var(--bg3);border-left:3px solid var(--gold);padding:10px 14px;border-radius:0 5px 5px 0;font-size:13px;line-height:1.55;margin-bottom:7px;}
.block-reason{background:rgba(231,76,60,0.07);border-left:3px solid var(--red);padding:10px 14px;border-radius:0 5px 5px 0;font-size:13px;line-height:1.55;color:var(--red);margin-bottom:7px;}
.warnings{display:flex;flex-direction:column;gap:4px;margin-top:5px;}
.warn-item{background:rgba(243,156,18,0.08);border:1px solid rgba(243,156,18,0.3);border-radius:3px;padding:4px 9px;font-size:11px;color:var(--amber);}

/* PERFORMANCE */
.score-bar{background:var(--bg3);border-radius:3px;height:6px;flex:1;}
.score-fill{height:100%;border-radius:3px;transition:width 0.4s;}
.score-high{background:var(--green);}.score-med{background:var(--amber);}.score-low{background:var(--red);}
.perf-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 2px;}
.perf-win{background:var(--green);}.perf-loss{background:var(--red);}

/* POSITION */
.pos-card{background:var(--bg3);border-radius:5px;padding:7px;font-size:11px;}
.pos-long {border-left:3px solid var(--green);}
.pos-short{border-left:3px solid var(--red);}
.pos-none {border-left:3px solid var(--border);color:var(--muted);text-align:center;padding:9px;}
.pos-row{display:grid;grid-template-columns:120px auto;justify-content:start;column-gap:12px;padding:2px 5px;}
.pos-row>span:last-child{text-align:right;}

/* CHECK ITEMS */
.check-item{display:flex;align-items:center;gap:6px;padding:2px 0;border-bottom:1px solid var(--bg3);font-size:11px;}
.check-item:last-child{border-bottom:none;}
.check-pass{color:var(--green);font-weight:700;min-width:30px;font-size:10px;}
.check-fail{color:var(--red);  font-weight:700;min-width:30px;font-size:10px;}
.check-na  {color:var(--muted);font-weight:700;min-width:30px;font-size:10px;}
.check-lbl {color:var(--text);}

/* KILL STATUS */
.kill-ok    {background:rgba(46,204,113,0.08);border:1px solid rgba(46,204,113,0.3);border-radius:4px;padding:3px 8px;color:var(--green);font-size:10px;text-align:center;flex-shrink:0;}
.kill-active{background:rgba(231,76,60,0.1); border:1px solid var(--red);          border-radius:4px;padding:4px 8px;color:var(--red);  font-size:10px;font-weight:700;text-align:center;flex-shrink:0;}

/* PAGE 2 */
.p2-content{padding:8px 12px 20px;display:flex;flex-direction:column;gap:10px;}
.p2-card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px 16px;}
.p2-card .card-title{font-size:9px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border);}
.p2-account-bar{display:grid;grid-template-columns:repeat(7,1fr);gap:6px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 16px;text-align:center;}
.acc-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:2px;}
.acc-val{font-size:14px;font-weight:700;}
.acc-bal{color:var(--gold);font-size:16px;}
.win{color:var(--green);font-weight:600;}.loss{color:var(--red);font-weight:600;}
.dir-long{color:var(--green);font-weight:700;}.dir-short{color:var(--red);font-weight:700;}
.p2-stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-bottom:10px;}
.p2-stat-box{background:var(--bg3);border-radius:5px;padding:9px 12px;text-align:center;}
.p2-stat-label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;}
.p2-stat-val{font-size:16px;font-weight:700;}
.p2-stat-sub{font-size:10px;color:var(--muted);margin-top:3px;}
.p2-section-hdr{font-size:9px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);margin:10px 0 5px;padding-bottom:3px;border-bottom:1px solid var(--bg3);}
.p2-table{width:100%;border-collapse:collapse;font-size:12px;}
.p2-table th{text-align:left;padding:5px 8px;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);}
.p2-table td{padding:5px 8px;border-bottom:1px solid var(--bg3);font-family:monospace;}
.p2-table tr:last-child td{border-bottom:none;}
.p2-table tr.tr-win td{background:rgba(46,204,113,0.04);}
.p2-table tr.tr-loss td{background:rgba(231,76,60,0.04);}
.month-best td{background:rgba(46,204,113,0.09)!important;}
.month-worst td{background:rgba(231,76,60,0.07)!important;}
.cons-warn{margin-top:8px;padding:5px 9px;background:rgba(231,76,60,0.1);border:1px solid var(--red);border-radius:3px;font-size:10px;color:var(--red);font-weight:700;}
</style>
</head>
<body>

<!-- SHUTDOWN MODAL -->
<div class="modal-overlay" id="shutdownModal">
  <div class="modal">
    <h3>Shut Down NikkeiTrader AI?</h3>
    <p>This will stop the trading engine and close the dashboard.</p>
    <div class="modal-trade-warn" id="tradeWarn" style="display:none">
      WARNING: A position is currently OPEN!<br>
      You must manually close this position via Capital.com.<br>
      The system will NOT close it automatically.
    </div>
    <p>Are you sure you want to shut down?</p>
    <div class="modal-btns">
      <button class="btn-cancel"  onclick="closeModal()">Cancel &mdash; Keep Running</button>
      <button class="btn-confirm" onclick="confirmShutdown()">Yes &mdash; Shut Down</button>
    </div>
  </div>
</div>

<!-- SHARED HEADER -->
<div class="header">
  <div class="header-brand">
    <div>
      <div class="logo">Nikkei<span>TRADER</span> A.I.<span class="version-tag">__VERSION_STRING__</span></div>
      <div class="subtitle">Japan 225 Spread Betting &mdash; Capital.com</div>
    </div>
  </div>
  <div class="header-price">
    <div class="hdr-price-lbl">Japan 225</div>
    <div class="hdr-price-val" id="hdrPrice">--</div>
  </div>
  <div class="header-right">
    <div class="excalibur-status" id="excaliburStatus">Excalibur: --</div>
    <button class="nav-btn" id="btnToP2" onclick="showPage(2)">P&amp;L &rarr;</button>
    <button class="nav-btn" id="btnToP3" onclick="showPage(3)">GUINEVERE &rarr;</button>
    <button class="nav-btn" id="btnToP4" onclick="showPage(4)">PHANTOM &rarr;</button>
    <button class="nav-btn" id="btnToP1" onclick="showPage(1)" style="display:none;">&larr; Trading</button>
    <button class="shutdown-btn" onclick="openModal()">&#9211; Shutdown</button>
    <div class="clock" id="clock">--:--:-- UTC</div>
  </div>
</div>

<!-- PAGE 1: TRADING DASHBOARD -->
<div id="page1" class="page-wrap">
  <div class="main" id="main-grid">
    <div style="grid-column:1/-1;color:var(--muted);padding:40px;text-align:center">Loading NikkeiTrader AI...</div>
  </div>
</div>

<!-- PAGE 2: PERFORMANCE & P&L -->
<div id="page2" class="page-wrap" style="display:none;">
  <div class="p2-content">
    <div class="p2-account-bar" id="p2-account-bar">
      <div style="color:var(--muted);font-size:11px;grid-column:1/-1;text-align:center;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-perf-detail">
      <div class="card-title gold">Arthur Self-Performance &mdash; Detail</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-monthly">
      <div class="card-title">Monthly Breakdown</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-trades">
      <div class="card-title">Nikkei Trade History</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
  </div>
</div>

<!-- PAGE 3: GUINEVERE (news sentiment + keyword editor) -->
<div id="page3" class="page-wrap" style="display:none;">
  <div class="guin-page">
    <div class="guin-head">
      <div class="card-title gold" style="border:none;margin:0;padding:0;">GUINEVERE &mdash; News Sentiment &amp; Keyword Editor</div>
      <button class="nav-btn" onclick="showPage(1)">&larr; Back to Dashboard</button>
    </div>
    <div class="card" id="newsCard" style="flex-shrink:0"><div class="card-title gold">GUINEVERE NEWS</div><div style="color:var(--muted);font-size:11px;">Loading news...</div></div>
  </div>
</div>

<!-- PAGE 4: PHANTOM TRADES -->
<div id="page4" class="page-wrap" style="display:none;">
  <div class="phantom-page">
    <div class="phantom-head">
      <div class="card-title gold" style="border:none;margin:0;padding:0;font-size:14px;">PHANTOM TRADES &mdash; Stay Out Quality</div>
      <button class="nav-btn" onclick="showPage(1)">&larr; Back to Dashboard</button>
    </div>
    <div id="phantomBody"><div style="color:var(--muted);font-size:12px;">Loading phantom trades...</div></div>
  </div>
</div>

<script>
var _currentPage = 1;

/* Phantom Trades page + compact Stay Out Quality (desk rollout 19 Jul 2026) */
var PHANTOM_PAGE = 4;
function renderSoqCompact(sq){
  sq = sq || {};
  var title = '<div class="card-title gold">STAY OUT QUALITY</div>';
  var hint = '<div style="margin-top:6px;font-size:9px;color:var(--muted);letter-spacing:0.4px;">CLICK FOR FULL PHANTOM TRADES &rarr;</div>';
  if(sq.status !== 'ok'){
    return '<div class="card" id="soqCompact" style="flex-shrink:0" onclick="showPage(PHANTOM_PAGE)">' + title +
      '<div style="color:var(--muted);font-size:11px;">Awaiting first decisions</div>' + hint + '</div>';
  }
  var qs = (sq.quality_score == null) ? 0 : sq.quality_score;
  var saved  = (sq.net_saved  == null) ? 0 : sq.net_saved;
  var missed = (sq.net_missed == null) ? 0 : sq.net_missed;
  return '<div class="card" id="soqCompact" style="flex-shrink:0" onclick="showPage(PHANTOM_PAGE)">' + title +
    '<div style="font-size:11px;margin-top:3px;">Quality: <span>' + qs + '%</span> &nbsp;|&nbsp; Last 50</div>' +
    '<div style="font-size:11px;margin:4px 0;">✅ Correct: ' + (sq.correct||0) + ' &nbsp; ❌ Wrong: ' + (sq.wrong||0) + ' &nbsp; ➖ Neutral: ' + (sq.neutral||0) + '</div>' +
    '<div style="font-size:11px;">Net Saved: <span class="bull">+£' + Math.abs(saved).toFixed(2) + '</span> &nbsp; Net Missed: <span class="bear">-£' + Math.abs(missed).toFixed(2) + '</span></div>' +
    hint + '</div>';
}
function fmtPhantomTs(ts){ if(!ts){ return '--'; } var s = String(ts).replace('T',' '); return (s.length>=16)?s.substring(0,16):s; }
function fmtPhantomGBP(v){ var n = parseFloat(v); if(isNaN(n)){ return '--'; } return '£' + n.toLocaleString('en-GB',{maximumFractionDigits:2}); }
function phMoveCell(v){
  var n = parseFloat(v);
  if(isNaN(n)){ return '<td class="ph-na">--</td>'; }
  var cls = (n>=0)?'bull':'bear';
  return '<td class="'+cls+'">'+(n>=0?'+£':'-£')+Math.abs(n).toFixed(2)+'</td>';
}
function renderPhantomBody(sq){
  sq = sq || {};
  if(!sq.status || sq.status === 'No data yet'){ return '<div style="color:var(--muted);font-size:12px;">Awaiting first phantom decisions</div>'; }
  if(sq.status !== 'ok'){ return '<div style="color:var(--muted);font-size:12px;">' + sq.status + '</div>'; }
  var q = (sq.quality_score == null) ? '--' : (sq.quality_score + '%');
  var saved  = (sq.net_saved  == null) ? 0 : sq.net_saved;
  var missed = (sq.net_missed == null) ? 0 : sq.net_missed;
  var html = '<div class="phantom-summary">' +
    '<div>Last 50 decisions &nbsp;|&nbsp; Quality: <span class="ps-q">' + q + '</span></div>' +
    '<div>✅ Correct: ' + (sq.correct||0) + ' &nbsp;&nbsp; ❌ Wrong: ' + (sq.wrong||0) + ' &nbsp;&nbsp; ➖ Neutral: ' + (sq.neutral||0) + '</div>' +
    '<div>Net Saved: <span class="bull">+£' + Math.abs(saved).toFixed(2) + '</span> &nbsp;&nbsp; Net Missed: <span class="bear">-£' + Math.abs(missed).toFixed(2) + '</span></div>' +
    '</div>';
  var decs = (sq.decisions || []).slice(); decs.reverse();
  html += '<div class="phantom-scroll"><table class="phantom-table"><thead><tr>' +
    '<th>Date/Time (UTC)</th><th>Direction</th><th>Entry Price</th><th>Confidence</th><th>5min</th><th>10min</th><th>15min</th><th>30min</th><th>1hr</th><th>2hr</th><th>Verdict</th>' +
    '</tr></thead><tbody>';
  for(var i=0;i<decs.length;i++){
    var r = decs[i] || {};
    var dir = r.direction_blocked || r.direction || '--';
    var entry = fmtPhantomGBP(r.price_at_decision);
    var conf = r.confidence || '--';
    var pnl = parseFloat(r.pnl_1hr);
    var pnlStr = isNaN(pnl) ? '--' : ((pnl>=0?'+£':'-£') + Math.abs(pnl).toFixed(2));
    var pnlCls = isNaN(pnl) ? '' : (pnl>=0?'bull':'bear');
    var v = r.verdict || 'PENDING';
    var vCls = (v==='CORRECT')?'v-correct':(v==='WRONG')?'v-wrong':(v==='NEUTRAL')?'v-neutral':'v-pending';
    html += '<tr><td>' + fmtPhantomTs(r.timestamp) + '</td><td>' + dir + '</td><td>' + entry + '</td><td>' + conf + '</td>' + phMoveCell(r.pnl_5min) + phMoveCell(r.pnl_10min) + phMoveCell(r.pnl_15min) + phMoveCell(r.pnl_30min) + phMoveCell(r.pnl_1hr) + phMoveCell(r.pnl_2hr) + '<td><span class="' + vCls + '">' + v + '</span></td></tr>';
  }
  html += '</tbody></table></div>';
  return html;
}

var hasOpenPosition = false;

/* ── Clock ──────────────────────────────────────────────────────────────── */
function updateClock(){
  var t = new Date();
  document.getElementById('clock').textContent =
    String(t.getUTCHours()).padStart(2,'0') + ':' +
    String(t.getUTCMinutes()).padStart(2,'0') + ':' +
    String(t.getUTCSeconds()).padStart(2,'0') + ' UTC';
}
setInterval(updateClock, 1000);
updateClock();

/* ── Countdown to next 5-minute candle boundary ─────────────────────────── */
function updateCountdown(){
  var el = document.getElementById('countdown');
  if(!el) return;
  var now = new Date();
  var secsIntoHour = now.getUTCMinutes() * 60 + now.getUTCSeconds();
  var secsIntoCandle = secsIntoHour % 300;
  var remaining = 300 - secsIntoCandle;
  var mins = Math.floor(remaining / 60);
  var secs = remaining % 60;
  el.textContent = 'Next update: ' + mins + ':' + String(secs).padStart(2,'0');
  el.className = 'countdown' + (remaining <= 10 ? ' green' : remaining <= 60 ? ' amber' : '');
}
setInterval(updateCountdown, 1000);

/* ── Page switching ─────────────────────────────────────────────────────── */
function showPage(n){
  var pages = {1:'page1', 2:'page2', 3:'page3', 4:'page4'};
  for(var k in pages){
    var el = document.getElementById(pages[k]);
    if(el){ el.style.display = (Number(k) === n) ? 'flex' : 'none'; }
  }
  var b1 = document.getElementById('btnToP1');
  var b2 = document.getElementById('btnToP2');
  var b3 = document.getElementById('btnToP3');
  var b4 = document.getElementById('btnToP4');
  if(b1){ b1.style.display = (n === 1) ? 'none' : 'inline-block'; }
  if(b2){ b2.style.display = (n === 2) ? 'none' : 'inline-block'; }
  if(b3){ b3.style.display = (n === 3) ? 'none' : 'inline-block'; }
  if(b4){ b4.style.display = (n === 4) ? 'none' : 'inline-block'; }
  _currentPage = n;
}

/* ── Shutdown modal ─────────────────────────────────────────────────────── */
function openModal(){
  document.getElementById('tradeWarn').style.display = hasOpenPosition ? 'block' : 'none';
  document.getElementById('shutdownModal').classList.add('open');
}
function closeModal(){
  document.getElementById('shutdownModal').classList.remove('open');
}
function confirmShutdown(){
  fetch('/api/shutdown', {method:'POST'})
    .then(function(){
      document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;background:#0d0d0d;color:#C8102E;font-family:monospace;font-size:18px;">NikkeiTrader AI shut down. You may close this window.</div>';
    })
    .catch(function(){ closeModal(); });
}

/* ── Formatting helpers ─────────────────────────────────────────────────── */
function fmt(v, dp){
  dp = (dp === undefined) ? 2 : dp;
  if(v === null || v === undefined || v !== v) return '--';
  return parseFloat(v).toFixed(dp);
}
function fmtPnl(v){
  if(v === null || v === undefined || v !== v) return '--';
  var n = parseFloat(v);
  return (n >= 0 ? '+' : '') + n.toFixed(2);
}
function trendClass(t){
  if(!t) return 'trend-neutral'; t = t.toUpperCase();
  if(t.indexOf('LONG') >= 0 || t.indexOf('BULL') >= 0) return 'trend-long';
  if(t.indexOf('SHORT') >= 0 || t.indexOf('BEAR') >= 0) return 'trend-short';
  return 'trend-neutral';
}
function trendLabel(t){
  if(!t) return 'NEUTRAL'; t = t.toUpperCase();
  if(t.indexOf('LONG') >= 0 || t.indexOf('BULL') >= 0) return 'LONG';
  if(t.indexOf('SHORT') >= 0 || t.indexOf('BEAR') >= 0) return 'SHORT';
  return 'NEUTRAL';
}
function decClass(d){
  if(!d) return 'dec-stay';
  if(d.indexOf('LONG') >= 0) return 'dec-long';
  if(d.indexOf('SHORT') >= 0) return 'dec-short';
  if(d === 'HOLD') return 'dec-hold';
  return 'dec-stay';
}
function indCls(v, thresh){
  thresh = thresh || 0; var n = parseFloat(v);
  if(isNaN(n)) return 'neut';
  return n > thresh ? 'bull' : n < thresh ? 'bear' : 'neut';
}
function sslCls(v){ return v ? 'bull' : 'bear'; }
function sslLbl(v){ return v ? 'BULL' : 'BEAR'; }

/* ── Left column: Daily / 1-Hour / 5-Minute cards ───────────────────────── */
function buildLeftCol(trend1d, trend1h, signal5m, ind1d, ind1h, ind5m, phase, updatedAt){
  var phaseLabel = (phase || '--').replace(/_/g,' ');
  return '<div class="col">' +
    '<div class="card" style="flex-shrink:0"><div class="card-title gold">Daily Trend</div>' +
    '<div class="trend-badge ' + trendClass(trend1d) + '">' + trendLabel(trend1d) + '</div>' +
    '<div class="ind-row"><span class="ind-label">SSL</span><span class="ind-val ' + sslCls(ind1d.ssl_bull) + '">' + sslLbl(ind1d.ssl_bull) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">RSI</span><span class="ind-val ' + indCls(ind1d.rsi,50) + '">' + fmt(ind1d.rsi,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Filter</span><span class="ind-val ' + (trendLabel(trend1d)==='LONG'?'bull':trendLabel(trend1d)==='SHORT'?'bear':'neut') + '">' +
    (trendLabel(trend1d)==='LONG'?'LONG only':trendLabel(trend1d)==='SHORT'?'SHORT only':'Both') + '</span></div>' +
    '</div>' +
    '<div class="card" style="flex-shrink:0"><div class="card-title gold">1-Hour Trend</div>' +
    '<div class="trend-badge ' + trendClass(trend1h) + '">' + trendLabel(trend1h) + '</div>' +
    '<div class="ind-row"><span class="ind-label">SSL Cloud</span><span class="ind-val ' + sslCls(ind1h.ssl_bull) + '">' + sslLbl(ind1h.ssl_bull) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">RSI</span><span class="ind-val ' + indCls(ind1h.rsi,50) + '">' + fmt(ind1h.rsi,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">MACD</span><span class="ind-val ' + indCls(ind1h.macd) + '">' + fmt(ind1h.macd,2) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">TMO</span><span class="ind-val ' + indCls(ind1h.tmo_main) + '">' + fmt(ind1h.tmo_main,3) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Chande MO</span><span class="ind-val ' + indCls(ind1h.chande_mo) + '">' + fmt(ind1h.chande_mo,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Money Flow</span><span class="ind-val ' + indCls(ind1h.money_flow) + '">' + fmt(ind1h.money_flow,4) + '</span></div>' +
    '</div>' +
    '<div class="card" style="flex-shrink:0"><div class="card-title gold">5-Minute Signal</div>' +
    '<div class="trend-badge ' + trendClass(signal5m) + '">' + trendLabel(signal5m) + '</div>' +
    '<div class="ind-row"><span class="ind-label">SSL Cloud</span><span class="ind-val ' + sslCls(ind5m.ssl_bull) + '">' + sslLbl(ind5m.ssl_bull) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">RSI</span><span class="ind-val ' + indCls(ind5m.rsi,50) + '">' + fmt(ind5m.rsi,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">MACD</span><span class="ind-val ' + indCls(ind5m.macd) + '">' + fmt(ind5m.macd,2) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">TMO</span><span class="ind-val ' + indCls(ind5m.tmo_main) + '">' + fmt(ind5m.tmo_main,3) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Chande MO</span><span class="ind-val ' + indCls(ind5m.chande_mo) + '">' + fmt(ind5m.chande_mo,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Money Flow</span><span class="ind-val ' + indCls(ind5m.money_flow) + '">' + fmt(ind5m.money_flow,4) + '</span></div>' +
    '</div>' +
    '<div class="card" style="flex:1"><div class="card-title gold">Session Phase</div>' +
    '<div class="phase-badge phase-' + (phase||'CLOSED') + '">' + phaseLabel + '</div>' +
    '<div id="countdown" class="countdown" style="margin-top:8px;">Next update: --:--</div>' +
    '<div class="last-updated">Last updated: ' + (updatedAt || '--') + '</div>' +
    '</div>' +
    '</div>';
}

/* ── Performance card (Page 1, compact) ─────────────────────────────────── */
function renderPerfCard(perf){
  var total = perf ? (perf.total_trades || 0) : 0;
  if(total === 0){
    return '<div class="card"><div class="card-title gold">Arthur Self-Performance</div>' +
      '<div style="color:var(--muted);font-size:11px;text-align:center;padding:8px 0">No trades yet — system ready</div></div>';
  }
  var score  = (perf.confidence_score != null ? perf.confidence_score : 50);
  var level  = perf.confidence_level || 'MEDIUM';
  var sc     = level==='HIGH' ? 'score-high' : (level==='LOW'||level==='VERY_LOW') ? 'score-low' : 'score-med';
  var lc     = level==='HIGH' ? 'bull'       : (level==='LOW'||level==='VERY_LOW') ? 'bear'      : 'neut';
  var stType = perf.streak_type  || '';
  var stCnt  = perf.streak_count || 0;
  var stCol  = stType==='WIN' ? 'var(--green)' : stType==='LOSS' ? 'var(--red)' : 'var(--muted)';
  var stStr  = stCnt > 0 ? (stCnt + ' ' + (stType==='WIN'?'WIN':'LOSS') + (stCnt>1?'S':'')) : '--';
  var r5     = perf.recent_5 || [];
  var dots   = r5.map(function(r){ return '<span class="perf-dot ' + (r==='WIN'?'perf-win':'perf-loss') + '"></span>'; }).join('');
  var cons   = perf.conservative
    ? '<div style="margin-top:4px;padding:3px 6px;background:rgba(231,76,60,0.1);border:1px solid var(--red);border-radius:3px;font-size:10px;color:var(--red);font-weight:700;">CONSERVATIVE MODE — STAY OUT</div>'
    : '';
  return '<div class="card"><div class="card-title gold">Arthur Self-Performance</div>' +
    '<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">' +
    '<span style="font-size:10px;color:var(--muted);min-width:60px">Confidence</span>' +
    '<div class="score-bar"><div class="score-fill ' + sc + '" style="width:' + score + '%"></div></div>' +
    '<span class="' + lc + '" style="font-size:12px;font-weight:700;min-width:80px;text-align:right">' + score + '/100 ' + level + '</span></div>' +
    '<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">' +
    '<span style="font-size:10px;color:var(--muted);min-width:60px">Last ' + r5.length + '</span>' +
    (dots || '<span style="color:var(--muted);font-size:10px">No trades</span>') + '</div>' +
    '<div style="display:flex;gap:14px;font-size:11px;color:var(--muted);">' +
    '<span>Streak: <strong style="color:' + stCol + '">' + stStr + '</strong></span>' +
    '<span>Trades: <strong style="color:var(--gold)">' + total + '</strong></span>' +
    '<span>WR: <strong style="color:var(--text)">' + fmt(perf.win_rate,1) + '%</strong></span>' +
    '</div>' + cons + '</div>';
}

/* ── Right panel: kill status, pre-checks/checklist, calendar ───────────── */
/* ── GUINEVERE NEWS panel (Part 5; polls /api/news every 60s) ───────────── */
var _newsData = null;
function renderNewsCompact(n){
  var title = '<div class="card-title gold">GUINEVERE</div>';
  var hint = '<div style="margin-top:6px;font-size:9px;color:var(--muted);letter-spacing:0.4px;">CLICK FOR FULL NEWS &amp; KEYWORD EDITOR &rarr;</div>';
  if(!n){
    return '<div class="card" id="newsCardCompact" style="flex-shrink:0" onclick="showPage(3)">' + title +
      '<div style="color:var(--muted);font-size:11px;">Loading...</div>' + hint + '</div>';
  }
  var sent  = n.sentiment || 'NEUTRAL';
  var scls  = sent === 'BULLISH' ? 'bull' : sent === 'BEARISH' ? 'bear' : 'neut';
  var score = (n.score === undefined || n.score === null) ? 0 : n.score;
  var scoreTxt = (score > 0 ? '+' : '') + score;
  var hls = (n.headlines || []).filter(function(h){ var s=(h.score===undefined||h.score===null)?0:h.score; return Math.abs(s) >= 1; });
  var topHTML;
  if(hls.length){
    var ttl = (hls[0].title || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    var hs  = hls[0].score;
    var hcls = hs > 0 ? 'bull' : 'bear';
    var hst  = (hs > 0 ? '+' : '') + hs;
    topHTML = '<div style="margin-top:5px;font-size:10px;display:flex;gap:6px;align-items:center;">' +
              '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + ttl + '</span>' +
              '<span class="' + hcls + '">' + hst + '</span></div>';
  } else {
    topHTML = '<div style="margin-top:5px;font-size:10px;color:var(--muted);">No significant headlines</div>';
  }
  return '<div class="card" id="newsCardCompact" style="flex-shrink:0" onclick="showPage(3)">' + title +
    '<div style="font-size:11px;">Sentiment: <span class="' + scls + '">' + sent + '</span>' +
    ' &nbsp; Score: <span class="' + scls + '">' + scoreTxt + '</span></div>' +
    topHTML + hint + '</div>';
}
function renderNewsCard(n){
  var title = '<div class="card-title gold">GUINEVERE NEWS</div>';
  if(!n){
    return '<div class="card" id="newsCard" style="flex-shrink:0">' + title +
      '<div style="color:var(--muted);font-size:11px;">Loading Nikkei news...</div></div>';
  }
  var sent  = n.sentiment || 'NEUTRAL';
  var scls  = sent === 'BULLISH' ? 'bull' : sent === 'BEARISH' ? 'bear' : 'neut';
  var score = (n.score === undefined || n.score === null) ? 0 : n.score;
  var scoreTxt = (score > 0 ? '+' : '') + score;
  var reason = n.reason || '';
  var noKey  = (reason.indexOf('No API key') >= 0);
  var body;
  if(noKey){
    body = '<div style="color:var(--muted);font-size:11px;line-height:1.5;">' + reason + '</div>';
  } else {
    var hl = (n.headlines || []).filter(function(h){ var s=(h.score===undefined||h.score===null)?0:h.score; return Math.abs(s) >= 1; });
    var hlHTML = '';
    if(hl.length > 0){
      hlHTML = hl.slice(0,5).map(function(h){
        var hs   = (h.score === undefined || h.score === null) ? 0 : h.score;
        var hcls = hs > 0 ? 'bull' : hs < 0 ? 'bear' : 'neut';
        var hst  = (hs > 0 ? '+' : '') + hs;
        var ttl  = (h.title || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        return '<div style="display:flex;gap:6px;font-size:10px;margin-top:3px;">' +
               '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + ttl + '</span>' +
               '<span class="' + hcls + '">' + hst + '</span></div>';
      }).join('');
    } else {
      hlHTML = '<div style="color:var(--muted);font-size:10px;margin-top:4px;">No significant headlines in current period</div>';
    }
    body =
      '<div style="font-size:11px;margin-bottom:3px;">Sentiment: <span class="' + scls + '">' + sent + '</span>' +
      ' &nbsp; Score: <span class="' + scls + '">' + scoreTxt + '</span></div>' +
      '<div class="last-updated">Updated: ' + (n.updated_at || '--') + '</div>' +
      hlHTML;
  }
  var macroHTML = '';
  if(n.macro_flag && n.macro_flag !== 'NEUTRAL'){
    var madj = (n.macro_adj === undefined || n.macro_adj === null) ? 0 : n.macro_adj;
    macroHTML = '<div class="last-updated" style="margin-top:5px;color:var(--amber)">Macro: ' + n.macro_flag +
                (madj ? ' (' + (madj > 0 ? '+' : '') + madj + ')' : '') + '</div>';
  }
  var cauCol = n.caution_active ? 'var(--amber)' : 'var(--muted)';
  var cauTxt = n.caution_active ? (n.caution_reason || 'Event caution window active') : 'No event caution window';
  var cauHTML = '<div class="last-updated" style="margin-top:6px;color:' + cauCol + '">Events: ' + cauTxt + '</div>';
  return '<div class="card" id="newsCard" style="flex-shrink:0">' + title + body + macroHTML + cauHTML + '</div>';
}
function pollNews(){
  fetch('/api/news')
    .then(function(r){ return r.json(); })
    .then(function(n){
      _newsData = n;
      var full = document.getElementById('newsCard');
      if(full){ full.outerHTML = renderNewsCard(n); }
      var comp = document.getElementById('newsCardCompact');
      if(comp){ comp.outerHTML = renderNewsCompact(n); }
    })
    .catch(function(e){ console.error('News poll error:', e); });
}

function renderRightPanel(d){
  var mode = d.panel_mode || 'pre_checks';
  var acc  = { killed: d.kill_switch, kill_reason: '' };

  var killHTML = d.kill_switch
    ? '<div class="kill-active">KILL SWITCH ACTIVE<br><small>Tier ' + (d.kill_tier||1) + '</small></div>'
    : '<div class="kill-ok">System OK — Trading Active</div>';

  var panelHTML = '';
  if(mode === 'claude'){
    var cl = d.checklist || {}; var items = Object.keys(cl);
    panelHTML = '<div class="card" style="flex:1;display:flex;flex-direction:column;">' +
      '<div class="card-title gold">Arthur Checklist</div>' +
      (items.length > 0
        ? items.map(function(k){
            var v = cl[k];
            return '<div class="check-item"><span class="' + (v ? 'check-pass' : 'check-fail') + '">' +
              (v ? 'PASS' : 'FAIL') + '</span><span class="check-lbl">' + k.replace(/_/g,' ') + '</span></div>';
          }).join('')
        : '<div style="color:var(--muted);font-size:11px;">No checklist yet</div>') +
      '</div>';
  } else {
    var checks = d.pre_checks || {}; var keys = Object.keys(checks);
    var chtml = keys.map(function(k){
      var v = checks[k]; var cls, icon;
      if(v === true){cls='check-pass';icon='PASS';}
      else if(v === false){cls='check-fail';icon='FAIL';}
      else{cls='check-na';icon='N/A';}
      return '<div class="check-item"><span class="' + cls + '">' + icon + '</span><span class="check-lbl">' + k + '</span></div>';
    }).join('');
    panelHTML = '<div class="card" style="flex:1;display:flex;flex-direction:column;">' +
      '<div class="card-title gold">Lancelot — Pre-Checks</div>' +
      (chtml || '<div style="color:var(--muted);font-size:11px;">Waiting for first tick...</div>') +
      '</div>';
  }

  var calText = d.calendar || 'Loading...';
  var calHTML = '<div class="card" style="flex-shrink:0"><div class="card-title gold">Guinevere — UK Calendar</div>' +
    '<div style="color:var(--text);font-size:11px;line-height:1.5;">' + calText + '</div></div>';

  return killHTML + panelHTML + calHTML + renderNewsCompact(_newsData) + renderSoqCompact(d.stay_out_quality);
}

/* ── STAY OUT QUALITY panel ─────────────────────────────────────────────── */
function renderStayOutQuality(d){
  var sq = d.stay_out_quality || {};
  var head = '<div class="card" style="flex-shrink:0"><div class="card-title gold">STAY OUT QUALITY</div>';
  if(sq.status !== 'ok'){
    return head + '<div style="color:var(--muted);font-size:11px;">Awaiting first decisions</div></div>';
  }
  var dec = sq.decisions || [];
  var netSaved  = sq.net_saved  || 0;
  var netMissed = sq.net_missed || 0;
  var body = '<div style="font-size:11px;color:var(--text);margin-bottom:4px;">' +
    'Last ' + dec.length + ' decisions &nbsp; Quality: <span class="gold" style="font-weight:700;">' + (sq.quality_score||0) + '%</span></div>' +
    '<div style="font-size:11px;margin-bottom:4px;">' +
    '<span class="bull">✅ Correct: ' + (sq.correct||0) + '</span> &nbsp; ' +
    '<span class="bear">❌ Wrong: ' + (sq.wrong||0) + '</span> &nbsp; ' +
    '<span class="neut">➖ Neutral: ' + (sq.neutral||0) + '</span></div>' +
    '<div style="font-size:11px;margin-bottom:6px;">' +
    '<span class="bull">Net Saved: +£' + Math.abs(netSaved).toFixed(2) + '</span> &nbsp; ' +
    '<span class="bear">Net Missed: -£' + Math.abs(netMissed).toFixed(2) + '</span></div>';
  var rows = dec.map(function(r){
    var v = r.verdict || '';
    var vc = v==='CORRECT' ? 'bull' : v==='WRONG' ? 'bear' : 'neut';
    var pnl = parseFloat(r.pnl_1hr || 0) || 0;
    return '<div class="ind-row" style="font-size:10px;">' +
      '<span class="ind-label">' + (r.timestamp || r.time || '') + '</span>' +
      '<span class="' + vc + '">' + (v || '--') + ' £' + pnl.toFixed(2) + '</span></div>';
  }).join('');
  return head + body + (rows || '<div style="color:var(--muted);font-size:10px;">No decisions yet</div>') + '</div>';
}

/* ── Page 1: trading dashboard ──────────────────────────────────────────── */
function renderPage1(d){
  var trend1d  = d.trend_1d   || 'NEUTRAL';
  var trend1h  = d.trend_1h   || 'NEUTRAL';
  var signal5m = d.signal_5m  || 'NEUTRAL';
  var decision = (d.decision && d.decision.decision) || 'STAY_OUT';
  var dec      = d.decision || {};
  var pos      = d.current_trade || null;
  var ind1h    = d.indicators_1h || {};
  var ind5m    = d.indicators_5m || {};
  var ind1d    = d.indicators_1d || {};
  var warnings = dec.warnings || [];
  var mode     = d.panel_mode || 'pre_checks';

  hasOpenPosition = !!(d.in_trade && pos);

  var hdrEl = document.getElementById('hdrPrice');
  if(hdrEl){ hdrEl.textContent = (d.nikkei_level||0).toLocaleString('en-GB',{minimumFractionDigits:1}); }

  var excaliburEl = document.getElementById('excaliburStatus');
  if(excaliburEl){
    if(d.connector_status === 'capitalcom'){
      excaliburEl.textContent = 'Excalibur: Capital.com ✅';
      excaliburEl.style.color = 'var(--green)';
    } else {
      excaliburEl.textContent = 'Excalibur: Yahoo Finance (fallback) ⚠️';
      excaliburEl.style.color = 'var(--amber)';
    }
  }

  var decText = decision.replace('ENTER_','').replace('EXIT_','EXIT ').replace(/_/g,' ');
  if(decision === 'STAY_OUT') decText = 'STAY OUT';

  var reasoning   = dec.reasoning || 'Waiting for next analysis cycle...';
  var blockReason = (d.pre_checks_reason) || '';
  var reasonBox = (blockReason && mode === 'pre_checks')
    ? '<div class="block-reason">' + blockReason + '</div>'
    : '<div class="reasoning">' + reasoning + '</div>';

  var warnHTML = (warnings.length > 0 && mode === 'claude')
    ? '<div class="warnings">' + warnings.map(function(w){ return '<div class="warn-item">'+w+'</div>'; }).join('') + '</div>'
    : '';

  function buildPosHTML(p, currentPrice){
    if(!p) return '<div class="pos-card pos-none">No open position<br><span style="font-size:10px">Watching for setup...</span></div>';
    var direction = p.direction || '--';
    var pc = direction==='LONG' ? 'pos-long' : 'pos-short';
    var dc = direction==='LONG' ? 'bull' : 'bear';
    // Floating P&L computed live from the current Nikkei price (serialised pnl_* are "None" while OPEN)
    var entry = parseFloat(p.entry_price);
    var cur   = parseFloat(currentPrice);
    var dir   = (p.direction||'').toUpperCase();
    var points = (isNaN(entry)||isNaN(cur)||cur===0) ? null : (dir==='SHORT' ? entry-cur : cur-entry);
    var stake  = parseFloat(p.stake);
    var fgbp   = (points===null||isNaN(stake)) ? null : points*stake;
    var ptsStr = (points===null ? '---' : (points>=0?'+':'')+points.toFixed(1));
    var pnlStr = '&pound;' + (fgbp===null ? '---' : (fgbp>=0?'+':'')+fgbp.toFixed(2));
    var pnlCls = (fgbp===null) ? '' : (fgbp>=0?'bull':'bear');
    var ptsCls = (points===null) ? '' : (points>=0?'bull':'bear');
    return '<div class="pos-card ' + pc + '">' +
      '<div class="pos-row"><span class="' + dc + '" style="font-weight:700">' + direction + '</span>' +
      '<span style="color:var(--muted)">' + (p.entry_time||'') + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Entry</span><span>' + fmt(p.entry_price,1) + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Stop</span><span class="bear">' + fmt(p.stop_loss,1) + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Target</span><span class="bull">' + fmt(p.take_profit,1) + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Stake</span><span>£' + fmt(p.stake,4) + '/pt</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Points</span><span class="' + ptsCls + '">' + ptsStr + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">P&amp;L</span><span class="' + pnlCls + '">' + pnlStr + '</span></div>' +
      '</div>';
  }

  var leftCol = buildLeftCol(trend1d, trend1h, signal5m, ind1d, ind1h, ind5m, d.phase, d.updated_at);

  var centreCol = '<div class="col">' +
    '<div class="card" style="flex-shrink:0"><div class="card-title gold">Arthur &mdash; AI Decision</div>' +
    '<div class="decision-big ' + decClass(decision) + '">' + decText + '</div>' +
    '<div class="dec-meta">Confidence: <span>' + (dec.confidence||'--') + '</span> &nbsp;|&nbsp; Session Bias: <span>' + (dec.session_bias||'--') + '</span></div>' +
    reasonBox + warnHTML +
    '</div>' +
    renderPerfCard(d.perf || {}) +
    '<div class="card" style="flex:1"><div class="card-title gold">Open Position</div>' +
    buildPosHTML(pos, d.nikkei_level) +
    '</div>' +
    '</div>';

  var rightCol = '<div class="col">' + renderRightPanel(d) + '</div>';

  document.getElementById('main-grid').innerHTML = leftCol + centreCol + rightCol;

  var _pb = document.getElementById('phantomBody');
  if(_pb){ _pb.innerHTML = renderPhantomBody(d.stay_out_quality); }
}

/* ── Page 2: P&L and performance ─────────────────────────────────────────── */
function renderPage2(d){
  var acc      = d.account       || {};
  var perf     = d.perf          || {};
  var trades   = d.trades        || [];
  var monthly  = d.monthly_stats || [];
  var breakdown= perf.breakdown  || {};
  var dirStats = breakdown.direction || {};
  var sesStats = breakdown.session   || {};
  var pnl      = acc.total_pnl   || 0;
  var dpnl     = acc.daily_pnl   || 0;

  document.getElementById('p2-account-bar').innerHTML =
    '<div><div class="acc-lbl">Balance</div>' +
    '<div class="acc-val acc-bal">£' + (acc.capital||1000).toLocaleString('en-GB',{minimumFractionDigits:2}) + '</div></div>' +
    '<div><div class="acc-lbl">Total P&amp;L</div>' +
    '<div class="acc-val ' + (pnl>=0?'win':'loss') + '">£' + fmtPnl(pnl) + '</div></div>' +
    '<div><div class="acc-lbl">Return</div>' +
    '<div class="acc-val ' + (pnl>=0?'win':'loss') + '">' + (acc.total_return>=0?'+':'') + fmt(acc.total_return) + '%</div></div>' +
    '<div><div class="acc-lbl">Today P&amp;L</div>' +
    '<div class="acc-val ' + (dpnl>=0?'win':'loss') + '">£' + fmtPnl(dpnl) + '</div></div>' +
    '<div><div class="acc-lbl">Trades</div>' +
    '<div class="acc-val gold">' + (acc.total_trades||0) + '</div></div>' +
    '<div><div class="acc-lbl">W / L</div>' +
    '<div class="acc-val"><span class="win">' + (acc.winners||0) + '</span> / <span class="loss">' + (acc.losers||0) + '</span></div></div>' +
    '<div><div class="acc-lbl">Win Rate</div>' +
    '<div class="acc-val ' + ((acc.win_rate||0)>=50?'win':'loss') + '">' + fmt(acc.win_rate,1) + '%</div></div>';

  var total = perf.total_trades || 0;
  var perfHTML = '';
  if(total === 0){
    perfHTML = '<div style="color:var(--muted);font-size:12px;padding:16px 0;text-align:center">No trades yet — system ready</div>';
  } else {
    var score  = (perf.confidence_score != null ? perf.confidence_score : 50);
    var level  = perf.confidence_level || 'MEDIUM';
    var sc     = level==='HIGH' ? 'score-high' : (level==='LOW'||level==='VERY_LOW') ? 'score-low' : 'score-med';
    var lc     = level==='HIGH' ? 'bull'       : (level==='LOW'||level==='VERY_LOW') ? 'bear'      : 'neut';
    var stType = perf.streak_type  || '';
    var stCnt  = perf.streak_count || 0;
    var stCol  = stType==='WIN' ? 'var(--green)' : stType==='LOSS' ? 'var(--red)' : 'var(--muted)';
    var stStr  = stCnt > 0 ? (stCnt + ' ' + (stType==='WIN'?'WIN':'LOSS') + (stCnt>1?'S':'')) : '--';

    perfHTML += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">' +
      '<span style="font-size:11px;color:var(--muted);min-width:80px">Confidence</span>' +
      '<div class="score-bar"><div class="score-fill ' + sc + '" style="width:' + score + '%"></div></div>' +
      '<span class="' + lc + '" style="font-size:14px;font-weight:700;min-width:110px;text-align:right">' + score + '/100 ' + level + '</span></div>';

    var last10 = trades.slice(0, 10);
    var dots10 = last10.map(function(t){
      return '<span class="perf-dot ' + (t.pnl_class==='win'?'perf-win':'perf-loss') + '"></span>';
    }).join('');
    perfHTML += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">' +
      '<span style="font-size:11px;color:var(--muted);min-width:80px">Last ' + last10.length + '</span>' +
      (dots10 || '<span style="color:var(--muted);font-size:11px">No trades</span>') + '</div>';

    perfHTML += '<div style="display:flex;gap:24px;font-size:12px;color:var(--muted);margin-bottom:14px;flex-wrap:wrap;">' +
      '<span>Streak: <strong style="color:' + stCol + '">' + stStr + '</strong></span>' +
      '<span>Total trades: <strong style="color:var(--gold)">' + total + '</strong></span>' +
      '<span>Win rate: <strong style="color:var(--text)">' + fmt(perf.win_rate,1) + '%</strong></span>' +
      '</div>';

    var dirKeys = Object.keys(dirStats);
    if(dirKeys.length > 0){
      perfHTML += '<div class="p2-section-hdr">Win Rate by Direction (LONG vs SHORT)</div><div class="p2-stat-grid">';
      dirKeys.forEach(function(dk){
        var ds  = dirStats[dk];
        var dcl = dk==='LONG' ? 'bull' : 'bear';
        var wcl = ds.win_rate >= 50 ? 'bull' : 'bear';
        perfHTML += '<div class="p2-stat-box">' +
          '<div class="p2-stat-label ' + dcl + '">' + dk + '</div>' +
          '<div class="p2-stat-val ' + wcl + '">' + ds.win_rate + '%</div>' +
          '<div class="p2-stat-sub">' + ds.wins + ' W / ' + (ds.trades-ds.wins) + ' L — ' + ds.trades + ' trades</div>' +
          '</div>';
      });
      perfHTML += '</div>';
    }

    var sesKeys = Object.keys(sesStats);
    if(sesKeys.length > 0){
      var sesLabel = {'MORNING':'Morning','AFTERNOON':'Afternoon','LUNCH_BREAK':'Lunch Lull'};
      perfHTML += '<div class="p2-section-hdr">Win Rate by Session (Morning vs Afternoon)</div><div class="p2-stat-grid">';
      sesKeys.forEach(function(sk){
        var ss  = sesStats[sk];
        var wcl = ss.win_rate >= 50 ? 'bull' : 'bear';
        perfHTML += '<div class="p2-stat-box">' +
          '<div class="p2-stat-label">' + (sesLabel[sk]||sk) + '</div>' +
          '<div class="p2-stat-val ' + wcl + '">' + ss.win_rate + '%</div>' +
          '<div class="p2-stat-sub">' + ss.wins + ' W / ' + (ss.trades-ss.wins) + ' L — ' + ss.trades + ' trades</div>' +
          '</div>';
      });
      perfHTML += '</div>';
    }

    if(perf.conservative){
      perfHTML += '<div class="cons-warn">CONSERVATIVE MODE ACTIVE — System staying out pending improved performance</div>';
    }
  }

  document.getElementById('p2-perf-detail').innerHTML =
    '<div class="card-title gold">Arthur Self-Performance — Detail</div>' + perfHTML;

  var monthHTML = '';
  if(monthly.length === 0){
    monthHTML = '<div style="color:var(--muted);font-size:12px;padding:14px 0;text-align:center">No trade data yet</div>';
  } else {
    var allPnls  = monthly.map(function(m){ return m.pnl; });
    var bestPnl  = Math.max.apply(null, allPnls);
    var worstPnl = Math.min.apply(null, allPnls);
    monthHTML = '<table class="p2-table"><thead><tr>' +
      '<th>Month</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>P&amp;L</th>' +
      '</tr></thead><tbody>';
    monthly.slice().reverse().forEach(function(m){
      var rowCls = '';
      if(monthly.length > 1){
        if(m.pnl === bestPnl)       rowCls = ' class="month-best"';
        else if(m.pnl === worstPnl) rowCls = ' class="month-worst"';
      }
      monthHTML += '<tr' + rowCls + '>' +
        '<td>' + m.month + '</td>' +
        '<td>' + m.trades + '</td>' +
        '<td>' + m.wins + '</td>' +
        '<td><span class="' + (m.win_rate>=50?'win':'loss') + '">' + m.win_rate + '%</span></td>' +
        '<td><span class="' + (m.pnl>=0?'win':'loss') + '">£' + fmtPnl(m.pnl) + '</span></td>' +
        '</tr>';
    });
    monthHTML += '</tbody></table>';
  }
  document.getElementById('p2-monthly').innerHTML =
    '<div class="card-title">Monthly Breakdown</div>' + monthHTML;

  var tradeHTML = '';
  if(trades.length === 0){
    tradeHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:14px 0">No trades yet — watching for setups</div>';
  } else {
    tradeHTML = '<table class="p2-table"><thead><tr>' +
      '<th>Dir</th><th>Entry Time</th><th>Entry Points</th>' +
      '<th>Exit Time</th><th>Exit Points</th><th>Points Gained</th><th>P&amp;L</th><th>Reason</th>' +
      '</tr></thead><tbody>';
    tradeHTML += trades.map(function(t){
      var rowCls = t.pnl_class==='win' ? ' class="tr-win"' : ' class="tr-loss"';
      return '<tr' + rowCls + '>' +
        '<td class="dir-' + t.direction.toLowerCase() + '">' + t.direction + '</td>' +
        '<td>' + t.entry_time + '</td>' +
        '<td>' + t.entry_price + '</td>' +
        '<td>' + t.exit_time + '</td>' +
        '<td>' + t.exit_price + '</td>' +
        '<td>' + t.points + '</td>' +
        '<td class="' + t.pnl_class + '">' + t.pnl + '</td>' +
        '<td style="color:var(--muted)">' + t.reason + '</td>' +
        '</tr>';
    }).join('');
    tradeHTML += '</tbody></table>';
  }
  document.getElementById('p2-trades').innerHTML =
    '<div class="card-title">Nikkei Trade History</div>' + tradeHTML;
}

/* ── Main refresh loop ──────────────────────────────────────────────────── */
function refreshDashboard(){
  fetch('/api/state')
    .then(function(r){ return r.json(); })
    .then(function(d){
      renderPage1(d);
      updateCountdown();
      renderPage2(d);
    })
    .catch(function(e){ console.error('Refresh error:', e); });
}

refreshDashboard();
setInterval(refreshDashboard, 5000);
pollNews();
setInterval(pollNews, 60000);
</script>
<!-- ARCHIE BRIEF (Job 5) -->
<script>
(function(){
  var ARCHIE_LABEL = '&#9993; Archie Brief';
  function fallback(txt, done){
    var ta=document.createElement('textarea');
    ta.value=txt; ta.style.position='fixed'; ta.style.top='-2000px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try{ document.execCommand('copy'); }catch(e){}
    document.body.removeChild(ta); done();
  }
  function copyText(txt, btn){
    function done(){
      btn.classList.add('archie-copied');
      btn.textContent='Copied!';
      setTimeout(function(){ btn.classList.remove('archie-copied'); btn.innerHTML=ARCHIE_LABEL; },2000);
    }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(done, function(){ fallback(txt, done); });
    } else { fallback(txt, done); }
  }
  window.archieBrief=function(btn){
    btn.textContent='...';
    fetch('/api/archie-brief').then(function(r){return r.text();}).then(function(txt){
      copyText(txt, btn);
    }).catch(function(){ btn.textContent='Error'; setTimeout(function(){ btn.innerHTML=ARCHIE_LABEL; },2000); });
  };
  function inject(){
    if(document.getElementById('archieBtn')) return;
    var st=document.createElement('style');
    st.textContent='.archie-btn{background:rgba(52,152,219,0.10);border:1px solid #3498db;color:#3498db;padding:4px 9px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:0.5px;text-transform:uppercase;transition:background 0.15s;}.archie-btn:hover{background:rgba(52,152,219,0.25);}.archie-btn.archie-copied{background:rgba(46,204,113,0.22);border-color:#2ecc71;color:#2ecc71;}';
    document.head.appendChild(st);
    var btn=document.createElement('button');
    btn.id='archieBtn'; btn.className='archie-btn'; btn.type='button';
    btn.innerHTML=ARCHIE_LABEL; btn.setAttribute('onclick','archieBrief(this)');
    var sd=document.querySelector('.shutdown-btn');
    if(sd && sd.parentNode){ sd.parentNode.insertBefore(btn, sd); }
    else { var hr=document.querySelector('.header-right')||document.querySelector('.header'); if(hr){ hr.appendChild(btn); } }
  }
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', inject); }
  else { inject(); }
})();
</script>
<!-- GUINEVERE KEYWORD EDITOR (Part 3) -->
<script>
(function(){
  var kw = {bullish:[], bearish:[], last_updated:'', updated_by:''};
  var loaded = false;
  var node = null;
  function esc(s){ s = String(s); s = s.split('&').join('&amp;'); s = s.split('<').join('&lt;'); s = s.split('>').join('&gt;'); s = s.split('"').join('&quot;'); return s; }
  function pills(list, kind){
    if(!list || !list.length){ return '<span class="kw-empty">none</span>'; }
    return list.map(function(k,i){
      var fn = kind === 'bull' ? 'kwRemoveBull(' : 'kwRemoveBear(';
      return '<span class="kw-pill kw-' + kind + '">' + esc(k) +
             '<span class="kw-x" onclick="' + fn + i + ')" title="remove">&times;</span></span>';
    }).join('');
  }
  function renderInner(){
    if(!node){ return; }
    var upd = 'Keywords last updated: ' + (kw.last_updated ? esc(kw.last_updated) : '--') +
              (kw.updated_by ? ' by ' + esc(kw.updated_by) : '');
    node.innerHTML =
      '<div class="card-title gold">GUINEVERE KEYWORDS</div>' +
      '<div class="kw-sec"><div class="kw-lbl kw-bull-lbl">BULLISH</div>' +
        '<div class="kw-pills">' + pills(kw.bullish, 'bull') + '</div>' +
        '<div class="kw-add"><input id="kwBullInput" class="kw-input" type="text" placeholder="add bullish keyword" />' +
          '<button class="kw-btn" type="button" onclick="kwAddBull()">Add Bullish</button></div>' +
      '</div>' +
      '<div class="kw-sec"><div class="kw-lbl kw-bear-lbl">BEARISH</div>' +
        '<div class="kw-pills">' + pills(kw.bearish, 'bear') + '</div>' +
        '<div class="kw-add"><input id="kwBearInput" class="kw-input" type="text" placeholder="add bearish keyword" />' +
          '<button class="kw-btn" type="button" onclick="kwAddBear()">Add Bearish</button></div>' +
      '</div>' +
      '<div class="kw-foot"><button class="kw-save" id="kwSaveBtn" type="button" onclick="kwSave()">Save</button>' +
        '<span class="kw-updated" id="kwUpdated">' + upd + '</span></div>';
  }
  function ensureNode(){
    if(!node){
      node = document.createElement('div');
      node.className = 'card';
      node.id = 'kwCard';
      node.style.flexShrink = '0';
      renderInner();
    }
    return node;
  }
  function mount(){
    var nc = document.getElementById('newsCard');
    if(!nc || !nc.parentNode){ return; }
    ensureNode();
    if(nc.nextSibling !== node){ nc.parentNode.insertBefore(node, nc.nextSibling); }
  }
  window.kwAddBull = function(){
    var el = document.getElementById('kwBullInput'); if(!el){ return; }
    var v = (el.value || '').trim(); if(!v){ return; }
    if(kw.bullish.map(function(x){return x.toLowerCase();}).indexOf(v.toLowerCase()) < 0){ kw.bullish.push(v); }
    el.value = ''; renderInner();
  };
  window.kwAddBear = function(){
    var el = document.getElementById('kwBearInput'); if(!el){ return; }
    var v = (el.value || '').trim(); if(!v){ return; }
    if(kw.bearish.map(function(x){return x.toLowerCase();}).indexOf(v.toLowerCase()) < 0){ kw.bearish.push(v); }
    el.value = ''; renderInner();
  };
  window.kwRemoveBull = function(i){ kw.bullish.splice(i,1); renderInner(); };
  window.kwRemoveBear = function(i){ kw.bearish.splice(i,1); renderInner(); };
  window.kwSave = function(){
    var btn = document.getElementById('kwSaveBtn'); if(btn){ btn.textContent = 'Saving...'; btn.disabled = true; }
    fetch('/api/keywords', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bullish:kw.bullish, bearish:kw.bearish, by:'Nick'})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        if(res && res.bullish){ kw.bullish = res.bullish; kw.bearish = res.bearish;
          kw.last_updated = res.last_updated || ''; kw.updated_by = res.updated_by || ''; }
        renderInner();
        var b = document.getElementById('kwSaveBtn');
        if(b){ b.textContent = 'Saved'; b.disabled = false;
          setTimeout(function(){ var b2=document.getElementById('kwSaveBtn'); if(b2){ b2.textContent='Save'; } }, 1800); }
      })
      .catch(function(){
        var b = document.getElementById('kwSaveBtn');
        if(b){ b.textContent = 'Error'; b.disabled = false;
          setTimeout(function(){ var b2=document.getElementById('kwSaveBtn'); if(b2){ b2.textContent='Save'; } }, 1800); }
      });
  };
  function fetchKw(){
    fetch('/api/keywords').then(function(r){ return r.json(); }).then(function(res){
      if(res){ kw.bullish = res.bullish || []; kw.bearish = res.bearish || [];
        kw.last_updated = res.last_updated || ''; kw.updated_by = res.updated_by || ''; }
      loaded = true; renderInner(); mount();
    }).catch(function(){ loaded = true; renderInner(); mount(); });
  }
  var _origRP1 = window.renderPage1;
  if(typeof _origRP1 === 'function'){
    window.renderPage1 = function(d){ _origRP1(d); mount(); };
  }
  var st = document.createElement('style');
  st.textContent = '#kwCard .kw-sec{margin-top:8px;}#kwCard .kw-lbl{font-size:10px;letter-spacing:0.5px;font-weight:bold;margin-bottom:4px;}#kwCard .kw-bull-lbl{color:#2ecc71;}#kwCard .kw-bear-lbl{color:#e74c3c;}#kwCard .kw-pills{display:flex;flex-wrap:wrap;gap:4px;}#kwCard .kw-pill{display:inline-flex;align-items:center;gap:4px;font-size:10px;padding:2px 6px;border-radius:10px;border:1px solid;}#kwCard .kw-bull{color:#2ecc71;border-color:rgba(46,204,113,0.5);background:rgba(46,204,113,0.10);}#kwCard .kw-bear{color:#e74c3c;border-color:rgba(231,76,60,0.5);background:rgba(231,76,60,0.10);}#kwCard .kw-x{cursor:pointer;font-weight:bold;opacity:0.7;}#kwCard .kw-x:hover{opacity:1;}#kwCard .kw-empty{color:var(--muted);font-size:10px;}#kwCard .kw-add{display:flex;gap:4px;margin-top:5px;}#kwCard .kw-input{flex:1;min-width:0;background:#1a1a1a;border:1px solid #333;color:#eee;font-size:10px;padding:3px 6px;border-radius:3px;}#kwCard .kw-btn{background:rgba(200,16,46,0.12);border:1px solid #C8102E;color:#C8102E;font-size:9px;padding:3px 7px;border-radius:3px;cursor:pointer;white-space:nowrap;text-transform:uppercase;letter-spacing:0.3px;}#kwCard .kw-btn:hover{background:rgba(200,16,46,0.28);}#kwCard .kw-foot{display:flex;align-items:center;gap:10px;margin-top:9px;}#kwCard .kw-save{background:rgba(46,204,113,0.15);border:1px solid #2ecc71;color:#2ecc71;font-size:10px;padding:4px 14px;border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:0.5px;}#kwCard .kw-save:hover{background:rgba(46,204,113,0.30);}#kwCard .kw-updated{color:var(--muted);font-size:9px;}';
  function boot(){ document.head.appendChild(st); fetchKw(); setInterval(mount, 2000); }
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', boot); }
  else { boot(); }
})();
</script>
<!-- PHANTOM BRIEF -->
<script>
(function(){
  var L='&#9993; PHANTOM BRIEF';
  function fb(txt,done){var ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.top='-2000px';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);done();}
  function cp(txt,btn){function done(){btn.classList.add('archie-copied');btn.textContent='Copied!';setTimeout(function(){btn.classList.remove('archie-copied');btn.innerHTML=L;},2000);}if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,function(){fb(txt,done);});}else{fb(txt,done);}}
  window.phantomBrief=function(btn){btn.textContent='...';fetch('/api/phantom-brief').then(function(r){return r.text();}).then(function(txt){cp(txt,btn);}).catch(function(){btn.textContent='Error';setTimeout(function(){btn.innerHTML=L;},2000);});};
  function inject(){
    if(document.getElementById('phantomBriefBtn'))return;
    var head=document.querySelector('.phantom-head');if(!head)return;
    if(!document.getElementById('phBriefStyle')){var st=document.createElement('style');st.id='phBriefStyle';st.textContent='.archie-btn{background:rgba(52,152,219,0.10);border:1px solid #3498db;color:#3498db;padding:4px 9px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:0.5px;text-transform:uppercase;}.archie-btn:hover{background:rgba(52,152,219,0.25);}.archie-btn.archie-copied{background:rgba(46,204,113,0.22);border-color:#2ecc71;color:#2ecc71;}';document.head.appendChild(st);}
    var btn=document.createElement('button');btn.id='phantomBriefBtn';btn.className='archie-btn';btn.type='button';btn.innerHTML=L;btn.setAttribute('onclick','phantomBrief(this)');
    head.appendChild(btn);
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',inject);}else{inject();}
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    page = HTML.replace("__VERSION_STRING__", VERSION_STRING)
    return Response(page, mimetype="text/html")


def _phantom_verdict(pnl, thr):
    if pnl is None:
        return None
    if pnl > thr:
        return 'WRONG'
    if pnl < -thr:
        return 'CORRECT'
    return 'NEUTRAL'


def build_phantom_brief():
    """Plain-text phantom-trades brief for pasting to Archie (Phantom Page
    Enhancements, 21 Jul 2026). Multi-horizon moves + 30min/2hr verdict
    distributions computed on the fly -- the stored 1hr verdict is unchanged."""
    import phantom_tracker as _pt
    from datetime import datetime, timezone
    name = "NikkeiTrader"
    has_market = False
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'phantom_trades.csv')
    rows = []
    if os.path.exists(csv_path):
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
        except Exception:
            rows = []
    recent = rows[-50:]

    def fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def thr_for(r):
        t = _pt.VERDICT_THRESHOLD
        if isinstance(t, dict):
            m = (r.get('market') or '').upper()
            if 'ETH' in m:
                return t.get('ETH', 4.0)
            if 'BTC' in m:
                return t.get('BTC', 14.0)
            return getattr(_pt, 'VERDICT_THRESHOLD_DEFAULT', 10.0)
        return t

    def mv(v):
        n = fnum(v)
        if n is None:
            return '--'
        return ('+£%.2f' % n) if n >= 0 else ('-£%.2f' % abs(n))

    correct = sum(1 for r in recent if r.get('verdict') == 'CORRECT')
    wrong = sum(1 for r in recent if r.get('verdict') == 'WRONG')
    neutral = sum(1 for r in recent if r.get('verdict') == 'NEUTRAL')
    total = correct + wrong + neutral
    quality = round(correct / total * 100) if total else 0
    net_saved = sum(fnum(r.get('pnl_1hr')) or 0 for r in recent if r.get('verdict') == 'CORRECT')
    net_missed = sum(fnum(r.get('pnl_1hr')) or 0 for r in recent if r.get('verdict') == 'WRONG')

    def dist(col):
        c = w = n = 0
        for r in recent:
            v = _phantom_verdict(fnum(r.get(col)), thr_for(r))
            if v == 'CORRECT':
                c += 1
            elif v == 'WRONG':
                w += 1
            elif v == 'NEUTRAL':
                n += 1
        return c, w, n

    c30, w30, n30 = dist('pnl_30min')
    c2h, w2h, n2h = dist('pnl_2hr')

    flips = both = wc = cw = 0
    for r in recent:
        v1 = _phantom_verdict(fnum(r.get('pnl_1hr')), thr_for(r))
        v2 = _phantom_verdict(fnum(r.get('pnl_2hr')), thr_for(r))
        if v1 and v2:
            both += 1
            if v1 != v2:
                flips += 1
                if v1 == 'WRONG' and v2 == 'CORRECT':
                    wc += 1
                elif v1 == 'CORRECT' and v2 == 'WRONG':
                    cw += 1
    flip_rate = round(flips / both * 100) if both else 0
    common = 'WRONG->CORRECT' if wc >= cw else 'CORRECT->WRONG'

    bar = '=' * 64
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    L = []
    L.append(bar)
    L.append('ARCHIE BRIEF -- %s PHANTOM TRADES' % name.upper())
    L.append('Generated: %s UTC' % ts)
    L.append(bar)
    L.append('')
    L.append('SUMMARY')
    L.append('  Quality: %d%% | Last %d decisions' % (quality, len(recent)))
    L.append('  Correct: %d | Wrong: %d | Neutral: %d' % (correct, wrong, neutral))
    L.append('  Net Saved: GBP +%.2f | Net Missed: GBP -%.2f' % (abs(net_saved), abs(net_missed)))
    L.append('')
    L.append('TIME HORIZON ANALYSIS (from available data)')
    L.append('  30min verdict distribution:')
    L.append('    Correct: %d | Wrong: %d | Neutral: %d' % (c30, w30, n30))
    L.append('  2hr verdict distribution:')
    L.append('    Correct: %d | Wrong: %d | Neutral: %d' % (c2h, w2h, n2h))
    L.append('  Verdict flip rate (1hr->2hr): %d%% of rows change verdict' % flip_rate)
    L.append('  Most common flip: %s (%d WRONG->CORRECT, %d CORRECT->WRONG)' % (common, wc, cw))
    L.append('')
    L.append('RECENT PHANTOM TRADES (last 10)')
    for r in reversed(recent[-10:]):
        tsr = (r.get('timestamp') or '')[:16].replace('T', ' ')
        mkt = ('%s | ' % (r.get('market') or '--')) if has_market else ''
        L.append('  %s | %s%s | conf %s | 5m:%s 10m:%s 15m:%s 30m:%s 1hr:%s 2hr:%s | %s' % (
            tsr, mkt, (r.get('direction_blocked') or '--'), (r.get('confidence') or '--'),
            mv(r.get('pnl_5min')), mv(r.get('pnl_10min')), mv(r.get('pnl_15min')),
            mv(r.get('pnl_30min')), mv(r.get('pnl_1hr')), mv(r.get('pnl_2hr')),
            (r.get('verdict') or 'PENDING')))
    L.append('')
    L.append(bar)
    L.append('End of %s Phantom Archie Brief' % name)
    L.append(bar)
    return '\n'.join(L)


@app.route("/api/phantom-brief")
def api_phantom_brief():
    return build_phantom_brief(), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/archie-brief")
def api_archie_brief():
    """Plain-text snapshot of current dashboard state for pasting to Archie."""
    import json as _json
    import archie_brief
    try:
        state = _json.loads(api_state().get_data(as_text=True))
    except Exception:
        state = get_state()
    txt = archie_brief.build_system_brief(state, "NikkeiTrader", "Japan 225", str(LOG_DIR))
    return txt, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/state")
def api_state():
    s = get_state()
    trade = s.get("current_trade")
    if trade is not None and hasattr(trade, "__dict__"):
        trade = {k: str(v) for k, v in trade.__dict__.items()}

    account = load_account_stats()
    trades  = load_trades()
    monthly = load_monthly_stats()
    perf    = s.get("perf") or {}
    flat    = compute_flat_fields(s)

    payload = {
        "mode":            s.get("mode", "PAPER"),
        "version":         s.get("version", APP_VERSION),
        "phase":           s.get("phase", "--"),
        "nikkei_level":      s.get("nikkei_level", 0.0),
        "connector_status": s.get("connector_status", "yahoo"),
        "capital":         s.get("capital", STARTING_CAPITAL),
        "daily_pnl":       s.get("daily_pnl", 0.0),
        "total_trades":    s.get("total_trades", 0),
        "win_rate":        s.get("win_rate", 0.0),
        "in_trade":        s.get("in_trade", False),
        "current_trade":   trade,
        "decision":        s.get("decision"),
        "panel_mode":      s.get("panel_mode", "pre_checks"),
        "pre_checks":      s.get("pre_checks"),
        "checklist":       s.get("checklist", {}),
        "trend_1d":        s.get("trend_1d", "NEUTRAL"),
        "trend_1h":        s.get("trend_1h", "NEUTRAL"),
        "signal_5m":       s.get("signal_5m", "NEUTRAL"),
        "indicators_1d":   s.get("indicators_1d", {}),
        "indicators_1h":   s.get("indicators_1h", {}),
        "indicators_5m":   s.get("indicators_5m", {}),
        "perf":            perf,
        "calendar":        s.get("calendar", ""),
        "kill_switch":     s.get("kill_switch", False),
        "kill_tier":       s.get("kill_tier", 0),
        "updated_at":      s.get("updated_at", "--"),
        "account":         account,
        "trades":          trades,
        "monthly_stats":   monthly,
        "stay_out_quality": get_stay_out_quality(),
        "version_string":  VERSION_STRING,
    }
    payload.update(flat)
    return jsonify(payload)


@app.route("/api/news")
def api_news():
    """Guinevere Nikkei news sentiment (Part 5) + UK-event caution window + macro flag."""
    try:
        data = guinevere_news.fetch_nikkei_sentiment() or {}
        ts = data.get("timestamp")
        payload = {
            "sentiment":  data.get("sentiment", "NEUTRAL"),
            "score":      data.get("score", 0),
            "headlines":  data.get("headlines", []),
            "reason":     data.get("reason", ""),
            "updated_at": ts.strftime("%H:%M:%S UTC") if hasattr(ts, "strftime") else "--",
            "macro_flag": data.get("macro_flag", "NEUTRAL"),
            "macro_adj":  data.get("macro_adj", 0),
        }
        try:
            cau_active, cau_reason = guinevere_news.get_nikkei_calendar_status()
        except Exception:
            cau_active, cau_reason = False, ""
        payload["caution_active"] = bool(cau_active)
        payload["caution_reason"] = cau_reason
        return jsonify(payload)
    except Exception as exc:
        return jsonify({
            "sentiment": "NEUTRAL", "score": 0, "headlines": [],
            "reason": "News error: " + str(exc), "updated_at": "--",
            "caution_active": False, "caution_reason": "",
            "macro_flag": "NEUTRAL", "macro_adj": 0,
        })


@app.route("/api/keywords", methods=["GET", "POST"])
def api_keywords():
    """Guinevere keyword editor (Part 3/5). GET returns the current bullish/bearish
    lists; POST {bullish:[...], bearish:[...], by:"Nick"} saves them LIVE (Guinevere
    re-reads logs/guinevere_keywords.json every 5 min) and logs each add/remove to
    logs/guinevere_keyword_changes.log."""
    if request.method == "GET":
        try:
            return jsonify(guinevere_news.get_keywords())
        except Exception as exc:
            return jsonify({"bullish": [], "bearish": [], "last_updated": "--",
                            "updated_by": "", "error": str(exc)}), 500
    try:
        body = request.get_json(force=True, silent=True) or {}
        bullish = body.get("bullish") or []
        bearish = body.get("bearish") or []
        by = (str(body.get("by") or "Nick").strip()) or "Nick"
        result = guinevere_news.save_keywords(bullish, bearish, updated_by=by)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/api/update", methods=["POST"])
def api_update():
    """Receive state push from main engine process."""
    try:
        new_state = request.get_json(force=True, silent=True) or {}
        push_state(new_state)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/lift-confidence", methods=["POST"])
def api_lift_confidence():
    """Request a manual Morgan confidence lift (Gaius intervention Step 4). Writes
    logs/confidence_lift.json; the trading engine applies it in-process on its next
    cycle -- LIVE, no restart. Optional JSON body {"to": <0-100>} (default 50)."""
    import json
    to = 50.0
    try:
        body = request.get_json(force=True, silent=True) or {}
        if body.get("to") is not None:
            to = max(0.0, min(100.0, float(body["to"])))
    except Exception:
        to = 50.0
    ts = datetime.now(timezone.utc).isoformat()
    reason = ("CONFIDENCE LIFT -- Gaius intervention. Manual reset to %g via "
              "/api/lift-confidence. %s" % (to, ts))
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "confidence_lift.json").write_text(
            json.dumps({"confidence": to, "reason": reason, "requested_utc": ts}),
            encoding="utf-8")
        return jsonify({"status": "lift_requested", "to": to,
                        "note": "engine applies on next cycle (live, no restart)"})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Write shutdown flag for main trader, then kill this dashboard process."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        SHUTDOWN_FLAG.write_text("shutdown requested\n", encoding="utf-8")
        log.info("Shutdown flag written -- main trader will exit on next check")
    except Exception as e:
        log.warning("Could not write shutdown flag: %s", e)

    def _kill():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"status": "shutting_down"})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ALBION STANDING RULE: all log timestamps are UTC (never BST/local). See main_nikkeitrader.py.
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    log.info("NikkeiTrader AI Dashboard starting on http://localhost:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
