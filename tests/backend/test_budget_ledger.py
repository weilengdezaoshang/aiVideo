"""预算账本验收(技术方案 §21 BUDGET-01..04;§13.2 原子预留与恢复)。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.evaluation.budgets import BudgetExhausted, BudgetLedger, BudgetPolicy, PriceUnknown


def test_budget01_concurrent_calls_cannot_spend_same_balance(tmp_path):
    """两个并发调用争用仅够一次的额度:只放行一个,预留合计不越界。"""
    ledger = BudgetLedger(tmp_path, "scope-1", BudgetPolicy(maxCost=0.5, prices={"generation": 0.5}))
    outcomes = []
    lock = threading.Lock()

    def attempt(index):
        try:
            ledger.reserve("run-1", "generation", 0.5, trial_id=f"t{index}")
            ok = True
        except BudgetExhausted:
            ok = False
        with lock:
            outcomes.append(ok)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(4):
            pool.submit(attempt, 0)
    assert outcomes.count(True) == 1
    summary = ledger.summary()
    assert summary.settled == 0.0
    assert summary.outstandingReserved == 0.5  # 未结算的预留继续占用,不允许重复花费


def test_budget01b_settled_plus_outbound_must_stay_under_cap(tmp_path):
    ledger = BudgetLedger(tmp_path, "scope-2", BudgetPolicy(maxCost=1.0, prices={"generation": 0.4}))
    first = ledger.reserve("run", "generation", 0.4)
    ledger.settle(first.reservationId, 0.4)
    second = ledger.reserve("run", "generation", 0.4)
    ledger.settle(second.reservationId, 0.4)
    with pytest.raises(BudgetExhausted, match="预算不足"):
        ledger.reserve("run", "generation", 0.4)
    # 已用 0.8,只剩 0.2 空间:小调用仍可放行
    ledger.reserve("run", "translate", 0.2)
    assert ledger.summary().totalCalls == 3


def test_budget02_unknown_outcome_keeps_reservation_across_reload(tmp_path):
    """请求发出后"进程崩溃":预留保留、结果未知;重启后不盲目重发也不释放。"""
    ledger = BudgetLedger(tmp_path, "scope-3", BudgetPolicy(maxCost=1.0, prices={"generation": 0.6}))
    reservation = ledger.reserve("run", "generation", 0.6)
    # 模拟崩溃:进程消失,没有 settle 记录;新进程重新加载账本
    reloaded = BudgetLedger(tmp_path, "scope-3", BudgetPolicy(maxCost=1.0, prices={"generation": 0.6}))
    restored = {r.reservationId: r for r in reloaded.reservations()}[reservation.reservationId]
    assert restored.status == "unknown"
    assert restored.note and "未知" in restored.note
    # 未知占用的 0.6 不释放:同价格的下一次调用将被拒绝,防止重复付费
    with pytest.raises(BudgetExhausted, match="未知"):
        reloaded.reserve("run", "generation", 0.6)
    # 显式对账(上游实际只计费 0.2)后恢复空间
    reloaded.settle(restored.reservationId, 0.2, note="上游确认成功")
    reloaded.reserve("run", "generation", 0.6)
    assert reloaded.summary().totalCalls == 2


def test_budget03_unknown_price_rejected_in_hard_mode(tmp_path):
    ledger = BudgetLedger(tmp_path, "scope-4", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5}))
    with pytest.raises(PriceUnknown, match="无可信计费上界"):
        ledger.reserve("run", "judge", None)
    with pytest.raises(PriceUnknown, match="无可信计费上界"):
        # 未登记的 callSite 从价格快照取不到 → 同样拒绝
        ledger.reserve("run", "download", None)
    # 无上限且未声明估算:直接拒绝(不进硬预算模式的 Provider 必须显式声明估算)
    open_ledger = BudgetLedger(tmp_path, "scope-4b", BudgetPolicy(maxCost=None))
    with pytest.raises(PriceUnknown):
        open_ledger.reserve("run", "generation", 0.5)


def test_budget04_retries_are_individually_reserved_and_capped(tmp_path):
    ledger = BudgetLedger(
        tmp_path, "scope-5", BudgetPolicy(maxCost=10.0, maxExternalCalls=2, prices={"generation": 0.5})
    )
    first = ledger.reserve("run", "generation", 0.5, attempt_index=0)
    ledger.settle(first.reservationId, 0.5)
    retry = ledger.reserve("run", "generation", 0.5, attempt_index=1)  # 重试单独预留
    ledger.settle(retry.reservationId, 0.5)
    with pytest.raises(BudgetExhausted, match="次数达到上限"):
        ledger.reserve("run", "generation", 0.5, attempt_index=2)
    assert ledger.summary().settled == 1.0


def test_release_only_before_send(tmp_path):
    ledger = BudgetLedger(tmp_path, "scope-6", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5}))
    reservation = ledger.reserve("run", "generation", 0.5)
    ledger.release(reservation.reservationId, note="发送前取消")
    assert ledger.summary().outstandingReserved == 0.0
    settled = ledger.reserve("run", "generation", 0.5)
    with pytest.raises(ValueError, match="不能释放"):
        ledger.settle(settled.reservationId, 0.5)
        ledger.release(settled.reservationId)


def test_ledger_persists_append_only_journal(tmp_path):
    ledger = BudgetLedger(tmp_path, "scope-7", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5}))
    reservation = ledger.reserve("run", "generation", 0.5)
    ledger.settle(reservation.reservationId, 0.3)
    lines = [
        json.loads(line)
        for line in (tmp_path / "budgets" / "scope-7.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [item["kind"] for item in lines] == ["reserve", "settle"]  # 只追加,先预留后结算
    reloaded = BudgetLedger(tmp_path, "scope-7", BudgetPolicy(maxCost=1.0, prices={"generation": 0.5}))
    assert reloaded.summary().settled == 0.3
