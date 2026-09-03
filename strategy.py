from __future__ import annotations
from dataclasses import dataclass
import json, time, random, math, os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

BANDS: Tuple[Tuple[str, float, float, str], ...] = (
    ("C00_05", 0.00, 0.05, "CHEAP"),
    ("C05_10", 0.05, 0.10, "CHEAP"),
    ("C10_15", 0.10, 0.15, "CHEAP"),
    ("C15_20", 0.15, 0.20, "CHEAP"),
    ("C20_30", 0.20, 0.30, "CHEAP"),
    ("M30_40", 0.30, 0.40, "MID"),
    ("M40_50", 0.40, 0.50, "MID"),
    ("M50_60", 0.50, 0.60, "MID"),
    ("M60_70", 0.60, 0.70, "MID"),
    ("R70_80", 0.70, 0.80, "CORE"),
    ("R80_90", 0.80, 0.90, "CORE"),
    ("H90_95", 0.90, 0.95, "HIGH"),
    ("H95_100", 0.95, 1.00, "HIGH"),
)

# Full-scale trader-history trajectory shares: 778,116 trades / 76,154 markets.
TRAJECTORY_SHARE = {
    "CHEAP": {"rising": 0.137582, "falling": 0.539948, "flat": 0.322470},
    "MID":   {"rising": 0.341446, "falling": 0.422447, "flat": 0.236107},
    "CORE":  {"rising": 0.499717, "falling": 0.280757, "flat": 0.219526},
    "HIGH":  {"rising": 0.586489, "falling": 0.133042, "flat": 0.280469},
}
TRAJECTORY_THRESHOLD = 0.005

BAND_INDEX = {band: i for i, (band, *_rest) in enumerate(BANDS)}


@dataclass
class Signal:
    side: str
    price: float
    score: float
    notional: float
    reason: str


class EmpiricalTraderProcess:
    """
    Observable trader-process model.

    Uses measured distributions for:
      * global intertrade cadence
      * fine price-band frequency
      * same-side continuation
      * fine-band x entry-number notional

    It deliberately does NOT claim to know the trader's hidden trigger.
    """

    PERSISTENCE = 0.8805590616

    def __init__(self, behavior: dict, seed: int = 20260831):
        self.rng = random.Random(seed)

        gap_rows = behavior.get("intertrade_gap_histogram_seconds") or []
        self.gaps = [float(x["gap_seconds"]) for x in gap_rows]
        self.gap_weights = [float(x["count"]) for x in gap_rows]

        band_rows = behavior.get("fine_bands") or []
        self.band_values = [str(x["fine_band"]) for x in band_rows]
        self.band_weights = [float(x["trade_share"]) for x in band_rows]

        if not self.gaps or not any(self.gap_weights):
            raise ValueError("trader_behavior.json missing intertrade gap distribution")
        if not self.band_values or not any(self.band_weights):
            raise ValueError("trader_behavior.json missing fine-band distribution")

    def sample_gap(self) -> float:
        return float(self.rng.choices(self.gaps, weights=self.gap_weights, k=1)[0])

    def sample_target_band(self) -> str:
        return str(self.rng.choices(self.band_values, weights=self.band_weights, k=1)[0])

    def should_continue_side(self) -> bool:
        return self.rng.random() < self.PERSISTENCE

    @staticmethod
    def distance_to_band(actual_band: str, target_band: str) -> int:
        return abs(BAND_INDEX.get(actual_band, 999) - BAND_INDEX.get(target_band, 999))



