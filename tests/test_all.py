import tempfile
from pathlib import Path
import pytest
from strategy import CapitalFirstStrategy
from paper_ledger import PaperLedger
from research_logger import ResearchLogger

def S(): return CapitalFirstStrategy(min_trade_gap_seconds=0,hard_cutoff_seconds=60,max_total_exposure=300)
def H(p,now=1000): return [{"ts":now-30,"best_bid":p},{"ts":now-10,"best_bid":p},{"ts":now-5,"best_bid":p},{"ts":now,"best_bid":p}]

def test_all_fine_bands():
    s=S(); expected=[(.02,'C00_05','CHEAP'),(.07,'C05_10','CHEAP'),(.12,'C10_15','CHEAP'),(.17,'C15_20','CHEAP'),(.25,'C20_30','CHEAP'),(.35,'M30_40','MID'),(.45,'M40_50','MID'),(.55,'M50_60','MID'),(.65,'M60_70','MID'),(.75,'R70_80','CORE'),(.85,'R80_90','CORE'),(.925,'H90_95','HIGH'),(.975,'H95_100','HIGH')]
    for price,band,regime in expected: assert s.fine_band(price)==(band,regime)

def test_40pct_high_sizing():
    s=S(); assert s.entry_target(.96,'BTC',0) > s.entry_target(.96,'BTC',3)

def test_high_candidate_not_blocked():
    s=S(); c=s._candidate('BTC','Up',.96,.99,0,H(.96),1000,None,0,0); assert c and c['regime']=='HIGH'

def test_total_300_cap():
    s=S(); x=s.decide(30,.99,.50,.96,.49,H(.96),H(.49),0,1000,now=1000,total_exposure=295,asset='BTC',market='BTC',process_target_band='H95_100'); assert x and 0 < x.notional <= 5.0

def test_no_depth_or_spread_gate():
    s=S(); assert s._candidate('BTC','Up',.96,.99,0,H(.96),1000,None,0,0) is not None

def test_final_minute_cutoff():
    assert S().decide(180,.51,.21,.50,.20,H(.50),H(.20),0,1000,now=1000,asset='BTC',market='BTC',process_target_band='M40_50') is None

def test_side_persistence_preference_path():
    s=S(); x=s.decide(120,.81,.99,.80,.98,H(.80),H(.98),0,1000,now=1000,market_entry_count=1,seconds_since_first_entry=90,thesis_side='Up',asset='BTC',market='BTC',process_target_band='R80_90'); assert x and x.side=='Up'

def test_trajectory_gradient():
    s=S(); falling=[{'ts':970,'best_bid':.25},{'ts':995,'best_bid':.24},{'ts':1000,'best_bid':.20}]; rising=[{'ts':970,'best_bid':.75},{'ts':995,'best_bid':.79},{'ts':1000,'best_bid':.80}]; a=s._candidate('BTC','Up',.20,.21,0,falling,1000,None,0,0); b=s._candidate('BTC','Up',.80,.81,0,rising,1000,None,0,0); assert a['trajectory_likelihood']==pytest.approx(.539948) and b['trajectory_likelihood']==pytest.approx(.499717)

def test_empirical_data_loaded():
    s=S(); assert len(s.fine_band_trade_share)==13 and len(s.entry_medians)==13 and s.notional_scale==pytest.approx(.4)

def test_cash_constraint():
    s=S(); x=s.decide(30,.99,.50,.96,.49,H(.96),H(.49),0,1.0,now=1000,asset='BTC',market='BTC',process_target_band='H95_100'); assert x and x.notional<=1.0

def test_empirical_process_target_band_changes_candidate_choice():
    s=S(); up=s._candidate('BTC','Up',.96,.97,0,H(.96),1000,None,0,0); down=s._candidate('BTC','Down',.25,.26,0,H(.25),1000,None,0,0); assert s.choose_process_candidate([up,down],'H95_100')['band']=='H95_100'; assert s.choose_process_candidate([up,down],'C20_30')['band']=='C20_30'

