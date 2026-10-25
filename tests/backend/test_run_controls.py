"""Refund engine, waiting-option matching, and queue view unit coverage."""
from backend.core.refunds import RefundSettings, Settlement, settle_decision
from backend.core.supervisor.subjects import match_waiting_option


def settles_to(result, charge, refund, state):
    return result == Settlement(charge=charge, refund=refund, state=state)


def test_settle_decision_defaults_match_legacy_behaviour():
    # 成功：实扣 actual，退差额
    assert settles_to(settle_decision("succeeded", 10, 7), 7, 3, "settled")
    assert settles_to(settle_decision("succeeded", 10, 10), 10, 0, "settled")
    assert settles_to(settle_decision("succeeded", 10, 0), 0, 10, "refunded")
    # 失败/拦截：全额退
    assert settles_to(settle_decision("failed", 10, 0), 0, 10, "refunded")
    assert settles_to(settle_decision("blocked_by_model", 10, 4), 0, 10, "refunded")
    # 取消：按服务商实际消耗扣
    assert settles_to(settle_decision("cancelled", 10, 3), 3, 7, "cancelled_with_charge")
    assert settles_to(settle_decision("cancelled", 10, 0), 0, 10, "refunded")


def test_settle_decision_switches_change_outcomes():
    settings = RefundSettings(cancelled_charge_actual=False)
    assert settles_to(settle_decision("cancelled", 10, 3, settings=settings), 0, 10, "refunded")
    settings = RefundSettings(failure_full_refund=False)
    assert settles_to(settle_decision("failed", 10, 2, settings=settings), 2, 8, "settled")
    settings = RefundSettings(blocked_full_refund=False)
    assert settles_to(settle_decision("blocked_by_model", 10, 0, settings=settings), 0, 10, "refunded")


def test_match_waiting_option_requires_unique_hit():
    options = [{"id": "0", "label": "杯子"}, {"id": "1", "label": "托盘"}, {"id": "all", "label": "全部"}]
    assert match_waiting_option(options, "抠杯子") == "0"
    assert match_waiting_option(options, "抠 托 盘 ") == "1"
    assert match_waiting_option(options, "全部都要") == "all"
    assert match_waiting_option(options, "来点别的") is None      # 未命中不猜
    assert match_waiting_option(options, "") is None
    ambiguous = [{"id": "0", "label": "杯子"}, {"id": "1", "label": "杯子把手"}]
    assert match_waiting_option(ambiguous, "杯子") == "0"           # 标签整体包含唯一精确命中
    assert match_waiting_option(ambiguous, "杯子把手") == "1"


def test_match_waiting_option_normalization_ignores_case_and_spaces():
    options = [{"id": "x", "label": "Red Cup"}]
    assert match_waiting_option(options, "用 red  cup 这个") == "x"
