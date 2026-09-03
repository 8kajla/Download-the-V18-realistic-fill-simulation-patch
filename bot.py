import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from market_discovery import discover, book, resolve
from paper_ledger import PaperLedger
from research_logger import ResearchLogger
from strategy import CapitalFirstStrategy
from immediate_clob_executor import ImmediateClobExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


def prepare_data_dir():
    data_dir = Path(os.getenv("DATA_DIR", "/app/data")).expanduser()
    fresh = os.getenv("FRESH_START", "false").lower() in ("1", "true", "yes", "on")
    if str(data_dir) in ("/", ".", ""):
        raise RuntimeError(f"Refusing unsafe DATA_DIR={data_dir!r}")
    data_dir.mkdir(parents=True, exist_ok=True)
    if fresh:
        for child in data_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    return data_dir


DATA = prepare_data_dir()
if os.getenv("PAPER_TRADING", "true").lower() != "true":
    raise SystemExit("SAFETY LOCK: PAPER_TRADING must be true")

strategy = CapitalFirstStrategy(
    bankroll=float(os.getenv("STARTING_CAPITAL", "1000")),
    max_total_exposure=float(os.getenv("MAX_TOTAL_EXPOSURE", "300")),
    start_sec=float(os.getenv("START_TRADING_SECOND", "0")),
    stop_sec=float(os.getenv("STOP_TRADING_SECOND", "240")),
    hard_cutoff_seconds=float(os.getenv("HARD_CUTOFF_SECONDS", "60")),
    min_trade_gap_seconds=float(os.getenv("MIN_TRADE_GAP_SECONDS", "0")),
)
ledger = PaperLedger(DATA / "paper_state.json", strategy.bankroll)
ledger.save()
strategy.restore_policy_state(ledger.trades)
research = ResearchLogger(DATA, ledger)

markets = {}
histories = {}
resolution_markets = {}
signal_markets = {}
last_trade = {}
ob_last = {}
last_disc = 0.0
last_report = 0.0
next_trade_at = 0.0
consecutive_errors = 0

BURST_GAP_SECONDS = float(os.getenv("BURST_GAP_SECONDS", "18"))
CADENCE_FALLBACK_SECONDS = max(0.5, float(os.getenv("CADENCE_FALLBACK_SECONDS", "2")))
DISCOVERY_INTERVAL_SECONDS = max(2.0, float(os.getenv("DISCOVERY_INTERVAL_SECONDS", "5")))
BOOK_WORKERS = max(2, int(os.getenv("BOOK_WORKERS", "8")))
LOOP_SECONDS = max(0.05, float(os.getenv("LOOP_SECONDS", "0.25")))
ORDERBOOK_SAMPLE_SECONDS = max(0.1, float(os.getenv("ORDERBOOK_SAMPLE_SECONDS", "1")))

BOOK_EXECUTOR = ThreadPoolExecutor(max_workers=BOOK_WORKERS, thread_name_prefix="v18-book")
executor = ImmediateClobExecutor(
    timeout=float(os.getenv("CLOB_EXECUTION_TIMEOUT_SECONDS", "5")),
    retries=int(os.getenv("CLOB_EXECUTION_RETRIES", "2")),
)


def market_entry_state(condition, now):
    entries = [
        t for t in ledger.trades
        if t.get("action") == "BUY" and t.get("condition") == condition
    ]
    if not entries:
        return {
            "count": 0, "seconds_since_first": 0.0,
            "seconds_since_previous": None, "side": None,
            "price": None, "burst_position": 0,
        }

    ordered = sorted(entries, key=lambda t: float(t.get("ts", now)))
    first = float(ordered[0].get("ts", now))
    prev = float(ordered[-1].get("ts", now))
    gaps = [
        float(c.get("ts", now)) - float(p.get("ts", now))
        for p, c in zip(ordered, ordered[1:])
    ]
    burst = 1
    for gap in reversed(gaps):
        if gap <= BURST_GAP_SECONDS:
            burst += 1
        else:
            break
    latest = ordered[-1]
    return {
        "count": len(ordered),
        "seconds_since_first": max(0.0, now - first),
        "seconds_since_previous": max(0.0, now - prev),
        "side": latest.get("side"),
        "price": latest.get("price"),
        "burst_position": burst,
    }


