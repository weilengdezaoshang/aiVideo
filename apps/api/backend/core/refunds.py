"""Refund policy engine: the worker settles through one configurable decision.

Every existing path stays byte-equivalent with the defaults; the switches only
exist to make the money rules auditable and adjustable per deployment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Outcome = Literal["succeeded", "failed", "blocked_by_model", "cancelled", "retry"]


class RefundSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIVERO_REFUNDS_", frozen=True, extra="forbid")
    # Provider-confirmed failures refund the full reservation.
    failure_full_refund: bool = True
    # Content-policy blocks are never charged.
    blocked_full_refund: bool = True
    # A cancelled in-flight task still pays what the provider actually burned.
    cancelled_charge_actual: bool = True
    # Quality retries reuse the same reservation (no second charge); turning
    # this off makes each retry burn a fresh reservation delta.
    quality_free_retry: bool = True


@dataclass(frozen=True)
class Settlement:
    charge: int
    refund: int
    state: str  # HostCreditLedger.state value.


def settle_decision(outcome: Outcome, quoted: int, actual: int, *,
                    settings: RefundSettings | None = None) -> Settlement:
    """Pure policy: given the outcome and the provider's actual cost, decide.

    ``actual`` is what the provider burned (0 for clean failures); the ledger
    invariant 0 <= actual <= quoted is enforced by the caller.
    """
    settings = settings or RefundSettings()
    if outcome == "blocked_by_model" and settings.blocked_full_refund:
        return Settlement(0, quoted, "refunded")
    if outcome == "failed" and settings.failure_full_refund:
        return Settlement(0, quoted, "refunded")
    if outcome == "cancelled":
        if not settings.cancelled_charge_actual:
            return Settlement(0, quoted, "refunded")
        return Settlement(actual, quoted - actual,
                          "cancelled_with_charge" if actual else "refunded")
    if outcome == "retry" and not settings.quality_free_retry:
        # The retry attempt is charged as its own consumption of the reservation.
        return Settlement(actual, quoted - actual, "settled" if actual else "refunded")
    charge = min(actual, quoted)
    state = "settled" if charge else "refunded"
    return Settlement(charge, quoted - charge, state)
