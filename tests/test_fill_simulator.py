
import tempfile
from pathlib import Path

from fill_simulator import FillSimulator
from paper_ledger import PaperLedger


class DummyResearch:
    def __init__(self):
        self.events = []

    def record_pending_order(self, order): self.events.append(("pending", order["order_id"]))
    def record_fill_progress(self, *a): self.events.append(("progress",))
    def record_fill(self, order, trade): self.events.append(("fill", order["order_id"]))
    def record_unfilled(self, order): self.events.append(("unfilled", order["order_id"]))
    def record_fill_error(self, *a): self.events.append(("error",))


def test_pending_does_not_touch_ledger():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = PaperLedger(root/"paper_state.json", 1000)
        r = DummyResearch()
        sim = FillSimulator(root/"pending_orders.json", ledger, r, lambda *_: {})
        sim.place(condition="c", token="t", market="m", side="Up",
                  target_price=.20, notional=5, placed_ts=100,
                  window_end_ts=400, depth_ahead=10,
                  meta={"regime":"CHEAP"})
        assert ledger.cash == 1000
        assert ledger.positions == {}
        assert sim.active_count() == 1


def test_first_trade_print_not_enough_when_queue_ahead_exists():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = PaperLedger(root/"paper_state.json", 1000)
        r = DummyResearch()
        sim = FillSimulator(root/"pending_orders.json", ledger, r, lambda *_: {})
        sim.place(condition="c", token="t", market="m", side="Up",
                  target_price=.20, notional=5, placed_ts=100,
                  window_end_ts=400, depth_ahead=10,
                  meta={})
        assert sim.on_trade_print("t", .19, 4, 101) == []
        assert sim.active_count() == 1


def test_queue_volume_fills_and_callback_occurs_at_real_print_price():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = PaperLedger(root/"paper_state.json", 1000)
        r = DummyResearch()
        seen = []
        def cb(order, px, ts):
            seen.append((px, ts))
            return ledger.buy(order["condition"], order["token"], order["market"],
                              order["side"], px, order["notional"], ts, order["meta"])
        sim = FillSimulator(root/"pending_orders.json", ledger, r, cb)
        sim.place(condition="c", token="t", market="m", side="Up",
                  target_price=.20, notional=5, placed_ts=100,
                  window_end_ts=400, depth_ahead=10,
                  meta={"regime":"CHEAP"})
        filled = sim.on_trade_print("t", .18, 6, 101)
        assert len(filled) == 0
        filled = sim.on_trade_print("t", .17, 4, 102)
        assert len(filled) == 1
        assert seen == [(.17, 102.0)]
        assert ledger.cash == 995
        assert sim.active_count() == 0


def test_zero_depth_fills_on_first_qualifying_print():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = PaperLedger(root/"paper_state.json", 1000)
        r = DummyResearch()
        sim = FillSimulator(root/"pending_orders.json", ledger, r,
                            lambda order, px, ts: {"price": px, "ts": ts})
        sim.place(condition="c", token="t", market="m", side="Down",
                  target_price=.50, notional=2, placed_ts=100,
                  window_end_ts=400, depth_ahead=0, meta={})
        filled = sim.on_trade_print("t", .49, 1, 110)
        assert len(filled) == 1
        assert sim.active_count() == 0


def test_expiry_marks_unfilled_without_ledger_side_effect():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = PaperLedger(root/"paper_state.json", 1000)
        r = DummyResearch()
        sim = FillSimulator(root/"pending_orders.json", ledger, r, lambda *_: {})
        sim.place(condition="c", token="t", market="m", side="Up",
                  target_price=.20, notional=5, placed_ts=100,
                  window_end_ts=200, depth_ahead=10, meta={"regime":"MID"})
        expired = sim.expire_due(200)
        assert len(expired) == 1
        assert expired[0]["status"] == "EXPIRED_UNFILLED"
        assert ledger.cash == 1000


def test_stats_report_fill_and_expiry_by_regime():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = PaperLedger(root/"paper_state.json", 1000)
        r = DummyResearch()
        sim = FillSimulator(root/"pending_orders.json", ledger, r,
                            lambda order, px, ts: ledger.buy(
                                order["condition"], order["token"], order["market"],
                                order["side"], px, order["notional"], ts, order["meta"]))
        sim.place(condition="c1", token="t1", market="m", side="Up",
                  target_price=.20, notional=2, placed_ts=100,
                  window_end_ts=200, depth_ahead=0, meta={"regime":"CHEAP"})
        sim.place(condition="c2", token="t2", market="m", side="Down",
                  target_price=.80, notional=2, placed_ts=100,
                  window_end_ts=200, depth_ahead=10, meta={"regime":"HIGH"})
        sim.on_trade_print("t1", .20, 1, 110)
        sim.expire_due(200)
        s = sim.stats()
        assert s["signals"] == 2
        assert s["filled"] == 1
        assert s["expired_unfilled"] == 1
        assert s["by_regime"]["CHEAP"]["fill_rate"] == 1.0
        assert s["by_regime"]["HIGH"]["expiry_rate"] == 1.0
