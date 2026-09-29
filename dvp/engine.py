"""Measures every open trade in the book as of a business date."""

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from dvp.bonds import clean_price
from dvp.book import AssetType, Book, Counterparty, Security, Trade
from dvp.calendars import settlement_calendar
from dvp.config import Settings
from dvp.exposure import (
    bond_add_on,
    equity_add_on,
    is_wrong_way,
    jump_to_default_loss,
    potential_future_exposure,
    replacement_cost,
)
from dvp.market import MarketData, MissingData, fx_series_id, history_through, value_on
from dvp.volatility import VolForecast, forecast

# Days of volatility forecast to compute per security: enough for any settlement horizon.
FORECAST_DAYS = 20


@dataclass(frozen=True)
class MarketInputs:
    """One security's market state at the end of the business date."""

    price: float  # per share, or clean price per 100 face
    price_date: dt.date
    vol: VolForecast  # of log returns (equities) or yield changes in % points (bonds)
    ytm: float | None = None  # bonds only, %


@dataclass(frozen=True)
class TradeExposure:
    trade: Trade
    security: Security
    counterparty: Counterparty
    failed: bool
    fail_age: int | None  # business days past the settlement date
    horizon_days: int  # business days of price risk left before settlement
    price: float
    price_date: dt.date
    fx: float  # CAD per unit of the trade currency
    value: float  # current market value of the trade, trade currency
    mtm: float  # signed replacement cost, trade currency (positive = a loss if they default)
    add_on: float  # adverse move over the horizon, trade currency
    pfe: float  # trade currency; for a wrong-way trade, at least the jump-to-default loss
    wrong_way: bool
    jump_to_default: float | None
    vol_model: str
    horizon_sigma: float

    @property
    def current_exposure(self) -> float:
        return max(0.0, self.mtm)

    @property
    def value_cad(self) -> float:
        return self.value * self.fx

    @property
    def mtm_cad(self) -> float:
        return self.mtm * self.fx

    @property
    def ce_cad(self) -> float:
        return self.current_exposure * self.fx

    @property
    def pfe_cad(self) -> float:
        return self.pfe * self.fx


@dataclass
class Run:
    as_of: dt.date
    exposures: list[TradeExposure]
    fx_rates: dict[str, tuple[float, dt.date]]
    vol_models: dict[str, str]  # security_id -> model used
    warnings: list[str] = field(default_factory=list)
    unmeasured: list[Trade] = field(default_factory=list)


def market_inputs(
    security: Security, market: MarketData, as_of: dt.date, settings: Settings
) -> MarketInputs:
    if security.asset_type is AssetType.EQUITY:
        assert security.ticker
        prices = history_through(market.equity(security.ticker), as_of)
        price, price_date = value_on(prices["close"], as_of)
        returns = np.diff(np.log(prices["adj_close"].to_numpy()))
        return MarketInputs(price, price_date, forecast(returns, FORECAST_DAYS, settings))
    assert security.benchmark and security.coupon is not None and security.maturity
    benchmark = history_through(market.series(security.benchmark), as_of).dropna()
    bench_yield, price_date = value_on(benchmark, as_of)
    ytm = bench_yield + security.spread_bp / 100
    price = clean_price(security.coupon, security.maturity, as_of, ytm)
    vol = forecast(np.diff(benchmark.to_numpy()), FORECAST_DAYS, settings)
    return MarketInputs(price, price_date, vol, ytm)


