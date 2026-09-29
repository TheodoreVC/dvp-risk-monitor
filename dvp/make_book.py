"""Generates the synthetic trade book (trades.csv) and sample counterparty news.

The book is a history of institutional trades settled trade-for-trade, not through a central
counterparty (which would take on the credit risk), over the last `days` business days. Trade
prices are real closes on the trade date plus a small execution difference. Most trades settle
on time (T+1, a few on extended settlement); a few fail and settle late; some are still failing.

A few situations are planted so every section of the report has something in it:
- Northshore Capital LP (BB hedge fund) sized to ~115% of its limit, with a legal event
- Harbourfront Asset Management (BBB asset manager) sized to ~88% (amber), with a leadership event
- fails aged 2, 7 (8% Basel factor) and 20 business days (50%), on the losing side of the market
- a sale of Royal Bank of Canada shares to Royal Bank of Canada (wrong-way)
The planted counterparties' open trades are scaled after the fact to hit their target
utilization, measured with the same engine as the report; everyone else is kept under 70%.
"""

import csv
import datetime as dt
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from dvp.bonds import clean_price
from dvp.book import (
    AssetType,
    Book,
    Counterparty,
    Security,
    Side,
    Trade,
    load_counterparties,
    load_securities,
)
from dvp.calendars import settlement_calendar
from dvp.config import Settings
from dvp.engine import measure_book
from dvp.limits import LimitGrid, aggregate, load_grid
from dvp.market import LiveMarketData, MarketData, fx_series_id, value_on

TRADES_PER_DAY = 24
BOND_SHARE = 0.25
HOME_MARKET_SHARE = 0.7
EXTENDED_SETTLEMENT_SHARE = 0.04  # T+3 by agreement
FAIL_RATE = 0.025
BOND_FACES = (1_000_000, 2_000_000, 5_000_000, 10_000_000, 25_000_000)
ACTIVITY = {"bank": 1.0, "insurer": 0.7, "pension": 0.8, "asset_manager": 1.2, "hedge_fund": 1.0}

NORTHSHORE, HARBOURFRONT, GRANITE_PEAK, LAKEVIEW, RBC = "C11", "C10", "C13", "C14", "C01"
TARGET_UTILIZATION = {NORTHSHORE: 1.15, HARBOURFRONT: 0.88}
MAX_OTHER_UTILIZATION = 0.70


def last_close(now: dt.datetime | None = None) -> dt.date:
    """The most recent completed TSX session."""
    now = now or dt.datetime.now(ZoneInfo("America/Toronto"))
    calendar = settlement_calendar("CA", "equity")
    today = now.date()
    if calendar.is_business_day(today) and now.time() >= dt.time(16, 30):
        return today
    return calendar.previous(today - dt.timedelta(days=1))


