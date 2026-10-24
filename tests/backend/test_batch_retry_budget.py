"""Retry budgets transfer ownership, rather than charging successful work twice."""
import pytest

from backend.core.batch.budget import AllocationError, CreditBudget, token_headroom


def test_retry_allocation_refunds_the_unspent_part_and_accepts_a_later_retry():
    parent = CreditBudget(limit=100, reserved=70, allocated=0, spent=70)
    allocated = parent.allocate(20)
    assert allocated == CreditBudget(100, 90, 20, 70)
    settled = allocated.settle(20, 5)
    assert settled == CreditBudget(100, 75, 0, 75)
    assert settled.allocate(25).reserved == 100
    # Original values are immutable and still describe the pre-retry receipt.
    assert parent.spent == 70


def test_concurrent_children_share_the_same_remaining_budget():
    parent = CreditBudget(100, 60, 0, 60).allocate(25)
    with pytest.raises(AllocationError, match="budget_rejected"):
        parent.allocate(16)
    both = parent.allocate(15)
    assert both.settle(25, 10).settle(15, 15) == CreditBudget(100, 85, 0, 85)


@pytest.mark.parametrize("quote,actual", [(5, 6), (11, 0), (-1, 0), (True, 0), (5, True)])
def test_an_unapproved_settlement_cannot_release_a_different_allocation(quote, actual):
    with pytest.raises(AllocationError):
        CreditBudget(100, 80, 10, 70).settle(quote, actual)


def test_known_and_unknown_token_usage_have_different_meanings():
    assert token_headroom(100, 10, [{"token_limit": 40, "settled": False}]) == 50
    assert token_headroom(100, 25, [{"token_limit": 40, "settled": True}]) == 75
    assert token_headroom(100, 25, [{"settled": True, "unknown_token_hold": 40}]) == 35