def test_empirical_cadence_has_observed_distribution():
    s=S(); vals=[s.sample_delay() for _ in range(5000)]; assert min(vals)==0.0 and max(vals)>=100.0

def test_side_persistence_is_empirical():
    s=S(); vals=[s.process.should_continue_side() for _ in range(50000)]; rate=sum(vals)/len(vals); assert .88 < rate < .906

def test_no_synthetic_band_trajectory_product():
    src=Path(__file__).parents[1].joinpath('strategy.py').read_text(); assert 'band_prior*trajectory_likelihood' not in src and 'band_prior * trajectory_likelihood' not in src

def test_resolution_accounting_and_research():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); ledger=PaperLedger(root/'paper_state.json',1000); m={'id':'m1','condition':'c1','slug':'btc-updown-5m-1000000000','asset':'BTC','market':'BTC Up or Down','start_ts':1000000000.0,'end_ts':1000000300.0}; ledger.buy('c1','up-token',m['market'],'Up',.20,1.0,1000000010,meta={'asset':'BTC','slug':m['slug'],'market_id':'m1'}); closed=ledger.settle('c1','up-token'); assert len(closed)==1 and closed[0]['pnl']==4.0; logger=ResearchLogger(root); logger.record_resolution(ts=1000000302,market=m,winner='Up',winner_token='up-token',closed=closed); assert 'RESOLVED' in (root/'resolutions.csv').read_text()

def test_v16_scheduler_targets_are_loaded_from_trader_data():
    s = S()
    assert s.VERSION == "V17_FULL_SCALE_TRADER_REPLICA_40PCT"
    assert sum(v for b,v in s.scheduler.trade_targets.items() if b.startswith("C")) == pytest.approx(0.484)
    assert s.scheduler.capital_targets["H95_100"] == pytest.approx(0.452749963176931)


def test_v16_strict_band_selection_never_falls_back():
    s = S()
    cheap = s._candidate('BTC', 'Up', .25, .26, 100, H(.25), 1000, None, 0, 0)
    high = s._candidate('BTC', 'Up', .96, .97, 100, H(.96), 1000, None, 0, 0)
    picked = s.choose_process_candidate([cheap, high], 'H95_100')
    assert picked['band'] == 'H95_100'
    picked = s.choose_process_candidate([cheap], 'H95_100')
    assert picked is None


def test_v16_policy_state_restores_from_ledger_trades():
    s = S()
    trades = [
        {'action':'BUY','price':0.96,'cost':5.0},
        {'action':'BUY','price':0.20,'cost':0.5},
        {'action':'SELL','price':0.99,'cost':99.0},
    ]
    s.restore_policy_state(trades)
    snap=s.distribution_snapshot()
    assert snap['trade']['H95_100'] == pytest.approx(0.5)
    assert snap['trade']['C20_30'] == pytest.approx(0.5)
    assert snap['capital']['H95_100'] == pytest.approx(5/5.5)


def test_v16_entry_targets_remain_empirical():
    s=S()
    assert s.entry_target(.96,'BTC',0) > s.entry_target(.96,'BTC',3)
    assert s.entry_target(.25,'BTC',0)==pytest.approx(0.43998,abs=.01)




def test_v16_global_band_selection_is_strict_and_feasible():
    s = S()
    cheap = s._candidate('BTC', 'Up', .25, .26, 100, H(.25), 1000, None, 0, 0)
    high = s._candidate('ETH', 'Up', .96, .97, 100, H(.96), 1000, None, 0, 0)
    assert s.choose_process_candidate([cheap, high], 'H95_100')['band'] == 'H95_100'
    assert s.choose_process_candidate([cheap], 'H95_100') is None


def test_v16_restore_is_deterministic_and_excludes_non_buys():
    s=S()
    trades=[
        {'action':'BUY','price':.96,'cost':5},
        {'action':'SELL','price':.96,'cost':5},
        {'action':'BUY','price':.25,'cost':.5},
    ]
    s.restore_policy_state(trades)
    snap=s.distribution_snapshot()
    assert snap['trade']['H95_100'] == pytest.approx(.5)
    assert snap['trade']['C20_30'] == pytest.approx(.5)



