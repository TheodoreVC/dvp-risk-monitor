import datetime as dt

import pytest

from dvp.bonds import add_months, clean_price


def test_add_months_clamps_to_month_end():
    assert add_months(dt.date(2026, 8, 31), -6) == dt.date(2026, 2, 28)
    assert add_months(dt.date(2026, 3, 1), 12) == dt.date(2027, 3, 1)
    assert add_months(dt.date(2026, 1, 15), -1) == dt.date(2025, 12, 15)


def test_par_bond_on_a_coupon_date():
    assert clean_price(4.0, dt.date(2031, 9, 1), dt.date(2026, 9, 1), 4.0) == pytest.approx(100.0)


def test_par_bond_between_coupons_is_close_to_par():
    price = clean_price(4.0, dt.date(2031, 9, 1), dt.date(2026, 11, 17), 4.0)
    assert price == pytest.approx(100.0, abs=0.02)


def test_price_falls_as_yield_rises_and_longer_bonds_move_more():
    settle = dt.date(2026, 9, 28)
    short = [clean_price(3.5, dt.date(2028, 9, 1), settle, y) for y in (3.4, 3.5, 3.6)]
    long = [clean_price(3.5, dt.date(2036, 6, 1), settle, y) for y in (3.4, 3.5, 3.6)]
    assert short[0] > short[1] > short[2]
    assert long[0] - long[2] > 3 * (short[0] - short[2])


def test_matured_bond_raises():
    with pytest.raises(ValueError):
        clean_price(4.0, dt.date(2026, 9, 1), dt.date(2026, 9, 1), 4.0)
