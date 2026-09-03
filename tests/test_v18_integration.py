
from pathlib import Path
from fill_simulator import FillSimulator
from paper_ledger import PaperLedger
from research_logger import ResearchLogger

def make(tmp):
    ledger=PaperLedger(tmp/"state.json",1000); research=ResearchLogger(tmp,ledger)
    def cb(order,price,ts): return ledger.buy(order["condition"],order["token"],order["market"],order["side"],price,order["notional"],ts,order.get("meta"))
    return ledger,FillSimulator(tmp/"pending.json",ledger,research,cb)

def test_signal_then_tape_fill(tmp_path):
    ledger,sim=make(tmp_path)
    sim.place(condition="c",token="t",market="m",side="Up",target_price=.42,notional=10,placed_ts=100,window_end_ts=200,depth_ahead=5,meta={"regime":"MID"})
    assert ledger.cash==1000 and not ledger.positions
    sim.on_trade_print("t",.43,100,101); assert ledger.cash==1000
    sim.on_trade_print("t",.42,4,102); assert ledger.cash==1000
    sim.on_trade_print("t",.41,1,103); assert ledger.cash<1000
    order=next(iter(sim.orders.values())); assert order["status"]=="FILLED" and order["fill_price"]==.41

def test_restart_preserves_pending(tmp_path):
    ledger,sim=make(tmp_path)
    sim.place(condition="c",token="t",market="m",side="Up",target_price=.42,notional=10,placed_ts=100,window_end_ts=200,depth_ahead=1,meta={})
    _,sim2=make(tmp_path)
    assert sim2.active_count()==1


def test_trade_feed_parses_last_trade_price(monkeypatch):
    import market_feed
    seen=[]
    feed=market_feed.MarketTradeFeed(lambda *args: seen.append(args))
    feed._on_message(None, '{"event_type":"last_trade_price","asset_id":"tok","price":"0.41","size":"7.5","timestamp":"1000"}')
    assert seen[0][0]=="tok" and seen[0][1]==0.41 and seen[0][2]==7.5 and seen[0][3]==1.0