def prepare_histories(h, now):
    for side in ("Up", "Down"):
        h[side] = [x for x in h.get(side, []) if float(x[0]) >= now - 60.0]


def startup_check():
    required = [
        "decisions.jsonl", "orderbooks.jsonl", "trades.csv",
        "markets.csv", "resolutions.csv", "pnl_1min.csv",
        "paper_state.json",
    ]
    missing = [x for x in required if not (DATA / x).exists()]
    if missing:
        raise RuntimeError(f"DATA STORE INITIALIZATION FAILED: {missing}")


def recover_position_markets():
    recovered = 0
    for position in list(ledger.positions.values()):
        condition = position.get("condition")
        if not condition or condition in resolution_markets:
            continue
        end_ts = position.get("end_ts")
        if end_ts is None:
            continue
        try:
            end_ts = float(end_ts)
        except (TypeError, ValueError):
            continue
        market = {
            "condition": str(condition),
            "id": str(position.get("market_id") or position.get("id") or ""),
            "slug": str(position.get("slug") or ""),
            "asset": str(position.get("asset") or ""),
            "market": str(position.get("market") or position.get("asset") or ""),
            "start_ts": float(position.get("start_ts") or max(0.0, end_ts - 300.0)),
            "end_ts": end_ts,
            "up": str(position.get("up_token") or ""),
            "down": str(position.get("down_token") or ""),
        }
        if market["id"] or market["slug"]:
            resolution_markets[str(condition)] = market
            recovered += 1
    if recovered:
        log.info(
            "STATE RECOVERY | resolution_markets=%d recovered_positions=%d",
            len(resolution_markets), recovered,
        )


def resolve_pending(now):
    conditions = set(resolution_markets) | set(signal_markets)
    for condition in list(conditions):
        market = (
            resolution_markets.get(condition)
            or signal_markets.get(condition)
            or markets.get(condition)
        )
        if not market or now < float(market.get("end_ts", 0)) + 2:
            continue
        try:
            token, outcome, status = resolve(market)
            if token:
                closed = ledger.settle(condition, token) if condition in resolution_markets else []
                pnl = sum(float(x["pnl"]) for x in closed)
                research.record_resolution(
                    ts=now, market=market, winner=outcome or token,
                    winner_token=token, closed=closed,
                )
                research.record_instant_resolution(market, token, now)
                log.info(
                    "RESOLUTION | %s | winner=%s | filled_pnl=%+.4f | closed=%d",
                    market["slug"], outcome or token, pnl, len(closed),
                )
                resolution_markets.pop(condition, None)
                signal_markets.pop(condition, None)
                markets.pop(condition, None)
                histories.pop(condition, None)
            elif status == "CLOSED_UNRESOLVED":
                research.record_resolution_error(
                    ts=now, market=market, status=status
                )
        except Exception as exc:
            research.record_resolution_error(
                ts=now, market=market, status=f"ERROR:{type(exc).__name__}"
            )
            log.warning("RESOLUTION ERROR | %s | %s", market.get("slug"), exc)


def pending_reserved():
    # There are no resting paper orders in this version. A signal either
    # consumes the current CLOB asks immediately or is recorded as unfilled.
    return 0.0


def record_signal(market, token, signal, target, notion, meta, now):
    order = {
        "order_id": f"instant-{market['condition']}-{now:.6f}",
        "condition": market["condition"],
        "token": token,
        "market": market["market"],
        "side": signal["side"],
        "target_price": target,
        "notional": notion,
        "placed_ts": now,
        "meta": meta,
    }
    research.record_instant_signal(order)
    return order


