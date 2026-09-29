"""Volatility forecasts over the settlement horizon: GARCH(1,1), or EWMA as a fallback.

Works on any series of daily changes: log returns for equities, yield changes for bonds. Only
data through the business date is ever passed in, so a forecast never sees the future.
"""

import math
import warnings
from dataclasses import dataclass

import numpy as np

from dvp.config import Settings


@dataclass(frozen=True)
class VolForecast:
    model: str
    # Forecast variance of the change on day 1, 2, ... ahead.
    daily_variance: tuple[float, ...]

    def horizon_sigma(self, days: int) -> float:
        """Standard deviation of the total change over the next `days` days: the daily
        variances add up (for a flat forecast this is the familiar sigma x sqrt(days))."""
        days = max(days, 1)
        v = self.daily_variance
        return math.sqrt(sum(v[:days]) + max(0, days - len(v)) * v[-1])


def ewma_forecast(changes: np.ndarray, lam: float, horizon: int) -> VolForecast:
    """RiskMetrics EWMA: tomorrow's variance = lam x today's + (1 - lam) x today's squared change.
    The forecast is flat over the horizon."""
    var = float(np.mean(changes[:30] ** 2))
    for r in changes:
        var = lam * var + (1 - lam) * float(r) ** 2
    return VolForecast("EWMA", (var,) * horizon)


def garch_forecast(changes: np.ndarray, horizon: int) -> VolForecast:
    """GARCH(1,1) with zero mean, fitted by maximum likelihood; its forecast reverts from today's
    variance toward the long-run level over the horizon."""
    from arch import arch_model

    # The optimizer is most reliable on data with a standard deviation near 1.
    scale = float(np.sqrt(np.mean(changes**2)))
    model = arch_model(changes / scale, mean="Zero", vol="GARCH", p=1, q=1, rescale=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = model.fit(disp="off", show_warning=False)
    if res.convergence_flag != 0:
        raise ValueError("GARCH fit did not converge")
    variance = res.forecast(horizon=horizon, reindex=False).variance.to_numpy()[-1] * scale**2
    return VolForecast("GARCH(1,1)", tuple(float(v) for v in variance))


def forecast(changes: np.ndarray, horizon: int, settings: Settings) -> VolForecast:
    """GARCH(1,1) when there's enough history and the fit is sensible, else EWMA."""
    changes = changes[np.isfinite(changes)][-settings.vol_lookback_days :]
    if len(changes) < 20:
        raise ValueError(f"only {len(changes)} daily observations; need at least 20")
    if len(changes) >= settings.garch_min_obs:
        try:
            fitted = garch_forecast(changes, horizon)
        except (ValueError, RuntimeError, np.linalg.LinAlgError):
            fitted = None
        # Reject a fit whose next-day volatility is far from the sample's (0.2x to 5x).
        sample_var = float(np.mean(changes**2))
        if fitted and 0.04 * sample_var <= fitted.daily_variance[0] <= 25 * sample_var:
            return fitted
    return ewma_forecast(changes, settings.ewma_lambda, horizon)
