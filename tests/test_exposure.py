import datetime as dt

import pytest

from dvp.bonds import clean_price
from dvp.book import Side
from dvp.exposure import (
    bond_add_on,
    equity_add_on,
    is_wrong_way,
    jump_to_default_loss,
    potential_future_exposure,
    replacement_cost,
)


def test_worked_example_replacement_cost_not_principal():
    # Buy 10,000 shares at $50 and the price rises to $52 before settlement: if the seller
    # defaults we buy elsewhere for $20,000 more. The $500,000 principal is never at risk.
    assert replacement_cost(Side.BUY, 10_000, 50.0, 52.0) == pytest.approx(20_000)


@pytest.mark.parametrize(
    ("side", "price", "expected"),
    [
        (Side.BUY, 52.0, 20_000),  # buyer loses when the price rises
        (Side.BUY, 48.0, -20_000),  # ...and gains when it falls
        (Side.SELL, 48.0, 20_000),  # seller loses when the price falls
        (Side.SELL, 52.0, -20_000),
    ],
)
def test_replacement_cost_sign_depends_on_side(side, price, expected):
    assert replacement_cost(side, 10_000, 50.0, price) == pytest.approx(expected)


def test_bond_replacement_cost_uses_price_per_100():
    # 5,000,000 face, price up from 99.00 to 99.50: 0.5% of face.
    assert replacement_cost(Side.BUY, 5_000_000, 99.0, 99.5, 0.01) == pytest.approx(25_000)


def test_pfe_uses_the_cushion_of_a_trade_in_our_favour():
    assert potential_future_exposure(mtm=0.0, add_on=10_000) == 10_000
    assert potential_future_exposure(mtm=-4_000, add_on=10_000) == 6_000
    assert potential_future_exposure(mtm=-15_000, add_on=10_000) == 0.0
    assert potential_future_exposure(mtm=5_000, add_on=10_000) == 15_000


def test_equity_add_on_is_z_sigma_value():
    # $520,000 position, 1.5% one-day volatility, 95%: 1.645 x 0.015 x 520,000
    assert equity_add_on(520_000, 0.015, 1.645) == pytest.approx(12_831)


def test_bond_add_on_direction_and_size():
    as_of, maturity = dt.date(2026, 9, 28), dt.date(2031, 9, 1)
    buy = bond_add_on(Side.BUY, 3.5, maturity, as_of, 3.6, 0.05, 1.645, 10_000_000)
    sell = bond_add_on(Side.SELL, 3.5, maturity, as_of, 3.6, 0.05, 1.645, 10_000_000)
    # About modified duration (~4.5) x 8.2bp x face; a buyer's loss (yields down) is a little
    # bigger than a seller's (yields up) because of convexity.
    assert 30_000 < sell < buy < 42_000
    price_up = clean_price(3.5, maturity, as_of, 3.6 - 1.645 * 0.05)
    assert buy == pytest.approx((price_up - clean_price(3.5, maturity, as_of, 3.6)) * 100_000)


def test_wrong_way_only_when_selling_the_counterpartys_own_securities(securities, counterparties):
    rbc, northshore = counterparties["C01"], counterparties["C11"]
    ry = securities["RY"]
    assert is_wrong_way(Side.SELL, ry, rbc)
    assert not is_wrong_way(Side.BUY, ry, rbc)  # right-way: cheaper to replace if RBC defaults
    assert not is_wrong_way(Side.SELL, ry, northshore)


def test_jump_to_default_loss():
    assert jump_to_default_loss(80.0, 1_000, 1.0, 0.0) == 80_000
    assert jump_to_default_loss(99.0, 1_000_000, 0.01, 40.0) == pytest.approx(590_000)
