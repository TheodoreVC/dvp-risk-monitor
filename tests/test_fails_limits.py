import math

import pytest

from dvp.book import Counterparty
from dvp.fails import basel_factor, bucket_label
from dvp.limits import LimitGrid, LimitStatus, limit_status, load_grid


@pytest.mark.parametrize(
    ("age", "factor", "label"),
    [
        (0, 0.0, "0-4"),
        (4, 0.0, "0-4"),
        (5, 0.08, "5-15"),
        (15, 0.08, "5-15"),
        (16, 0.50, "16-30"),
        (30, 0.50, "16-30"),
        (31, 0.75, "31-45"),
        (45, 0.75, "31-45"),
        (46, 1.00, "46+"),
        (200, 1.00, "46+"),
    ],
)
def test_basel_failed_trade_buckets(age, factor, label):
    assert basel_factor(age) == factor
    assert bucket_label(age) == label


def test_limit_status_thresholds():
    assert limit_status(79_999, 100_000, 0.8) is LimitStatus.OK
    assert limit_status(80_000, 100_000, 0.8) is LimitStatus.AMBER
    assert limit_status(100_000, 100_000, 0.8) is LimitStatus.EXCESS
    assert limit_status(1, 0, 0.8) is LimitStatus.EXCESS  # no limit: any exposure is an excess
    assert limit_status(0, 0, 0.8) is LimitStatus.OK


def test_grid_lookup_by_rating_and_kind(tmp_path):
    path = tmp_path / "grid.csv"
    path.write_text("rating,bank,hedge_fund\nAA,4000000,1000000\nBB,500000,\n", encoding="utf-8")
    grid = load_grid(path)
    bank = Counterparty("X", "X", "bank", "CA", "BB", "X")
    fund = Counterparty("Y", "Y", "hedge_fund", "CA", "BB", "Y")
    assert grid.limit_for(bank) == 500_000
    assert grid.limit_for(fund) == 0.0  # blank cell: no appetite
    assert grid.ratings == ["AA", "BB"]


def test_zero_limit_utilization_is_infinite():
    from dvp.limits import CounterpartyExposure

    cpty = Counterparty("Y", "Y", "hedge_fund", "CA", "BB", "Y")
    row = CounterpartyExposure(cpty, 1, 0, 0, 1e6, 0.0, 5_000, 0.0, LimitStatus.EXCESS, False)
    assert math.isinf(row.utilization)
    assert row.excess == 5_000
    assert LimitGrid({}).limit_for(cpty) == 0.0
