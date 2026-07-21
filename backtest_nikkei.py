"""
NikkeiTrader AI -- backtest_nikkei.py

Japan 225 spread betting backtest engine using the proven TrendSurfer indicator suite.

Strategy  : SSL Cloud + RSI + MACD + TMO + Chande MO + Money Flow
Timeframes: Daily (trend filter) + 1h (trend confirmation) + 5m (entry timing)

Nikkei mechanics:
  Spread betting P&L = points_moved x stake_per_point
  Stake per point    = MAX_RISK_PER_TRADE / stop_distance_pts
  IG spread cost     = 1 point x stake_per_point per trade
  No overnight positions -- force close at 16:20 UK time

Parameter sweep: 12 combinations
  Trailing stop    : 20 / 30 / 40 points
  Lunch lull filter: ON (skip 12:00-13:30) / OFF (allow entries)
  GBP/USD filter   : ON (require 6/6 if context conflicts) / OFF

Output:
  logs/backtest_nikkei_results.txt  -- best configuration full detail
  logs/backtest_nikkei_sweep.csv    -- all 12 combinations
  logs/backtest_nikkei_trades.csv   -- all trades across all combos
"""

import csv
import logging
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("NikkeiTrader.Backtest")

# ── Constants ──────────────────────────────────────────────────────────────────

STARTING_CAPITAL_GBP = 500.0
MAX_RISK_PER_TRADE   = 10.0        # £10 = 2% of £500 account
IG_SPREAD_POINTS     = 1.0         # IG Markets Japan 225 spread (1 point)
TP_POINTS            = 200.0       # Safety ceiling take-profit in points
LOGS_DIR             = Path(__file__).parent / "logs"

Nikkei_TICKER   = "^N225"
GBPUSD_TICKER = "GBPUSD=X"
SP500_TICKER  = "ES=F"

UK_TZ = "Europe/London"

# Force-close time (UK local)
FORCE_CLOSE_HOUR = 16
FORCE_CLOSE_MIN  = 20

# Session phase labels
PRE_OPEN      = "PRE_OPEN"
MORNING = "MORNING"
LUNCH_BREAK    = "LUNCH_BREAK"
AFTERNOON     = "AFTERNOON"
CLOSING       = "CLOSING"
CLOSED        = "CLOSED"

TRADEABLE_PHASES = {MORNING, AFTERNOON, LUNCH_BREAK}

# Parameter sweep grid
TS_OPTIONS     = [20, 30, 40]   # trailing stop distances in points
LUNCH_OPTIONS  = [True, False]  # True = skip lunch entries
GBPUSD_OPTIONS = [True, False]  # True = apply GBP/USD context filter

# Reference stats for comparison table
BTC_STRICT = {"trades_pw": 3.4, "win_rate": 62.1, "ann_return": 40.6, "max_consec": 3}
ETH_STRICT = {"trades_pw": 5.2, "win_rate": 47.7, "ann_return": 44.0, "max_consec": 6}


# ── Indicator functions (identical implementations to TideTraderAI) ───────────

def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(series: pd.Series,
               fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast    = series.ewm(span=fast,   adjust=False).mean()
    ema_slow    = series.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd":      macd_line,
        "signal":    signal_line,
        "histogram": macd_line - signal_line,
    })