def measure_trade(
    trade: Trade,
    security: Security,
    counterparty: Counterparty,
    inputs: MarketInputs,
    fx: float,
    as_of: dt.date,
    settings: Settings,
) -> TradeExposure:
    calendar = settlement_calendar(security.country, security.asset_type)
    failed = trade.is_failed(as_of)
    if failed:
        fail_age: int | None = calendar.count(trade.settle_date, as_of)
        horizon = settings.fail_horizon_days
    else:
        fail_age = None
        horizon = max(settings.min_horizon_days, calendar.count(as_of, trade.settle_date))
    sigma = inputs.vol.horizon_sigma(horizon)

    value = trade.quantity * inputs.price * security.multiplier
    mtm = replacement_cost(
        trade.side, trade.quantity, trade.trade_price, inputs.price, security.multiplier
    )
    if security.asset_type is AssetType.EQUITY:
        add_on = equity_add_on(value, sigma, settings.z)
    else:
        assert security.coupon is not None and security.maturity and inputs.ytm is not None
        add_on = bond_add_on(
            trade.side,
            security.coupon,
            security.maturity,
            as_of,
            inputs.ytm,
            sigma,
            settings.z,
            trade.quantity,
        )
    pfe = potential_future_exposure(mtm, add_on)

    wrong_way = is_wrong_way(trade.side, security, counterparty)
    jtd = None
    if wrong_way:
        default_price = (
            inputs.price * settings.equity_recovery
            if security.asset_type is AssetType.EQUITY
            else 100 * settings.bond_recovery
        )
        jtd = jump_to_default_loss(
            trade.trade_price, trade.quantity, security.multiplier, default_price
        )
        pfe = max(pfe, jtd)

    return TradeExposure(
        trade=trade,
        security=security,
        counterparty=counterparty,
        failed=failed,
        fail_age=fail_age,
        horizon_days=horizon,
        price=inputs.price,
        price_date=inputs.price_date,
        fx=fx,
        value=value,
        mtm=mtm,
        add_on=add_on,
        pfe=pfe,
        wrong_way=wrong_way,
        jump_to_default=jtd,
        vol_model=inputs.vol.model,
        horizon_sigma=sigma,
    )


def measure_book(book: Book, market: MarketData, as_of: dt.date, settings: Settings) -> Run:
    """Every trade open at the end of `as_of`, marked with market data known by then.

    A trade that can't be priced isn't dropped silently: it's listed as unmeasured.
    """
    run = Run(as_of=as_of, exposures=[], fx_rates={}, vol_models={})
    inputs: dict[str, MarketInputs | None] = {}
    fx_rates: dict[str, float | None] = {}

    for trade in book.open_trades(as_of):
        security = book.securities[trade.security_id]
        if security.security_id not in inputs:
            inputs[security.security_id] = _inputs_or_warn(security, market, as_of, settings, run)
        if security.currency not in fx_rates:
            fx_rates[security.currency] = _fx_or_warn(security.currency, market, as_of, run)
        security_inputs = inputs[security.security_id]
        fx = fx_rates[security.currency]
        if security_inputs is None or fx is None:
            run.unmeasured.append(trade)
            continue
        counterparty = book.counterparties[trade.counterparty_id]
        run.exposures.append(
            measure_trade(trade, security, counterparty, security_inputs, fx, as_of, settings)
        )
    return run


def _inputs_or_warn(
    security: Security, market: MarketData, as_of: dt.date, settings: Settings, run: Run
) -> MarketInputs | None:
    try:
        result = market_inputs(security, market, as_of, settings)
    except (MissingData, ValueError) as e:
        run.warnings.append(f"{security.security_id}: no usable market data ({e})")
        return None
    run.vol_models[security.security_id] = result.vol.model
    if (as_of - result.price_date).days > settings.stale_price_days:
        run.warnings.append(
            f"{security.security_id}: stale price from {result.price_date.isoformat()}"
        )
    return result


def _fx_or_warn(currency: str, market: MarketData, as_of: dt.date, run: Run) -> float | None:
    series_id = fx_series_id(currency)
    if series_id is None:
        return 1.0
    try:
        rate, rate_date = value_on(market.series(series_id), as_of)
    except MissingData as e:
        run.warnings.append(f"{currency}: no FX rate ({e})")
        return None
    run.fx_rates[currency] = (rate, rate_date)
    return rate
