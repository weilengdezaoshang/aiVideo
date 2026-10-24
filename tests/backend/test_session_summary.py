import pytest

from backend.core.session_summary import SummaryInput, SummarySettings, compact


def test_compaction_preserves_recent_messages_and_decision_references():
    settings = SummarySettings(threshold_chars=10, keep_recent_messages=1, excerpt_chars=4,
                               max_summary_chars=30)
    decisions = {"decision-1": "必须保留角色服装，不允许改成视频"}
    rows = [SummaryInput(1, "user", "生成一只小兔子"), SummaryInput(2, "assistant", "已确认服装"),
            SummaryInput(3, "user", "现在只改背景")]
    result = compact(rows, previous_cursor=0, previous_summary="", confirmed_decisions=decisions,
                     settings=settings)
    assert result.cursor == 2
    assert result.decision_ids == ("decision-1",)
    assert decisions == {"decision-1": "必须保留角色服装，不允许改成视频"}
    assert "现在只改背景" not in result.text
    assert len(result.text) <= settings.max_summary_chars
    assert result == compact(rows, previous_cursor=0, previous_summary="",
                             confirmed_decisions=decisions, settings=settings)


def test_compaction_does_not_recompress_processed_history_or_small_input():
    settings = SummarySettings(threshold_chars=100, keep_recent_messages=1)
    assert compact([SummaryInput(1, "user", "短消息")], previous_cursor=0,
                   previous_summary="", confirmed_decisions={}, settings=settings) is None
    assert compact([SummaryInput(1, "user", "x" * 200)], previous_cursor=1,
                   previous_summary="old", confirmed_decisions={}, settings=settings) is None


def test_compaction_rejects_duplicate_authoritative_sequences():
    with pytest.raises(ValueError, match="duplicate_summary_sequence"):
        compact([SummaryInput(1, "user", "a"), SummaryInput(1, "user", "b")],
                previous_cursor=0, previous_summary="", confirmed_decisions={})