class _Generator:
    def __init__(
        self,
        securities: dict[str, Security],
        counterparties: dict[str, Counterparty],
        market: MarketData,
        as_of: dt.date,
        rng: np.random.Generator,
    ) -> None:
        self.securities = securities
        self.counterparties = counterparties
        self.market = market
        self.as_of = as_of
        self.rng = rng
        self.trades: list[Trade] = []

    def price_on(self, security: Security, day: dt.date) -> float:
        """Close on the day (equities), or the price at the benchmark yield + spread (bonds)."""
        if security.asset_type is AssetType.EQUITY:
            assert security.ticker
            return value_on(self.market.equity(security.ticker)["close"], day)[0]
        assert security.benchmark and security.coupon is not None and security.maturity
        bench, _ = value_on(self.market.series(security.benchmark), day)
        return clean_price(
            security.coupon, security.maturity, day, bench + security.spread_bp / 100
        )

    def fx_on(self, currency: str, day: dt.date) -> float:
        series_id = fx_series_id(currency)
        return 1.0 if series_id is None else value_on(self.market.series(series_id), day)[0]

    def add(
        self,
        counterparty: Counterparty,
        security: Security,
        side: Side,
        day: dt.date,
        value_cad: float | None = None,
        settle_days: int = 1,
        settled_on: dt.date | None | str = "auto",
    ) -> Trade:
        calendar = settlement_calendar(security.country, security.asset_type)
        close = self.price_on(security, day)
        if security.asset_type is AssetType.EQUITY:
            price = round(close * (1 + self.rng.normal(0, 0.004)), 2)
            if value_cad is None:
                value_cad = float(np.clip(self.rng.lognormal(np.log(1_500_000), 0.8), 2e5, 1.5e7))
            quantity = max(100.0, round(value_cad / self.fx_on(security.currency, day) / price, -2))
        else:
            price = round(close + self.rng.normal(0, 0.03), 3)
            if value_cad is None:
                quantity = float(self.rng.choice(BOND_FACES, p=[0.25, 0.3, 0.25, 0.15, 0.05]))
            else:
                face = value_cad / self.fx_on(security.currency, day) / (price / 100)
                quantity = max(100_000.0, round(face, -5))
        settle = calendar.add(day, settle_days)
        if settled_on == "auto":
            settled_on = self._settlement(calendar, settle)
        trade = Trade(
            trade_id=f"T{len(self.trades) + 1:05d}",
            counterparty_id=counterparty.counterparty_id,
            security_id=security.security_id,
            side=side,
            quantity=quantity,
            trade_price=price,
            trade_date=day,
            settle_date=settle,
            settled_on=settled_on,  # type: ignore[arg-type]
        )
        self.trades.append(trade)
        return trade

    def _settlement(self, calendar, settle: dt.date) -> dt.date | None:
        """On time, or late with a small probability; None if still open at the business date."""
        if settle > self.as_of:
            return None
        if self.rng.random() < FAIL_RATE:
            settled = calendar.add(settle, 1 + int(self.rng.geometric(0.4)))
            return None if settled > self.as_of else settled
        return settle

    def random_day(self, day: dt.date) -> None:
        cpty_list = list(self.counterparties.values())
        weights = np.array([ACTIVITY[c.kind] for c in cpty_list])
        for _ in range(self.rng.poisson(TRADES_PER_DAY)):
            counterparty = cpty_list[self.rng.choice(len(cpty_list), p=weights / weights.sum())]
            security = self.pick_security(counterparty)
            calendar = settlement_calendar(security.country, security.asset_type)
            if not calendar.is_business_day(day):
                continue
            side = Side.BUY if self.rng.random() < 0.5 else Side.SELL
            settle_days = 3 if self.rng.random() < EXTENDED_SETTLEMENT_SHARE else 1
            self.add(counterparty, security, side, day, settle_days=settle_days)

    def pick_security(self, counterparty: Counterparty) -> Security:
        asset_type = AssetType.BOND if self.rng.random() < BOND_SHARE else AssetType.EQUITY
        home = self.rng.random() < HOME_MARKET_SHARE
        pool = [
            s
            for s in self.securities.values()
            if s.asset_type is asset_type and (s.country == counterparty.country) == home
        ]
        return pool[self.rng.integers(len(pool))]

    def failing_trade(
        self, counterparty_id: str, security_id: str, age: int, value_cad: float | None = None
    ) -> None:
        """A trade `age` business days past its settlement date and still open, on the side that
        has lost value since it was traded (so it carries current exposure)."""
        security = self.securities[security_id]
        calendar = settlement_calendar(security.country, security.asset_type)
        settle = calendar.add(self.as_of, -age)
        day = calendar.add(settle, -1)
        side = (
            Side.BUY
            if self.price_on(security, self.as_of) > self.price_on(security, day)
            else Side.SELL
        )
        self.add(
            self.counterparties[counterparty_id],
            security,
            side,
            day,
            value_cad=value_cad,
            settled_on=None,
        )


