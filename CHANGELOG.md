# NikkeiTrader A.I. — Changelog

All notable changes to NikkeiTrader (Albion Trading Desk). All times UTC.

## [1.2.0] - 2026-07-24
### Changed — fully bidirectional (remove Morgan SHORT gate) (Cody, Nick's direct order)
- Removed the inline **Morgan SHORT gate** from `main_nikkeitrader.py`: the block that
  required Morgan confidence ≥65 for any `proposed_direction == "SHORT"` (and recorded a
  `MORGAN_SHORT_GATE` phantom on block) is deleted. SHORTs now take the SAME confidence
  bar, pre-checks and sizing as LONGs.
- `proposed_direction` logic already symmetric (daily SSL BULL→LONG, BEAR→SHORT; NEUTRAL
  only on None/NaN daily) — no change needed.
- `agent_brain_nikkei.py`: DIRECTION AWARENESS made direction-neutral; removed the
  "SHORT requires Morgan ≥65" MORGAN SHORT GATE section and replaced with a DIRECTION
  SYMMETRY hard rule (LONG/SHORT assessed on identical terms).
- Three-zone Morgan HARD BLOCK (<30 suspends new entries) left fully intact — separate
  feature, unchanged.

## [1.0.0] - 2026-07-21
### Added — initial build (Cody, commissioned by Nick & Archie)
- New original desk system: **NikkeiTrader**, Japan 225 CFD on Capital.com, port 5008,
  £1,000 paper book. First new original system since the initial desk build. Fills the
  overnight 00:00–06:30 UTC window.
- Cloned from **FTSETrader** (AlbionTraderAI) and adapted for the Nikkei:
  - **Parameters** (Gaius Commission 003): stop 300pt / target 600pt (1:2) / spread 10pt /
    stake £0.10/pt (£30 = 3% risk) / profit ladder £15/£30/£45.
  - **Session** rewritten to pure Tokyo/UTC (no DST): MORNING 00:00–02:29, LUNCH_BREAK
    02:30–03:29, AFTERNOON 03:30–06:19, CLOSING 06:20–06:29, force close 06:20 UTC.
  - **Arthur** prompt: BoJ/JPY macro awareness (weak yen = bullish), overnight-gap
    awareness, correct ~66,000 price context, Tokyo session, no fabricated win-rates.
  - **Guinevere**: Japan/yen directional keyword set + Japan news query.
  - **Calendar**: Japanese national holidays and BoJ policy-meeting days as full-day
    hard blocks (BoJ timing is unpredictable within the short session).
  - **Phantom** verdict threshold 300pt; full 5/10/15/30min/1hr/2hr logging + PHANTOM →
    page (last 50, scrollable) + phantom Archie brief (inherited from the 21 Jul upgrade).
  - Dashboard on 5008 with a deep-red (#C8102E) Japan theme.
- Desktop shortcuts (Start / Dashboard / Watchdog) with browser auto-launch; added to
  START_ALBION.bat as step 9/10 (denominators corrected to 1/10..10/10, closing SNAG 20).
- Confirmed by Nick (21 Jul): epic `J225`, min stake 0.1/pt, BoJ days 30–31 Jul /
  17–18 Sep / 29–30 Oct / 17–18 Dec 2026.
- Backtest-provisional — revisit stop/target/threshold once live phantom + trade data
  accumulate. No NikkeiBenchmark yet (gradual benchmark rollout, separate commission).
