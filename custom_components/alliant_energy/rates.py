"""Choose the cost rate that prices each hour of a cost statistic.

A static rate from the options wins for every hour. Otherwise each hour uses
the effective rate (amount / usage) of the billing cycle that contains it. See
docs/adr/0005-cost-rate-precedence.md and
docs/adr/0012-price-old-hours-by-billing-cycle.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo

# Where a rate came from, in order of precedence.
RATE_SOURCE_OPTIONS = "options"  # static rate set in the integration options
RATE_SOURCE_BILL = "bill"  # marginal rate from the latest itemized bill
RATE_SOURCE_BILLING_CYCLE = "billing_cycle"  # a billing cycle's amount / usage


@dataclass(frozen=True)
class MarginalRate:
    """A bill's marginal rate for a service, and when its billing cycle started."""

    rate: float
    start: date | None  # None when the bill gives no billing cycle start


@dataclass(frozen=True)
class KnownRates:
    """The rates known for a service before its billing cycles are read."""

    static: float | None = None
    # Marginal rates from the bills read, any order; the latest bill's at least.
    marginals: tuple[MarginalRate, ...] = ()


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
    """The identity of every cost rate built with this static rate.

    "derived:2" since each billing cycle uses its own bill's marginal rate
    (ADR 0014); cost recorded as "derived" is repriced once.
    """
    return f"static:{static!r}" if static is not None else "derived:2"


def cost_rate(
    static: float | None,
    marginals: list[tuple[float, datetime | None]],
    cycles: list[BillingCycle],
) -> CostRate | None:
    """Return the cost rate for one service, or None when no rate is known.

    ``marginals`` are (rate, billing cycle start) from the bills read. Each
    covers its cycle until the next bill's cycle starts; the latest covers every
    later hour. Older hours use their billing cycle's effective rate.
    """
    if static is not None:
        return CostRate([_Span(None, None, static, RATE_SOURCE_OPTIONS)])
    spans = [
        _Span(cycle.start, cycle.end, cycle.cost / cycle.usage, RATE_SOURCE_BILLING_CYCLE)
        for cycle in sorted(cycles, key=lambda cycle: cycle.start)
        # A cycle billed at 0 has no cost data yet; treat it as missing.
        if cycle.cost and cycle.usage
    ]
    # The latest bill can also be among the past bills read: one rate per start.
    placed = sorted({start: rate for rate, start in marginals if start is not None}.items())
    unplaced = [rate for rate, start in marginals if start is None]
    if not placed and unplaced:
        # A bill without its cycle's start takes over after the latest billed
        # cycle, or prices every hour when no cycle is known.
        placed = [(spans[-1].end if spans else None, unplaced[0])]
    if placed:
        first = placed[0][0]
        if first is not None:
            spans = [
                _Span(span.start, min(span.end, first), span.rate, span.source)
                for span in spans
                if span.start is not None and span.end is not None and span.start < first
            ]
        else:
            spans = []
        for n, (start, rate) in enumerate(placed):
            end = placed[n + 1][0] if n + 1 < len(placed) else None
            spans.append(_Span(start, end, rate, RATE_SOURCE_BILL))
    return CostRate(spans) if spans else None


def known_cost_rate(known: KnownRates, cycles: list[BillingCycle], tz: tzinfo) -> CostRate | None:
    """The cost rate from known rates; a bill's cycle starts at local midnight."""
    marginals = [
        (m.rate, datetime.combine(m.start, time(), tz) if m.start else None)
        for m in known.marginals
    ]
    return cost_rate(known.static, marginals, cycles)
