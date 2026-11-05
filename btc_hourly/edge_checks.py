"""Paper-only edge gates: macro skip, vol-shock buffer, longshot floor."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import config

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# Scheduled US macro prints (date, ET clock HH:MM). Extend as needed.
# NFP usually first Friday 8:30 ET; CPI ~8:30 ET; FOMC 14:00 ET.
MACRO_EVENTS: list[tuple[date, str, str]] = [
    # (date, "HH:MM" ET, label) — verified BLS / Fed calendars 2026-11-05
    (date(2026, 11, 6), "08:30", "NFP"),
    (date(2026, 11, 10), "08:30", "CPI"),
    (date(2026, 12, 4), "08:30", "NFP"),
    (date(2026, 12, 9), "14:00", "FOMC"),
    (date(2026, 12, 10), "08:30", "CPI"),
]


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    edge_buffer: float = config.EDGE_BUFFER


def _macro_windows(now: Optional[datetime] = None) -> list[tuple[datetime, datetime, str]]:
    """Skip window: -60m to +90m around print, applied to the hourly event hours that settle into the shock."""
    now = now or datetime.now(PT)
    windows = []
    for d, hhmm, label in MACRO_EVENTS:
        h, m = map(int, hhmm.split(":"))
        print_et = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
        start = print_et - timedelta(minutes=60)
        end = print_et + timedelta(minutes=90)
        windows.append((start, end, label))
    return windows


def in_macro_skip(now: Optional[datetime] = None, event_end_pt: Optional[datetime] = None) -> GateResult:
    """True when we should skip new paper buys (print window or settling into post-print hours)."""
    now = now or datetime.now(PT)
    check_times = [now.astimezone(ET)]
    if event_end_pt is not None:
        check_times.append(event_end_pt.astimezone(ET))
    for start, end, label in _macro_windows(now):
        for t in check_times:
            if start <= t <= end:
                return GateResult(False, f"macro skip {label} window {start.isoformat()}–{end.isoformat()}")
    return GateResult(True, "macro clear")


def vol_shock_buffer(dvol: Optional[float], dvol_prev: Optional[float],
                     short_rv: Optional[float], long_rv: Optional[float]) -> GateResult:
    """Bump EDGE_BUFFER when DVOL jumps 3+ pts or short RV is 2x longer RV."""
    buf = config.EDGE_BUFFER
    reasons = []
    if dvol is not None and dvol_prev is not None:
        if abs(dvol - dvol_prev) >= config.DVOL_SHOCK_PTS:
            buf += config.VOL_SHOCK_BUFFER_BUMP
            reasons.append(f"DVOL shock {dvol_prev:.1f}->{dvol:.1f}")
    if short_rv is not None and long_rv is not None and long_rv > 0:
        if short_rv >= config.RV_SHOCK_MULT * long_rv:
            if config.VOL_SHOCK_BUFFER_BUMP not in [config.VOL_SHOCK_BUFFER_BUMP] or "RV shock" not in " ".join(reasons):
                if not any("RV shock" in r for r in reasons):
                    buf = max(buf, config.EDGE_BUFFER + config.VOL_SHOCK_BUFFER_BUMP)
                    reasons.append(f"RV shock short={short_rv:.3f} long={long_rv:.3f}")
    if reasons:
        return GateResult(True, "; ".join(reasons), edge_buffer=buf)
    return GateResult(True, "vol calm", edge_buffer=buf)


def price_gate(price: float) -> GateResult:
    if price < config.MIN_PRICE:
        return GateResult(False, f"longshot floor: {price:.2f} < MIN_PRICE {config.MIN_PRICE}")
    if price > config.MAX_PRICE:
        return GateResult(False, f"price {price:.2f} > MAX_PRICE {config.MAX_PRICE}")
    return GateResult(True, "price ok")


def minutes_to_close_ok(minutes_left: float) -> GateResult:
    if minutes_left <= config.SKIP_LAST_MINUTES:
        return GateResult(False, f"skip last {config.SKIP_LAST_MINUTES}m ({minutes_left:.1f}m left)")
    return GateResult(True, "time ok")
