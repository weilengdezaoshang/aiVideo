"""Pure accounting for a retry allocation; the caller serializes its database writes."""
from dataclasses import dataclass, replace


class AllocationError(ValueError):
    pass


@dataclass(frozen=True)
class CreditBudget:
    limit: int
    reserved: int
    allocated: int
    spent: int

    def __post_init__(self):
        values = (self.limit, self.reserved, self.allocated, self.spent)
        if any(type(value) is not int or value < 0 for value in values):
            raise AllocationError("invalid_budget")
        if not self.spent + self.allocated <= self.reserved <= self.limit:
            raise AllocationError("inconsistent_budget")

    def allocate(self, quote: int):
        if type(quote) is not int or quote < 0:
            raise AllocationError("invalid_quote")
        if quote > self.limit - self.reserved:
            raise AllocationError("budget_rejected")
        return replace(self, reserved=self.reserved + quote, allocated=self.allocated + quote)

    def settle(self, quote: int, actual: int):
        """Transfer a child's reconciled cost without changing the user's credit ledger.

        Receipt idempotency belongs in the transaction, rather than in these numbers:
        two distinct child allocations may legitimately have the same quote and cost.
        """
        if any(type(value) is not int or value < 0 for value in (quote, actual)):
            raise AllocationError("invalid_settlement")
        if actual > quote or quote > self.allocated:
            raise AllocationError("unapproved_settlement")
        return replace(self, reserved=self.reserved - (quote - actual),
                       allocated=self.allocated - quote, spent=self.spent + actual)


def token_headroom(limit: int, used: int, allocations: list[dict]) -> int:
    """Unknown calls retain their possible use; they are never reported as zero use."""
    if any(type(value) is not int or value < 0 for value in (limit, used)) or used > limit:
        raise AllocationError("invalid_token_budget")
    held = 0
    for item in allocations:
        value = item.get("unknown_token_hold", 0) if item.get("settled") else item.get("token_limit", 0)
        if type(value) is not int or value < 0:
            raise AllocationError("invalid_token_allocation")
        held += value
    return max(0, limit - used - held)
