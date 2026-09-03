# Polymarket V18 — Railway-ready paper/shadow bot

This repository is a clean V18 deployment package. **There is no V17 folder inside it.**

## What changed
- Strategy signals create durable shadow `PENDING` orders instead of instant ledger fills.
- Public Polymarket CLOB `last_trade_price` WebSocket events drive simulated fills.
- Queue position is approximated from displayed bid depth captured at signal time.
- Only a simulated fill mutates `paper_state.json`.
- Pending orders expire at market end as `EXPIRED_UNFILLED`.
- Fill price is the observed trade-print price, not the requested target.
- Fill latency, fill rate, unfilled rate and regime/band data are logged.
- Instant-fill shadow records are retained for upper-bound comparison.
- Pending orders survive process restarts when `/app/data` is persistent.

## Railway
1. Create a new GitHub repo from this package and connect it to Railway.
2. Set the variables from `railway.env.example`.
3. Keep `PAPER_TRADING=true`. This build does not submit live CLOB orders.
4. For restart durability, mount a Railway persistent volume at `/app/data`.
5. Do not set `FRESH_START=true` for a continuing run; it intentionally wipes the data directory.

## Important limitation
The queue model is explicitly approximate. Public trade prints cannot reveal our exact queue position, and this simulator does not model market impact, settlement latency, or other live execution effects.
