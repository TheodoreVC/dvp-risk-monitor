import datetime as dt

import numpy as np
import pandas as pd
import pytest

from dvp.book import AssetType, Book, Counterparty, Security, Side, Trade
from dvp.market import MissingData


class FakeMarketData:
    """In-memory market data for tests: no network."""

    def __init__(
        self,
        equities: dict[str, pd.DataFrame] | None = None,
        series: dict[str, pd.Series] | None = None,
    ) -> None:
        self._equities = equities or {}
        self._series = series or {}

    def equity(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._equities:
            raise MissingData(ticker)
        return self._equities[ticker]

    def series(self, series_id: str) -> pd.Series:
        if series_id not in self._series:
            raise MissingData(series_id)
        return self._series[series_id]


def flat_prices(
    end: dt.date, price: float, days: int = 400, daily_vol: float = 0.01, seed: int = 0
):
    """Prices ending exactly at `price` on `end`, with i.i.d. returns of the given volatility."""
    index = pd.bdate_range(end=end, periods=days)
    rng = np.random.default_rng(seed)
    log_path = np.cumsum(rng.normal(0, daily_vol, days))
    closes = price * np.exp(log_path - log_path[-1])
    return pd.DataFrame({"close": closes, "adj_close": closes}, index=index)


def constant_series(end: dt.date, value: float, days: int = 400, daily_sd: float = 0.0, seed=1):
    index = pd.bdate_range(end=end, periods=days)
    rng = np.random.default_rng(seed)
    walk = np.cumsum(rng.normal(0, daily_sd, days)) if daily_sd else np.zeros(days)
    return pd.Series(value + walk - walk[-1], index=index)


AS_OF = dt.date(2026, 9, 28)  # a Monday


@pytest.fixture
def securities() -> dict[str, Security]:
    return {
        "RY": Security("RY", AssetType.EQUITY, "Royal Bank", "CAD", "CA", "RBC", ticker="RY.TO"),
        "AAPL": Security("AAPL", AssetType.EQUITY, "Apple", "USD", "US", "APPLE", ticker="AAPL"),
        "GOC-5Y": Security(
            "GOC-5Y",
            AssetType.BOND,
            "Canada 3.5% 2031",
            "CAD",
            "CA",
            "GOC",
            coupon=3.5,
            maturity=dt.date(2031, 9, 1),
            benchmark="valet:BD.CDN.5YR.DQ.YLD",
        ),
    }


@pytest.fixture
def counterparties() -> dict[str, Counterparty]:
    return {
        "C01": Counterparty("C01", "Royal Bank of Canada", "bank", "CA", "AA", "RBC", "RY", "TSX"),
        "C11": Counterparty("C11", "Northshore Capital LP", "hedge_fund", "CA", "BB", "NORTHSHORE"),
    }


def trade(**overrides) -> Trade:
    fields = dict(
        trade_id="T1",
        counterparty_id="C11",
        security_id="RY",
        side=Side.BUY,
        quantity=10_000,
        trade_price=50.0,
        trade_date=AS_OF,
        settle_date=dt.date(2026, 9, 29),
        settled_on=None,
    )
    fields.update(overrides)
    return Trade(**fields)


@pytest.fixture
def make_book(securities, counterparties):
    def _make(*trades: Trade) -> Book:
        return Book(securities, counterparties, list(trades))

    return _make
