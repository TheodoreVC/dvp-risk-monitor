"""Fixed-coupon bond prices from yields, for marking bond trades.

Yahoo Finance has no prices for individual bonds, so a bond is priced off its benchmark government
yield plus a fixed spread, and moved with that yield.
"""

import calendar
import datetime as dt


def add_months(day: dt.date, months: int) -> dt.date:
    """Same day of the month `months` later (or earlier), clamped to the month's last day."""
    years, month0 = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month0 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def clean_price(
    coupon: float, maturity: dt.date, settle: dt.date, ytm: float, freq: int = 2
) -> float:
    """Clean price per 100 face. `coupon` and `ytm` are % a year, compounded `freq` times a year.

    Street convention: each cash flow is discounted over whole coupon periods plus the fraction of
    the current period still to run; interest accrued since the last coupon (actual/actual) is
    subtracted, since it's paid separately at settlement.
    """
    if settle >= maturity:
        raise ValueError("bond has matured")
    step = 12 // freq
    # Coupon dates count back from maturity; k is the number of coupons after the next one.
    k = 0
    while add_months(maturity, -step * (k + 1)) > settle:
        k += 1
    next_coupon = add_months(maturity, -step * k)
    last_coupon = add_months(maturity, -step * (k + 1))
    w = (next_coupon - settle).days / (next_coupon - last_coupon).days
    c = coupon / freq
    y = ytm / 100 / freq
    n = k + 1
    dirty = sum(c / (1 + y) ** (i + w) for i in range(n)) + 100 / (1 + y) ** (n - 1 + w)
    accrued = c * (1 - w)
    return dirty - accrued
