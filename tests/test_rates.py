"""Tests for choosing the cost rate of each hour (ADR 0005, ADR 0012)."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.alliant_energy.rates import (
    RATE_SOURCE_BILL,
    RATE_SOURCE_BILLING_CYCLE,
    RATE_SOURCE_OPTIONS,
    BillingCycle,
    KnownRates,
    MarginalRate,
    cost_rate,
    known_cost_rate,
)

# Two billing cycles: September at $0.25/kWh effective, October at $0.20.
SEPTEMBER = BillingCycle(
    start=datetime(2026, 9, 1, 5, tzinfo=UTC),
    end=datetime(2026, 10, 1, 5, tzinfo=UTC),
    usage=400.0,
    cost=100.0,
)
OCTOBER = BillingCycle(
    start=datetime(2026, 10, 1, 5, tzinfo=UTC),
    end=datetime(2026, 11, 1, 5, tzinfo=UTC),
    usage=500.0,
    cost=100.0,
)


def test_static_rate_prices_every_hour() -> None:
    rate = cost_rate(
        static=0.15,
        marginals=[(0.17, datetime(2026, 10, 1, 5, tzinfo=UTC))],
        cycles=[SEPTEMBER, OCTOBER],
    )

    assert rate is not None
    assert rate(datetime(2020, 1, 1, tzinfo=UTC)) == pytest.approx(0.15)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.15)
    assert rate.current == (pytest.approx(0.15), RATE_SOURCE_OPTIONS)


def test_without_static_or_marginal_each_hour_uses_its_billing_cycle() -> None:
    rate = cost_rate(static=None, marginals=[], cycles=[OCTOBER, SEPTEMBER])

    assert rate is not None
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.25)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.20)
    # Before the first known cycle: the earliest rate; after the last: the latest.
    assert rate(datetime(2026, 1, 1, tzinfo=UTC)) == pytest.approx(0.25)
    assert rate(datetime(2026, 12, 1, tzinfo=UTC)) == pytest.approx(0.20)
    assert rate.current == (pytest.approx(0.20), RATE_SOURCE_BILLING_CYCLE)


def test_no_rate_without_static_marginal_or_cycles() -> None:
    assert cost_rate(static=None, marginals=[], cycles=[]) is None


def test_billing_cycle_with_cost_zero_counts_as_missing() -> None:
    """Its hours take the nearest cycle with a cost, never a $0 rate."""
    unbilled = BillingCycle(start=SEPTEMBER.start, end=SEPTEMBER.end, usage=400.0, cost=0.0)
    rate = cost_rate(static=None, marginals=[], cycles=[unbilled, OCTOBER])

    assert rate is not None
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.20)


def test_marginal_rate_covers_its_billing_cycle_and_later_hours() -> None:
    """Older hours keep their own billing cycle's effective rate."""
    rate = cost_rate(
        static=None,
        marginals=[(0.17, datetime(2026, 10, 1, 5, tzinfo=UTC))],
        cycles=[SEPTEMBER, OCTOBER],
    )

    assert rate is not None
    assert rate(datetime(2026, 9, 30, 23, tzinfo=UTC)) == pytest.approx(0.25)
    assert rate(datetime(2026, 10, 1, 5, tzinfo=UTC)) == pytest.approx(0.17)
    assert rate(datetime(2026, 12, 1, tzinfo=UTC)) == pytest.approx(0.17)
    assert rate.current == (pytest.approx(0.17), RATE_SOURCE_BILL)


def test_marginal_rate_without_billing_cycles_prices_older_hours_too() -> None:
    rate = cost_rate(
        static=None,
        marginals=[(0.17, datetime(2026, 10, 1, 5, tzinfo=UTC))],
        cycles=[],
    )

    assert rate is not None
    assert rate(datetime(2026, 1, 1, tzinfo=UTC)) == pytest.approx(0.17)


def test_identity_changes_only_with_the_static_rate() -> None:
    """A new bill or marginal rate keeps the identity; a static rate sets it."""
    october = datetime(2026, 10, 1, 5, tzinfo=UTC)
    effective = cost_rate(static=None, marginals=[], cycles=[OCTOBER])
    marginal = cost_rate(static=None, marginals=[(0.17, october)], cycles=[OCTOBER])
    static = cost_rate(static=0.15, marginals=[(0.17, october)], cycles=[OCTOBER])
    other_static = cost_rate(static=0.16, marginals=[], cycles=[])

    assert effective is not None and marginal is not None
    assert static is not None and other_static is not None
    assert effective.identity == marginal.identity
    assert len({effective.identity, static.identity, other_static.identity}) == 3


def _cycle(start_month: int, end_month: int, cost: float) -> BillingCycle:
    """A 2026 billing cycle of 100 units from one month's 1st to another's."""
    return BillingCycle(
        start=datetime(2026, start_month, 1, 6, tzinfo=UTC),
        end=datetime(2026, end_month, 1, 6, tzinfo=UTC),
        usage=100.0,
        cost=cost,
    )