class TraderPolicyScheduler:
    """Stateful scheduler for the trader's observed fine-band policy.

    The scheduler controls the *distribution of accepted entries*, not the
    hidden trigger.  It uses the trader's measured fine-band trade shares,
    while the empirical entry-size table controls dollars per entry.
    """

    def __init__(self, behavior, seed=20260831):
        self.rng = random.Random(seed)
        raw_trade_targets = {
            str(x["fine_band"]): float(x["trade_share"])
            for x in behavior.get("fine_bands", [])
        }
        raw_regime = {
            "CHEAP": sum(v for b, v in raw_trade_targets.items() if b.startswith("C")),
            "MID": sum(v for b, v in raw_trade_targets.items() if b.startswith("M")),
            "CORE": sum(v for b, v in raw_trade_targets.items() if b.startswith("R")),
            "HIGH": sum(v for b, v in raw_trade_targets.items() if b.startswith("H")),
        }
        # Current bot trades BTC/ETH/SOL/BNB. The full trader-history benchmark
        # for that four-asset universe is 48.4/30.6/12.2/8.8 by regime.
        six_asset_mode = os.getenv("V17_SIX_ASSET_MODE", "false").lower() in (
            "1", "true", "yes", "on"
        )
        target_regime = (
            {"CHEAP": 0.593, "MID": 0.242, "CORE": 0.093, "HIGH": 0.072}
            if six_asset_mode else
            {"CHEAP": 0.484, "MID": 0.306, "CORE": 0.122, "HIGH": 0.088}
        )
        self.benchmark_name = "six_asset" if six_asset_mode else "four_asset"
        self.trade_targets = {}
        for band, share in raw_trade_targets.items():
            regime = ("CHEAP" if band.startswith("C") else
                      "MID" if band.startswith("M") else
                      "CORE" if band.startswith("R") else "HIGH")
            self.trade_targets[band] = share / raw_regime[regime] * target_regime[regime]

        self.capital_targets = {
            str(x["fine_band"]): float(x["notional_share"])
            for x in behavior.get("fine_bands", [])
        }
        self.capital_targets = {
            str(x["fine_band"]): float(x["notional_share"])
            for x in behavior.get("fine_bands", [])
        }
        self.bands = list(self.trade_targets)
        if not self.bands or set(self.bands) != set(self.capital_targets):
            raise ValueError("invalid trader fine-band distribution")
        self.trade_counts = {b: 0 for b in self.bands}
        self.capital = {b: 0.0 for b in self.bands}

    def observe(self, band, notional):
        band = str(band)
        if band not in self.trade_counts:
            raise ValueError(f"unknown fine band: {band}")
        self.trade_counts[band] += 1
        self.capital[band] += max(0.0, float(notional))

    def restore(self, trades, fine_band_fn):
        """Rebuild distribution state from durable ledger trades."""
        self.trade_counts = {b: 0 for b in self.bands}
        self.capital = {b: 0.0 for b in self.bands}
        for trade in trades or []:
            if trade.get("action") != "BUY":
                continue
            try:
                band, _ = fine_band_fn(float(trade.get("price")))
                cost = float(trade.get("cost", 0.0))
            except (TypeError, ValueError):
                continue
            if band in self.trade_counts:
                self.trade_counts[band] += 1
                self.capital[band] += max(0.0, cost)

    def _shares_after(self, band, notional):
        total_trades = sum(self.trade_counts.values()) + 1
        total_capital = sum(self.capital.values()) + max(0.0, float(notional))
        trades = {}
        capital = {}
        for b in self.bands:
            trades[b] = (self.trade_counts[b] + (1 if b == band else 0)) / total_trades
            capital[b] = (
                self.capital[b] + (float(notional) if b == band else 0.0)
            ) / total_capital if total_capital > 0 else 0.0
        return trades, capital

    def projected_state(self, band, notional):
        return self._shares_after(band, notional)

    def band_is_within_trade_quota(self, band):
        """Hard cumulative quota: a band cannot run ahead of its trader share.

        For N observed trades, the next accepted trade in a band is allowed
        only when the resulting count is <= ceil((N+1)*target). This prevents
        one continuously available band (the M50-60 failure seen in V16) from
        consuming the stream simply because it happens to be liquid.
        """
        total = sum(self.trade_counts.values())
        target = self.trade_targets.get(band, 0.0)
        allowed = int(math.ceil((total + 1) * target))
        return self.trade_counts.get(band, 0) + 1 <= max(1, allowed)

    def projected_score(self, band, notional):
        trades, capital = self.projected_state(band, notional)
        score = 0.0
        for b in self.bands:
            # Positive values mean the band is still under target; negative
            # values mean it is already over target. Weight trade count more
            # heavily because count is the exact observable quota we can
            # enforce without inventing execution availability.
            t_gap = (self.trade_targets[b] - trades[b]) / max(self.trade_targets[b], 1e-9)
            c_gap = (self.capital_targets[b] - capital[b]) / max(self.capital_targets[b], 1e-9)
            score += 2.0 * t_gap + c_gap
        return score

    def choose_band(self, candidates, allow_over_quota=False):
        if not candidates:
            return None
        by_band = {}
        for c in candidates:
            by_band.setdefault(c["band"], []).append(c)

        # Normal path: enforce the trader's exact cumulative count quota.
        # This preserves the V16/V17 distribution behavior byte-for-byte in
        # the default case.
        quota_eligible = {
            band: rows for band, rows in by_band.items()
            if self.band_is_within_trade_quota(band)
        }
        if quota_eligible:
            scored = []
            for band, rows in quota_eligible.items():
                min_error = min(
                    self.projected_score(band, float(c["target"]))
                    for c in rows
                )
                best_row = min(
                    rows,
                    key=lambda c: self.projected_score(band, float(c["target"]))
                )
                trades, capital = self.projected_state(
                    band, float(best_row["target"])
                )
                t_deficit = (
                    self.trade_targets[band] - trades[band]
                ) / max(self.trade_targets[band], 1e-9)
                c_deficit = (
                    self.capital_targets[band] - capital[band]
                ) / max(self.capital_targets[band], 1e-9)
                scored.append((-2.0 * t_deficit - c_deficit, min_error, band))

            scored.sort()
            return scored[0][2]

        # Emergency availability path used only by the execution loop after a
        # cadence tick has remained due for several seconds. The trader's
        # distribution is still the objective, but refusing every available
        # band can create artificial multi-minute gaps that are not present in
        # the observed intertrade process. Prefer the least over-target band
        # and use projected trade/capital error as a tie-breaker.
        if not allow_over_quota:
            return None

        total = sum(self.trade_counts.values())
        emergency = []
        for band, rows in by_band.items():
            best_row = min(
                rows,
                key=lambda c: self.projected_score(band, float(c["target"]))
            )
            trades, capital = self.projected_state(
                band, float(best_row["target"])
            )
            target = max(self.trade_targets.get(band, 0.0), 1e-9)
            current_share = self.trade_counts.get(band, 0) / max(total, 1)
            projected_share = trades.get(band, 0.0)
            over_now = max(0.0, current_share - self.trade_targets.get(band, 0.0))
            over_after = max(0.0, projected_share - self.trade_targets.get(band, 0.0))
            # Large penalty for quota overshoot, but still allow the trade when
            # no quota-eligible band is currently available.
            overshoot_penalty = (
                25.0 * over_now / target +
                50.0 * over_after / target
            )
            t_deficit = (
                self.trade_targets[band] - projected_share
            ) / target
            c_deficit = (
                self.capital_targets[band] - capital.get(band, 0.0)
            ) / max(self.capital_targets[band], 1e-9)
            score = overshoot_penalty - 2.0 * t_deficit - c_deficit
            emergency.append((score, self.distance_hint(band), band))

        emergency.sort()
        return emergency[0][2]

    @staticmethod
    def distance_hint(band):
        # Stable deterministic tie-breaker matching the fixed band order.
        return BAND_INDEX.get(band, 999)

    def shares(self):
        total_t = sum(self.trade_counts.values())
        total_c = sum(self.capital.values())
        return {
            "trade": {
                b: self.trade_counts[b] / total_t if total_t else 0.0
                for b in self.bands
            },
            "capital": {
                b: self.capital[b] / total_c if total_c else 0.0
                for b in self.bands
            },
        }

    def target_report(self):
        actual = self.shares()
        return {
            b: {
                "target_trade_share": self.trade_targets[b],
                "actual_trade_share": actual["trade"][b],
                "target_capital_share": self.capital_targets[b],
                "actual_capital_share": actual["capital"][b],
            }
            for b in self.bands
        }


