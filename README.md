# NikkeiTrader A.I.

Part of the **Albion Trading Desk**. NikkeiTrader trades the **Japan 225 (Nikkei 225)**
index via CFD on Capital.com, filling the overnight gap (00:00–06:30 UTC) when no other
original desk system is active. Paper trading only, £1,000 book.

- **Port:** 5008 · **Instrument:** Japan 225 (Capital.com epic `J225`) · **Balance:** £1,000
- **Template:** FTSETrader (AlbionTraderAI) · **Direction:** BIDIRECTIONAL · **Session:** Tokyo cash, all times UTC
- **Commissioned:** Gaius Commission 002 (19 Jul 2026) + Commission 003 backtest (21 Jul 2026)

## Parameters (Gaius Commission 003, backtest-provisional)
- **Stop:** 300 points · **Target:** 600 points (1:2 R:R) · **Spread:** 10 points
- **Stake:** £0.10 per point (£30 = 3% risk on the 300pt stop) · **Min stake:** 0.1/pt (confirmed)
- **Morgan SHORT gate:** ≥ 65 · **Profit Protection Ladder:** £15/£30/£45 (150/300/450pt)

## Session (all UTC — Tokyo cash session, no DST)
| Phase | Window | Trading |
|---|---|---|
| PRE_OPEN | 23:00–23:59 | no entries |
| MORNING | 00:00–02:29 | **full — Tokyo open, highest liquidity/range** |
| LUNCH_BREAK | 02:30–03:29 | blocked (Tokyo lunch recess) |
| AFTERNOON | 03:30–06:19 | full |
| CLOSING | 06:20–06:29 | no entries; force close 06:20 UTC |
| CLOSED | 06:30–22:59, weekends, JP holidays | — |

The edge is front-loaded to the Tokyo open (00:00–02:30 UTC). Force close before the
06:30 Tokyo cash close; never hold overnight.

## Arthurian stack
Arthur (Claude, Nikkei/BoJ/JPY-aware, overnight-gap-aware) · Merlin (Japan 225 feed,
Capital.com + `^N225` yfinance fallback) · Lancelot (pre-checks, session + Morgan SHORT
gate) · Excalibur (Capital.com connector) · Guinevere (Japan/yen news — a weaker yen is
**bullish** for the Nikkei) · Morgan (confidence, starts 50) · Stanley (paper trader) ·
Galahad (watchdog) · Percival (Pushover). Full phantom logging (17-col snapshot +
5/10/15/30min/1hr/2hr moves), PHANTOM → page (last 50, scrollable), phantom Archie brief.

## Running
```
python dashboard_nikkei.py     # port 5008
python watchdog_nikkei.py      # supervises main_nikkeitrader.py
```
Or the desktop shortcut **Start NikkeiTrader** (dashboard + watchdog + browser).

## Go-live checklist (paper is fine now)
- Capital.com epic `J225` — confirmed.
- Minimum stake 0.1/pt — confirmed.
- BoJ hard-block days (full-day): 30–31 Jul, 17–18 Sep, 29–30 Oct, 17–18 Dec 2026 — wired.
- Japanese holidays (exchange closed): 11 Aug, 21–23 Sep, 12 Oct, 3 Nov, 23 Nov 2026 — wired.

All times UTC.
