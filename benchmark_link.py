"""
benchmark_link.py — read the matched Benchmark system's live position state.
Albion Trading Desk — Phantom/Benchmark Link fix (27 Jul 2026).

At each phantom STAY_OUT we record whether the matched benchmark was FLAT (could
have taken the trade -> a FAIR Arthur-vs-benchmark comparison) or already IN a
position (its one-trade guard would have blocked it anyway -> UNFAIR credit). See
the Stage-1 diagnostic: ~30%+ of headline "Net Saved" was structurally unfair.

Design: a short-timeout HTTP GET of the benchmark dashboard's /api/state, which
already exposes in_trade + position. NEVER raises -> any failure returns UNKNOWN,
so this can sit in the live STAY_OUT path without ever stopping a phantom record.
All times UTC. Benchmark port = original port + 20 (NikkeiBench 5028).
"""
import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

# Matched benchmark's state endpoint (localhost; benchmark port = original + 20).
BENCHMARK_STATE_URL = "http://localhost:5028/api/state"
TIMEOUT_SECONDS = 1.5

# Single-instrument system: the benchmark state is a top-level dict (no per-market
# sub-dicts). Left empty; read_availability ignores the market argument.
PER_MARKET_KEYS = {}


def _extract(sub):
    """(state, available) from a benchmark state dict, or ('UNKNOWN', None)."""
    if not isinstance(sub, dict):
        return ("UNKNOWN", None)
    in_trade = sub.get("in_trade")
    if in_trade is None:
        return ("UNKNOWN", None)
    if in_trade:
        pos = sub.get("position") if isinstance(sub.get("position"), dict) else {}
        direction = (pos or {}).get("direction") or "IN_TRADE"
        return (str(direction).upper(), False)
    return ("FLAT", True)


def read_availability(market=None):
    """Return (benchmark_state, benchmark_available) for the matched benchmark.
      benchmark_state:     'FLAT'/'LONG'/'SHORT'/'IN_TRADE'/'UNKNOWN'
      benchmark_available: True (FLAT) / False (in trade) / None (unknown)
    Never raises; on any error returns ('UNKNOWN', None)."""
    try:
        req = urllib.request.Request(BENCHMARK_STATE_URL, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 -- must never break the STAY_OUT path
        logger.debug("benchmark_link: state read failed (%s) -> UNKNOWN", exc)
        return ("UNKNOWN", None)

    key = PER_MARKET_KEYS.get(str(market).upper()) if market else None
    sub = data.get(key) if key else data
    return _extract(sub)