def _calc_ssl_cloud(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    sma_high = df["high"].rolling(period).mean()
    sma_low  = df["low"].rolling(period).mean()
    hlv = pd.Series(
        np.where(df["close"] > sma_high, 1,
        np.where(df["close"] < sma_low,  -1, np.nan)),
        index=df.index,
    ).ffill()
    ssl_up   = np.where(hlv < 0, sma_low,  sma_high)
    ssl_down = np.where(hlv < 0, sma_high, sma_low)
    return pd.DataFrame({
        "ssl_up":   ssl_up,
        "ssl_down": ssl_down,
        "ssl_bull": ssl_up > ssl_down,
    }, index=df.index)


def _calc_tmo(df: pd.DataFrame, length: int = 14, calc_length: int = 5) -> pd.DataFrame:
    mom    = np.sign(df["close"] - df["open"]).rolling(length).sum()
    main   = mom.ewm(span=calc_length, adjust=False).mean()
    smooth = main.ewm(span=calc_length, adjust=False).mean()
    return pd.DataFrame({"tmo_main": main, "tmo_smooth": smooth}, index=df.index)


def _calc_chande(df: pd.DataFrame, period: int = 20) -> pd.Series:
    diff   = df["close"].diff()
    up_sum = diff.clip(lower=0).rolling(period).sum()
    dn_sum = (-diff.clip(upper=0)).rolling(period).sum()
    denom  = up_sum + dn_sum
    return (100 * (up_sum - dn_sum) / denom.replace(0, np.nan)).rename("chande_mo")


def _calc_money_flow(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan)  # guard against zero-volume index bars
    mfv = tp * vol * np.sign(df["close"] - df["open"])
    return (mfv.rolling(period).sum() / vol.rolling(period).sum().replace(0, np.nan)
            ).rename("money_flow")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all TrendSurfer indicators on a full OHLCV DataFrame."""
    if df.empty:
        return df
    df = df.copy()
    df["rsi"]            = _calc_rsi(df["close"])
    macd_df              = _calc_macd(df["close"])
    df["macd"]           = macd_df["macd"]
    df["macd_signal"]    = macd_df["signal"]
    df["macd_histogram"] = macd_df["histogram"]
    ssl_df               = _calc_ssl_cloud(df)
    df["ssl_up"]         = ssl_df["ssl_up"]
    df["ssl_down"]       = ssl_df["ssl_down"]
    df["ssl_bull"]       = ssl_df["ssl_bull"]
    tmo_df               = _calc_tmo(df)
    df["tmo_main"]       = tmo_df["tmo_main"]
    df["tmo_smooth"]     = tmo_df["tmo_smooth"]
    df["chande_mo"]      = _calc_chande(df)
    df["money_flow"]     = _calc_money_flow(df)
    return df


# ── Indicator scoring (identical to TideTraderAI strategy_btc._score_bar) ─────

def _score_bar(bar: pd.Series) -> dict:
    """Score a single bar: count how many of 6 indicators vote LONG or SHORT."""
    scores = {}

    ssl_bull = bar.get("ssl_bull")
    if pd.notna(ssl_bull):
        scores["ssl"] = 1 if ssl_bull else -1

    rsi = bar.get("rsi")
    if pd.notna(rsi):
        if   rsi > 55: scores["rsi"] = 1
        elif rsi < 45: scores["rsi"] = -1
        else:          scores["rsi"] = 0

    hist = bar.get("macd_histogram")
    if pd.notna(hist):
        scores["macd"] = 1 if hist > 0 else -1

    tmo_main   = bar.get("tmo_main")
    tmo_smooth = bar.get("tmo_smooth")
    if pd.notna(tmo_main) and pd.notna(tmo_smooth):
        scores["tmo"] = 1 if tmo_main > tmo_smooth else -1

    cmo = bar.get("chande_mo")
    if pd.notna(cmo):
        scores["chande"] = 1 if cmo > 0 else -1

    mf = bar.get("money_flow")
    if pd.notna(mf):
        scores["money_flow"] = 1 if mf > 0 else -1

    return {
        "scores":      scores,
        "long_count":  sum(1 for v in scores.values() if v == 1),
        "short_count": sum(1 for v in scores.values() if v == -1),
        "total":       len(scores),
    }


# ── Session phase logic ────────────────────────────────────────────────────────

def get_session_phase(ts_uk) -> str:
    """Return the Nikkei trading session phase for a UK-local pandas Timestamp."""
    if ts_uk.weekday() >= 5:
        return CLOSED

    t = ts_uk.hour * 60 + ts_uk.minute   # minutes since midnight

    if   t < 480:   return CLOSED        # before 08:00
    elif t < 495:   return PRE_OPEN      # 08:00 – 08:14
    elif t < 720:   return MORNING # 08:15 – 11:59
    elif t < 810:   return LUNCH_BREAK    # 12:00 – 13:29
    elif t < 930:   return AFTERNOON     # 13:30 – 15:29
    elif t < 990:   return CLOSING       # 15:30 – 16:29
    else:           return CLOSED        # 16:30+


# ── GBP/USD context ────────────────────────────────────────────────────────────

def _enrich_gbpusd(df: pd.DataFrame) -> pd.DataFrame:
    """Precompute GBP/USD 1h trend direction using a 5-period EMA."""
    if df.empty:
        return df
    df = df.copy()
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["gbpusd_dir"] = np.where(
        df["close"] > df["ema5"] * 1.0010, "GBPUSD_RISING",
        np.where(df["close"] < df["ema5"] * 0.9990, "GBPUSD_FALLING",
                 "GBPUSD_NEUTRAL"),
    )
    return df


def get_gbpusd_context(df_gbpusd: pd.DataFrame, ts) -> str:
    """Return the GBP/USD 1h direction at or before timestamp ts."""
    mask = df_gbpusd.index <= ts
    if not mask.any():
        return "GBPUSD_NEUTRAL"
    val = df_gbpusd[mask]["gbpusd_dir"].iloc[-1]
    return str(val) if isinstance(val, str) else "GBPUSD_NEUTRAL"


# ── Trend helpers ──────────────────────────────────────────────────────────────

def _last_bar_at(df: pd.DataFrame, ts) -> Optional[pd.Series]:
    mask = df.index <= ts
    if not mask.any():
        return None
    return df[mask].iloc[-1]


def _daily_ssl(bar_1d: Optional[pd.Series]) -> str:
    if bar_1d is None:
        return "NEUTRAL"
    val = bar_1d.get("ssl_bull")
    if pd.isna(val):
        return "NEUTRAL"
    return "LONG" if val else "SHORT"


def _1h_ssl(bar_1h: Optional[pd.Series]) -> str:
    if bar_1h is None:
        return "NEUTRAL"
    val = bar_1h.get("ssl_bull")
    if pd.isna(val):
        return "NEUTRAL"
    return "LONG" if val else "SHORT"


def _1h_rsi_ok(bar_1h: pd.Series, direction: str) -> bool:
    rsi = bar_1h.get("rsi")
    if pd.isna(rsi):
        return True
    return (rsi > 55) if direction == "LONG" else (rsi < 45)


def _is_choppy(bar: pd.Series) -> bool:
    """Block entry when RSI and TMO are both near-neutral (choppy market)."""
    rsi = bar.get("rsi")
    tmo = bar.get("tmo_main")
    if pd.isna(rsi) or pd.isna(tmo):
        return False
    return 44 <= rsi <= 56 and -0.5 <= tmo <= 0.5


def _candle_confirms(bar: pd.Series, direction: str) -> bool:
    c, o = float(bar["close"]), float(bar["open"])
    return (c > o) if direction == "LONG" else (c < o)


# ── Spread betting trade record ────────────────────────────────────────────────

@dataclass
class NikkeiTrade:
    """One Japan 225 spread bet. P&L = pnl_pts × stake."""

    direction:   str
    entry_price: float     # Nikkei index level (e.g. 8215.0)
    stop_pts:    float     # trailing stop distance in index points
    entry_time:  object = field(default=None)
    session:     str    = field(default="")

    def __post_init__(self):
        self.stake       = MAX_RISK_PER_TRADE / self.stop_pts   # £ per point
        self.trail_best  = self.entry_price
        if self.direction == "LONG":
            self.stop_loss   = self.entry_price - self.stop_pts
            self.take_profit = self.entry_price + TP_POINTS
        else:
            self.stop_loss   = self.entry_price + self.stop_pts
            self.take_profit = self.entry_price - TP_POINTS

        self.exit_price  = None
        self.exit_time   = None
        self.exit_reason = None
        self.pnl_pts     = None
        self.pnl_gbp     = None

    def update_trailing_stop(self, price: float) -> None:
        if self.direction == "LONG" and price > self.trail_best:
            self.trail_best = price
            new_sl = price - self.stop_pts
            if new_sl > self.stop_loss:
                self.stop_loss = new_sl
        elif self.direction == "SHORT" and price < self.trail_best:
            self.trail_best = price
            new_sl = price + self.stop_pts
            if new_sl < self.stop_loss:
                self.stop_loss = new_sl

    def close(self, price: float, reason: str) -> None:
        self.exit_price  = price
        self.exit_reason = reason
        self.pnl_pts     = (price - self.entry_price) if self.direction == "LONG" \
                           else (self.entry_price - price)
        self.pnl_gbp     = self.pnl_pts * self.stake


# ── Yahoo Finance data fetching ────────────────────────────────────────────────

def _yf_download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.sort_index(inplace=True)
    return df


def _fetch_nikkei_fine() -> tuple:
    """Fetch finest Nikkei intraday data with fallbacks. Returns (df, resolution_str)."""
    for ticker, interval, period, min_bars, label in [
        (Nikkei_TICKER, "5m",  "60d", 200, "^N225 5m"),
        (Nikkei_TICKER, "15m", "60d", 100, "^N225 15m (FALLBACK -- 5m unavailable)"),
        ("EWU",       "5m",  "60d",  50, "EWU 5m (PROXY -- ^N225 5m/15m unavailable)"),
    ]:
        log.info("    Trying %s %s...", ticker, interval)
        df = _yf_download(ticker, interval, period)
        if len(df) >= min_bars:
            log.info("    OK: %d bars [%s -> %s]",
                     len(df),
                     df.index[0].strftime("%Y-%m-%d %H:%M"),
                     df.index[-1].strftime("%Y-%m-%d %H:%M"))
            return add_indicators(df), label
        log.warning("    Only %d bars -- trying fallback...", len(df))

    raise RuntimeError("Could not obtain usable Nikkei intraday data from Yahoo Finance")


def fetch_historical_data() -> tuple:
    """Fetch and enrich all data sources. Returns (df_1d, df_1h, df_fine, df_gbpusd, df_sp500, resolution)."""
    log.info("Fetching historical data from Yahoo Finance...")

    log.info("  ^N225 1d (2y)...")
    df_1d = add_indicators(_yf_download(Nikkei_TICKER, "1d", "2y"))
    log.info("    %d candles  [%s -> %s]",
             len(df_1d),
             df_1d.index[0].strftime("%Y-%m-%d"),
             df_1d.index[-1].strftime("%Y-%m-%d"))

    log.info("  ^N225 1h (90d)...")
    df_1h = add_indicators(_yf_download(Nikkei_TICKER, "1h", "90d"))
    log.info("    %d candles  [%s -> %s]",
             len(df_1h),
             df_1h.index[0].strftime("%Y-%m-%d %H:%M"),
             df_1h.index[-1].strftime("%Y-%m-%d %H:%M"))

    log.info("  Nikkei finest intraday...")
    df_fine, resolution = _fetch_nikkei_fine()
    log.info("    Resolution used: %s", resolution)

    log.info("  GBPUSD=X 1h (90d)...")
    df_gbpusd = _yf_download(GBPUSD_TICKER, "1h", "90d")
    if not df_gbpusd.empty:
        df_gbpusd = _enrich_gbpusd(df_gbpusd)
        log.info("    %d candles  [%s -> %s]",
                 len(df_gbpusd),
                 df_gbpusd.index[0].strftime("%Y-%m-%d %H:%M"),
                 df_gbpusd.index[-1].strftime("%Y-%m-%d %H:%M"))
    else:
        log.warning("    GBPUSD data unavailable -- GBP/USD filter will be inactive")

    log.info("  ES=F 1d (2y)  [S&P 500 context, not used in signals]...")
    df_sp500 = _yf_download(SP500_TICKER, "1d", "2y")
    if not df_sp500.empty:
        log.info("    %d candles", len(df_sp500))
    else:
        log.warning("    S&P 500 data unavailable")

    return df_1d, df_1h, df_fine, df_gbpusd, df_sp500, resolution


# ── Single backtest run ────────────────────────────────────────────────────────

def run_single_backtest(
    label:            str,
    df_1d:            pd.DataFrame,
    df_1h:            pd.DataFrame,
    df_fine:          pd.DataFrame,
    df_gbpusd:        pd.DataFrame,
    stop_pts:         float,
    lunch_restricted: bool,
    gbpusd_filter:    bool,
) -> tuple:
    """
    Replay df_fine bar by bar.
    Returns (stats_dict, completed_trades_list).
    """
    log.info("")
    log.info("=" * 60)
    log.info("  %s", label)
    log.info("  stop=%dpt  lunch=%s  gbpusd=%s  bars=%d",
             stop_pts,
             "SKIP" if lunch_restricted else "open",
             "ON"   if gbpusd_filter    else "off",
             len(df_fine))
    log.info("=" * 60)

    capital        = STARTING_CAPITAL_GBP
    current_trade: Optional[NikkeiTrade] = None
    completed      = []

    bars_total      = len(df_fine)
    bars_in_session = 0
    trade_peak_pts  = 0.0
    trade_tp_hit    = False

    for ts, bar in df_fine.iterrows():

        ts_uk = ts.tz_convert(UK_TZ)
        phase = get_session_phase(ts_uk)

        # ── Force close at 16:20 UK time (or once CLOSING phase starts) ───────
        is_force_close = (
            phase in (CLOSING, CLOSED)
            or (ts_uk.hour == FORCE_CLOSE_HOUR and ts_uk.minute >= FORCE_CLOSE_MIN)
        )

        if current_trade is not None and is_force_close:
            exit_px = float(bar["close"])
            current_trade.close(exit_px, "SESSION_CLOSE")
            current_trade.exit_time = ts
            current_trade._peak_pts = trade_peak_pts
            current_trade._tp_hit   = trade_tp_hit
            cost = IG_SPREAD_POINTS * current_trade.stake
            current_trade._cost_gbp = cost
            current_trade._net_pnl  = current_trade.pnl_gbp - cost
            capital += current_trade._net_pnl
            log.info(
                "  FORCE CLOSE | %s | %+.1fpt | net=GBP %+.2f | cap=GBP %.2f",
                current_trade.direction, current_trade.pnl_pts,
                current_trade._net_pnl, capital,
            )
            completed.append(current_trade)
            current_trade  = None
            trade_peak_pts = 0.0
            trade_tp_hit   = False
            continue

        # ── Manage open trade: update trailing stop, check SL/TP ──────────────
        if current_trade is not None:
            high = float(bar["high"])
            low  = float(bar["low"])
            exit_reason = None

            if current_trade.direction == "LONG":
                current_trade.update_trailing_stop(high)
                trade_peak_pts = max(trade_peak_pts, high - current_trade.entry_price)
                if high >= current_trade.take_profit:
                    trade_tp_hit = True
                if low <= current_trade.stop_loss:
                    exit_reason = "STOP_LOSS"
                    exit_px     = current_trade.stop_loss
                elif high >= current_trade.take_profit:
                    exit_reason = "TAKE_PROFIT"
                    exit_px     = current_trade.take_profit
            else:
                current_trade.update_trailing_stop(low)
                trade_peak_pts = max(trade_peak_pts, current_trade.entry_price - low)
                if low <= current_trade.take_profit:
                    trade_tp_hit = True
                if high >= current_trade.stop_loss:
                    exit_reason = "STOP_LOSS"
                    exit_px     = current_trade.stop_loss
                elif low <= current_trade.take_profit:
                    exit_reason = "TAKE_PROFIT"
                    exit_px     = current_trade.take_profit

            if exit_reason:
                current_trade.close(exit_px, exit_reason)
                current_trade.exit_time = ts
                current_trade._peak_pts = trade_peak_pts
                current_trade._tp_hit   = trade_tp_hit
                cost = IG_SPREAD_POINTS * current_trade.stake
                current_trade._cost_gbp = cost
                current_trade._net_pnl  = current_trade.pnl_gbp - cost
                capital += current_trade._net_pnl
                log.info(
                    "  CLOSED | %s | %+.1fpt | net=GBP %+.2f | %s | cap=GBP %.2f",
                    current_trade.direction, current_trade.pnl_pts,
                    current_trade._net_pnl, exit_reason, capital,
                )
                completed.append(current_trade)
                current_trade  = None
                trade_peak_pts = 0.0
                trade_tp_hit   = False

            continue   # never look for entries on bars where a trade is active

        # ── Entry gate ─────────────────────────────────────────────────────────
        if phase not in TRADEABLE_PHASES:
            continue
        if phase == LUNCH_BREAK and lunch_restricted:
            continue

        bars_in_session += 1

        bar_1h = _last_bar_at(df_1h, ts)
        bar_1d = _last_bar_at(df_1d, ts)
        if bar_1h is None:
            continue

        # Daily and 1h SSL must agree (NEUTRAL daily allows 1h to decide)
        d_ssl = _daily_ssl(bar_1d)
        h_ssl = _1h_ssl(bar_1h)

        if   d_ssl == "NEUTRAL": ssl_dir = h_ssl
        elif h_ssl == "NEUTRAL": ssl_dir = d_ssl
        elif d_ssl == h_ssl:     ssl_dir = d_ssl
        else:                    continue   # disagreement

        if ssl_dir == "NEUTRAL":
            continue

        # Choppiness filter
        if _is_choppy(bar):
            continue

        score = _score_bar(bar)

        # Precompute GBP/USD context (shared across LONG/SHORT check)
        gbpusd_ctx = "GBPUSD_NEUTRAL"
        if gbpusd_filter and not df_gbpusd.empty:
            gbpusd_ctx = get_gbpusd_context(df_gbpusd, ts)

        for direction in ("LONG", "SHORT"):
            if ssl_dir != direction:
                continue

            # Candle must confirm direction
            if not _candle_confirms(bar, direction):
                continue

            # 1h RSI filter
            if not _1h_rsi_ok(bar_1h, direction):
                continue

            # Minimum indicator threshold
            min_req = 5  # base: 5 of 6

            # Lunch lull: require 6/6 when we're allowing lunch entries
            if phase == LUNCH_BREAK:
                min_req = 6

            # GBP/USD context: require 6/6 when direction conflicts with GBP context
            if gbpusd_filter:
                conflict = (direction == "LONG"  and gbpusd_ctx == "GBPUSD_RISING") or \
                           (direction == "SHORT" and gbpusd_ctx == "GBPUSD_FALLING")
                if conflict:
                    min_req = max(min_req, 6)

            count = score["long_count"] if direction == "LONG" else score["short_count"]
            if count < min_req:
                continue

            # ── Open trade ────────────────────────────────────────────────────
            entry_px = float(bar["close"])
            current_trade = NikkeiTrade(
                direction   = direction,
                entry_price = entry_px,
                stop_pts    = stop_pts,
                entry_time  = ts,
                session     = phase,
            )
            log.info(
                "  OPENED | %s | %s | %.1f | sl=%.1f | tp=%.1f"
                " | stake=GBP%.4f/pt | cost=GBP%.4f",
                direction, phase, entry_px,
                current_trade.stop_loss, current_trade.take_profit,
                current_trade.stake,
                IG_SPREAD_POINTS * current_trade.stake,
            )
            break

    # ── Close any trade still open at end of data ──────────────────────────────
    if current_trade is not None:
        last_close = float(df_fine.iloc[-1]["close"])
        current_trade.close(last_close, "BACKTEST_END")
        current_trade.exit_time = df_fine.index[-1]
        current_trade._peak_pts = trade_peak_pts
        current_trade._tp_hit   = trade_tp_hit
        cost = IG_SPREAD_POINTS * current_trade.stake
        current_trade._cost_gbp = cost
        current_trade._net_pnl  = current_trade.pnl_gbp - cost
        capital += current_trade._net_pnl
        completed.append(current_trade)
        log.info("  Backtest end -- forced close at %.1f", last_close)

    stats = _compute_stats(completed, capital, bars_total, bars_in_session, df_fine)
    stats.update({
        "label":            label,
        "stop_pts":         stop_pts,
        "lunch_restricted": lunch_restricted,
        "gbpusd_filter":    gbpusd_filter,
    })
    return stats, completed


# ── Statistics ─────────────────────────────────────────────────────────────────

def _compute_stats(completed: list, final_capital: float,
                   bars_total: int, bars_in_session: int,
                   df_fine: pd.DataFrame) -> dict:
    total  = len(completed)
    longs  = [t for t in completed if t.direction == "LONG"]
    shorts = [t for t in completed if t.direction == "SHORT"]

    net_wins   = [t for t in completed if getattr(t, "_net_pnl", 0) > 0]
    net_losses = [t for t in completed if getattr(t, "_net_pnl", 0) <= 0]

    gross_pnl   = sum(t.pnl_gbp or 0 for t in completed)
    total_costs = sum(getattr(t, "_cost_gbp", 0) for t in completed)
    net_pnl     = sum(getattr(t, "_net_pnl",  0) for t in completed)
    win_rate    = len(net_wins) / total * 100 if total else 0.0

    avg_net_win  = (sum(getattr(t, "_net_pnl", 0) for t in net_wins)
                    / len(net_wins) if net_wins else 0.0)
    avg_net_loss = (sum(getattr(t, "_net_pnl", 0) for t in net_losses)
                    / len(net_losses) if net_losses else 0.0)
    best_trade   = max((getattr(t, "_net_pnl", 0) for t in completed), default=0.0)
    worst_trade  = min((getattr(t, "_net_pnl", 0) for t in completed), default=0.0)

    avg_loss_abs = abs(avg_net_loss)
    break_even   = (avg_loss_abs / (avg_net_win + avg_loss_abs) * 100
                    if (avg_net_win + avg_loss_abs) > 0 else 0.0)

    max_consec = streak = 0
    for t in completed:
        if getattr(t, "_net_pnl", 0) < 0:
            streak += 1
            max_consec = max(max_consec, streak)
        else:
            streak = 0

    period_days     = max((df_fine.index[-1] - df_fine.index[0]).days, 1)
    weeks           = max(period_days / 7, 0.01)
    trades_per_week = total / weeks
    total_return_pct = (final_capital - STARTING_CAPITAL_GBP) / STARTING_CAPITAL_GBP * 100
    ann_return       = total_return_pct * 365 / period_days

    monthly: dict = defaultdict(float)
    for t in completed:
        if t.exit_time:
            monthly[t.exit_time.strftime("%Y-%m")] += getattr(t, "_net_pnl", 0)
    best_month  = max(monthly.items(), key=lambda x: x[1], default=("--", 0.0))
    worst_month = min(monthly.items(), key=lambda x: x[1], default=("--", 0.0))

    exit_counts: dict = defaultdict(int)
    for t in completed:
        exit_counts[t.exit_reason or "UNKNOWN"] += 1

    sl_trades          = [t for t in completed if t.exit_reason == "STOP_LOSS"]
    avg_peak_before_sl = (sum(getattr(t, "_peak_pts", 0) for t in sl_trades)
                          / len(sl_trades) if sl_trades else 0.0)
    tp_hit_before_sl   = sum(1 for t in sl_trades if getattr(t, "_tp_hit", False))

    def _dir_stats(subset):
        w = [t for t in subset if getattr(t, "_net_pnl", 0) > 0]
        return {
            "count":    len(subset),
            "wins":     len(w),
            "win_rate": len(w) / len(subset) * 100 if subset else 0.0,
            "net_pnl":  sum(getattr(t, "_net_pnl", 0) for t in subset),
        }

    by_session = {
        ph: _dir_stats([t for t in completed if t.session == ph])
        for ph in (MORNING, AFTERNOON, LUNCH_BREAK)
    }

    return {
        "total":              total,
        "wins":               len(net_wins),
        "losses":             len(net_losses),
        "win_rate":           win_rate,
        "gross_pnl":          gross_pnl,
        "total_costs":        total_costs,
        "net_pnl":            net_pnl,
        "starting_capital":   STARTING_CAPITAL_GBP,
        "final_capital":      final_capital,
        "total_return_pct":   total_return_pct,
        "ann_return":         ann_return,
        "period_days":        period_days,
        "weeks":              weeks,
        "trades_per_week":    trades_per_week,
        "avg_net_win":        avg_net_win,
        "avg_net_loss":       avg_net_loss,
        "best_trade":         best_trade,
        "worst_trade":        worst_trade,
        "break_even_wr":      break_even,
        "max_consec_loss":    max_consec,
        "best_month":         best_month,
        "worst_month":        worst_month,
        "monthly":            dict(monthly),
        "exit_counts":        dict(exit_counts),
        "sl_count":           len(sl_trades),
        "avg_peak_before_sl": avg_peak_before_sl,
        "tp_hit_before_sl":   tp_hit_before_sl,
        "long":               _dir_stats(longs),
        "short":              _dir_stats(shorts),
        "by_session":         by_session,
        "bars_total":         bars_total,
        "bars_in_session":    bars_in_session,
    }


# ── Parameter sweep ────────────────────────────────────────────────────────────

def run_sweep(df_1d, df_1h, df_fine, df_gbpusd) -> list:
    """Run all 12 parameter combinations. Returns list of (stats, trades) tuples."""
    all_results = []
    combo_num   = 0

    for stop_pts in TS_OPTIONS:
        for lunch in LUNCH_OPTIONS:
            for gbpusd in GBPUSD_OPTIONS:
                combo_num += 1
                label = (
                    f"Nikkei_TS{stop_pts}pt"
                    f"_Lunch{'SKIP' if lunch else 'open'}"
                    f"_GBPUSD{'ON' if gbpusd else 'off'}"
                )
                stats, trades = run_single_backtest(
                    label            = label,
                    df_1d            = df_1d,
                    df_1h            = df_1h,
                    df_fine          = df_fine,
                    df_gbpusd        = df_gbpusd,
                    stop_pts         = stop_pts,
                    lunch_restricted = lunch,
                    gbpusd_filter    = gbpusd,
                )
                stats["combo_num"] = combo_num
                all_results.append((stats, trades))

    return all_results


# ── Output: sweep CSV ──────────────────────────────────────────────────────────

def _save_sweep_csv(all_results: list) -> None:
    path = LOGS_DIR / "backtest_nikkei_sweep.csv"
    fields = [
        "combo", "label", "stop_pts", "lunch_restricted", "gbpusd_filter",
        "total_trades", "trades_per_week", "win_rate_pct",
        "gross_pnl_gbp", "costs_gbp", "net_pnl_gbp",
        "starting_capital", "final_capital", "net_return_pct", "ann_return_pct",
        "max_consec_losses", "period_days",
        "morning_prime_trades", "morning_prime_win_pct",
        "afternoon_trades",     "afternoon_win_pct",
        "lunch_lull_trades",    "lunch_lull_win_pct",
        "long_trades",  "long_win_pct",  "long_net_pnl",
        "short_trades", "short_win_pct", "short_net_pnl",
        "sl_exits", "tp_exits", "session_close_exits", "backtest_end_exits",
        "avg_peak_before_sl_pts",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s, _ in all_results:
            ec  = s["exit_counts"]
            bs  = s["by_session"]
            mp  = bs.get(MORNING, {"count": 0, "win_rate": 0.0})
            af  = bs.get(AFTERNOON,     {"count": 0, "win_rate": 0.0})
            ll  = bs.get(LUNCH_BREAK,    {"count": 0, "win_rate": 0.0})
            w.writerow({
                "combo":                  s["combo_num"],
                "label":                  s["label"],
                "stop_pts":               s["stop_pts"],
                "lunch_restricted":       s["lunch_restricted"],
                "gbpusd_filter":          s["gbpusd_filter"],
                "total_trades":           s["total"],
                "trades_per_week":        f"{s['trades_per_week']:.2f}",
                "win_rate_pct":           f"{s['win_rate']:.1f}",
                "gross_pnl_gbp":          f"{s['gross_pnl']:.2f}",
                "costs_gbp":              f"{s['total_costs']:.3f}",
                "net_pnl_gbp":            f"{s['net_pnl']:.2f}",
                "starting_capital":       f"{s['starting_capital']:.2f}",
                "final_capital":          f"{s['final_capital']:.2f}",
                "net_return_pct":         f"{s['total_return_pct']:.2f}",
                "ann_return_pct":         f"{s['ann_return']:.1f}",
                "max_consec_losses":      s["max_consec_loss"],
                "period_days":            s["period_days"],
                "morning_prime_trades":   mp["count"],
                "morning_prime_win_pct":  f"{mp['win_rate']:.1f}",
                "afternoon_trades":       af["count"],
                "afternoon_win_pct":      f"{af['win_rate']:.1f}",
                "lunch_lull_trades":      ll["count"],
                "lunch_lull_win_pct":     f"{ll['win_rate']:.1f}",
                "long_trades":            s["long"]["count"],
                "long_win_pct":           f"{s['long']['win_rate']:.1f}",
                "long_net_pnl":           f"{s['long']['net_pnl']:.2f}",
                "short_trades":           s["short"]["count"],
                "short_win_pct":          f"{s['short']['win_rate']:.1f}",
                "short_net_pnl":          f"{s['short']['net_pnl']:.2f}",
                "sl_exits":               ec.get("STOP_LOSS",     0),
                "tp_exits":               ec.get("TAKE_PROFIT",   0),
                "session_close_exits":    ec.get("SESSION_CLOSE", 0),
                "backtest_end_exits":     ec.get("BACKTEST_END",  0),
                "avg_peak_before_sl_pts": f"{s['avg_peak_before_sl']:.1f}",
            })
    log.info("Sweep CSV saved -> %s", path)


# ── Output: all-trades CSV ─────────────────────────────────────────────────────

def _save_trades_csv(all_results: list) -> None:
    path = LOGS_DIR / "backtest_nikkei_trades.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "combo", "label", "stop_pts", "lunch_restricted", "gbpusd_filter",
            "trade_no", "direction", "session",
            "entry_time", "exit_time",
            "entry_pts", "exit_pts", "pnl_pts",
            "stake_gbp_per_pt", "gross_pnl_gbp", "cost_gbp", "net_pnl_gbp",
            "peak_pts", "tp_hit_before_sl", "exit_reason",
        ])
        for s, trades in all_results:
            for i, t in enumerate(trades, 1):
                w.writerow([
                    s["combo_num"], s["label"],
                    s["stop_pts"], s["lunch_restricted"], s["gbpusd_filter"],
                    i, t.direction, t.session,
                    t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
                    t.exit_time.strftime("%Y-%m-%d %H:%M")  if t.exit_time  else "",
                    f"{t.entry_price:.1f}",
                    f"{t.exit_price:.1f}"  if t.exit_price is not None else "",
                    f"{t.pnl_pts:.1f}"    if t.pnl_pts    is not None else "",
                    f"{t.stake:.4f}",
                    f"{t.pnl_gbp:.2f}"   if t.pnl_gbp    is not None else "",
                    f"{getattr(t, '_cost_gbp', 0):.4f}",
                    f"{getattr(t, '_net_pnl',  0):.2f}",
                    f"{getattr(t, '_peak_pts', 0):.1f}",
                    "YES" if getattr(t, "_tp_hit", False) else "no",
                    t.exit_reason or "",
                ])
    log.info("Trades CSV saved -> %s", path)


# ── Output: best-config results text file ──────────────────────────────────────

def _save_results_txt(best_stats: dict, best_trades: list,
                       df_fine: pd.DataFrame, resolution: str) -> None:
    path     = LOGS_DIR / "backtest_nikkei_results.txt"
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p_start  = df_fine.index[0].strftime("%Y-%m-%d")
    p_end    = df_fine.index[-1].strftime("%Y-%m-%d")
    s        = best_stats
    ec       = s["exit_counts"]

    lines = [
        "=" * 72,
        "  NikkeiTrader AI -- Japan 225 Spread Betting Backtest",
        f"  Generated  : {now_str}",
        f"  Data       : {resolution}",
        f"  Period     : {p_start} to {p_end}  ({s['period_days']} days)",
        f"  Account    : GBP {STARTING_CAPITAL_GBP:.0f}  |  "
            f"Max risk: GBP {MAX_RISK_PER_TRADE:.0f}/trade  |  "
            f"IG spread: {IG_SPREAD_POINTS:.0f}pt",
        "=" * 72,
        "",
        f"  BEST CONFIGURATION: {s['label']}",
        f"  Trailing stop  : {s['stop_pts']} points",
        f"  Lunch filter   : {'ON -- skip 12:00-13:30' if s['lunch_restricted'] else 'OFF -- trade during lunch'}",
        f"  GBP/USD filter : {'ON -- require 6/6 on context conflict' if s['gbpusd_filter'] else 'OFF -- ignore GBP/USD'}",
        "",
        "-- Account overview ------------------------------------------",
        f"  Starting capital   : GBP {s['starting_capital']:>10.2f}",
        f"  Final capital      : GBP {s['final_capital']:>10.2f}",
        f"  Gross P&L          : GBP {s['gross_pnl']:>+10.2f}",
        f"  Trading costs      : GBP {-s['total_costs']:>+10.2f}",
        f"  Net P&L            : GBP {s['net_pnl']:>+10.2f}",
        f"  Net return         :     {s['total_return_pct']:>+9.2f}%",
        f"  Annualised est.    :     {s['ann_return']:>+9.1f}%",
        "",
        "-- Trade summary ---------------------------------------------",
        f"  Total trades       : {s['total']}",
        f"  Trades per week    : {s['trades_per_week']:.1f}",
        f"  Wins (net)         : {s['wins']}",
        f"  Losses (net)       : {s['losses']}",
        f"  Win rate           : {s['win_rate']:.1f}%",
        f"  Break-even WR      : {s['break_even_wr']:.1f}%",
        f"  Avg net win        : GBP {s['avg_net_win']:>+.2f}",
        f"  Avg net loss       : GBP {s['avg_net_loss']:>+.2f}",
        f"  Best trade (net)   : GBP {s['best_trade']:>+.2f}",
        f"  Worst trade (net)  : GBP {s['worst_trade']:>+.2f}",
        f"  Max consec. losses : {s['max_consec_loss']}",
        "",
        "-- Exit reason breakdown -------------------------------------",
        f"  Stop loss          : {ec.get('STOP_LOSS',     0)}",
        f"  Take profit        : {ec.get('TAKE_PROFIT',   0)}",
        f"  Session close 16:20: {ec.get('SESSION_CLOSE', 0)}",
        f"  End of data        : {ec.get('BACKTEST_END',  0)}",
        "",
        "-- Session phase performance ---------------------------------",
    ]

    for ph, label in [(MORNING, "Morning  8:15-12:00"),
                      (AFTERNOON,     "Afternoon 13:30-15:30"),
                      (LUNCH_BREAK,    "Lunch    12:00-13:30")]:
        bs = s["by_session"].get(ph, {"count": 0, "win_rate": 0.0, "net_pnl": 0.0})
        lines.append(
            f"  {label} : {bs['count']:>3} trades"
            f"  |  {bs['win_rate']:.0f}% WR"
            f"  |  net GBP {bs['net_pnl']:>+.2f}"
        )

    lines += [
        "",
        "-- By direction ----------------------------------------------",
        f"  LONG  : {s['long']['count']} trades"
               f"  |  {s['long']['wins']} wins"
               f"  |  {s['long']['win_rate']:.1f}% WR"
               f"  |  net GBP {s['long']['net_pnl']:>+.2f}",
        f"  SHORT : {s['short']['count']} trades"
               f"  |  {s['short']['wins']} wins"
               f"  |  {s['short']['win_rate']:.1f}% WR"
               f"  |  net GBP {s['short']['net_pnl']:>+.2f}",
        "",
        "-- Monthly breakdown (net P&L) --------------------------------",
        f"  Best month  : {s['best_month'][0]}   GBP {s['best_month'][1]:>+.2f}",
        f"  Worst month : {s['worst_month'][0]}   GBP {s['worst_month'][1]:>+.2f}",
        "",
        "  Month-by-month:",
    ]
    for month in sorted(s["monthly"]):
        lines.append(f"    {month}   GBP {s['monthly'][month]:>+8.2f}")

    lines += [
        "",
        "-- Peak profit analysis (SL trades only) ----------------------",
        f"  Stop-loss trades   : {s['sl_count']}",
        f"  Avg peak before SL : {s['avg_peak_before_sl']:.1f} points",
        f"  TP level hit first : {s['tp_hit_before_sl']} trades"
               f"  (price reached +{TP_POINTS:.0f}pt then reversed to SL)",
        "",
        "-- Trade log --------------------------------------------------",
    ]
    for i, t in enumerate(best_trades, 1):
        ent = t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "--"
        ext = t.exit_time.strftime("%Y-%m-%d %H:%M")  if t.exit_time  else "--"
        tp_flag = " [TP-HIT]" if getattr(t, "_tp_hit", False) else ""
        lines.append(
            f"  #{i:>3}  {t.direction:<5}  {t.session:<14}"
            f"  {ent} - {ext}"
            f"  GBP {getattr(t, '_net_pnl', 0):>+6.2f}"
            f"  {getattr(t, '_peak_pts', 0):>+6.1f}pt pk"
            f"  {t.exit_reason}{tp_flag}"
        )

    lines += ["", "=" * 72]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Best-config results saved -> %s", path)


# ── Terminal: sweep table ──────────────────────────────────────────────────────

def _print_sweep_table(all_results: list, df_fine: pd.DataFrame, resolution: str) -> None:
    p_start = df_fine.index[0].strftime("%Y-%m-%d")
    p_end   = df_fine.index[-1].strftime("%Y-%m-%d")
    p_days  = max((df_fine.index[-1] - df_fine.index[0]).days, 1)

    W   = 11
    sep = "=" * (44 + W * 4)

    print()
    print(sep)
    print("  NIKKEITRADER AI -- Japan 225 SPREAD BETTING  |  PARAMETER SWEEP")
    print(f"  Data    : {resolution}")
    print(f"  Period  : {p_start} to {p_end}  ({p_days} days)")
    print(f"  Account : GBP {STARTING_CAPITAL_GBP:.0f}  |  "
          f"Max risk GBP {MAX_RISK_PER_TRADE:.0f}/trade  |  "
          f"IG spread {IG_SPREAD_POINTS:.0f}pt  |  TP ceiling +{TP_POINTS:.0f}pts")
    print(sep)
    print(f"  {'#':>2}  {'Configuration':<34}  "
          f"{'Trades':>{W}} {'Win%':>{W}} {'Net P&L':>{W+3}} {'Ann%':>{W}}")
    print("  " + "-" * (40 + W * 4))

    for s, _ in all_results:
        cfg = (
            f"TS={s['stop_pts']}pt "
            f"{'Lunch:SKIP' if s['lunch_restricted'] else 'Lunch:open'} "
            f"{'GBPUSD:ON' if s['gbpusd_filter'] else 'GBPUSD:off'}"
        )
        tag = " <-- BEST" if s["combo_num"] == max(
            all_results, key=lambda x: x[0]["net_pnl"]
        )[0]["combo_num"] else ""
        print(
            f"  {s['combo_num']:>2}  {cfg:<34}  "
            f"{s['total']:>{W}}"
            f"  {s['win_rate']:>6.1f}%"
            f"  GBP {s['net_pnl']:>+8.2f}"
            f"  {s['ann_return']:>+7.1f}%{tag}"
        )

    best = max(all_results, key=lambda x: x[0]["net_pnl"])[0]
    print(sep)
    print(f"  BEST: #{best['combo_num']}  {best['label']}")
    print(f"  Net P&L: GBP {best['net_pnl']:>+.2f}  "
          f"({best['total_return_pct']:>+.2f}% over {p_days} days  "
          f"/ {best['ann_return']:>+.1f}% annualised)")
    print(f"  Trades: {best['total']}  |  Win rate: {best['win_rate']:.1f}%  "
          f"|  Trades/week: {best['trades_per_week']:.1f}  "
          f"|  Max consec. losses: {best['max_consec_loss']}")
    print(sep)


# ── Terminal: Nikkei analysis questions ─────────────────────────────────────────

def _print_analysis(all_results: list, df_fine: pd.DataFrame) -> None:
    best_stats = max(all_results, key=lambda x: x[0]["net_pnl"])[0]
    p_days     = best_stats["period_days"]

    # Group by trailing stop size
    by_stop = {
        pts: [(s, t) for s, t in all_results if s["stop_pts"] == pts]
        for pts in TS_OPTIONS
    }
    best_by_stop = {
        pts: max(group, key=lambda x: x[0]["net_pnl"])[0]
        for pts, group in by_stop.items()
    }

    # Lunch filter effect
    with_lunch   = [(s, t) for s, t in all_results if s["lunch_restricted"]]
    no_lunch     = [(s, t) for s, t in all_results if not s["lunch_restricted"]]
    avg_w_lunch  = sum(s["net_pnl"] for s, _ in with_lunch)  / len(with_lunch)
    avg_no_lunch = sum(s["net_pnl"] for s, _ in no_lunch)    / len(no_lunch)

    # GBP/USD filter effect
    with_gbp    = [(s, t) for s, t in all_results if s["gbpusd_filter"]]
    no_gbp      = [(s, t) for s, t in all_results if not s["gbpusd_filter"]]
    avg_w_gbp   = sum(s["net_pnl"] for s, _ in with_gbp)  / len(with_gbp)
    avg_no_gbp  = sum(s["net_pnl"] for s, _ in no_gbp)    / len(no_gbp)

    # Session split on best config
    bs = best_stats["by_session"]
    mp = bs.get(MORNING, {"count": 0, "win_rate": 0.0, "net_pnl": 0.0})
    af = bs.get(AFTERNOON,     {"count": 0, "win_rate": 0.0, "net_pnl": 0.0})
    ll = bs.get(LUNCH_BREAK,    {"count": 0, "win_rate": 0.0, "net_pnl": 0.0})

    best_stop_pts, best_stop_stats = max(best_by_stop.items(),
                                         key=lambda x: x[1]["net_pnl"])

    sep = "=" * 72
    div = "  " + "-" * 60

    print()
    print(sep)
    print("  Nikkei-SPECIFIC ANALYSIS -- 6 KEY QUESTIONS")
    print(sep)

    # Q1
    print()
    print("  Q1: SIGNALS PER WEEK -- Japan 225 vs BTC/GBP vs ETH/GBP")
    print(div)
    print(f"  Japan 225 best config : {best_stats['trades_per_week']:.1f} trades/week"
          f"  ({best_stats['total']} over {p_days} days)")
    print(f"  BTC/GBP STRICT       : {BTC_STRICT['trades_pw']:.1f} trades/week")
    print(f"  ETH/GBP STRICT       : {ETH_STRICT['trades_pw']:.1f} trades/week")
    diff = best_stats["trades_per_week"]
    if diff < BTC_STRICT["trades_pw"]:
        print("  >> Nikkei generates FEWER signals than either crypto.")
        print("     This is expected: Nikkei trades only ~8.25h/day vs crypto 24h.")
        print("     Signal density per trading hour may be comparable.")
    elif diff < ETH_STRICT["trades_pw"]:
        print("  >> Nikkei signal frequency sits between BTC and ETH.")
    else:
        print("  >> Nikkei generates MORE signals than crypto -- note 8.25h day vs 24h.")

    # Q2
    print()
    print("  Q2: SESSION PERFORMANCE -- MORNING vs AFTERNOON")
    print(div)
    print(f"  Morning Prime 8:15-12:00 : {mp['count']:>3} trades"
          f"  |  {mp['win_rate']:.0f}% WR  |  net GBP {mp['net_pnl']:>+.2f}")
    print(f"  Afternoon    13:30-15:30 : {af['count']:>3} trades"
          f"  |  {af['win_rate']:.0f}% WR  |  net GBP {af['net_pnl']:>+.2f}")
    if ll["count"] > 0:
        print(f"  Lunch Lull   12:00-13:30 : {ll['count']:>3} trades"
              f"  |  {ll['win_rate']:.0f}% WR  |  net GBP {ll['net_pnl']:>+.2f}"
              f"  (lunch filter was OFF in best config)")

    better_session = "MORNING PRIME" if mp["net_pnl"] >= af["net_pnl"] else "AFTERNOON"
    print(f"  >> Better session (net P&L): {better_session}")

    lunch_verdict = "HELPFUL" if avg_w_lunch >= avg_no_lunch else "NOT HELPFUL"
    print(f"  >> Lunch lull filter: {lunch_verdict}")
    print(f"     With filter (skip lunch) avg net P&L: GBP {avg_w_lunch:>+.2f}")
    print(f"     Without filter (trade lunch) avg net P&L: GBP {avg_no_lunch:>+.2f}")

    # Q3
    print()
    print("  Q3: GBP/USD CONTEXT FILTER")
    print(div)
    gbp_verdict = "IMPROVES RESULTS" if avg_w_gbp >= avg_no_gbp else "HURTS RESULTS"
    print(f"  >> GBP/USD filter: {gbp_verdict}")
    print(f"     Avg net P&L WITH GBP/USD filter ON  : GBP {avg_w_gbp:>+.2f}")
    print(f"     Avg net P&L WITH GBP/USD filter OFF : GBP {avg_no_gbp:>+.2f}")
    diff_gbp = avg_w_gbp - avg_no_gbp
    print(f"     Difference: GBP {diff_gbp:>+.2f}")
    if abs(diff_gbp) < 2.0:
        print("     Effect is small -- GBP/USD context appears weak on this sample.")

    # Q4
    print()
    print("  Q4: LONG vs SHORT -- DIRECTIONAL BIAS")
    print(div)
    lng = best_stats["long"]
    srt = best_stats["short"]
    print(f"  LONG  : {lng['count']} trades  |  {lng['win_rate']:.0f}% WR"
          f"  |  net GBP {lng['net_pnl']:>+.2f}")
    print(f"  SHORT : {srt['count']} trades  |  {srt['win_rate']:.0f}% WR"
          f"  |  net GBP {srt['net_pnl']:>+.2f}")
    bias = "LONG-biased" if lng["count"] > srt["count"] else "SHORT-biased"
    better_dir = "LONG" if lng["net_pnl"] >= srt["net_pnl"] else "SHORT"
    print(f"  >> Signal generation is {bias} on this period")
    print(f"  >> Better direction by net P&L: {better_dir}")
    wr_diff = abs(lng["win_rate"] - srt["win_rate"])
    if wr_diff >= 10:
        dominant = "LONG" if lng["win_rate"] > srt["win_rate"] else "SHORT"
        print(f"  >> NOTABLE: {dominant} trades have {wr_diff:.0f}pp higher win rate --"
              " consider bias filter in live system")

    # Q5
    print()
    print("  Q5: OPTIMAL TRAILING STOP IN POINTS")
    print(div)
    approx_nikkei = 8000   # approximate Japan 225 level for % context
    for pts in TS_OPTIONS:
        s = best_by_stop[pts]
        pct_of_idx = pts / approx_nikkei * 100
        print(f"  {pts:>2}pt stop: best net GBP {s['net_pnl']:>+.2f}"
              f"  ({s['total']} trades, {s['win_rate']:.0f}% WR)"
              f"  = {pct_of_idx:.3f}% of Nikkei at 8,000")
    print(f"  >> OPTIMAL: {best_stop_pts} points"
          f"  (net GBP {best_stop_stats['net_pnl']:>+.2f}  /"
          f"  {best_stop_pts/approx_nikkei*100:.3f}% of index at 8,000)")

    # Q6
    print()
    print("  Q6: IS THE STRATEGY VIABLE ON Japan 225?")
    print(div)
    viable = (best_stats["net_pnl"] > 0) and (best_stats["win_rate"] >= 40)
    answer = "YES" if viable else ("MARGINAL" if best_stats["net_pnl"] > 0 else "NO")
    print(f"  >> {answer}")
    if answer == "YES":
        print(f"     Net positive: GBP {best_stats['net_pnl']:>+.2f}"
              f"  |  {best_stats['ann_return']:>+.1f}% annualised (est.)")
        print(f"     Win rate: {best_stats['win_rate']:.1f}%"
              f"  |  Max consecutive losses: {best_stats['max_consec_loss']}")
        print(f"     Sample is {p_days} days -- not yet statistically robust.")
        print(f"     Recommend: paper trade 4 weeks, then reassess.")
    elif answer == "MARGINAL":
        print(f"     Profit: GBP {best_stats['net_pnl']:>+.2f} but weak win rate"
              f" ({best_stats['win_rate']:.1f}%)")
        print(f"     Needs further optimisation before live deployment.")
    else:
        print(f"     Strategy not profitable on this Nikkei sample.")
        print(f"     Review entry rules and try 1h timeframe for longer history.")
    print(sep)


# ── Terminal: comparison table ─────────────────────────────────────────────────

def _print_comparison_table(best_stats: dict) -> None:
    W   = 18
    sep = "=" * (40 + W * 3)

    def row(label, btc_v, eth_v, nikkei_v):
        print(f"  {label:<40} {str(btc_v):>{W}} {str(eth_v):>{W}} {str(nikkei_v):>{W}}")

    def div():
        print("  " + "-" * (38 + W * 3 + 2))

    nikkei_ann = best_stats["ann_return"]

    print()
    print(sep)
    print("  PERFORMANCE COMPARISON: BTC/GBP vs ETH/GBP vs Japan 225")
    print("  (all: STRICT mode, best configuration each)")
    print(sep)
    print(f"  {'':40} {'BTC/GBP':>{W}} {'ETH/GBP':>{W}} {'Japan 225':>{W}}")
    div()

    row("Trading method",
        "Crypto spot", "Crypto spot", "Spread bet (IG)")
    row("Tax on profits",
        "CGT liable", "CGT liable", "TAX FREE")
    div()
    row("Trades per week",
        f"{BTC_STRICT['trades_pw']:.1f}",
        f"{ETH_STRICT['trades_pw']:.1f}",
        f"{best_stats['trades_per_week']:.1f}")
    row("Win rate",
        f"{BTC_STRICT['win_rate']:.1f}%",
        f"{ETH_STRICT['win_rate']:.1f}%",
        f"{best_stats['win_rate']:.1f}%")
    row("Max consecutive losses",
        str(BTC_STRICT["max_consec"]),
        str(ETH_STRICT["max_consec"]),
        str(best_stats["max_consec_loss"]))
    div()
    row("Gross annualised return (est.)",
        f"{BTC_STRICT['ann_return']:>+.1f}%",
        f"{ETH_STRICT['ann_return']:>+.1f}%",
        f"{nikkei_ann:>+.1f}%")
    btc_after_tax = BTC_STRICT["ann_return"] * 0.76   # approx 24% CGT
    eth_after_tax = ETH_STRICT["ann_return"] * 0.76
    row("After-tax annualised (est.)",
        f"{btc_after_tax:>+.1f}%",
        f"{eth_after_tax:>+.1f}%",
        f"{nikkei_ann:>+.1f}%  (tax free)")
    div()
    row("Stop distance",
        "2% of price", "2% of price",
        f"{best_stats['stop_pts']:.0f} index points")
    row("Max risk per trade",
        "~2% of pos.", "~2% of pos.",
        f"GBP {MAX_RISK_PER_TRADE:.0f} fixed")
    row("Cost per trade",
        "0.34% (limit)", "0.34% (limit)",
        f"{IG_SPREAD_POINTS:.0f}pt x GBP{MAX_RISK_PER_TRADE/best_stats['stop_pts']:.3f}/pt")
    print(sep)
    print()
    print("  TAX NOTE: UK spread betting profits are exempt from Capital Gains Tax.")
    print(f"  {nikkei_ann:>+.0f}% gross on Nikkei = {nikkei_ann:>+.0f}% kept in full.")
    print(f"  Same gross return on BTC/ETH ~ {nikkei_ann*0.76:>+.0f}% after estimated CGT.")
    if btc_after_tax > nikkei_ann > 0:
        gap = btc_after_tax - nikkei_ann
        print(f"  BTC leads after tax by ~{gap:.0f}pp, but Nikkei is tax-free and lower risk.")
    elif nikkei_ann > btc_after_tax:
        print(f"  Nikkei BEATS crypto on after-tax basis by ~{nikkei_ann - btc_after_tax:.0f}pp.")
    print(sep)


# ── Terminal: honest verdict ───────────────────────────────────────────────────

def _print_verdict(best_stats: dict, all_results: list) -> None:
    sep = "=" * 72
    div = "  " + "-" * 60
    s   = best_stats
    viable = s["net_pnl"] > 0 and s["win_rate"] >= 40

    # Identify which filters actually helped
    # Find same stop_pts config with no filters for baseline comparison
    baseline = next(
        (st for st, _ in all_results
         if st["stop_pts"] == s["stop_pts"]
         and not st["lunch_restricted"]
         and not st["gbpusd_filter"]),
        None
    )
    filter_gain = (s["net_pnl"] - baseline["net_pnl"]) if baseline else 0.0

    print()
    print(sep)
    print("  NIKKEITRADER AI -- HONEST VERDICT")
    print(sep)

    print()
    print("  1. DOES THE STRATEGY WORK ON Japan 225?")
    print(div)
    if viable and s["ann_return"] > 20:
        verdict1 = "YES -- Strategy is profitable on Japan 225."
    elif viable:
        verdict1 = "YES (MODEST) -- Profitable but modest annualised return."
    elif s["net_pnl"] > 0:
        verdict1 = "MARGINAL -- Profitable but win rate is low."
    else:
        verdict1 = "NOT YET -- Net loss on sample period."
    print(f"  >> {verdict1}")

    print()
    print("  2. BEST CONFIGURATION FOUND")
    print(div)
    print(f"  Configuration  : {s['label']}")
    print(f"  Trailing stop  : {s['stop_pts']} points"
          f"  (= {s['stop_pts']/8000*100:.3f}% of Nikkei at 8,000)")
    print(f"  Lunch filter   : {'ON -- skip 12:00-13:30 entries' if s['lunch_restricted'] else 'OFF -- trade through lunch'}")
    print(f"  GBP/USD filter : {'ON -- require 6/6 when GBP context conflicts' if s['gbpusd_filter'] else 'OFF -- ignore GBP/USD direction'}")
    print(f"  Trades/week    : {s['trades_per_week']:.1f}")
    print(f"  Win rate       : {s['win_rate']:.1f}%")
    print(f"  Net P&L        : GBP {s['net_pnl']:>+.2f}  ({s['total_return_pct']:>+.2f}% over {s['period_days']} days)")
    print(f"  Annualised est.: {s['ann_return']:>+.1f}%")

    print()
    print("  3. Nikkei-SPECIFIC FACTORS THAT MOST IMPROVED RESULTS")
    print(div)
    if filter_gain > 0:
        print(f"  >> Session + GBP/USD filters added GBP {filter_gain:>+.2f} vs no-filter baseline")
    print(f"  a) Session-only trading: no overnight exposure to gap risk or news")
    print(f"  b) No-entry zones (PRE_OPEN, CLOSING): removes auction noise")
    print(f"  c) Points-based stop ({s['stop_pts']}pt) suited to Nikkei intraday ATR")
    print(f"  d) Spread bet tax-free status: every GBP earned is fully retained")
    lunch_effect = "Skipping lunch hours reduced false signals" if s["lunch_restricted"] \
                   else "Lunch entries contributed to trade count"
    gbp_effect   = "GBP/USD context filtered out contrary trades" if s["gbpusd_filter"] \
                   else "GBP/USD context had minimal benefit on this sample"
    print(f"  e) {lunch_effect}")
    print(f"  f) {gbp_effect}")

    print()
    print("  4. CONCERNS AND RED FLAGS")
    print(div)
    concerns = []
    if s["period_days"] < 55:
        concerns.append(
            f"SMALL SAMPLE: {s['period_days']} days of 5m data -- "
            f"Yahoo Finance caps at ~60 days. Not statistically robust.")
    if s["total"] < 25:
        concerns.append(
            f"FEW TRADES: {s['total']} total -- need 100+ trades for reliable stats.")
    if s["max_consec_loss"] >= 5:
        concerns.append(
            f"DRAWDOWN RISK: {s['max_consec_loss']} consecutive losses on "
            f"GBP {STARTING_CAPITAL_GBP:.0f} account could feel uncomfortable.")
    lng_wr = s["long"]["win_rate"]
    srt_wr = s["short"]["win_rate"]
    if abs(lng_wr - srt_wr) >= 20:
        concerns.append(
            f"DIRECTIONAL IMBALANCE: LONG {lng_wr:.0f}% vs SHORT {srt_wr:.0f}% -- "
            f"consider disabling the weaker direction in live system.")
    if not concerns:
        concerns.append("No major red flags identified on this sample.")
    for c in concerns:
        print(f"  >> {c}")

    print()
    print("  5. WORTH BUILDING AS NIKKEITRADER AI?")
    print(div)
    if s["net_pnl"] > 0:
        print("  >> YES -- Proceed to live build with these guard rails:")
        print(f"     1. Paper trade for minimum 4 weeks before using real money")
        print(f"     2. Start at minimum stake: GBP {MAX_RISK_PER_TRADE/max(TS_OPTIONS):.2f}/pt"
              f" ({max(TS_OPTIONS)}pt stop)")
        print(f"     3. Track morning vs afternoon win rate separately from day one")
        print(f"     4. Treat first 90 days of live data as extended validation")
        print(f"     5. Spread betting losses are NOT tax deductible -- keep max risk at GBP {MAX_RISK_PER_TRADE:.0f}")
        print(f"     6. IG Markets may widen spread beyond 1pt during news events -- check")
    else:
        print("  >> NOT YET -- Do not build live system until strategy is net positive.")
        print("     Consider:")
        print("     1. Running a 1h-bar backtest using 2 years of data (avoid 60-day cap)")
        print("     2. Reviewing whether Japan 225 5m data from Yahoo Finance is complete")
        print("     3. Adjusting indicator parameters for Nikkei's lower volatility profile")

    print()
    print(sep)
    print("  OUTPUT FILES:")
    print("  logs/backtest_nikkei_results.txt  -- best configuration full detail")
    print("  logs/backtest_nikkei_sweep.csv    -- all 12 combinations")
    print("  logs/backtest_nikkei_trades.csv   -- every trade across all combos")
    print(sep)


# ── S&P 500 correlation note ───────────────────────────────────────────────────

def _print_sp500_note(df_sp500: pd.DataFrame, df_1d: pd.DataFrame) -> None:
    """Quick Nikkei / S&P 500 daily correlation note (context only)."""
    if df_sp500.empty or df_1d.empty:
        return
    try:
        combined = pd.DataFrame({
            "nikkei": df_1d["close"],
            "sp500": df_sp500["close"],
        }).dropna()
        if len(combined) < 10:
            return
        corr = combined["nikkei"].pct_change().corr(combined["sp500"].pct_change())
        print()
        print(f"  S&P 500 context: Japan 225 / S&P 500 daily return correlation = {corr:.2f}")
        if corr > 0.6:
            print(f"  >> Strong positive correlation ({corr:.2f}) -- US session direction"
                  " is meaningful context for Nikkei morning gaps.")
        elif corr > 0.3:
            print(f"  >> Moderate correlation ({corr:.2f}) -- worth adding ES=F daily"
                  " trend as an optional filter in future.")
        else:
            print(f"  >> Weak correlation ({corr:.2f}) -- S&P 500 filter unlikely to help.")
    except Exception:
        pass


# ── Entry point ────────────────────────────────────────────────────────────────

def run_backtest() -> None:
    LOGS_DIR.mkdir(exist_ok=True)

    log.info("NikkeiTrader AI -- Japan 225 Spread Betting Backtest")
    log.info("Account: GBP %.0f  |  Max risk: GBP %.0f/trade  |  IG spread: %.0f pt",
             STARTING_CAPITAL_GBP, MAX_RISK_PER_TRADE, IG_SPREAD_POINTS)
    log.info("Sweep: %d TS options x %d lunch options x %d GBPUSD options = %d combos",
             len(TS_OPTIONS), len(LUNCH_OPTIONS), len(GBPUSD_OPTIONS),
             len(TS_OPTIONS) * len(LUNCH_OPTIONS) * len(GBPUSD_OPTIONS))

    df_1d, df_1h, df_fine, df_gbpusd, df_sp500, resolution = fetch_historical_data()

    log.info("Starting 12-combination parameter sweep...")
    all_results = run_sweep(df_1d, df_1h, df_fine, df_gbpusd)

    best_stats, best_trades = max(all_results, key=lambda x: x[0]["net_pnl"])

    _save_sweep_csv(all_results)
    _save_trades_csv(all_results)
    _save_results_txt(best_stats, best_trades, df_fine, resolution)

    _print_sweep_table(all_results, df_fine, resolution)
    _print_analysis(all_results, df_fine)
    _print_comparison_table(best_stats)
    _print_verdict(best_stats, all_results)
    _print_sp500_note(df_sp500, df_1d)

    log.info("")
    log.info("Backtest complete.")
    log.info("  logs/backtest_nikkei_results.txt")
    log.info("  logs/backtest_nikkei_sweep.csv")
    log.info("  logs/backtest_nikkei_trades.csv")


if __name__ == "__main__":
    run_backtest()
