"""Fake recognition only. Real mask composition is local and creates no stored assets."""
import io

import pytest
from PIL import Image
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from backend.agent.admission.tool_types import TypedTask, review_tool_types
from backend.agent.media.catalog import Catalog
from backend.core.tools import build_catalog, estimate_cost
from backend.core.tools.detection import (Candidate, DetectionError, DetectionResult, ImageIdentity,
    RawCandidate, normalize_candidates, normalize_rembg, validate_mask_source)
from backend.storage import apply_mask

SOURCE = ImageIdentity(asset_id="source", sha256="a" * 64, width=8, height=8)


def png(mode="L", value=255, size=(8, 8)):
    stream = io.BytesIO()
    Image.new(mode, size, value).save(stream, format="PNG")
    return stream.getvalue()


class FakeDetection:
    def __init__(self, count):
        self.count = count

    def detect(self):
        return [RawCandidate(png(), f"mask-{n}", f"主体 {n}", 0.9 - n * 0.1) for n in range(self.count)]


@pytest.mark.parametrize("count", [0, 1, 2], ids=["no-subject", "single-subject", "multiple-subjects"])
def test_fake_detection_outcomes_and_schema(count):
    result = normalize_candidates(FakeDetection(count).detect(), SOURCE, "operation")
    assert result.candidate_count == count
    assert result.result_type == ("detected" if count else "no_subject")
    c = build_catalog(Catalog.load(None), "v1")
    definition = c.get_tool("detect_subject", "v1")
    Draft202012Validator(definition.result_schema).validate(result.model_dump())
    assert estimate_cost(definition, {"image": {"asset_id": "source", "media_type": "image"}}) == 0
    for candidate in result.candidates:
        assert candidate.mask.media_type == "mask" and candidate.confidence is not None
        validate_mask_source(candidate.mask, SOURCE)


def test_rembg_aggregate_does_not_invent_instances_or_confidence():
    result = normalize_rembg(png(), SOURCE, "op", "handle")
    assert result.granularity == "combined_foreground"
    assert result.candidate_count == 1
    assert result.candidates[0].label == "合并前景"
    assert result.candidates[0].confidence is None
    assert result.candidates[0].confidence_source == "unknown"
    assert normalize_rembg(png(value=0), SOURCE, "op", "handle").result_type == "no_subject"


@pytest.mark.parametrize("bad", [b"", b"not an image", png(mode="RGB", value="red"), png(size=(2, 2))])
def test_bad_masks_fail_explicitly_instead_of_becoming_no_subject(bad):
    with pytest.raises(DetectionError):
        normalize_rembg(bad, SOURCE, "op", "handle")


def test_candidate_count_and_confidence_are_validated():
    with pytest.raises(ValidationError):
        DetectionResult(result_type="no_subject", candidate_count=1, granularity="instances", candidates=[])
    candidate = normalize_rembg(png(), SOURCE, "op", "handle").candidates[0]
    with pytest.raises(ValidationError):
        Candidate.model_validate({**candidate.model_dump(), "confidence": 0.8})
    with pytest.raises(DetectionError, match="source_mismatch"):
        validate_mask_source(candidate.mask, SOURCE.model_copy(update={"sha256": "b" * 64}))


def tasks(mask="$t1.candidates[0].mask", image_id="source"):
    image = {"asset_id": image_id, "media_type": "image"}
    return [TypedTask(id="t1", tool="detect_subject", parameters={"image": image}, depends_on=[]),
            TypedTask(id="t2", tool="cutout", parameters={"image": image, "mask": mask}, depends_on=["t1"])]


def test_detect_to_cutout_mask_chain_passes_gate_two_with_runtime_constraints():
    c = build_catalog(Catalog.load(None), "v1")
    result = review_tool_types(tasks(), c, c.lock)
    assert result.valid
    assert result.execution_constraints[0]["when"] == {"result_type": "detected"}
    detected = normalize_rembg(png(), SOURCE, "op", "handle").model_dump()
    assert review_tool_types(tasks(), c, c.lock, results={"t1": detected}).valid
    assert not review_tool_types(tasks(image_id="different"), c, c.lock, results={"t1": detected}).valid


@pytest.mark.parametrize("ref", ["$t1.output", "$t1.candidates[0].label", "$missing.output", "$t1.candidates[3].mask"])
def test_bad_type_missing_dependency_or_index_is_rejected(ref):
    c = build_catalog(Catalog.load(None), "v1")
    detected = normalize_rembg(png(), SOURCE, "op", "handle").model_dump()
    result = review_tool_types(tasks(mask=ref), c, c.lock, results={"t1": detected})
    assert not result.valid


def test_no_subject_cannot_feed_cutout_and_model_does_not_choose_a_candidate():
    c = build_catalog(Catalog.load(None), "v1")
    empty = normalize_rembg(png(value=0), SOURCE, "op", "handle").model_dump()
    result = review_tool_types(tasks(), c, c.lock, results={"t1": empty})
    assert not result.valid and result.issues[0]["code"] == "producer_result_branch_mismatch"
    multi = normalize_candidates(FakeDetection(2).detect(), SOURCE, "op").model_dump()
    assert review_tool_types(tasks(mask="$t1.candidates[1].mask"), c, c.lock, results={"t1": multi}).valid
    undeclared = tasks()
    undeclared[1] = undeclared[1].model_copy(update={"depends_on": []})
    assert not review_tool_types(undeclared, c, c.lock).valid


def test_composition_produces_new_png_and_original_bytes_are_unchanged():
    original = png(mode="RGB", value="red")
    before = bytes(original)
    mask = png(value=128)
    result = apply_mask(original, mask)
    assert original == before and result != original
    with Image.open(io.BytesIO(result)) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 128
