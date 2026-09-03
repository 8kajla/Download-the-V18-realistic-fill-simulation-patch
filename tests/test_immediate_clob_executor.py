import pytest
from immediate_clob_executor import ImmediateClobExecutor

class FakeStrategy:
    BANDS = [
        ("C00_05", 0.00, 0.05, "CHEAP"),
        ("M40_50", 0.40, 0.50, "MID"),
        ("M50_60", 0.50, 0.60, "MID"),
        ("H95_100", 0.95, 1.00, "HIGH"),
    ]

def test_walks_current_asks_inside_selected_band(monkeypatch):
    ex=ImmediateClobExecutor(timeout=1,retries=0)
    monkeypatch.setattr(ex,"snapshot",lambda token: {"asks":[
        (0.41,5.0),(0.45,10.0),(0.51,100.0)],"bids":[],"ts":1.0})
    r=ex.execute(token="t",notional=6.0,strategy_band="M40_50",
                 signal_bid=.40,strategy=FakeStrategy)
    assert r["filled"]
    assert r["executed_notional"]==6.0
    assert r["vwap"]==pytest.approx(6/(2.05/.41 + 3.95/.45))
    assert len(r["levels"])==2

def test_requires_full_in_band_liquidity(monkeypatch):
    ex=ImmediateClobExecutor(timeout=1,retries=0)
    monkeypatch.setattr(ex,"snapshot",lambda token: {"asks":[
        (0.41,5.0)],"bids":[],"ts":1.0})
    r=ex.execute(token="t",notional=6.0,strategy_band="M40_50",
                 signal_bid=.40,strategy=FakeStrategy)
    assert not r["filled"]
    assert r["reason"]=="insufficient_in_band_liquidity"
    assert r["requested_notional"]==6.0

def test_ignores_cheaper_ask_outside_band(monkeypatch):
    ex=ImmediateClobExecutor(timeout=1,retries=0)
    monkeypatch.setattr(ex,"snapshot",lambda token: {"asks":[
        (0.35,100.0),(0.45,10.0)],"bids":[],"ts":1.0})
    r=ex.execute(token="t",notional=4.0,strategy_band="M40_50",
                 signal_bid=.40,strategy=FakeStrategy)
    assert r["filled"]
    assert r["vwap"]==pytest.approx(.45)
