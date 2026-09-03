import logging, os, shutil, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from market_discovery import discover, book, book_snapshot, resolve
from market_feed import MarketTradeFeed
from paper_ledger import PaperLedger
from research_logger import ResearchLogger
from strategy import CapitalFirstStrategy
from fill_simulator import FillSimulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s UTC %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log=logging.getLogger("bot")

def prepare_data_dir():
    data_dir=Path(os.getenv("DATA_DIR","/app/data")).expanduser()
    fresh=os.getenv("FRESH_START","false").lower() in ("1","true","yes","on")
    if str(data_dir) in ("/",".",""): raise RuntimeError(f"Refusing unsafe DATA_DIR={data_dir!r}")
    data_dir.mkdir(parents=True,exist_ok=True)
    if fresh:
        for child in data_dir.iterdir(): shutil.rmtree(child) if child.is_dir() else child.unlink()
    return data_dir

DATA=prepare_data_dir()
if os.getenv("PAPER_TRADING","true").lower()!="true": raise SystemExit("SAFETY LOCK: PAPER_TRADING must be true")
strategy=CapitalFirstStrategy(
    bankroll=float(os.getenv("STARTING_CAPITAL","1000")),
    max_total_exposure=float(os.getenv("MAX_TOTAL_EXPOSURE","300")),
    start_sec=float(os.getenv("START_TRADING_SECOND","0")),
    stop_sec=float(os.getenv("STOP_TRADING_SECOND","240")),
    hard_cutoff_seconds=float(os.getenv("HARD_CUTOFF_SECONDS","60")),
    min_trade_gap_seconds=float(os.getenv("MIN_TRADE_GAP_SECONDS","0")),
)
ledger=PaperLedger(DATA/"paper_state.json",strategy.bankroll); ledger.save()
strategy.restore_policy_state(ledger.trades)
research=ResearchLogger(DATA,ledger)
markets={}; histories={}; resolution_markets={}; last_trade={}; ob_last={}
last_disc=last_report=0.0; next_trade_at=0.0; consecutive_errors=0
BURST_GAP_SECONDS=float(os.getenv("BURST_GAP_SECONDS","18"))
CADENCE_FALLBACK_SECONDS=max(0.5,float(os.getenv("CADENCE_FALLBACK_SECONDS","2")))
DISCOVERY_INTERVAL_SECONDS=max(2.0,float(os.getenv("DISCOVERY_INTERVAL_SECONDS","5")))
BOOK_WORKERS=max(2,int(os.getenv("BOOK_WORKERS","8")))
LOOP_SECONDS=max(0.05,float(os.getenv("LOOP_SECONDS", "0.25")))
BOOK_EXECUTOR=ThreadPoolExecutor(max_workers=BOOK_WORKERS,thread_name_prefix="v18-book")

FILL_ENABLED=os.getenv("FILL_SIMULATION","true").lower() in ("1","true","yes","on")
FILL_EXPIRY_ENABLED=os.getenv("FILL_EXPIRY_ENABLED","true").lower() in ("1","true","yes","on")

def market_entry_state(condition,now):
    entries=[t for t in ledger.trades if t.get("action")=="BUY" and t.get("condition")==condition]
    if not entries: return {"count":0,"seconds_since_first":0.0,"seconds_since_previous":None,"side":None,"price":None,"burst_position":0}
    ordered=sorted(entries,key=lambda t:float(t.get("ts",now))); first=float(ordered[0].get("ts",now)); prev=float(ordered[-1].get("ts",now))
    gaps=[float(c.get("ts",now))-float(p.get("ts",now)) for p,c in zip(ordered,ordered[1:])]; burst=1
    for gap in reversed(gaps):
        if gap<=BURST_GAP_SECONDS: burst+=1
        else: break
    latest=ordered[-1]
    return {"count":len(ordered),"seconds_since_first":max(0,now-first),"seconds_since_previous":max(0,now-prev),"side":latest.get("side"),"price":latest.get("price"),"burst_position":burst}

