"""Choose the cost rate that prices each hour of a cost statistic.

A static rate from the options wins for every hour. Otherwise each hour uses
the effective rate (amount / usage) of the billing cycle that contains it. See
docs/adr/0005-cost-rate-precedence.md and
docs/adr/0012-price-old-hours-by-billing-cycle.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

from .models import RATE_SOURCE_BILL, RATE_SOURCE_BILLING_CYCLE, RATE_SOURCE_OPTIONS


@dataclass(frozen=True)
class KnownRates:
    """The rates known for a service before its billing cycles are read."""

    static: float | None = None
    marginal: float | None = None  # from the latest bill
    marginal_from: date | None = None  # start of that bill's billing cycle


@dataclass(frozen=True)
class BillingCycle:
    """One billing cycle of a service: its span, usage, and billed amount."""

    start: datetime
    end: datetime
    usage: float
    cost: float | None


@dataclass(frozen=True)
class _Span:
    """A rate that applies from ``start`` until ``end`` (None: open-ended)."""

    start: datetime | None
    end: datetime | None
    rate: float
    source: str


class CostRate:
    """The cost rate of every hour for one service.

    Spans are in time order. An hour in a gap uses the span that ended before
    it; an hour before the first span uses the first.
    """

    def __init__(self, spans: list[_Span]) -> None:
        self._spans = spans

    def __call__(self, hour: datetime) -> float:
        return self._span(hour).rate

    @property
    def identity(self) -> str:
        """Changes only when the static rate does: a change reprices the history.

        A new bill or marginal rate keeps it, because imported hours keep the
        rate they were priced with (ADR 0012).
        """
        first = self._spans[0]
        return cost_identity(first.rate if first.source == RATE_SOURCE_OPTIONS else None)

    @property
    def current(self) -> tuple[float, str]:
        """The rate for the newest hour, and where it came from."""
        span = self._spans[-1]
        return span.rate, span.source

    def _span(self, hour: datetime) -> _Span:
        # The last span that starts at or before the hour: the one containing
        # it, or, in a gap, the one that ended just before it.
        found = self._spans[0]
        for span in self._spans:
            if span.start is not None and span.start > hour:
                break
            found = span
        return found


def cost_identity(static: float | None) -> str:
    """The identity of every cost rate built with this static rate."""
    return f"static:{static!r}" if static is not None else "derived"


def cost_rate(
    static: float | None,
    marginal: float | None,
    marginal_from: datetime | None,
    cycles: list[BillingCycle],
) -> CostRate | None:
    """Return the cost rate for one service, or None when no rate is known."""
    if static is not None:
        return CostRate([_Span(None, None, static, RATE_SOURCE_OPTIONS)])
    spans = [
        _Span(cycle.start, cycle.end, cycle.cost / cycle.usage, RATE_SOURCE_BILLING_CYCLE)
        for cycle in sorted(cycles, key=lambda cycle: cycle.start)
        # A cycle billed at 0 has no cost data yet; treat it as missing.
        if cycle.cost and cycle.usage
    ]
    if marginal is not None:
        # The bill's marginal rate takes over from its billing cycle onward.
        # Without the cycle's start, it takes over after the latest billed cycle.
        if marginal_from is None and spans:
            marginal_from = spans[-1].end
        if marginal_from is not None:
            spans = [
                _Span(span.start, min(span.end, marginal_from), span.rate, span.source)
                for span in spans
                if span.start is not None and span.end is not None and span.start < marginal_from
            ]
        spans.append(_Span(marginal_from, None, marginal, RATE_SOURCE_BILL))
    return CostRate(spans) if spans else None


def known_cost_rate(known: KnownRates, cycles: list[BillingCycle], tz: tzinfo) -> CostRate | None:
    """The cost rate from known rates; a marginal rate starts at local midnight."""
    marginal_from = (
        datetime.combine(known.marginal_from, time(), tz) if known.marginal_from else None
    )
    return cost_rate(known.static, known.marginal, marginal_from, cycles)