def test_hour_in_a_gap_takes_the_cycle_that_ended_before_it() -> None:
    """A $0 cycle mid-history (or any gap) never takes the newest rate."""
    cycles = [_cycle(1, 2, 10.0), _cycle(2, 3, 0.0), _cycle(3, 4, 20.0)]
    effective = cost_rate(static=None, marginals=[], cycles=cycles)
    marginal = cost_rate(
        static=None, marginals=[(0.5, datetime(2026, 5, 1, 5, tzinfo=UTC))], cycles=cycles
    )

    assert effective is not None and marginal is not None
    assert effective(datetime(2026, 2, 15, tzinfo=UTC)) == pytest.approx(0.10)
    assert marginal(datetime(2026, 2, 15, tzinfo=UTC)) == pytest.approx(0.10)
    # April falls between March's cycle and the marginal rate's start.
    assert marginal(datetime(2026, 4, 15, tzinfo=UTC)) == pytest.approx(0.20)


def test_marginal_rate_without_a_start_covers_hours_after_the_latest_billed_cycle() -> None:
    """Older hours keep their effective rates even when the bill lacks a period."""
    rate = cost_rate(static=None, marginals=[(0.17, None)], cycles=[SEPTEMBER, OCTOBER])

    assert rate is not None
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.25)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.20)
    assert rate(datetime(2026, 11, 15, tzinfo=UTC)) == pytest.approx(0.17)
    assert rate.current == (pytest.approx(0.17), RATE_SOURCE_BILL)


@pytest.mark.parametrize(
    ("cycle_start", "midnight_utc"),
    [
        (date(2026, 3, 8), datetime(2026, 3, 8, 6, tzinfo=UTC)),  # DST starts: CST
        (date(2026, 3, 9), datetime(2026, 3, 9, 5, tzinfo=UTC)),  # first CDT day
        (date(2026, 11, 1), datetime(2026, 11, 1, 5, tzinfo=UTC)),  # DST ends: CDT
    ],
)
def test_marginal_rate_starts_at_local_midnight_across_dst(cycle_start, midnight_utc) -> None:
    before = BillingCycle(
        start=datetime(2026, 1, 1, 6, tzinfo=UTC), end=midnight_utc, usage=100.0, cost=25.0
    )
    known = KnownRates(marginals=(MarginalRate(0.17, cycle_start),))
    rate = known_cost_rate(known, [before], ZoneInfo("America/Chicago"))

    assert rate is not None
    assert rate(midnight_utc) == pytest.approx(0.17)
    assert rate(midnight_utc.replace(hour=midnight_utc.hour - 1)) == pytest.approx(0.25)


def test_each_bill_prices_its_own_billing_cycle() -> None:
    """Past bills read too: each cycle uses its own bill's marginal rate (ADR 0014)."""
    august = BillingCycle(
        start=datetime(2026, 8, 1, 5, tzinfo=UTC),
        end=datetime(2026, 9, 1, 5, tzinfo=UTC),
        usage=400.0,
        cost=100.0,
    )
    rate = cost_rate(
        static=None,
        marginals=[
            (0.17, datetime(2026, 10, 1, 5, tzinfo=UTC)),  # latest bill
            (0.15, datetime(2026, 9, 1, 5, tzinfo=UTC)),  # a past bill
        ],
        cycles=[august, SEPTEMBER, OCTOBER],
    )

    assert rate is not None
    assert rate(datetime(2026, 8, 15, tzinfo=UTC)) == pytest.approx(0.25)  # no bill read
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.15)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.17)
    assert rate(datetime(2026, 12, 1, tzinfo=UTC)) == pytest.approx(0.17)
    assert rate.current == (pytest.approx(0.17), RATE_SOURCE_BILL)


def test_derived_cost_from_before_per_cycle_marginal_rates_is_repriced() -> None:
    """Cost priced under ADR 0012 recorded the identity "derived"."""
    rate = cost_rate(static=None, marginals=[], cycles=[OCTOBER])
    assert rate is not None
    assert rate.identity != "derived"


def test_latest_bill_without_a_start_still_prices_the_newest_hours() -> None:
    """It takes over after the latest billed cycle, not before older bills' cycles."""
    rate = cost_rate(
        static=None,
        marginals=[
            (0.19, None),  # the latest bill, first; its PDF gave no cycle start
            (0.16, datetime(2026, 9, 1, 5, tzinfo=UTC)),
            (0.15, datetime(2026, 8, 1, 5, tzinfo=UTC)),
        ],
        cycles=[SEPTEMBER],  # billed through 2026-10-01
    )

    assert rate is not None
    assert rate(datetime(2026, 8, 15, tzinfo=UTC)) == pytest.approx(0.15)
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.16)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.19)
    assert rate.current == (pytest.approx(0.19), RATE_SOURCE_BILL)


def test_newer_bill_wins_when_two_bills_share_a_cycle() -> None:
    """Marginal rates come newest first, so a corrected rebill beats the original."""
    october = datetime(2026, 10, 1, 5, tzinfo=UTC)
    rate = cost_rate(static=None, marginals=[(0.17, october), (0.18, october)], cycles=[])

    assert rate is not None
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.17)