def record_filled_trade(market, token, signal, result, meta, now, state):
    # One ledger BUY at the VWAP represents the aggregate ask walk.
    trade = ledger.buy(
        market["condition"],
        token,
        market["market"],
        signal["side"],
        float(result["vwap"]),
        float(result["executed_notional"]),
        now,
        meta,
    )
    resolution_markets[market["condition"]] = market
    last_trade[market["condition"]] = now

    research.record_trade(
        trade=trade,
        market=market,
        elapsed=now - float(market.get("start_ts", now)),
        left=max(0.0, float(market.get("end_ts", now)) - now),
        entry_count_before=state["count"],
        burst_position=state["burst_position"],
        seconds_since_previous=state["seconds_since_previous"],
        up_bid=meta.get("up_bid"),
        up_ask=meta.get("up_ask"),
        up_depth=meta.get("up_depth"),
        down_bid=meta.get("down_bid"),
        down_ask=meta.get("down_ask"),
        down_depth=meta.get("down_depth"),
        score=meta.get("trajectory_likelihood"),
        momentum=meta.get("movement"),
        cash_after=ledger.cash,
        exposure_after=ledger.exposure(market["condition"]),
        fine_band=meta.get("fine_band"),
    )
    ledger.save()
    return trade


recover_position_markets()


def report(books):
    global last_report
    now = time.time()
    interval = float(os.getenv("REPORT_INTERVAL_SECONDS", "60"))
    if now - last_report < interval:
        return
    last_report = now
    metrics = ledger.mark(books)
    metrics["positions"] = len(ledger.positions)
    research.record_pnl(now, metrics)
    es = executor.stats()

    log.info(
        "P&L $%+.2f | cash=$%.2f | open=$%.2f | positions=%d | "
        "signals=%d filled=%d fill_rate=%.1f%% unfilled=%d book_errors=%d",
        metrics["pnl"], metrics["cash"], metrics["open_cost"],
        metrics["positions"], es["signals"], es["filled"],
        100.0 * es["fill_rate"], es["unfilled"], es["book_errors"],
    )

    snap = strategy.distribution_snapshot()
    for band, actual in snap["trade"].items():
        target = strategy.scheduler.trade_targets.get(band, 0.0)
        cap_actual = snap["capital"].get(band, 0.0)
        cap_target = strategy.scheduler.capital_targets.get(band, 0.0)
        log.info(
            "BAND | %s | trades=%.4f target=%.4f | capital=%.4f target=%.4f",
            band, actual, target, cap_actual, cap_target,
        )


