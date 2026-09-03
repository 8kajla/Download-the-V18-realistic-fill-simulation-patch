
# V18 realistic fill simulation integration

This patch is designed for the current public V17 repository. The current repo
does NOT contain `feeds/market_book.py`, so the new simulator is transport
agnostic: wire any existing/live trade-print feed into
`fill_simulator.on_trade_print(token, price, size, ts)`. Do not synthesize fills
from the REST book itself.

## 1. New file

Copy `fill_simulator.py` into the repo root.

## 2. Bot state

In `bot.py`:
- import `FillSimulator`.
- rename the existing resolution dictionary from `pending` to
  `resolution_markets`.
- create `pending_orders.json` through FillSimulator.
- initialize `fill_sim = FillSimulator(DATA / "pending_orders.json", ledger,
  research, fill_callback=...)`.

## 3. Fill callback

The callback should be the only place where `ledger.buy()` is called for the
new execution path:

    def commit_fill(order, fill_price, fill_ts):
        return ledger.buy(
            order["condition"], order["token"], order["market"],
            order["side"], fill_price, order["notional"], fill_ts,
            order["meta"],
        )

This ensures the ledger is untouched at signal time.

## 4. Signal path

Replace the current direct `ledger.buy(...)` section with:
- `target_price = signal["bid"]` (the resting bid proxy currently selected by V17)
- `depth_ahead = signal.get("depth") or 0.0`
- `fill_sim.place(...)`
- record the signal as a candidate, NOT as a trade.
- only after a real fill callback, call `research.record_trade(...)`.

Keep the existing 60-second hard strategy cutoff. The order expiry is
independent and runs at `market["end_ts"]`.

## 5. Resolution state

Do not put a pending order into the resolution-market dictionary. Resolution
tracking should be based on `ledger.positions` only. Once a shadow order fills,
the callback creates the position and the market is then eligible for settlement.

## 6. Trade-print transport

The repo currently uses REST CLOB `/book` polling and has websocket-client in
requirements, but no market WebSocket consumer in the tree. Therefore:
- preferred transport: use the project's existing market WebSocket module if
  you add/restore it;
- fallback: implement a small trade-print poller against a supported public
  trade endpoint and normalize its events to `(token, price, size, ts)`.
Do NOT treat a changing best bid/ask as a trade print.

## 7. Side-by-side instant-fill experiment

For every signal, log both:
- `INSTANT_SIGNAL`: the old theoretical result at signal price;
- `SHADOW_PENDING`: the realistic order state.

Do not add the instant signal to `ledger.positions`. For comparison, create a
separate accumulator or CSV that calculates what the old result would have
been. This lets the live paper ledger remain realistic while preserving the
upper-bound comparison.

## 8. Required metrics

Add durable CSV/JSON metrics:
- signal count
- filled count
- expired-unfilled count
- fill rate
- expiry rate
- average/p50/p90 fill latency
- fill rate and expiry rate by regime and fine band
- theoretical instant-fill notional/P&L versus filled-only P&L
- distribution of entry_count for FILLED only

The simulator already persists order state and computes fill/expiry rates by
regime. Extend the logger using the same pattern for fine bands and P&L.

## 9. Important modeling limitation

`depth_ahead` is a queue-position approximation, not a true queue identifier.
It is the public displayed size captured at placement and should be reported as
such in every research document/result.

## 10. Safety

V17's paper-only lock remains mandatory. This patch does not add any live-order
path.
