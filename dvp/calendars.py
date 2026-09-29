"""Settlement calendars: which days count as business days in each settlement location.

Settlement dates (T+1) and the age of a fail are counted in business days of the market where the
trade settles. Canadian trades use the TSX calendar (CDS follows it). US equities use the NYSE
calendar (DTC); US bonds use the federal holidays plus Good Friday, close to SIFMA's recommended
bond market closures (the bond market is shut on Columbus Day and Veterans Day, NYSE is not).
"""

import datetime as dt
from collections.abc import Iterable
from functools import cache

import exchange_calendars as xcals
import numpy as np
import pandas as pd
from pandas.tseries.holiday import GoodFriday, USFederalHolidayCalendar


class BusinessCalendar:
    """Business days for one settlement location: weekdays that aren't holidays."""

    def __init__(self, name: str, holidays: Iterable[dt.date]) -> None:
        self.name = name
        self._holidays = np.array(sorted(set(holidays)), dtype="datetime64[D]")

    def is_business_day(self, day: dt.date) -> bool:
        return bool(np.is_busday(np.datetime64(day, "D"), holidays=self._holidays))

    def add(self, day: dt.date, n: int) -> dt.date:
        """The business day `n` business days after `day` (T+n)."""
        out = np.busday_offset(np.datetime64(day, "D"), n, roll="forward", holidays=self._holidays)
        return out.astype(dt.date)

    def previous(self, day: dt.date) -> dt.date:
        """`day` if it's a business day, else the business day before it."""
        out = np.busday_offset(np.datetime64(day, "D"), 0, roll="backward", holidays=self._holidays)
        return out.astype(dt.date)

    def count(self, start: dt.date, end: dt.date) -> int:
        """Business days in (start, end]; negative when end is before start."""
        one = np.timedelta64(1, "D")
        return int(
            np.busday_count(
                np.datetime64(start, "D") + one,
                np.datetime64(end, "D") + one,
                holidays=self._holidays,
            )
        )


@cache
def _exchange_holidays(code: str) -> tuple[dt.date, ...]:
    """Weekdays the exchange was (or will be) closed, over the range exchange_calendars covers."""
    sessions = xcals.get_calendar(code).sessions
    weekdays = pd.bdate_range(sessions[0], sessions[-1])
    return tuple(d.date() for d in weekdays.difference(sessions))


@cache
def _us_bond_holidays() -> tuple[dt.date, ...]:
    start, end = dt.date(2000, 1, 1), dt.date(2040, 12, 31)
    federal = USFederalHolidayCalendar().holidays(start, end)
    good_fridays = GoodFriday.dates(start, end)
    return tuple(d.date() for d in federal.union(good_fridays))


@cache
def settlement_calendar(country: str, asset_type: str) -> BusinessCalendar:
    if country == "CA":
        return BusinessCalendar("TSX / CDS", _exchange_holidays("XTSE"))
    if country == "US" and asset_type == "bond":
        return BusinessCalendar("US bond market", _us_bond_holidays())
    if country == "US":
        return BusinessCalendar("NYSE / DTC", _exchange_holidays("XNYS"))
    raise ValueError(f"no settlement calendar for {country} {asset_type}")
