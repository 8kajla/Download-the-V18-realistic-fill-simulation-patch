import tempfile
from pathlib import Path

from paper_ledger import PaperLedger
from research_logger import ResearchLogger


def market():
    return {
        "id": "m1", "condition": "c1", "slug": "btc-updown-5m-1000000000",
        "asset": "BTC", "market": "BTC Up or Down", "start_ts": 1000000000.0,
        "end_ts": 1000000300.0,
    }


def test_settle_returns_dicts_and_research_logger_handles_them():
    with tempfile.TemporaryDirectory() as td:
        ledger = PaperLedger(Path(td) / "paper_state.json", 1000)
        m = market()
        ledger.buy(
            "c1", "up-token", m["market"], "Up", 0.20, 1.0, 1000000010,
            meta={"asset":"BTC", "slug":m["slug"], "market_id":m["id"], "start_ts":m["start_ts"], "end_ts":m["end_ts"]}
        )
        closed = ledger.settle("c1", "up-token")
        assert len(closed) == 1
        assert isinstance(closed[0], dict)
        assert closed[0]["pnl"] == 4.0

        logger = ResearchLogger(td)
        logger.rebuild_from_ledger(ledger)
        logger.record_resolution(ts=1000000302, market=m, winner="Up", winner_token="up-token", closed=closed)
        text = (Path(td) / "resolutions.csv").read_text()
        assert "RESOLVED" in text
        assert ",4.0," in text


def test_losing_settlement_is_negative_and_logged():
    with tempfile.TemporaryDirectory() as td:
        ledger = PaperLedger(Path(td) / "paper_state.json", 1000)
        m = market()
        ledger.buy(
            "c1", "down-token", m["market"], "Down", 0.80, 2.0, 1000000010,
            meta={"asset":"BTC", "slug":m["slug"], "market_id":m["id"], "start_ts":m["start_ts"], "end_ts":m["end_ts"]}
        )
        closed = ledger.settle("c1", "up-token")
        assert len(closed) == 1
        assert closed[0]["settlement_per_share"] == 0.0
        assert closed[0]["pnl"] == -2.0

        logger = ResearchLogger(td)
        logger.rebuild_from_ledger(ledger)
        logger.record_resolution(ts=1000000302, market=m, winner="Up", winner_token="up-token", closed=closed)
        text = (Path(td) / "resolutions.csv").read_text()
        assert "RESOLVED" in text
