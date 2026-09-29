"""Settings: every number a risk manager might want to change, in one place."""

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPORTS_DIR = ROOT / "reports"

# Exposures are reported and limits set in Canadian dollars.
REPORT_CURRENCY = "CAD"


@dataclass(frozen=True)
class Settings:
    # One-sided 95% quantile of the normal distribution: PFE is the replacement cost that an
    # adverse price move exceeds only 5% of the time.
    z: float = 1.645
    # A pending trade is at risk until it actually settles, so never less than one business day.
    min_horizon_days: int = 1
    # A failed trade has no known settlement date; assume it settles within this many business days.
    fail_horizon_days: int = 5
    # Volatility: GARCH(1,1) on up to ~3 years of daily changes; EWMA if GARCH can't be fitted.
    vol_lookback_days: int = 750
    garch_min_obs: int = 250
    ewma_lambda: float = 0.94
    # Limit utilization at or above this is amber (early warning); 100% or more is an excess.
    amber_utilization: float = 0.80
    # A price older than this many calendar days before the business date is flagged as stale.
    stale_price_days: int = 5
    # Counterparty news that puts a name on the watchlist.
    news_lookback_days: int = 30
    news_categories: tuple[str, ...] = ("legal", "leadership", "deal")
    # Wrong-way trades: what the security is assumed to be worth if its issuer, our
    # counterparty, defaults (equity: nothing; senior bonds: a typical recovery on par).
    equity_recovery: float = 0.0
    bond_recovery: float = 0.40
