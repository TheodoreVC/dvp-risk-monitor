"""Market data: equity closes (Yahoo Finance), government yields and FX (Bank of Canada, FRED).

Every series is cached under data/cache so a run can be reproduced and the sources aren't re-hit.
The engine only sees the MarketData protocol, so a source can be swapped out; tests use a fake.
"""

import datetime as dt
import io
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from dvp.config import CACHE_DIR, REPORT_CURRENCY

USER_AGENT = "dvp-risk-monitor/0.1"
VALET_URL = "https://www.bankofcanada.ca/valet/observations/{series}/json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
EASTERN = ZoneInfo("America/Toronto")


class MissingData(LookupError):
    pass


class MarketData(Protocol):
    def equity(self, ticker: str) -> pd.DataFrame:
        """Daily 'close' (as traded) and 'adj_close' (adjusted for dividends and splits, for
        returns), indexed by date."""
        ...

    def series(self, series_id: str) -> pd.Series:
        """A daily series indexed by date: 'valet:<id>' (Bank of Canada) or 'fred:<id>'."""
        ...


def fx_series_id(currency: str) -> str | None:
    """Bank of Canada daily rate: CAD per one unit of `currency` (None for CAD itself)."""
    return None if currency == REPORT_CURRENCY else f"valet:FX{currency}{REPORT_CURRENCY}"


def history_through(data: pd.Series | pd.DataFrame, as_of: dt.date) -> pd.Series | pd.DataFrame:
    """Only what was known by the end of `as_of`: no lookahead."""
    return data.loc[: pd.Timestamp(as_of)]


def value_on(series: pd.Series, as_of: dt.date) -> tuple[float, dt.date]:
    """The latest value on or before `as_of`, and its date."""
    known = history_through(series, as_of).dropna()
    if known.empty:
        raise MissingData(f"no data on or before {as_of}")
    return float(known.iloc[-1]), known.index[-1].date()


class LiveMarketData:
    """Fetches from the public sources and caches each series as parquet.

    A cached series is re-fetched when it ends before the business date and hasn't already been
    fetched today (or always, with refresh=True).
    """

    def __init__(
        self,
        as_of: dt.date,
        cache_dir: Path = CACHE_DIR,
        start: dt.date = dt.date(2020, 1, 1),
        refresh: bool = False,
    ) -> None:
        self.as_of = as_of
        self.cache_dir = cache_dir
        self.start = start
        self.refresh = refresh
        self._memory: dict[str, pd.DataFrame] = {}

    def equity(self, ticker: str) -> pd.DataFrame:
        return self._cached(f"yahoo_{ticker}", lambda: _fetch_yahoo(ticker, self.start))

    def series(self, series_id: str) -> pd.Series:
        source, _, sid = series_id.partition(":")
        fetchers = {"valet": _fetch_valet, "fred": _fetch_fred}
        if source not in fetchers or not sid:
            raise ValueError(f"unknown series {series_id!r}; use valet:<id> or fred:<id>")
        frame = self._cached(
            f"{source}_{sid}", lambda: fetchers[source](sid, self.start).to_frame("value")
        )
        return frame["value"]

    def _cached(self, name: str, fetch: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        if name in self._memory:
            return self._memory[name]
        path = self.cache_dir / f"{name.replace('^', '_').replace('=', '_')}.parquet"
        data = pd.read_parquet(path) if path.exists() else None
        if data is None or self.refresh or self._behind(data, path):
            try:
                data = fetch()
            except MissingData:
                raise
            except Exception as e:  # any source failure: this series is missing, the run goes on
                raise MissingData(f"{name}: {e}") from e
            if data.empty:
                raise MissingData(f"{name}: source returned no data")
            path.parent.mkdir(parents=True, exist_ok=True)
            data.to_parquet(path)
        self._memory[name] = data
        return data

    def _behind(self, data: pd.DataFrame, path: Path) -> bool:
        fetched_on = dt.datetime.fromtimestamp(path.stat().st_mtime).date()
        return data.index[-1].date() < self.as_of and fetched_on < dt.date.today()


def _get(url: str, params: dict[str, str], user_agent: str | None = USER_AGENT) -> httpx.Response:
    """GET with a timeout, retrying transient failures with exponential backoff."""
    error: Exception | None = None
    for attempt in range(4):
        if attempt:
            time.sleep(2**attempt)
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=30,
                headers={"User-Agent": user_agent} if user_agent else None,
                follow_redirects=True,
            )
        except httpx.TransportError as e:
            error = e
            continue
        if response.status_code == 429 or response.status_code >= 500:
            error = httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )
            continue
        response.raise_for_status()
        return response
    raise MissingData(f"{url}: {error}")


def _fetch_yahoo(ticker: str, start: dt.date) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        ticker, start=start.isoformat(), auto_adjust=False, progress=False, multi_level_index=False
    )
    if raw is None or raw.empty:
        raise MissingData(f"Yahoo Finance returned nothing for {ticker}")
    out = raw[["Close", "Adj Close"]].rename(columns={"Close": "close", "Adj Close": "adj_close"})
    out.index = pd.DatetimeIndex(raw.index).tz_localize(None).normalize()
    return _completed_sessions(out.dropna())


def _completed_sessions(data: pd.DataFrame) -> pd.DataFrame:
    """Drop today's bar while the market is still open: it's a live price, not a close."""
    now = dt.datetime.now(EASTERN)
    if now.time() < dt.time(16, 30):
        data = data.loc[data.index < pd.Timestamp(now.date())]
    return data


def _fetch_valet(series: str, start: dt.date) -> pd.Series:
    response = _get(VALET_URL.format(series=series), {"start_date": start.isoformat()})
    values = {
        pd.Timestamp(o["d"]): float(o[series]["v"])
        for o in response.json()["observations"]
        if o.get(series, {}).get("v") not in (None, "")
    }
    return pd.Series(values, dtype=float).sort_index()


def _fetch_fred(series: str, start: dt.date) -> pd.Series:
    # FRED's CSV endpoint stalls requests with an unfamiliar User-Agent; httpx's default works.
    response = _get(FRED_URL, {"id": series, "cosd": start.isoformat()}, user_agent=None)
    frame = pd.read_csv(io.StringIO(response.text), na_values=["."])
    values = pd.Series(
        frame.iloc[:, 1].to_numpy(dtype=float), index=pd.to_datetime(frame.iloc[:, 0])
    )
    return values.dropna().sort_index()
