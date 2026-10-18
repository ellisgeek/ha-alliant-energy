"""Tests for choosing the cost rate of each hour (ADR 0005, ADR 0012)."""

from datetime import UTC, datetime

import pytest

from custom_components.alliant_energy.models import (
    RATE_SOURCE_BILL,
    RATE_SOURCE_BILLING_CYCLE,
    RATE_SOURCE_OPTIONS,
)
from custom_components.alliant_energy.rates import BillingCycle, cost_rate

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
        marginal=0.17,
        marginal_from=datetime(2026, 10, 1, 5, tzinfo=UTC),
        cycles=[SEPTEMBER, OCTOBER],
    )

    assert rate is not None
    assert rate(datetime(2020, 1, 1, tzinfo=UTC)) == pytest.approx(0.15)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.15)
    assert rate.current == (pytest.approx(0.15), RATE_SOURCE_OPTIONS)


def test_without_static_or_marginal_each_hour_uses_its_billing_cycle() -> None:
    rate = cost_rate(static=None, marginal=None, marginal_from=None, cycles=[OCTOBER, SEPTEMBER])

    assert rate is not None
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.25)
    assert rate(datetime(2026, 10, 15, tzinfo=UTC)) == pytest.approx(0.20)
    # Before the first known cycle: the earliest rate; after the last: the latest.
    assert rate(datetime(2026, 1, 1, tzinfo=UTC)) == pytest.approx(0.25)
    assert rate(datetime(2026, 12, 1, tzinfo=UTC)) == pytest.approx(0.20)
    assert rate.current == (pytest.approx(0.20), RATE_SOURCE_BILLING_CYCLE)


def test_no_rate_without_static_marginal_or_cycles() -> None:
    assert cost_rate(static=None, marginal=None, marginal_from=None, cycles=[]) is None


def test_billing_cycle_with_cost_zero_counts_as_missing() -> None:
    """Its hours take the nearest cycle with a cost, never a $0 rate."""
    unbilled = BillingCycle(start=SEPTEMBER.start, end=SEPTEMBER.end, usage=400.0, cost=0.0)
    rate = cost_rate(static=None, marginal=None, marginal_from=None, cycles=[unbilled, OCTOBER])

    assert rate is not None
    assert rate(datetime(2026, 9, 15, tzinfo=UTC)) == pytest.approx(0.20)


def test_marginal_rate_covers_its_billing_cycle_and_later_hours() -> None:
    """Older hours keep their own billing cycle's effective rate."""
    rate = cost_rate(
        static=None,
        marginal=0.17,
        marginal_from=datetime(2026, 10, 1, 5, tzinfo=UTC),
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
        marginal=0.17,
        marginal_from=datetime(2026, 10, 1, 5, tzinfo=UTC),
        cycles=[],
    )

    assert rate is not None
    assert rate(datetime(2026, 1, 1, tzinfo=UTC)) == pytest.approx(0.17)


def test_identity_changes_only_with_the_static_rate() -> None:
    """A new bill or marginal rate keeps the identity; a static rate sets it."""
    october = datetime(2026, 10, 1, 5, tzinfo=UTC)
    effective = cost_rate(static=None, marginal=None, marginal_from=None, cycles=[OCTOBER])
    marginal = cost_rate(static=None, marginal=0.17, marginal_from=october, cycles=[OCTOBER])
    static = cost_rate(static=0.15, marginal=0.17, marginal_from=october, cycles=[OCTOBER])
    other_static = cost_rate(static=0.16, marginal=None, marginal_from=None, cycles=[])

    assert effective is not None and marginal is not None
    assert static is not None and other_static is not None
    assert effective.identity == marginal.identity
    assert len({effective.identity, static.identity, other_static.identity}) == 3