def test_v16_band_capital_calibration_is_data_derived():
    s=S()
    assert s.band_size_multiplier["C00_05"] > 1.0
    assert s.band_size_multiplier["M40_50"] < 1.0
    assert s.band_size_multiplier["H95_100"] > 1.0
    # The calibrated representative band dollars reconstruct the trader's
    # aggregate capital shares when weighted by the trader's trade shares.
    vals=[]
    for x in s.behavior["fine_bands"]:
        b=x["fine_band"]
        vals.append(float(x["trade_share"]) * s.entry_expected_band_target(b))
    total=sum(vals)
    assert total>0
    max_err=0.0
    for x,v in zip(s.behavior["fine_bands"],vals):
        implied=v/total
        max_err=max(max_err, abs(implied-float(x["notional_share"])))
    assert max_err < 0.005



def test_v16_distribution_converges_when_all_bands_are_available():
    s=S()
    candidates=[
        {"band": x["fine_band"], "target": s.entry_expected_band_target(x["fine_band"])}
        for x in s.behavior["fine_bands"]
    ]
    for _ in range(3000):
        band=s.choose_distribution_band(candidates)
        target=next(c["target"] for c in candidates if c["band"]==band)
        s.observe_trade_distribution(band,target)
    actual=s.distribution_snapshot()
    max_trade=max(abs(actual["trade"][x["fine_band"]]-s.scheduler.trade_targets[x["fine_band"]]) for x in s.behavior["fine_bands"])
    # Capital targets remain the empirically observed full-trader capital
    # distribution; allow a slightly wider tolerance because the four-asset
    # count target and six-asset capital target are intentionally distinct.
    max_cap=max(abs(actual["capital"][x["fine_band"]]-float(x["notional_share"])) for x in s.behavior["fine_bands"])
    assert max_trade < 0.01
    assert max_cap < 0.03



def test_v16_invalid_zero_ask_is_rejected():
    s=S()
    assert s._candidate('BTC','Up',.96,0.0,100,H(.96),1000,None,0,0) is None
    assert s._candidate('BTC','Up',.96,.95,100,H(.96),1000,None,0,0) is None


def test_v16_m50_cumulative_quota_cannot_dominate():
    s = S()
    # Reproduce the observed failure: M50-60 has already captured ~90% of
    # the stream. The scheduler must refuse another M50-60 trade even when it
    # is the only candidate.
    for _ in range(20):
        s.observe_trade_distribution("M50_60", 0.9)
    assert s.choose_distribution_band([{"band": "M50_60", "target": 0.9}]) is None


def test_v16_quota_prefers_underrepresented_available_band():
    s = S()
    for _ in range(20):
        s.observe_trade_distribution("M50_60", 0.9)
    # C20-30 is still under its quota, so it should be selected over M50-60.
    band = s.choose_distribution_band([
        {"band": "M50_60", "target": 0.9},
        {"band": "C20_30", "target": 0.4},
    ])
    assert band == "C20_30"


def test_v161_bot_banner_and_strategy_version():
    assert 'V18 REALISTIC CLOB TRADE-TAPE FILL SIMULATION' in Path(__file__).resolve().parents[1].joinpath('bot.py').read_text()


def test_v17_entry_count_decreases_all_regimes():
    s=S()
    for price in (.25,.50,.80,.97):
        vals=[s.entry_target(price,'BTC',i) for i in (0,1,3)]
        assert vals[0] >= vals[2] - 1e-9
        if price != .97:
            assert vals[0] >= vals[1] - 1e-9

def test_v17_full_scale_trajectory_threshold():
    s=S()
    h=[{'ts':970,'best_bid':.25},{'ts':995,'best_bid':.255},{'ts':1000,'best_bid':.255}]
    c=s._candidate('BTC','Up',.255,.26,0,h,1000,None,0,0)
    assert c['trajectory']=='flat'