def generate(data_dir: Path, as_of: dt.date | None, seed: int, days: int) -> Book:
    as_of = as_of or last_close()
    securities = load_securities(data_dir / "securities.csv")
    counterparties = load_counterparties(data_dir / "counterparties.csv")
    grid = load_grid(data_dir / "limit_grid.csv")
    market = LiveMarketData(as_of)
    gen = _Generator(securities, counterparties, market, as_of, np.random.default_rng(seed))

    calendar = settlement_calendar("CA", "equity")
    for back in range(days - 1, -1, -1):
        gen.random_day(calendar.add(as_of, -back))

    # Planted situations (see the module docstring).
    for security_id in ("SHOP", "CNQ", "UST-10Y", "BN"):
        gen.add(counterparties[NORTHSHORE], securities[security_id], Side.BUY, as_of)
    gen.failing_trade(HARBOURFRONT, "SHOP", age=7)
    gen.failing_trade(LAKEVIEW, "NVDA", age=20, value_cad=3_000_000)
    gen.failing_trade(GRANITE_PEAK, "UST-10Y", age=2)
    rbc = counterparties[RBC]
    gen.add(rbc, securities["RY"], Side.SELL, as_of, value_cad=0.5 * grid.limit_for(rbc))

    book = Book(securities, counterparties, gen.trades)
    book = _calibrate(book, market, grid, as_of, gen.rng)
    _write_trades(data_dir / "trades.csv", book.trades)
    _write_events(data_dir / "counterparty_events.csv", as_of)
    open_now = len(book.open_trades(as_of))
    print(
        f"Wrote {len(book.trades)} trades ({open_now} open on {as_of}) to {data_dir / 'trades.csv'}"
    )
    return book


def _calibrate(
    book: Book, market: MarketData, grid: LimitGrid, as_of: dt.date, rng: np.random.Generator
) -> Book:
    """Scale open trades so planted counterparties hit their target utilization, and anyone
    else above 70% comes down to somewhere between 30% and 60%. Every exposure is proportional
    to quantity, so one pass is enough (up to rounding of the quantities)."""
    settings = Settings()
    run = measure_book(book, market, as_of, settings)
    rows = aggregate(run.exposures, grid, set(), settings.amber_utilization)
    factors: dict[str, float] = {}
    for row in rows:
        cid = row.counterparty.counterparty_id
        if cid in TARGET_UTILIZATION and row.pfe > 0:
            factors[cid] = TARGET_UTILIZATION[cid] / row.utilization
        elif row.utilization > MAX_OTHER_UTILIZATION:
            factors[cid] = rng.uniform(0.3, 0.6) / row.utilization

    trades = []
    for t in book.trades:
        factor = factors.get(t.counterparty_id)
        if factor is None or not t.is_open(as_of):
            trades.append(t)
            continue
        lot = 100 if book.securities[t.security_id].asset_type is AssetType.EQUITY else 100_000
        trades.append(replace(t, quantity=max(lot, round(t.quantity * factor / lot) * lot)))
    return replace(book, trades=trades)


def _write_trades(path: Path, trades: list[Trade]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "trade_id",
                "counterparty_id",
                "security_id",
                "side",
                "quantity",
                "trade_price",
                "trade_date",
                "settle_date",
                "settled_on",
            ]
        )
        for t in sorted(trades, key=lambda t: (t.trade_date, t.trade_id)):
            writer.writerow(
                [
                    t.trade_id,
                    t.counterparty_id,
                    t.security_id,
                    t.side,
                    f"{t.quantity:.0f}",
                    t.trade_price,
                    t.trade_date.isoformat(),
                    t.settle_date.isoformat(),
                    t.settled_on.isoformat() if t.settled_on else "",
                ]
            )


def _write_events(path: Path, as_of: dt.date) -> None:
    """Sample news for the fictional counterparties (real ones come from Attention Alerts)."""
    events = [
        (
            NORTHSHORE,
            as_of - dt.timedelta(days=4),
            "legal",
            "A securities regulator opened a review of the fund's trade reporting.",
        ),
        (
            HARBOURFRONT,
            as_of - dt.timedelta(days=12),
            "leadership",
            "The chief investment officer resigned; an interim CIO was named.",
        ),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["counterparty_id", "date", "category", "summary", "source"])
        for cid, day, category, summary in events:
            writer.writerow([cid, day.isoformat(), category, summary, "sample (fictional)"])
