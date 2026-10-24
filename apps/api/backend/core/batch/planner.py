"""A retry is an authenticated rule ticket, never a second model interpretation."""
import copy
import hashlib
import json

from sqlalchemy import select

from backend.core.supervisor.contracts import Plan, PlannerResult

from .compile import CompileError
from .orm import CoreBatchRetry


def proposal_hash(value: dict) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


class RetryPlanner:
    """Only a real receipt bound to this child Run can select this planner.

    The ordinary supervisor persists this result and performs all configured
    reviews. This class does not grant execution admission or create tasks.
    """

    def __init__(self, factory, run_id, parent_run_id):
        self.factory = factory
        self.run_id = run_id
        self.parent_run_id = parent_run_id

    async def plan(self, context: dict) -> PlannerResult:
        async with self.factory() as tx:
            receipt = await tx.scalar(select(CoreBatchRetry).where(
                CoreBatchRetry.child_run_id == self.run_id,
                CoreBatchRetry.parent_run_id == self.parent_run_id))
            if receipt is None:
                raise CompileError("retry_receipt_missing")
            data = receipt.document or {}
            proposal = data.get("proposal")
            if not isinstance(proposal, dict) or proposal_hash(proposal) != data.get("proposal_hash"):
                raise CompileError("retry_receipt_corrupt")
            if str(context.get("run_id")) != str(self.run_id):
                raise CompileError("retry_context_mismatch")
            if data.get("layer") != "rule_batch_retry":
                raise CompileError("retry_receipt_corrupt")
            frozen = copy.deepcopy(proposal)
            result = PlannerResult(plan=Plan.model_validate(frozen),
                invariants={}, confidence=1, tokens=0, raw_output={
                    "layer": "rule_batch_retry", "retry_receipt_id": str(receipt.id),
                    "proposal_hash": data["proposal_hash"], "plan": frozen,
                })
            return result
