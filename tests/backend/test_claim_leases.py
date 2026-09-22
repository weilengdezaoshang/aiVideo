"""领取租约:到期轮询与 Outbox 发布必须刷新时间戳,避免 beat 重复投递。"""

import inspect

from backend.infrastructure.repositories import JobRepository, OutboxRepository


def test_claim_due_polls_clears_next_poll_at():
    source = inspect.getsource(JobRepository.claim_due_polls)
    assert "job.next_poll_at = None" in source
    assert "flush()" in source


def test_claim_publishing_refreshes_next_attempt_at():
    source = inspect.getsource(OutboxRepository.claim_and_mark_publishing)
    assert "row.next_attempt_at = claimed_at" in source
    assert "flush()" in source
