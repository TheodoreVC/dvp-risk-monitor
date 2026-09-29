"""The trade book and its reference data (counterparties, securities), loaded from CSV.

Trades carry both their contractual settlement date and the day they actually settled, so the book
can be replayed as of any date: a trade is open from its trade date until it settles, and failed
once it is still open at the end of its settlement date.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Side(StrEnum):
    """Our side of the trade. BUY: the counterparty delivers securities and we pay cash."""

    BUY = "buy"
    SELL = "sell"


class AssetType(StrEnum):
    EQUITY = "equity"
    BOND = "bond"


@dataclass(frozen=True)
class Security:
    security_id: str
    asset_type: AssetType
    description: str
    currency: str
    country: str  # where it settles: CA or US
    issuer_group: str  # corporate group of the issuer, for wrong-way risk
    ticker: str | None = None  # Yahoo Finance symbol (equities)
    coupon: float | None = None  # % a year, paid semi-annually (bonds)
    maturity: dt.date | None = None
    benchmark: str | None = None  # yield series, e.g. "valet:BD.CDN.5YR.DQ.YLD" or "fred:DGS5"
    spread_bp: float = 0.0  # yield spread over the benchmark

    @property
    def multiplier(self) -> float:
        """Value per unit of quantity per unit of price: shares at $/share, bond face at $/100."""
        return 1.0 if self.asset_type is AssetType.EQUITY else 0.01


@dataclass(frozen=True)
class Counterparty:
    counterparty_id: str
    name: str
    kind: str  # bank, insurer, pension, asset_manager, hedge_fund
    country: str
    rating: str  # internal rating, AAA to BB
    group: str  # corporate group, for wrong-way risk
    ticker: str | None = None  # listed parent, for news
    exchange: str | None = None


@dataclass(frozen=True)
class Trade:
    trade_id: str
    counterparty_id: str
    security_id: str
    side: Side
    quantity: float  # shares, or bond face amount
    trade_price: float  # per share, or clean price per 100 face
    trade_date: dt.date
    settle_date: dt.date  # contractual settlement date
    settled_on: dt.date | None = None  # when it actually settled; None = not yet

    def is_open(self, as_of: dt.date) -> bool:
        """Traded, and not settled by the end of `as_of`."""
        return self.trade_date <= as_of and (self.settled_on is None or self.settled_on > as_of)

    def is_failed(self, as_of: dt.date) -> bool:
        """Still open at the end of its settlement date, or later."""
        return self.is_open(as_of) and as_of >= self.settle_date


@dataclass(frozen=True)
class Book:
    securities: dict[str, Security]
    counterparties: dict[str, Counterparty]
    trades: list[Trade]

    def open_trades(self, as_of: dt.date) -> list[Trade]:
        return [t for t in self.trades if t.is_open(as_of)]

    @property
    def business_date(self) -> dt.date:
        """The latest trade date: the day the book is a snapshot of."""
        return max(t.trade_date for t in self.trades)


class BookError(ValueError):
    pass


def _rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            yield line, {k: (v or "").strip() for k, v in row.items()}


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _optional(value: str) -> str | None:
    return value or None


def load_securities(path: Path) -> dict[str, Security]:
    out: dict[str, Security] = {}
    for line, r in _rows(path):
        try:
            asset_type = AssetType(r["asset_type"])
            sec = Security(
                security_id=r["security_id"],
                asset_type=asset_type,
                description=r["description"],
                currency=r["currency"],
                country=r["country"],
                issuer_group=r["issuer_group"],
                ticker=_optional(r["ticker"]),
                coupon=float(r["coupon"]) if r["coupon"] else None,
                maturity=_date(r["maturity"]) if r["maturity"] else None,
                benchmark=_optional(r["benchmark"]),
                spread_bp=float(r["spread_bp"] or 0),
            )
        except (KeyError, ValueError) as e:
            raise BookError(f"{path.name} line {line}: {e}") from e
        if asset_type is AssetType.EQUITY and not sec.ticker:
            raise BookError(f"{path.name} line {line}: equity needs a ticker")
        if asset_type is AssetType.BOND and not (
            sec.coupon is not None and sec.maturity and sec.benchmark
        ):
            raise BookError(f"{path.name} line {line}: bond needs coupon, maturity and benchmark")
        if sec.security_id in out:
            raise BookError(f"{path.name} line {line}: duplicate security {sec.security_id}")
        out[sec.security_id] = sec
    return out


def load_counterparties(path: Path) -> dict[str, Counterparty]:
    out: dict[str, Counterparty] = {}
    for line, r in _rows(path):
        try:
            cpty = Counterparty(
                counterparty_id=r["counterparty_id"],
                name=r["name"],
                kind=r["kind"],
                country=r["country"],
                rating=r["rating"],
                group=r["group"],
                ticker=_optional(r["ticker"]),
                exchange=_optional(r["exchange"]),
            )
        except KeyError as e:
            raise BookError(f"{path.name} line {line}: missing column {e}") from e
        if cpty.counterparty_id in out:
            raise BookError(
                f"{path.name} line {line}: duplicate counterparty {cpty.counterparty_id}"
            )
        out[cpty.counterparty_id] = cpty
    return out


def load_trades(
    path: Path, securities: dict[str, Security], counterparties: dict[str, Counterparty]
) -> list[Trade]:
    trades: list[Trade] = []
    seen: set[str] = set()
    for line, r in _rows(path):
        try:
            trade = Trade(
                trade_id=r["trade_id"],
                counterparty_id=r["counterparty_id"],
                security_id=r["security_id"],
                side=Side(r["side"]),
                quantity=float(r["quantity"]),
                trade_price=float(r["trade_price"]),
                trade_date=_date(r["trade_date"]),
                settle_date=_date(r["settle_date"]),
                settled_on=_date(r["settled_on"]) if r["settled_on"] else None,
            )
        except (KeyError, ValueError) as e:
            raise BookError(f"{path.name} line {line}: {e}") from e
        problem = _trade_problem(trade, securities, counterparties, seen)
        if problem:
            raise BookError(f"{path.name} line {line}: {problem}")
        seen.add(trade.trade_id)
        trades.append(trade)
    return trades


def _trade_problem(
    t: Trade,
    securities: dict[str, Security],
    counterparties: dict[str, Counterparty],
    seen: set[str],
) -> str | None:
    if t.trade_id in seen:
        return f"duplicate trade {t.trade_id}"
    if t.security_id not in securities:
        return f"unknown security {t.security_id}"
    if t.counterparty_id not in counterparties:
        return f"unknown counterparty {t.counterparty_id}"
    if t.quantity <= 0 or t.trade_price <= 0:
        return "quantity and price must be positive"
    if t.settle_date < t.trade_date:
        return "settles before it trades"
    if t.settled_on is not None and t.settled_on < t.settle_date:
        return "settled before its settlement date"
    return None


def load_book(data_dir: Path) -> Book:
    securities = load_securities(data_dir / "securities.csv")
    counterparties = load_counterparties(data_dir / "counterparties.csv")
    trades = load_trades(data_dir / "trades.csv", securities, counterparties)
    if not trades:
        raise BookError("trades.csv has no trades")
    return Book(securities, counterparties, trades)
