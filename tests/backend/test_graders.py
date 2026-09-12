"""类型化判分验收(技术方案 §21 GRADE-01/GRADE-02)与依赖图汇总(§10.5)。"""

from backend.evaluation.graders.judge import StubJudge
from backend.evaluation.graders.rules import Answer, grade_case
from backend.evaluation.models import Check


def boolean_check(check_id="has_cat", expected=True, depends=None):
    return Check(
        id=check_id, kind="boolean", question="图中是否出现猫?", expected=expected, dependsOn=depends or []
    )


def test_grade01_negative_answer_is_not_substring_matched():
    """旧实现:回答“没有猫”包含“猫”被误判通过;类型化规则必须判不通过。"""
    check = boolean_check()
    answers = {"has_cat": Answer("has_cat", False, evidence="图中没有猫,只有狗")}
    grading = grade_case("trial-g1", [check], answers)
    assert grading.grades[0].status == "fail"
    assert grading.verdict == "fail"
    assert grading.score == 0.0


def test_grade02_keeps_contradiction_and_fails_via_dependency():
    """没有杯子却回答杯子是红色:保留原始矛盾证据,依赖失败,总分不升高。"""
    checks = [
        boolean_check("has_cup", True),
        Check(id="cup_red", kind="boolean", question="杯子是红色的吗?", expected=True, dependsOn=["has_cup"]),
    ]
    answers = {
        "has_cup": Answer("has_cup", False, evidence="图中没有杯子"),
        "cup_red": Answer("cup_red", True, evidence="画面右侧有一个红色杯子"),
    }
    grading = grade_case("trial-g2", checks, answers)
    by_id = {grade.checkId: grade for grade in grading.grades}
    assert by_id["has_cup"].status == "fail"
    assert by_id["cup_red"].status == "dependency_failed"
    # 矛盾证据保留:依赖失败的子项仍保存裁判原始回答
    assert by_id["cup_red"].observed is True
    assert by_id["has_cup"].observed is False
    assert grading.verdict == "fail"
    # dependency_failed 保留在分母并贡献 0(§10.5),不得虚增加权分
    assert grading.score == 0.0


def test_integer_and_text_typed_comparison():
    count = Check(id="cat_count", kind="integer", question="共有几只猫?", expected=3)
    word = Check(id="sign_word", kind="text", question="招牌上的英文?", expected="OPEN")
    grading = grade_case(
        "trial-g3",
        [count, word],
        {"cat_count": Answer("cat_count", 3), "sign_word": Answer("sign_word", "open")},
    )
    by_id = {grade.checkId: grade for grade in grading.grades}
    assert by_id["cat_count"].status == "pass"
    assert by_id["sign_word"].status == "pass"  # 规范化等值(strip+casefold),非子串
    assert grading.verdict == "pass"
    assert grading.score == 1.0


def test_type_mismatch_is_inconclusive_not_fail():
    """裁判回答类型不符(用字符串回答数字)记未定,整条用例结论未定,不算零分通过。"""
    count = Check(id="cat_count", kind="integer", question="共有几只猫?", expected=3)
    grading = grade_case("trial-g4", [count], {"cat_count": Answer("cat_count", "三只")})
    assert grading.grades[0].status == "inconclusive"
    assert grading.grades[0].observed == "三只"
    assert grading.verdict == "undetermined"
    assert grading.score is None


def test_missing_answer_is_judge_error_and_blocks_verdict():
    check = boolean_check()
    grading = grade_case("trial-g5", [check], {})
    assert grading.grades[0].status == "error"
    assert grading.verdict == "undetermined"


def test_unknown_dependency_answer_propagates_inconclusive():
    """父项无法判定时子项不可判,整条用例不产生假通过。"""
    checks = [
        boolean_check("has_cup", True),
        Check(id="cup_red", kind="boolean", question="杯子红色?", expected=True, dependsOn=["has_cup"]),
    ]
    answers = {
        "has_cup": Answer("has_cup", None, evidence="图片模糊"),
        "cup_red": Answer("cup_red", True),
    }
    grading = grade_case("trial-g6", checks, answers)
    by_id = {grade.checkId: grade for grade in grading.grades}
    assert by_id["has_cup"].status == "inconclusive"
    assert by_id["cup_red"].status == "inconclusive"
    assert grading.verdict == "undetermined"


def test_weighted_score_uses_check_weights():
    checks = [
        Check(id="chk-a", kind="boolean", question="a?", expected=True, weight=3),
        Check(id="chk-b", kind="boolean", question="b?", expected=True, weight=1),
    ]
    answers = {"chk-a": Answer("chk-a", True), "chk-b": Answer("chk-b", False)}
    grading = grade_case("trial-g7", checks, answers)
    assert grading.verdict == "fail"
    assert grading.score == 0.75  # (3*1 + 1*0) / 4


def test_soft_check_fail_does_not_fail_case_but_lowers_score():
    """非必需软项失败:用例通过,分数反映扣减(硬/软分离,§10.5)。"""
    checks = [
        boolean_check("has_cat", True),
        Check(id="aesthetic", kind="boolean", question="构图是否美观?", expected=True, required=False),
    ]
    answers = {"has_cat": Answer("has_cat", True), "aesthetic": Answer("aesthetic", False)}
    grading = grade_case("trial-g8", checks, answers)
    assert grading.verdict == "pass"
    assert grading.score == 0.5


def test_stub_judge_marks_synthetic_source():
    check = boolean_check()
    result = StubJudge().grade_artifact(b"png", "image/png", [check])
    assert result.answers["has_cat"].observed is True
    assert result.answers["has_cat"].source == "stub"