def prepare_histories(h,now):
    for side in ("Up","Down"): h[side]=[x for x in h.get(side,[]) if float(x[0])>=now-60]

def pending_reserved():
    return sum(float(o.get("notional",0)) for o in fill_sim.orders.values() if o.get("status")=="PENDING") if FILL_ENABLED else 0.0

def startup_check():
    required=["decisions.jsonl","orderbooks.jsonl","trades.csv","markets.csv","resolutions.csv","pnl_1min.csv","paper_state.json"]
    missing=[x for x in required if not (DATA/x).exists()]
    if missing: raise RuntimeError(f"DATA STORE INITIALIZATION FAILED: {missing}")

def fill_callback(order,fill_price,fill_ts):
    meta=dict(order.get("meta") or {})
    market=markets.get(order["condition"],{})
    market=market or {"condition":order["condition"],"id":meta.get("market_id",""),"slug":meta.get("slug",""),"asset":meta.get("asset",""),"market":meta.get("market",meta.get("asset","")),"start_ts":meta.get("start_ts",0),"end_ts":order["window_end_ts"],"up":meta.get("up_token",""),"down":meta.get("down_token","")}
    trade=ledger.buy(order["condition"],order["token"],order["market"],order["side"],fill_price,order["notional"],fill_ts,meta)
    resolution_markets[order["condition"]]=market
    last_trade[order["condition"]]=fill_ts
    research.record_trade(trade=trade,market=market,elapsed=fill_ts-float(market.get("start_ts",fill_ts)),left=max(0,float(market.get("end_ts",fill_ts))-fill_ts),
        entry_count_before=meta.get("entry_count_before",0),burst_position=meta.get("burst_position",0),seconds_since_previous=meta.get("seconds_since_previous_trade"),
        up_bid=meta.get("up_bid"),up_ask=meta.get("up_ask"),up_depth=meta.get("up_depth"),down_bid=meta.get("down_bid"),down_ask=meta.get("down_ask"),down_depth=meta.get("down_depth"),
        score=meta.get("trajectory_likelihood"),momentum=meta.get("movement"),cash_after=ledger.cash,exposure_after=ledger.exposure(order["condition"]),fine_band=meta.get("fine_band"))
    research.record_execution_comparison_fill(order,trade)
    ledger.save()
    log.info("FILL | %s | %s | target=%.4f fill=%.4f latency=%.3fs notional=$%.2f",market.get("asset"),order.get("side"),order["target_price"],fill_price,order.get("fill_latency_s",0),order["notional"])
    return trade

fill_sim=FillSimulator(DATA/os.getenv("FILL_STATE_FILE","pending_orders.json"),ledger,research,fill_callback)
feed=MarketTradeFeed(lambda token,price,size,ts,event: fill_sim.on_trade_print(token,price,size,ts)) if FILL_ENABLED else None

def resolve_pending(now):
    for condition,market in list(resolution_markets.items()):
        if now<float(market.get("end_ts",0))+2: continue
        try:
            token,outcome,status=resolve(market)
            if token:
                closed=ledger.settle(condition,token); pnl=sum(float(x["pnl"]) for x in closed)
                research.record_resolution(ts=now,market=market,winner=outcome or token,winner_token=token,closed=closed)
                research.record_instant_resolution(market,token,now)
                log.info("RESOLUTION | %s | winner=%s | filled_pnl=%+.4f | closed=%d",market["slug"],outcome or token,pnl,len(closed))
                resolution_markets.pop(condition,None); markets.pop(condition,None); histories.pop(condition,None)
            elif status=="CLOSED_UNRESOLVED": research.record_resolution_error(ts=now,market=market,status=status)
        except Exception as exc:
            research.record_resolution_error(ts=now,market=market,status=f"ERROR:{type(exc).__name__}")
            log.warning("RESOLUTION ERROR | %s | %s",market.get("slug"),exc)

