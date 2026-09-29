"""Grid-based credit limits.

Each counterparty's pre-settlement limit is read off a grid by its internal rating and its type
(a BB hedge fund gets far less than a BB bank). Its total exposure is then checked against the
limit: amber from 80% utilization as an early warning, an excess from 100%.
"""

import csv
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dvp.book import Counterparty
from dvp.engine import TradeExposure


class LimitStatus(StrEnum):
    OK = "OK"
    AMBER = "AMBER"
    EXCESS = "EXCESS"


@dataclass(frozen=True)
class LimitGrid:
    limits: dict[tuple[str, str], float]  # (rating, counterparty kind) -> CAD

    def limit_for(self, counterparty: Counterparty) -> float:
        """A blank grid cell means no appetite for that rating and type: a limit of zero,
        so any exposure is an excess."""
        return self.limits.get((counterparty.rating, counterparty.kind), 0.0)

    @property
    def ratings(self) -> list[str]:
        return list(dict.fromkeys(rating for rating, _ in self.limits))


def load_grid(path: Path) -> LimitGrid:
    """CSV with one row per rating and one column per counterparty kind, in CAD."""
    limits: dict[tuple[str, str], float] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rating = row.pop("rating").strip()
            for kind, amount in row.items():
                if amount and amount.strip():
                    limits[(rating, kind.strip())] = float(amount)
    return LimitGrid(limits)


@dataclass(frozen=True)
class CounterpartyExposure:
    counterparty: Counterparty
    trades: int
    failed: int
    wrong_way: int
    # Gross value of the open trades: what would be at risk if they settled free of payment.
    settlement_value: float
    current_exposure: float
    pfe: float
    limit: float
    status: LimitStatus
    on_watch: bool

    @property
    def utilization(self) -> float:
        if self.limit > 0:
            return self.pfe / self.limit
        return math.inf if self.pfe > 0 else 0.0

    @property
    def excess(self) -> float:
        return max(0.0, self.pfe - self.limit)


def limit_status(pfe: float, limit: float, amber: float) -> LimitStatus:
    if pfe > 0 and pfe >= limit:
        return LimitStatus.EXCESS
    if limit > 0 and pfe >= amber * limit:
        return LimitStatus.AMBER
    return LimitStatus.OK


def aggregate(
    exposures: list[TradeExposure],
    grid: LimitGrid,
    watchlist: set[str],
    amber: float,
) -> list[CounterpartyExposure]:
    """Exposure per counterparty, highest utilization first.

    Trade exposures are added up with no netting (each DVP trade settles on its own) and no
    diversification between prices (as if every price moved against us at once). Both make
    the total conservative.
    """
    by_counterparty: dict[str, list[TradeExposure]] = {}
    for e in exposures:
        by_counterparty.setdefault(e.counterparty.counterparty_id, []).append(e)

    rows = []
    for trades in by_counterparty.values():
        counterparty = trades[0].counterparty
        pfe = sum(e.pfe_cad for e in trades)
        limit = grid.limit_for(counterparty)
        rows.append(
            CounterpartyExposure(
                counterparty=counterparty,
                trades=len(trades),
                failed=sum(e.failed for e in trades),
                wrong_way=sum(e.wrong_way for e in trades),
                settlement_value=sum(abs(e.value_cad) for e in trades),
                current_exposure=sum(e.ce_cad for e in trades),
                pfe=pfe,
                limit=limit,
                status=limit_status(pfe, limit, amber),
                on_watch=counterparty.counterparty_id in watchlist,
            )
        )
    return sorted(rows, key=lambda r: (r.utilization, r.pfe), reverse=True)
