"""Composition geometry remains native on canvas and respects the anchor CAS."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.layout import place_layered


def _manifest():
    return {"canvas": {"width": 1080, "height": 1920}, "layers": [
        {"role": "background", "kind": "color", "color": "#FFFFFF",
         "transform": {"x": 0, "y": 0, "width": 1080, "height": 1920}},
        {"role": "subject", "kind": "image", "asset_id": "subject",
         "transform": {"x": 200, "y": 600, "width": 680, "height": 680}},
    ]}


def test_portrait_canvas_uses_one_scale_and_keeps_square_subject_square():
    data = {"objects": {}, "order": []}
    node, _, rect = place_layered(data, "task", SimpleNamespace(id="preview", ext="png"),
        {"mode": "right"}, WorkerSettings(), _manifest())
    assert rect["width"] / rect["height"] == pytest.approx(1080 / 1920)
    subject = data["objects"][node + ":subject"]
    assert subject["width"] == pytest.approx(subject["height"])
    assert subject["x"] >= rect["x"] and subject["y"] >= rect["y"]
    assert subject["x"] + subject["width"] <= rect["x"] + rect["width"]
    assert subject["y"] + subject["height"] <= rect["y"] + rect["height"]


def test_anchor_replacement_and_replay_do_not_duplicate_or_erase_user_edits():
    data = {"objects": {"anchor": {"id": "anchor", "kind": "image", "x": 10, "y": 20,
        "width": 120, "height": 160, "assetVersion": 4}}, "order": ["anchor"]}
    placement = {"mode": "replace", "anchor_id": "anchor", "anchor_version": 4}
    args = ("task", SimpleNamespace(id="preview", ext="png"), placement, WorkerSettings(), _manifest())
    node, conflict, _ = place_layered(data, *args)
    assert node == "anchor" and conflict is False
    data["objects"]["anchor:subject"]["x"] += 7
    edited = deepcopy(data)
    assert place_layered(data, *args)[0] == "anchor"
    assert data == edited


def test_changed_anchor_keeps_original_and_places_a_new_group():
    old = {"id": "anchor", "kind": "image", "x": 0, "y": 0,
           "width": 120, "height": 160, "assetVersion": 5}
    data = {"objects": {"anchor": deepcopy(old)}, "order": ["anchor"]}
    node, conflict, _ = place_layered(data, "task", SimpleNamespace(id="preview", ext="png"),
        {"mode": "replace", "anchor_id": "anchor", "anchor_version": 4}, WorkerSettings(), _manifest())
    assert node != "anchor" and conflict is True
    assert data["objects"]["anchor"] == old