class CapitalFirstStrategy:
    VERSION = "V17_FULL_SCALE_TRADER_REPLICA_40PCT"
    DATA_FILE = Path(__file__).with_name("trader_behavior.json")
    BANDS = BANDS
    HARD_CUTOFF = 60.0

    # Full-scale entry-count sizing direction from the 778,116-trade analysis.
    # Ratios are applied to each fine band's first-entry empirical median so
    # price-band sizing is retained while entry-count direction is corrected.
    ENTRY_POSITION_RATIOS = {
        "CHEAP": {"first": 1.0, "2nd-3rd": 0.7241379, "4th+": 0.3793103},
        "MID":   {"first": 1.0, "2nd-3rd": 1.0, "4th+": 0.9405941},
        "CORE":  {"first": 1.0, "2nd-3rd": 0.9051282, "4th+": 0.5641026},
        "HIGH":  {"first": 1.0, "2nd-3rd": 1.0810811, "4th+": 0.6752252},
    }

    def __init__(
        self,
        bankroll=1000,
        start_sec=0,
        stop_sec=240,
        hard_cutoff_seconds=60,
        max_total_exposure=300,
        min_trade_gap_seconds=0,
        behavior_file=None,
        seed=20260831,
        **_,
    ):
        self.bankroll = float(bankroll)
        self.start_sec = max(0.0, float(start_sec))
        self.stop_sec = min(300.0, float(stop_sec))
        self.hard_cutoff_seconds = max(60.0, float(hard_cutoff_seconds))
        self.max_total_exposure = max(0.0, float(max_total_exposure))
        self.min_trade_gap_seconds = max(0.0, float(min_trade_gap_seconds))
        self._last_trade_at: Optional[float] = None

        path = Path(behavior_file) if behavior_file else self.DATA_FILE
        with path.open(encoding="utf-8") as f:
            self.behavior = json.load(f)

        self.notional_scale = float(self.behavior.get("notional_scale", 0.4))
        self.process = EmpiricalTraderProcess(self.behavior, seed=seed)
        self.cadence = self.process
        self.scheduler = TraderPolicyScheduler(self.behavior, seed=seed)
        self.fine_band_trade_share = {
            str(x["fine_band"]): float(x["trade_share"])
            for x in self.behavior.get("fine_bands", [])
        }
        self.entry_medians = self.behavior["entry_median_by_fine_band"]
        self.band_size_multiplier = self._derive_band_size_multipliers()

    def _derive_band_size_multipliers(self):
        """Calibrate each fine-band size curve to the trader's observed
        aggregate dollars while preserving the configured notional scale.

        Entry-number medians describe the shape of sizing, while the raw
        band totals provide the trustworthy aggregate dollar target.  The
        multiplier bridges those two measurements without inventing a new
        cross-band sizing rule.
        """
        stats_by_band = self.behavior.get("entry_stats_by_fine_band", {})
        rows = []
        for x in self.behavior.get("fine_bands", []):
            band = str(x["fine_band"])
            stats = stats_by_band.get(band, {})
            total_n = sum(int(v.get("n", 0)) for v in stats.values())
            if total_n <= 0:
                rows.append((band, float(x["trade_share"]), 1.0, 1.0))
                continue
            model_avg = sum(
                int(v.get("n", 0)) * float(v.get("scaled_median_notional", 0.0))
                for v in stats.values()
            ) / total_n
            target_avg = (
                float(x["notional"]) / float(x["trades"])
            ) * self.notional_scale
            ratio = target_avg / model_avg if model_avg > 0 else 1.0
            rows.append((band, float(x["trade_share"]), model_avg, ratio))

        base_total = sum(trade_share * avg for _, trade_share, avg, _ in rows)
        weighted_total = sum(
            trade_share * avg * ratio
            for _, trade_share, avg, ratio in rows
        )
        common = base_total / weighted_total if weighted_total > 0 else 1.0
        return {
            band: ratio * common
            for band, _, _, ratio in rows
        }

    def entry_expected_band_target(self, band):
        """Expected 40%-scale notional for a representative entry in a band."""
        stats = self.behavior.get("entry_stats_by_fine_band", {}).get(str(band), {})
        total_n = sum(int(v.get("n", 0)) for v in stats.values())
        if total_n <= 0:
            return 0.0
        base = sum(
            int(v.get("n", 0)) * float(v.get("scaled_median_notional", 0.0))
            for v in stats.values()
        ) / total_n
        return base * self.band_size_multiplier.get(str(band), 1.0)

    @classmethod
    def fine_band(cls, price):
        p = float(price)
        for band, lo, hi, regime in cls.BANDS:
            if lo <= p < hi:
                return band, regime
        if p == 1.0:
            return "H95_100", "HIGH"
        return None, None

    def entry_target(self, price, market="BTC", entry_count=0):
        del market
        band, _ = self.fine_band(price)
        if not band:
            return 0.0
        lookup = self.entry_medians.get(band, {})
        first = float(lookup.get("1", 0.0))
        if first <= 0:
            return 0.0
        _, regime = self.fine_band(price)
        n = int(entry_count)
        position = "first" if n == 0 else ("2nd-3rd" if n <= 2 else "4th+")
        ratio = self.ENTRY_POSITION_RATIOS[regime][position]
        # Keep V16's empirical band calibration, but correct the entry-count
        # direction using the full-scale trader-history ratios.
        multiplier = self.band_size_multiplier.get(band, 1.0)
        return max(0.10, first * ratio * multiplier)

    capital_target = entry_target

    @staticmethod
    def _points(history):
        out = []
        for item in history or []:
            try:
                if isinstance(item, dict):
                    ts = float(item["ts"])
                    price = float(item.get("best_bid", item.get("mid")))
                else:
                    ts, price = float(item[0]), float(item[1])
                if 0.0 < price < 1.0:
                    out.append((ts, price))
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        return sorted(out)

    @classmethod
    def movement(cls, price, history, now):
        points = cls._points(history)
        result = {}
        for seconds in (1, 3, 5, 10, 30):
            previous = [p for ts, p in points if ts <= float(now) - seconds]
            result[f"m{seconds}"] = float(price) - previous[-1] if previous else 0.0
        return result

    @staticmethod
    def _trajectory_class(delta):
        if delta > TRAJECTORY_THRESHOLD:
            return "rising"
        if delta < -TRAJECTORY_THRESHOLD:
            return "falling"
        return "flat"

    def _candidate(
        self,
        market,
        side,
        bid,
        ask,
        depth,
        history,
        now,
        thesis_side,
        entries,
        burst_age,
    ):
        if bid is None:
            return None
        try:
            bid = float(bid)
            ask = None if ask is None else float(ask)
            depth = None if depth is None else float(depth)
        except (TypeError, ValueError):
            return None
        if not 0.0 < bid < 1.0:
            return None
        if ask is not None and not (0.0 < ask <= 1.0):
            return None
        if ask is not None and ask < bid:
            return None
        band, regime = self.fine_band(bid)
        if not regime:
            return None
        mv = self.movement(bid, history, now)
        trajectory = self._trajectory_class(mv["m5"])
        trajectory_share = TRAJECTORY_SHARE[regime][trajectory]
        target = self.entry_target(bid, market, entries)
        return {
            "side": side,
            "bid": bid,
            "ask": ask,
            "depth": depth,
            "band": band,
            "regime": regime,
            "trajectory": trajectory,
            "trajectory_likelihood": trajectory_share,
            "band_prior": self.fine_band_trade_share.get(band, 0.0),
            "same_side": bool(thesis_side and side == thesis_side),
            "target": target,
            "movement": mv,
            "entries": int(entries),
            "burst_age": float(burst_age),
            "reason": (
                f"{self.VERSION} target_band={band} band={band} regime={regime} "
                f"trajectory={trajectory} band_share={self.fine_band_trade_share.get(band,0.0):.6f} "
                f"trajectory_share={trajectory_share:.3f} same_side={bool(thesis_side and side == thesis_side)} "
                f"passive=bid target_40pct=${target:.2f} entry_count={int(entries)} "
                f"burst_age={float(burst_age):.1f}s bid={bid:.4f} "
                f"ask={ask if ask is not None else 0.0:.4f} depth={depth if depth is not None else 0.0:.2f} "
                f"m1={mv['m1']:+.4f} m3={mv['m3']:+.4f} m5={mv['m5']:+.4f} "
                f"m10={mv['m10']:+.4f} m30={mv['m30']:+.4f}"
            ),
        }

    def build_candidates_for_market(
        self,
        elapsed,
        up_ask,
        down_ask,
        up_bid,
        down_bid,
        up_history,
        down_history,
        now,
        asset=None,
        market=None,
        thesis_side=None,
        market_entry_count=0,
        seconds_since_first_entry=0,
        up_depth=0,
        down_depth=0,
    ):
        """Return all currently eligible side candidates for one market.

        Side persistence is sampled once for this market's scheduled decision,
        then the global scheduler chooses the fine band across all markets.
        This prevents per-market band sampling from distorting the trader's
        global distribution.
        """
        elapsed = float(elapsed)
        now = float(now)
        if elapsed < self.start_sec or elapsed >= self.stop_sec:
            return []
        if self.stop_sec - elapsed <= self.hard_cutoff_seconds:
            return []

        m = str(market or asset or "BTC").upper()
        candidates = [
            c for c in (
                self._candidate(
                    m, "Up", up_bid, up_ask, up_depth, up_history,
                    now, thesis_side, market_entry_count, seconds_since_first_entry,
                ),
                self._candidate(
                    m, "Down", down_bid, down_ask, down_depth, down_history,
                    now, thesis_side, market_entry_count, seconds_since_first_entry,
                ),
            )
            if c is not None
        ]
        if not thesis_side or len(candidates) <= 1:
            return candidates

        same = [c for c in candidates if c["side"] == thesis_side]
        flip = [c for c in candidates if c["side"] != thesis_side]
        if self.process.should_continue_side():
            return same or candidates
        return flip or candidates

    def choose_process_candidate(self, candidates, target_band=None, thesis_side=None):
        if not candidates:
            return None
        if target_band is None:
            target_band = self.scheduler.choose_band(candidates)
        if target_band is None:
            return None
        # STRICT: no fallback to a different fine price band.
        targeted = [c for c in candidates if c["band"] == target_band]
        if not targeted:
            return None
        return min(
            targeted,
            key=lambda c: (-c["trajectory_likelihood"], c["bid"]),
        )

    def choose_distribution_band(self, candidates):
        return self.scheduler.choose_band(candidates)

    def restore_policy_state(self, trades):
        self.scheduler.restore(trades, self.fine_band)

    def observe_trade_distribution(self, band, notional):
        self.scheduler.observe(band, notional)

    def distribution_snapshot(self):
        return self.scheduler.shares()

    def distribution_report(self):
        return self.scheduler.target_report()

    def sample_target_band(self):
        return self.process.sample_target_band()

    def sample_delay(self):
        return self.process.sample_gap()

    def decide(
        self,
        elapsed,
        up_ask,
        down_ask,
        up_bid,
        down_bid,
        up_history,
        down_history,
        current_exposure,
        available_cash,
        up_depth=0,
        down_depth=0,
        now=None,
        asset_exposure=0,
        total_exposure=0,
        market_entry_count=0,
        seconds_since_first_entry=0,
        thesis_side=None,
        thesis_price=None,
        asset=None,
        market=None,
        process_target_band=None,
    ):

        del current_exposure, asset_exposure, thesis_price
        now = time.time() if now is None else float(now)
        candidates = self.build_candidates_for_market(
            elapsed,
            up_ask,
            down_ask,
            up_bid,
            down_bid,
            up_history,
            down_history,
            now,
            asset=asset,
            market=market,
            thesis_side=thesis_side,
            market_entry_count=market_entry_count,
            seconds_since_first_entry=seconds_since_first_entry,
            up_depth=up_depth,
            down_depth=down_depth,
        )
        if not candidates:
            return None

        target_band = process_target_band or self.scheduler.choose_band(candidates)
        best = self.choose_process_candidate(candidates, target_band)
        if best is None:
            return None

        remaining = max(0.0, self.max_total_exposure - float(total_exposure))
        target = float(best["target"])
        notion = min(target, max(0.0, float(available_cash)), remaining)
        if notion < 0.10:
            return None

        self._last_trade_at = now
        mv = best["movement"]
        reason = (
            f"{self.VERSION} target_band={target_band} band={best['band']} "
            f"regime={best['regime']} trajectory={best['trajectory']} "
            f"band_share={best['band_prior']:.6f} "
            f"trajectory_share={best['trajectory_likelihood']:.3f} "
            f"same_side={best['same_side']} passive=bid "
            f"target_40pct=${target:.2f} entry_count={market_entry_count} "
            f"burst_age={float(seconds_since_first_entry):.1f}s "
            f"bid={best['bid']:.4f} "
            f"ask={best['ask'] if best['ask'] is not None else 0:.4f} "
            f"depth={best['depth'] if best['depth'] is not None else 0:.2f} "
            f"m1={mv['m1']:+.4f} m3={mv['m3']:+.4f} "
            f"m5={mv['m5']:+.4f} m10={mv['m10']:+.4f} "
            f"m30={mv['m30']:+.4f} elapsed={float(elapsed):.1f}s "
            f"left={self.stop_sec-float(elapsed):.1f}s"
        )
        return Signal(
            best['side'], best['bid'], best['trajectory_likelihood'],
            round(notion, 2), reason,
        )

    def size(self, price, regime=None, market="BTC", entry_count=0, **_):
        del regime
        return self.entry_target(price, market, entry_count)