def main():
    global last_disc, next_trade_at, consecutive_errors

    startup_check()
    log.info(
        "BOT B | PAPER ONLY | IMMEDIATE CLOB TAKER SIMULATION | "
        "V17 FULL-SCALE TRADER REPLICA 40PCT | STRATEGY UNCHANGED"
    )

    while True:
        try:
            now = time.time()

            if now - last_disc >= DISCOVERY_INTERVAL_SECONDS:
                for m in discover():
                    markets[m["condition"]] = m

                for condition, m in list(markets.items()):
                    if any(
                        p.get("condition") == condition
                        for p in ledger.positions.values()
                    ):
                        resolution_markets[condition] = m

                    if (
                        m.get("end_ts", 0) < now - 30
                        and condition not in resolution_markets
                        and condition not in signal_markets
                    ):
                        markets.pop(condition, None)

                last_disc = now
                log.info(
                    "MARKETS | active=%d resolution=%d",
                    len(markets), len(resolution_markets),
                )

            resolve_pending(now)

            books = {}
            if now >= next_trade_at:
                eligible = []
                scannable = []

                for m in list(markets.values()):
                    elapsed = now - float(m["start_ts"])
                    left = float(m["end_ts"]) - now
                    if (
                        left <= 0
                        or elapsed < 0
                        or elapsed > 300
                        or not m.get("accepting_orders")
                    ):
                        continue
                    scannable.append((m, elapsed, left))

                futures = {
                    BOOK_EXECUTOR.submit(book, token): (m, tname)
                    for m, _, _ in scannable
                    for tname, token in (("up", m["up"]), ("down", m["down"]))
                    if token
                }
                mb = {}
                for future in as_completed(futures):
                    m, tname = futures[future]
                    try:
                        mb.setdefault(m["condition"], {})[tname] = future.result()
                    except Exception as exc:
                        log.warning("BOOK ERROR | %s | %s", m.get("slug"), exc)

                for m, elapsed, left in scannable:
                    pair = mb.get(m["condition"], {})
                    if "up" not in pair or "down" not in pair:
                        continue

                    ub, ua, ud, uad = pair["up"]
                    db, da, dd, dad = pair["down"]
                    if m.get("up"):
                        books[m["up"]] = ub
                    if m.get("down"):
                        books[m["down"]] = db

                    h = histories.setdefault(
                        m["condition"], {"Up": [], "Down": []}
                    )
                    if ub is not None:
                        h["Up"].append((now, ub))
                    if db is not None:
                        h["Down"].append((now, db))
                    prepare_histories(h, now)

                    if now - ob_last.get(m["condition"], 0) >= ORDERBOOK_SAMPLE_SECONDS:
                        research.record_orderbook(
                            ts=now, market=m, elapsed=elapsed, left=left,
                            up_bid=ub, up_ask=ua, up_depth=ud,
                            down_bid=db, down_ask=da, down_depth=dd,
                            up_ask_depth=uad, down_ask_depth=dad,
                        )
                        ob_last[m["condition"]] = now

                    state = market_entry_state(m["condition"], now)
                    candidates = strategy.build_candidates_for_market(
                        elapsed, ua, da, ub, db, h["Up"], h["Down"], now,
                        asset=m["asset"], market=m["asset"],
                        thesis_side=state["side"],
                        market_entry_count=state["count"],
                        seconds_since_first_entry=state["seconds_since_first"],
                        up_depth=ud, down_depth=dd,
                    )
                    for candidate in candidates:
                        candidate.update(
                            _market=m, _state=state, _elapsed=elapsed,
                            _left=left,
                        )
                        # A strategy candidate is only immediately executable
                        # if its current ask remains inside the same fine band.
                        if candidate.get("ask") is not None:
                            ask_band, _ = strategy.fine_band(float(candidate["ask"]))
                            candidate["_clob_executable"] = (
                                ask_band == candidate["band"]
                            )
                        else:
                            candidate["_clob_executable"] = False
                        eligible.append(candidate)

                if eligible:
                    # Preserve the trader's exact fine-band scheduler. When
                    # multiple markets exist in the chosen band, prefer a
                    # candidate that can actually be taken now.
                    target_band = strategy.choose_distribution_band(eligible)
                    if (
                        target_band is None
                        and now - next_trade_at >= CADENCE_FALLBACK_SECONDS
                    ):
                        target_band = strategy.scheduler.choose_band(
                            eligible, allow_over_quota=True
                        )

                    band_candidates = [
                        c for c in eligible if c["band"] == target_band
                    ]
                    executable_candidates = [
                        c for c in band_candidates if c["_clob_executable"]
                    ]
                    if executable_candidates:
                        band_candidates = executable_candidates

                    if band_candidates:
                        best = max(
                            band_candidates,
                            key=lambda c: (
                                c["trajectory_likelihood"],
                                c["band_prior"],
                                -c["bid"],
                            ),
                        )
                        market = best["_market"]
                        state = best["_state"]
                        signal = strategy.choose_process_candidate(
                            [best], target_band
                        )

                        if signal:
                            reserved = pending_reserved()
                            remaining = max(
                                0.0,
                                strategy.max_total_exposure
                                - ledger.total_open_cost()
                                - reserved,
                            )
                            available = max(0.0, ledger.cash - reserved)
                            notion = min(
                                float(signal["target"]), available, remaining
                            )

                            if (
                                notion >= float(
                                    os.getenv("MIN_PAPER_FILL_USD", "0.10")
                                )
                                and best["_left"] > strategy.hard_cutoff_seconds
                            ):
                                token = (
                                    market["up"]
                                    if signal["side"] == "Up"
                                    else market["down"]
                                )
                                band, regime = strategy.fine_band(signal["bid"])
                                target = float(signal["bid"])

                                meta = {
                                    "slug": market["slug"],
                                    "asset": market["asset"],
                                    "market": market["market"],
                                    "start_ts": market["start_ts"],
                                    "end_ts": market["end_ts"],
                                    "market_id": market["id"],
                                    "up_token": market["up"],
                                    "down_token": market["down"],
                                    "model_version": strategy.VERSION,
                                    "entry_count_before": state["count"],
                                    "burst_position": state["burst_position"],
                                    "seconds_since_first_entry": state["seconds_since_first"],
                                    "seconds_since_previous_trade": state["seconds_since_previous"],
                                    "regime": regime,
                                    "fine_band": band,
                                    "execution_mode": "IMMEDIATE_CLOB_TAKER",
                                    "target_capital": target,
                                    "strategy_reference_bid": target,
                                    "strategy_reference_ask": best.get("ask"),
                                    "bid_size": signal.get("depth") or 0.0,
                                    "up_bid": best.get("bid") if best.get("side") == "Up" else None,
                                    "up_ask": best.get("ask") if best.get("side") == "Up" else None,
                                    "up_depth": best.get("depth") if best.get("side") == "Up" else None,
                                    "down_bid": best.get("bid") if best.get("side") == "Down" else None,
                                    "down_ask": best.get("ask") if best.get("side") == "Down" else None,
                                    "down_depth": best.get("depth") if best.get("side") == "Down" else None,
                                    "trajectory_likelihood": signal["trajectory_likelihood"],
                                    "movement": signal.get("movement"),
                                }

                                order = record_signal(
                                    market, token, signal, target, notion, meta, now
                                )

                                result = executor.execute(
                                    token=token,
                                    notional=notion,
                                    strategy_band=band,
                                    signal_bid=target,
                                    strategy=strategy,
                                )

                                next_trade_at = now + max(
                                    0.0, strategy.cadence.sample_gap()
                                )

                                if result["filled"]:
                                    executed = float(result["executed_notional"])
                                    meta.update(
                                        {
                                            "execution_price": float(result["vwap"]),
                                            "execution_vwap": float(result["vwap"]),
                                            "execution_slippage": float(result["slippage"]),
                                            "execution_levels": result["levels"],
                                            "execution_book_ts": result.get("book_ts"),
                                            "execution_book_levels": result.get("book_levels_seen"),
                                        }
                                    )
                                    trade = record_filled_trade(
                                        market, token, signal, result, meta, now, state
                                    )
                                    strategy.observe_trade_distribution(band, executed)
                                    order.update(
                                        {
                                            "fill_ts": now,
                                            "fill_price": float(result["vwap"]),
                                            "fill_latency_s": 0.0,
                                        }
                                    )
                                    research.record_execution_comparison_fill(
                                        order, trade
                                    )
                                    log.info(
                                        "CLOB FILL | %s | %s | band=%s | ref=%.4f "
                                        "VWAP=%.4f | slippage=%+.4f | notional=$%.2f | levels=%d",
                                        market["asset"], signal["side"], band, target,
                                        result["vwap"], result["slippage"], executed,
                                        len(result["levels"]),
                                    )
                                else:
                                    log.info(
                                        "CLOB UNFILLED | %s | %s | band=%s | "
                                        "ref=%.4f | notional=$%.2f | reason=%s",
                                        market["asset"], signal["side"], band,
                                        target, notion, result["reason"],
                                    )

                                signal_markets[market["condition"]] = market

            report(books)
            time.sleep(LOOP_SECONDS)
            consecutive_errors = 0

        except KeyboardInterrupt:
            BOOK_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception as exc:
            consecutive_errors += 1
            log.exception("LOOP ERROR #%d | %s", consecutive_errors, exc)
            time.sleep(min(10.0, LOOP_SECONDS * max(2, consecutive_errors)))


if __name__ == "__main__":
    main()
