"""Immediate paper execution against the currently visible Polymarket CLOB.

This module never submits a live order. It reads the public order book and
simulates a taker BUY using the asks that are available at the instant of the
signal. Strategy allocation is kept separate from execution price.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("clob_executor")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com").rstrip("/")


class ImmediateClobExecutor:
    """Paper taker executor with strict strategy-band preservation.

    A signal selects a fine strategy band. Execution may use any visible ask
    inside that same band, but it never changes the strategy band to fit the
    book. The requested notional must be fully available in-band; otherwise
    the signal is recorded as unfilled rather than partially changing the
    capital allocation.
    """

    def __init__(self, timeout=5.0, retries=2):
        self.timeout = max(1.0, float(timeout))
        self.retries = max(0, int(retries))
        self.session = requests.Session()
        retry = Retry(
            total=self.retries,
            connect=self.retries,
            read=self.retries,
            backoff_factor=0.25,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": "polymarket-paper-immediate-executor/1.0"})
        self._signals = 0
        self._filled = 0
        self._unfilled = 0
        self._book_errors = 0

    @staticmethod
    def _levels(rows) -> List[Tuple[float, float]]:
        levels = []
        for row in rows or []:
            try:
                price = float(row.get("price") if isinstance(row, dict) else row[0])
                size = float(row.get("size") if isinstance(row, dict) else row[1])
            except (TypeError, ValueError, KeyError, IndexError):
                continue
            if 0.0 < price < 1.0 and size > 0.0:
                levels.append((price, size))
        return sorted(levels, key=lambda x: x[0])

    def snapshot(self, token: str) -> dict:
        response = self.session.get(
            f"{CLOB}/book", params={"token_id": str(token)}, timeout=self.timeout
        )
        response.raise_for_status()
        data = response.json()
        asks = self._levels(data.get("asks"))
        bids = self._levels(data.get("bids"))
        return {"asks": asks, "bids": bids, "ts": time.time()}

    @staticmethod
    def _band_bounds(strategy, band):
        for name, lo, hi, _regime in strategy.BANDS:
            if name == band:
                return float(lo), float(hi)
        raise ValueError(f"unknown strategy fine band: {band}")

    def execute(self, *, token: str, notional: float, strategy_band: str,
                signal_bid: float, strategy=None) -> dict:
        """Return an all-or-nothing paper taker fill at current ask levels.

        The executor consumes the cheapest visible asks within the selected
        strategy band until the requested dollar notional is covered. If the
        band does not contain enough visible liquidity, nothing is filled.
        """
        requested = float(notional)
        signal_bid = float(signal_bid)
        self._signals += 1
        if requested <= 0:
            self._unfilled += 1
            return self._empty("invalid_notional", signal_bid, requested_notional=requested, strategy_band=strategy_band)
        if strategy is None:
            raise ValueError("strategy is required for fine-band bounds")

        lo, hi = self._band_bounds(strategy, strategy_band)
        try:
            snap = self.snapshot(token)
        except Exception as exc:
            self._book_errors += 1
            self._unfilled += 1
            LOG.warning("CLOB BOOK ERROR | token=%s | %s", token, exc)
            return self._empty("book_error", signal_bid, requested_notional=requested, strategy_band=strategy_band, error=str(exc))

        # Strict band preservation: only asks inside the signal's fine band
        # count. This means execution cannot silently turn a Cheap signal into
        # a Mid/Core/High investment merely because another ask is available.
        eligible = [(p, s) for p, s in snap["asks"] if lo <= p < hi]
        if not eligible:
            self._unfilled += 1
            return self._empty("no_in_band_ask", signal_bid, snapshot=snap, requested_notional=requested, strategy_band=strategy_band)

        remaining = requested
        fills = []
        for price, size in eligible:
            capacity = price * size
            take_notional = min(remaining, capacity)
            if take_notional <= 0:
                continue
            shares = take_notional / price
            fills.append((price, shares, take_notional))
            remaining -= take_notional
            if remaining <= 1e-9:
                break

        if remaining > 1e-7:
            self._unfilled += 1
            return self._empty(
                "insufficient_in_band_liquidity",
                signal_bid,
                snapshot=snap,
                requested_notional=requested,
                strategy_band=strategy_band,
                available_notional=requested - remaining,
            )

        total_notional = sum(x[2] for x in fills)
        total_shares = sum(x[1] for x in fills)
        vwap = total_notional / total_shares if total_shares else 0.0
        self._filled += 1
        return {
            "filled": True,
            "reason": "filled_current_clob",
            "requested_notional": requested,
            "executed_notional": total_notional,
            "shares": total_shares,
            "vwap": vwap,
            "signal_bid": signal_bid,
            "strategy_band": strategy_band,
            "band_low": lo,
            "band_high": hi,
            "slippage": vwap - signal_bid,
            "levels": [{"price": p, "shares": sh, "notional": n} for p, sh, n in fills],
            "book_ts": snap["ts"],
            "book_levels_seen": len(snap["asks"]),
        }

    @staticmethod
    def _empty(reason, signal_bid, snapshot=None, requested_notional=0.0,
               strategy_band=None, **extra):
        out = {
            "filled": False,
            "reason": reason,
            "requested_notional": float(requested_notional),
            "executed_notional": 0.0,
            "shares": 0.0,
            "vwap": None,
            "signal_bid": signal_bid,
            "strategy_band": strategy_band,
            "slippage": None,
        }
        if snapshot is not None:
            out["book_ts"] = snapshot.get("ts")
            out["book_levels_seen"] = len(snapshot.get("asks", []))
        out.update(extra)
        return out

    def stats(self):
        return {
            "signals": self._signals,
            "filled": self._filled,
            "unfilled": self._unfilled,
            "fill_rate": self._filled / self._signals if self._signals else 0.0,
            "book_errors": self._book_errors,
        }
