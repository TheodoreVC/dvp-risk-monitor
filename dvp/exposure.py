"""What a single DVP trade could cost us if the counterparty defaults before it settles.

Under delivery versus payment, securities and cash change hands together, so a default before
settlement never costs the trade's full value: we still have our cash, or our securities. What it
costs is redoing the trade with someone else at today's market price: the replacement cost.

    current exposure       = max(0, replacement cost now)
    potential future exp.  = max(0, replacement cost now + add-on)

where the add-on is the adverse price move we expect to be exceeded only 5% of the time before
the trade settles.
"""

import datetime as dt

from dvp.bonds import clean_price
from dvp.book import Counterparty, Security, Side


def replacement_cost(
    side: Side, quantity: float, trade_price: float, price: float, multiplier: float = 1.0
) -> float:
    """Signed cost of redoing the trade at `price` if the counterparty failed now.

    Buying, we'd have to buy elsewhere, so we lose if the price has risen. Selling, we'd have to
    sell elsewhere, so we lose if it has fallen. Negative: the trade is in our favour.
    """
    change = (price - trade_price) * quantity * multiplier
    return change if side is Side.BUY else -change


def potential_future_exposure(mtm: float, add_on: float) -> float:
    """Replacement cost after an adverse move of `add_on`. A trade already in our favour
    (negative `mtm`) absorbs part of the move before it costs anything."""
    return max(0.0, mtm + add_on)


def equity_add_on(value: float, horizon_sigma: float, z: float) -> float:
    """A z-sigma price move over the settlement horizon, on the position's current value."""
    return z * horizon_sigma * abs(value)


def bond_add_on(
    side: Side,
    coupon: float,
    maturity: dt.date,
    as_of: dt.date,
    ytm: float,
    yield_sigma: float,
    z: float,
    face: float,
) -> float:
    """Increase in replacement cost if the yield moves z sigma against us: a buyer loses when
    yields fall (the bond costs more to replace), a seller when they rise. Close to
    modified duration x price x z x yield sigma, but repriced exactly."""
    shocked = ytm - z * yield_sigma if side is Side.BUY else ytm + z * yield_sigma
    now = clean_price(coupon, maturity, as_of, ytm)
    stressed = clean_price(coupon, maturity, as_of, shocked)
    return abs(stressed - now) * face / 100


def is_wrong_way(side: Side, security: Security, counterparty: Counterparty) -> bool:
    """We're selling the counterparty securities its own group issued.

    If it defaults, those securities fall at the same moment, and we're left holding them at the
    fallen price: exposure is largest exactly when default happens. Buying them from it is the
    opposite, right-way case: they get cheaper to replace.
    """
    return side is Side.SELL and security.issuer_group == counterparty.group


def jump_to_default_loss(
    trade_price: float, quantity: float, multiplier: float, default_price: float
) -> float:
    """Replacement cost of a sale if the security drops to `default_price` on the default."""
    return max(0.0, (trade_price - default_price) * quantity * multiplier)
