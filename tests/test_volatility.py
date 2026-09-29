import numpy as np
import pytest

from dvp.config import Settings
from dvp.volatility import VolForecast, ewma_forecast, forecast


def test_horizon_sigma_adds_variances():
    flat = VolForecast("EWMA", (0.0001,) * 5)
    assert flat.horizon_sigma(1) == pytest.approx(0.01)
    assert flat.horizon_sigma(4) == pytest.approx(0.02)  # sigma x sqrt(4)
    assert flat.horizon_sigma(0) == pytest.approx(0.01)  # at least one day
    assert flat.horizon_sigma(9) == pytest.approx(0.03)  # beyond the forecast: last day repeats
    curve = VolForecast("GARCH(1,1)", (0.0004, 0.0003, 0.0002))
    assert curve.horizon_sigma(3) == pytest.approx(np.sqrt(0.0009))


def test_ewma_recovers_a_constant_volatility():
    changes = np.random.default_rng(0).normal(0, 0.02, 2000)
    fitted = ewma_forecast(changes, 0.94, 5)
    assert fitted.horizon_sigma(1) == pytest.approx(0.02, rel=0.3)


def test_garch_on_a_garch_process():
    # Simulate GARCH(1,1): omega=2e-6, alpha=0.08, beta=0.9 (long-run daily vol 1%).
    rng = np.random.default_rng(1)
    n, omega, alpha, beta = 1500, 2e-6, 0.08, 0.9
    var, changes = omega / (1 - alpha - beta), np.empty(n)
    for i in range(n):
        changes[i] = rng.normal(0, np.sqrt(var))
        var = omega + alpha * changes[i] ** 2 + beta * var
    fitted = forecast(changes, 10, Settings())
    assert fitted.model == "GARCH(1,1)"
    assert 0.004 < fitted.horizon_sigma(1) < 0.025


def test_short_history_falls_back_to_ewma():
    changes = np.random.default_rng(2).normal(0, 0.01, 100)
    assert forecast(changes, 5, Settings()).model == "EWMA"


def test_too_little_history_raises():
    with pytest.raises(ValueError):
        forecast(np.array([0.01, -0.01]), 5, Settings())
