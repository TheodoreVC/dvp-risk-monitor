"""Settlement fails, aged into the Basel failed-trade buckets.

A failed DVP trade still can't lose its principal, but it stays exposed to replacement cost for
as long as it stays open, and the longer a fail ages the likelier it is that something is wrong
with the counterparty. Basel's treatment of unsettled transactions (CRE70) charges capital on DVP
trades still open 5 or more business days after the agreed settlement date: the positive current
exposure times a factor that grows with the age of the fail.
"""

from dataclasses import dataclass

from dvp.engine import TradeExposure

# (first business day past settlement, factor), oldest bucket first.
BASEL_DVP_FAIL_FACTORS: tuple[tuple[int, float], ...] = (
    (46, 1.00),
    (31, 0.75),
    (16, 0.50),
    (5, 0.08),
)


def basel_factor(age: int) -> float:
    for first_day, factor in BASEL_DVP_FAIL_FACTORS:
        if age >= first_day:
            return factor
    return 0.0


def bucket_label(age: int) -> str:
    if age >= 46:
        return "46+"
    for first_day, last_day in ((31, 45), (16, 30), (5, 15)):
        if age >= first_day:
            return f"{first_day}-{last_day}"
    return "0-4"


@dataclass(frozen=True)
class Fail:
    exposure: TradeExposure
    age: int

    @property
    def factor(self) -> float:
        return basel_factor(self.age)

    @property
    def capital_charge(self) -> float:
        """Capital held against the fail, CAD."""
        return self.exposure.ce_cad * self.factor


def aged_fails(exposures: list[TradeExposure]) -> list[Fail]:
    """Failed trades, oldest first."""
    fails = [Fail(e, e.fail_age) for e in exposures if e.failed and e.fail_age is not None]
    return sorted(fails, key=lambda f: (-f.age, -f.exposure.ce_cad))
