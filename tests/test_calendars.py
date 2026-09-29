import datetime as dt

from dvp.calendars import settlement_calendar

CA = settlement_calendar("CA", "equity")
US_EQ = settlement_calendar("US", "equity")
US_BOND = settlement_calendar("US", "bond")


def test_t_plus_one_skips_the_weekend():
    assert CA.add(dt.date(2026, 9, 25), 1) == dt.date(2026, 9, 28)  # Friday -> Monday


def test_canadian_thanksgiving_delays_canadian_but_not_us_equity_settlement():
    friday = dt.date(2026, 10, 9)  # Monday 2026-10-12 is Thanksgiving (CA) / Columbus Day (US)
    assert CA.add(friday, 1) == dt.date(2026, 10, 13)
    assert US_EQ.add(friday, 1) == dt.date(2026, 10, 12)  # NYSE is open on Columbus Day
    assert US_BOND.add(friday, 1) == dt.date(2026, 10, 13)  # the bond market is not


def test_count_is_business_days_in_half_open_interval():
    assert CA.count(dt.date(2026, 9, 28), dt.date(2026, 9, 29)) == 1
    assert CA.count(dt.date(2026, 9, 25), dt.date(2026, 9, 28)) == 1  # over a weekend
    assert CA.count(dt.date(2026, 9, 28), dt.date(2026, 9, 28)) == 0
    assert CA.count(dt.date(2026, 9, 29), dt.date(2026, 9, 28)) == -1


def test_previous_rolls_back_from_a_weekend():
    assert CA.previous(dt.date(2026, 9, 27)) == dt.date(2026, 9, 25)
    assert CA.previous(dt.date(2026, 9, 28)) == dt.date(2026, 9, 28)