def report(books):
    global last_report
    now=time.time(); interval=float(os.getenv("REPORT_INTERVAL_SECONDS","60"))
    if now-last_report<interval:return
    last_report=now; metrics=ledger.mark(books); metrics["positions"]=len(ledger.positions); research.record_pnl(now,metrics)
    fs=fill_sim.stats(); log.info("P&L $%+.2f | cash=$%.2f | open=$%.2f | positions=%d | signals=%d filled=%d fill_rate=%.1f%% unfilled=%d",metrics["pnl"],metrics["cash"],metrics["open_cost"],metrics["positions"],fs["signals"],fs["filled"],100*fs["fill_rate"],fs["expired_unfilled"])

def main():
    global last_disc,next_trade_at,consecutive_errors
    startup_check(); log.info("BOT B | PAPER ONLY | V18 REALISTIC CLOB TRADE-TAPE FILL SIMULATION | V17 FULL-SCALE TRADER REPLICA 40PCT")
    if feed: feed.start()
    while True:
        try:
            now=time.time()
            if now-last_disc>=DISCOVERY_INTERVAL_SECONDS:
                for m in discover(): markets[m["condition"]]=m
                tokens=[]
                for condition,m in list(markets.items()):
                    if any(p.get("condition")==condition for p in ledger.positions.values()): resolution_markets[condition]=m
                    if m.get("end_ts",0)<now-30 and condition not in resolution_markets: markets.pop(condition,None)
                    else: tokens += [m.get("up"),m.get("down")]
                if feed: feed.set_tokens(tokens)
                last_disc=now
                log.info("MARKETS | active=%d resolution=%d pending_orders=%d",len(markets),len(resolution_markets),fill_sim.active_count())
            if FILL_ENABLED and FILL_EXPIRY_ENABLED: fill_sim.expire_due(now)
            resolve_pending(now)
            books={}; market_list=list(markets.values())
            if now>=next_trade_at:
                eligible=[]; cadence_due=next_trade_at
                scannable=[]
                for m in market_list:
                    elapsed=now-m["start_ts"]; left=m["end_ts"]-now
                    if left<=0 or elapsed<0 or elapsed>300 or not m.get("accepting_orders"): continue
                    scannable.append((m,elapsed,left))
                futures={BOOK_EXECUTOR.submit(book,t):(m,tname) for m in scannable for tname,t in (("up",m["up"]),("down",m["down"]))}
                mb={}
                for f in as_completed(futures):
                    m,tname=futures[f]
                    try: mb.setdefault(m["condition"],{})[tname]=f.result()
                    except Exception as exc: log.warning("BOOK ERROR | %s | %s",m.get("slug"),exc)
                for m,elapsed,left in scannable:
                    pair=mb.get(m["condition"],{});
                    if "up" not in pair or "down" not in pair: continue
                    ub,ua,ud,uad=pair["up"]; db,da,dd,dad=pair["down"]; books[m["up"]]=ub; books[m["down"]]=db
                    h=histories.setdefault(m["condition"],{"Up":[],"Down":[]})
                    if ub is not None:h["Up"].append((now,ub))
                    if db is not None:h["Down"].append((now,db))
                    prepare_histories(h,now)
                    if now-ob_last.get(m["condition"],0)>=float(os.getenv("ORDERBOOK_SAMPLE_SECONDS","1")):
                        research.record_orderbook(ts=now,market=m,elapsed=elapsed,left=left,up_bid=ub,up_ask=ua,up_depth=ud,down_bid=db,down_ask=da,down_depth=dd,up_ask_depth=uad,down_ask_depth=dad)
                        ob_last[m["condition"]]=now
                    st=market_entry_state(m["condition"],now)
                    for c in strategy.build_candidates_for_market(elapsed,ua,da,ub,db,h["Up"],h["Down"],now,asset=m["asset"],market=m["asset"],thesis_side=st["side"],market_entry_count=st["count"],seconds_since_first_entry=st["seconds_since_first"],up_depth=ud,down_depth=dd):
                        c.update(_market=m,_state=st,_elapsed=elapsed,_left=left,_up_ask=ua,_down_ask=da,_up_depth=ud,_down_depth=dd); eligible.append(c)
                if eligible:
                    target_band=strategy.choose_distribution_band(eligible)
                    if target_band is None and now-cadence_due>=CADENCE_FALLBACK_SECONDS: target_band=strategy.scheduler.choose_band(eligible,allow_over_quota=True)
                    band_candidates=[c for c in eligible if c["band"]==target_band]
                    if band_candidates:
                        best=max(band_candidates,key=lambda c:(c["trajectory_likelihood"],c["band_prior"],-c["bid"])); m=best["_market"]; st=best["_state"]
                        signal=strategy.choose_process_candidate([best],target_band)
                        if signal:
                            reserved=pending_reserved(); remaining=max(0,strategy.max_total_exposure-ledger.total_open_cost()-reserved); available=max(0,ledger.cash-reserved)
                            notion=min(float(signal["target"]),available,remaining)
                            if notion>=float(os.getenv("MIN_PAPER_FILL_USD","0.10")) and best["_left"]>strategy.hard_cutoff_seconds:
                                token=m["up"] if signal["side"]=="Up" else m["down"]; band,regime=strategy.fine_band(signal["bid"]); target=float(signal["bid"])
                                try: snap=book_snapshot(token); depth_ahead=next((size for price,size in snap["bids"] if abs(price-signal["bid"])<1e-9),0.0)
                                except Exception: depth_ahead=float(signal.get("depth") or 0.0)
                                meta={"slug":m["slug"],"asset":m["asset"],"market":m["market"],"start_ts":m["start_ts"],"end_ts":m["end_ts"],"market_id":m["id"],"up_token":m["up"],"down_token":m["down"],"model_version":strategy.VERSION,"entry_count_before":st["count"],"burst_position":st["burst_position"],"seconds_since_first_entry":st["seconds_since_first"],"seconds_since_previous_trade":st["seconds_since_previous"],"regime":regime,"fine_band":band,"execution_mode":"V18_TRADE_TAPE_QUEUE_PROXY","target_capital":target,"bid_size":signal.get("depth") or 0.0,"up_bid":best.get("bid") if best.get("side")=="Up" else None,"down_bid":best.get("bid") if best.get("side")=="Down" else None,"up_ask":best.get("ask") if best.get("side")=="Up" else None,"down_ask":best.get("ask") if best.get("side")=="Down" else None,"up_depth":best.get("depth") if best.get("side")=="Up" else None,"down_depth":best.get("depth") if best.get("side")=="Down" else None,"trajectory_likelihood":signal["trajectory_likelihood"],"movement":signal.get("movement")}
                                order=fill_sim.place(condition=m["condition"],token=token,market=m["market"],side=signal["side"],target_price=target,notional=notion,placed_ts=now,window_end_ts=float(m["end_ts"]),depth_ahead=depth_ahead,meta=meta) if FILL_ENABLED else None
                                research.record_instant_signal(order or {"order_id":"instant-disabled","condition":m["condition"],"token":token,"market":m["market"],"side":signal["side"],"target_price":target,"notional":notion,"placed_ts":now,"meta":meta})
                                strategy.observe_trade_distribution(band,notion); next_trade_at=now+max(0.0,strategy.cadence.sample_gap())
                                log.info("SIGNAL PENDING | %s | %s | target=%.4f notional=$%.2f depth_ahead=%.2f",m["asset"],signal["side"],target,notion,depth_ahead)
            report(books); time.sleep(LOOP_SECONDS); consecutive_errors=0
        except KeyboardInterrupt:
            if feed: feed.stop()
            raise
        except Exception as exc:
            consecutive_errors+=1; log.exception("LOOP ERROR #%d | %s",consecutive_errors,exc); time.sleep(min(10,LOOP_SECONDS*max(2,consecutive_errors)))

if __name__=="__main__": main()
