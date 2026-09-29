"""End to end on a small book with fake market data: trade exposures, limits, fails, report."""

import datetime as dt

import pytest

from dvp.book import Side
from dvp.config import Settings
from dvp.engine import measure_book
from dvp.fails import aged_fails
from dvp.limits import LimitGrid, LimitStatus, aggregate
from dvp.report import build_report, render
from tests.conftest import AS_OF, FakeMarketData, constant_series, flat_prices, trade


@pytest.fixture
def market():
    return FakeMarketData(
        equities={
            "RY.TO": flat_prices(AS_OF, 52.0, daily_vol=0.01),
            "AAPL": flat_prices(AS_OF, 200.0, daily_vol=0.02, seed=3),
        },
        series={
            "valet:FXUSDCAD": constant_series(AS_OF, 1.40),
            "valet:BD.CDN.5YR.DQ.YLD": constant_series(AS_OF, 3.6, daily_sd=0.05),
        },
    )


def test_pending_equity_trade(make_book, market):
    book = make_book(trade(side=Side.BUY, quantity=10_000, trade_price=50.0))
    run = measure_book(book, market, AS_OF, Settings())
    [e] = run.exposures
    assert not e.failed and e.horizon_days == 1
    assert e.mtm == pytest.approx(20_000)  # the worked example
    assert e.value == pytest.approx(520_000)
    assert e.add_on == pytest.approx(1.645 * e.horizon_sigma * 520_000)
    assert e.pfe == pytest.approx(e.mtm + e.add_on)
    assert 0.005 < e.horizon_sigma < 0.02


def test_price_is_the_close_on_the_business_date_not_later(make_book, market):
    earlier = AS_OF - dt.timedelta(days=7)
    book = make_book(trade(trade_date=earlier, settle_date=AS_OF))
    run = measure_book(book, market, earlier, Settings())
    [e] = run.exposures
    assert e.price_date <= earlier


def test_failed_trade_uses_the_fail_horizon(make_book, market):
    book = make_book(trade(trade_date=dt.date(2026, 9, 17), settle_date=dt.date(2026, 9, 18)))
    run = measure_book(book, market, AS_OF, Settings())
    [e] = run.exposures
    assert e.failed and e.fail_age == 6 and e.horizon_days == 5
    [fail] = aged_fails(run.exposures)
    assert fail.factor == 0.08
    assert fail.capital_charge == pytest.approx(0.08 * 20_000)


def test_settled_trades_are_not_in_the_book(make_book, market):
    book = make_book(
        trade(trade_date=dt.date(2026, 9, 24), settle_date=dt.date(2026, 9, 25),
              settled_on=dt.date(2026, 9, 25)),
    )  # fmt: skip
    assert measure_book(book, market, AS_OF, Settings()).exposures == []


def test_usd_trade_is_converted_to_cad(make_book, market):
    book = make_book(trade(security_id="AAPL", quantity=1_000, trade_price=190.0))
    [e] = measure_book(book, market, AS_OF, Settings()).exposures
    assert e.fx == 1.40
    assert e.mtm_cad == pytest.approx(10_000 * 1.40)


def test_bond_trade(make_book, market):
    book = make_book(trade(security_id="GOC-5Y", quantity=10_000_000, trade_price=99.0))
    [e] = measure_book(book, market, AS_OF, Settings()).exposures
    assert e.value == pytest.approx(e.price * 100_000)
    assert e.mtm == pytest.approx((e.price - 99.0) * 100_000)
    assert e.add_on > 0


def test_wrong_way_trade_takes_jump_to_default(make_book, market):
    book = make_book(trade(counterparty_id="C01", side=Side.SELL, quantity=1_000, trade_price=52.0))
    [e] = measure_book(book, market, AS_OF, Settings()).exposures
    assert e.wrong_way
    assert e.jump_to_default == pytest.approx(52_000)  # the shares go to zero on default
    assert e.pfe == pytest.approx(52_000)


def test_missing_market_data_is_reported_not_dropped(make_book):
    book = make_book(trade())
    run = measure_book(book, FakeMarketData(), AS_OF, Settings())
    assert run.exposures == [] and [t.trade_id for t in run.unmeasured] == ["T1"]


def test_stale_price_warning(make_book):
    stale = FakeMarketData(equities={"RY.TO": flat_prices(AS_OF - dt.timedelta(days=10), 52.0)})
    run = measure_book(make_book(trade()), stale, AS_OF, Settings())
    assert any("stale" in w for w in run.warnings)


def test_limits_and_report(make_book, market):
    book = make_book(
        trade(trade_id="T1", quantity=10_000, trade_price=50.0),
        trade(trade_id="T2", quantity=2_000, trade_price=52.0),
        trade(trade_id="T3", counterparty_id="C01", quantity=100, trade_price=52.0),
    )
    run = measure_book(book, market, AS_OF, Settings())
    grid = LimitGrid({("BB", "hedge_fund"): 20_000, ("AA", "bank"): 4_000_000})
    rows = aggregate(run.exposures, grid, {"C11"}, 0.8)
    northshore = next(r for r in rows if r.counterparty.counterparty_id == "C11")
    assert northshore.trades == 2
    assert northshore.pfe == pytest.approx(sum(e.pfe for e in run.exposures[:2]))
    assert northshore.status is LimitStatus.EXCESS and northshore.on_watch
    assert rows[0] is northshore  # highest utilization first

    history = [
        {"business_date": "2026-09-24", "counterparty_id": "C11", "status": "OK"},
        {"business_date": "2026-09-25", "counterparty_id": "C11", "status": "EXCESS"},
    ]
    report = build_report(run, grid, [], history, Settings(), dt.datetime(2026, 9, 29, 12))
    assert report.excess_since["C11"] == dt.date(2026, 9, 25)
    text = render(report)
    assert "EXCESS Northshore Capital LP" in text and "since 2026-09-25" in text
