"""EWMA/HAR-style vol + Black-Scholes-ish P(above K) for hourly contracts."""
from __future__ import annotations

import math
from typing import Optional

import config


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_vol_annual(closes: list[float], span: int = 60) -> Optional[float]:
    """Annualized vol from 1-min log returns (EWMA-ish over last `span`)."""
    if len(closes) < max(10, span // 2):
        return None
    rets = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 5:
        return None
    # EWMA variance
    lam = 0.94
    var = rets[0] ** 2
    for r in rets[1:]:
        var = lam * var + (1 - lam) * r * r
    # 1-min -> annual: sqrt(525600) approx for crypto 24/7
    sigma = math.sqrt(max(var, 1e-18)) * math.sqrt(365.25 * 24 * 60)
    return sigma


def blend_vol(rv: Optional[float], dvol_pct: Optional[float]) -> float:
    """Blend realized (decimal) with DVOL (%). Return decimal annual vol."""
    dvol = (dvol_pct / 100.0) if dvol_pct is not None else None
    if rv is None and dvol is None:
        return 0.50  # fallback 50%
    if rv is None:
        return float(dvol)
    if dvol is None:
        return float(rv)
    w = config.DVOL_BLEND
    return (1 - w) * rv + w * dvol


def p_above(spot: float, strike: float, sigma: float, tau_years: float, mu: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or sigma <= 0 or tau_years <= 0:
        return 0.5 if spot >= strike else 0.5
    # Settlement is 60s average — slightly damp final-minute variance; ignore for MVP
    vol_sqrt_t = sigma * math.sqrt(tau_years)
    if vol_sqrt_t < 1e-12:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) + (mu - 0.5 * sigma * sigma) * tau_years) / vol_sqrt_t
    return float(min(1.0, max(0.0, _norm_cdf(d2))))


def taker_fee(price: float) -> float:
    return config.TAKER_FEE_RATE * price * (1.0 - price)


def ev_yes(p: float, price: float, buffer: float) -> float:
    return p - price - taker_fee(price) - buffer


def kelly_fraction(p: float, price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    edge = p - price
    if edge <= 0:
        return 0.0
    return max(0.0, edge / (1.0 - price))