def test_v17_four_asset_regime_targets():
    s=S()
    targets=s.scheduler.trade_targets
    cheap=sum(v for b,v in targets.items() if b.startswith('C'))
    mid=sum(v for b,v in targets.items() if b.startswith('M'))
    core=sum(v for b,v in targets.items() if b.startswith('R'))
    high=sum(v for b,v in targets.items() if b.startswith('H'))
    assert cheap==pytest.approx(.484, abs=.001)
    assert mid==pytest.approx(.306, abs=.001)
    assert core==pytest.approx(.122, abs=.001)
    assert high==pytest.approx(.088, abs=.001)

def test_v17_persistence_constant():
    s=S()
    assert s.process.PERSISTENCE==pytest.approx(.8805590616)

def test_v17_six_asset_mode_is_optional():
    import os, importlib, market_discovery
    old=os.environ.get('V17_SIX_ASSET_MODE')
    os.environ['V17_SIX_ASSET_MODE']='true'
    try:
        md=importlib.reload(market_discovery)
        assert 'DOGE' in md.ASSET_SLUGS and 'HYPE' in md.ASSET_SLUGS
        s=S()
        assert s.scheduler.benchmark_name == "six_asset"
        assert sum(v for b,v in s.scheduler.trade_targets.items() if b.startswith("C")) == pytest.approx(.593)
    finally:
        if old is None: os.environ.pop('V17_SIX_ASSET_MODE',None)
        else: os.environ['V17_SIX_ASSET_MODE']=old
        importlib.reload(market_discovery)


def test_v17_entry_count_ratios_are_exact_by_regime():
    s = S()
    expected = {
        "CHEAP": (1.0, 0.7241379, 0.3793103),
        "MID": (1.0, 1.0, 0.9405941),
        "CORE": (1.0, 0.9051282, 0.5641026),
        "HIGH": (1.0, 1.0810811, 0.6752252),
    }
    prices = {
        "CHEAP": .25,
        "MID": .55,
        "CORE": .85,
        "HIGH": .96,
    }
    for regime, price in prices.items():
        first, second, fourth = expected[regime]
        a = s.entry_target(price, "BTC", 0)
        b = s.entry_target(price, "BTC", 1)
        c = s.entry_target(price, "BTC", 3)
        assert b / a == pytest.approx(second, rel=1e-6, abs=1e-6)
        assert c / a == pytest.approx(fourth, rel=1e-6, abs=1e-6)


def test_v17_high_entry_shape_preserves_intended_second_entry_bump():
    s = S()
    first = s.entry_target(.96, "BTC", 0)
    second = s.entry_target(.96, "BTC", 1)
    fourth = s.entry_target(.96, "BTC", 3)
    assert second > first
    assert fourth < first
    assert fourth < second


def test_v17_strict_scheduler_still_blocks_over_quota_by_default():
    s = S()
    for _ in range(20):
        s.observe_trade_distribution("M50_60", .9)
    assert s.scheduler.choose_band([
        {"band": "M50_60", "target": .9},
    ]) is None


def test_v17_cadence_fallback_only_when_explicitly_enabled():
    s = S()
    for _ in range(20):
        s.observe_trade_distribution("M50_60", .9)
    c = [{"band": "M50_60", "target": .9}]
    assert s.scheduler.choose_band(c) is None
    assert s.scheduler.choose_band(c, allow_over_quota=True) == "M50_60"


def test_v17_cadence_fallback_prefers_least_overrepresented_band():
    s = S()
    for _ in range(20):
        s.observe_trade_distribution("M50_60", .9)
    s.observe_trade_distribution("C20_30", .4)
    picked = s.scheduler.choose_band([
        {"band": "M50_60", "target": .9},
        {"band": "C20_30", "target": .4},
    ], allow_over_quota=True)
    assert picked == "C20_30"


def test_v18_execution_speed_settings_are_subsecond_and_bounded():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert 'LOOP_SECONDS", "0.25"' in src
    assert 'DISCOVERY_INTERVAL_SECONDS' in src
    assert 'FILL_SIMULATION' in src
    assert 'MarketTradeFeed' in src
