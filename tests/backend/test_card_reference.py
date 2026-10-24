"""The 30-case card coreference gate. All cases must pass; no model calls."""
from backend.core.card_reference import CardCandidate, resolve_card_reference
from backend.evaluation.card_reference_cases import case_rows, load_cases


def test_fixture_contains_thirty_cases_and_matches_inline_rows():
    cases = load_cases()
    assert len(cases) >= 30
    assert [case.case_id for case in cases] == [row["case_id"] for row in case_rows()]


def test_all_thirty_cases_resolve_as_expected():
    failures = []
    for case in load_cases():
        resolution = resolve_card_reference(
            [CardCandidate.model_validate(card) for card in case.cards], case.utterance)
        if (resolution.status != case.expected_status or resolution.reason != case.expected_reason
                or resolution.card_id != case.expected_card_id):
            failures.append((case.case_id, resolution.status, resolution.reason,
                             resolution.card_id, case.expected_status, case.expected_reason,
                             case.expected_card_id))
    assert not failures, failures


def test_delisted_cards_never_resolve():
    resolution = resolve_card_reference(
        [CardCandidate(card_id="k", card_kind="style", name="水彩", available=False)],
        "用这个风格生成")
    assert resolution.status == "unavailable"
    assert resolution.reason == "no_style_card"


def test_ambiguous_never_guesses():
    resolution = resolve_card_reference(
        [CardCandidate(card_id="a", card_kind="subject", name="橘子", subject_category="猫"),
         CardCandidate(card_id="b", card_kind="subject", name="布丁", subject_category="猫")],
        "这只猫戴帽子")
    assert resolution.status == "ambiguous"
    assert set(resolution.candidate_ids) == {"a", "b"}
    assert resolution.card_id is None
