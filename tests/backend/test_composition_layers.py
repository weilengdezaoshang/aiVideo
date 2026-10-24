"""Deterministic text layout and composition. The pinned Noto file is the only CJK face."""

import asyncio
import hashlib
import io
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend.agent.admission.tool_types import TypedTask, review_tool_types
from backend.agent.media.catalog import Catalog
from backend.agent.media.contracts import CatalogConfig
from backend.core.composition.api import install
from backend.core.composition.cmap import covered_codepoints
from backend.core.composition.compose import compose_layers
from backend.core.composition.errors import CompositionError
from backend.core.composition.fonts import FONT_PATH, FONT_SHA256, FontFace, bundled_registry
from backend.core.composition.layout import layout_text
from backend.core.composition.persist import materialize_text_reference
from backend.core.composition.presets import PRESETS
from backend.core.execution.contracts import WorkerSettings
from backend.core.execution.layout import canvas_insert_allowed, place_layered
from backend.core.execution.local import LocalAdapter
from backend.core.tools import build_catalog
from backend.core.tools.contracts import ToolSettings

COPY = "月圆人团圆"
LIMITS = dict(min_font_size=8, max_font_size=512, max_copy_chars=200, min_edge=64, max_edge=4096,
              line_height=1.3, max_lines=32)


def _layout(**overrides):
    params = {"copy": COPY, "font_id": "noto-sans-sc", "size": 64, "color": "#112233",
              "alignment": "center", "region": "title", "preset": "feed_square"}
    params.update(overrides)
    return layout_text(params, bundled_registry(), **LIMITS)


def _png(size, color):
    image = Image.new("RGBA", size, color)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    return raw.getvalue()


def _compose(**kwargs):
    base = dict(width=None, height=None, preset="feed_square", texts=[], registry=bundled_registry(),
                min_edge=64, max_edge=4096, subject_margin_ratio=0, max_pixels=4_000_000,
                background={"kind": "color", "color": "#FFFFFF"})
    base.update(kwargs)
    return compose_layers(**base)


def test_pinned_font_covers_the_title_and_rejects_holes():
    face = bundled_registry().get("noto-sans-sc")
    assert face.sha256 == FONT_SHA256
    assert hashlib.sha256(FONT_PATH.read_bytes()).hexdigest() == FONT_SHA256
    covered = covered_codepoints(FONT_PATH)
    assert all(ord(char) in covered for char in COPY)
    assert ord(" ") in covered
    assert ord("\u0301") in covered
    assert 0x1F600 not in covered
    try:
        _layout(copy=COPY + "😀")
    except CompositionError as exc:
        assert exc.code == "missing_glyph"
    else:
        raise AssertionError("missing_glyph")
    kept = _layout(copy="月 圆\n人")
    assert kept["copy"] == "月 圆\n人"
    assert [line["text"] for line in kept["lines"]] == ["月 圆", "人"]
    try:
        _layout(copy="月", size=400, width=64, height=400, preset=None, region="title")
    except CompositionError as exc:
        assert exc.code == "overflow"
    else:
        raise AssertionError("overflow")


def test_title_matches_pillow_line_box():
    laid = _layout()
    assert laid["copy"] == COPY
    assert laid["font_id"] == "noto-sans-sc"
    line = laid["lines"][0]
    image = Image.new("L", (1200, 400), 0)
    from PIL import ImageDraw, ImageFont
    font = ImageFont.truetype(FONT_PATH, 64)
    ImageDraw.Draw(image).text((line["x"], line["y"]), line["text"], font=font, fill=255, anchor="lt")
    pixels = image.load()
    ys = [y for y in range(image.height) if any(pixels[x, y] for x in range(image.width))]
    assert ys and min(ys) >= line["y"]
    assert max(ys) < line["y"] + line["height"]


def test_empty_and_unproven_subjects_fail_closed():
    empty = _png((8, 8), (0, 0, 0, 0))
    try:
        _compose(subject_png=empty, subject_sha256=hashlib.sha256(empty).hexdigest(), subject_asset_id="s")
    except CompositionError as exc:
        assert exc.code == "need_subject"
    else:
        raise AssertionError("empty")
    opaque = _png((8, 8), (1, 2, 3, 255))
    try:
        _compose(subject_png=opaque, subject_sha256=hashlib.sha256(opaque).hexdigest(), subject_asset_id="s")
    except CompositionError as exc:
        assert exc.code == "need_subject"
    else:
        raise AssertionError("opaque")
    try:
        _compose(subject_png=b"not-a-png", subject_sha256=hashlib.sha256(b"not-a-png").hexdigest(), subject_asset_id="s")
    except CompositionError as exc:
        assert exc.code == "param_invalid"
    else:
        raise AssertionError("bad image")
    try:
        _compose(background={"kind": "color", "color": "#GGGGGG"})
    except CompositionError as exc:
        assert exc.code == "invalid_color"
    else:
        raise AssertionError("color")


def test_background_only_and_masked_opaque_subject():
    _png_bytes, manifest = _compose(texts=[_layout()])
    assert [layer["role"] for layer in manifest["layers"]] == ["background", "text"]
    assert manifest["layers"][1]["text"] == COPY
    opaque = _png((32, 32), (9, 9, 9, 255))
    mask = Image.new("L", (32, 32), 0)
    mask.putpixel((1, 1), 255)
    raw = io.BytesIO()
    mask.save(raw, format="PNG")
    mask_bytes = raw.getvalue()
    _out, proven = _compose(subject_png=opaque, subject_sha256=hashlib.sha256(opaque).hexdigest(),
                            subject_asset_id="subj", subject_proof={"source": "cutout",
                            "mask_sha256": hashlib.sha256(mask_bytes).hexdigest()}, mask_png=mask_bytes)
    assert [layer["role"] for layer in proven["layers"]].count("subject") == 1


def _ink_box(png: bytes, predicate):
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    pixels = image.load()
    found = None
    for y in range(image.height):
        for x in range(image.width):
            if predicate(pixels[x, y]):
                if found is None:
                    found = [x, y, x, y]
                else:
                    found[0] = min(found[0], x)
                    found[1] = min(found[1], y)
                    found[2] = max(found[2], x)
                    found[3] = max(found[3], y)
    return found


def test_three_sizes_keep_a_real_cutout():
    image = Image.new("RGBA", (540, 540), (200, 10, 10, 255))
    image.putpixel((0, 0), (0, 0, 0, 0))
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    subject = raw.getvalue()
    digest = hashlib.sha256(subject).hexdigest()
    wide = _png((640, 120), (0, 180, 0, 255))
    wide_sha = hashlib.sha256(wide).hexdigest()
    for preset in PRESETS:
        png_bytes, manifest = _compose(preset=preset, texts=[_layout(copy="月圆\n人团圆", preset=preset, alignment="left")],
                                       subject_png=subject, subject_sha256=digest, subject_asset_id="subject-1",
                                       subject_margin_ratio=0.08,
                                       background={"kind": "image"}, background_png=wide, background_sha256=wide_sha)
        placed = next(layer for layer in manifest["layers"] if layer["role"] == "subject")["transform"]
        ink = _ink_box(png_bytes, lambda px: px[0] > 150 and px[1] < 40 and px[3] > 0)
        assert ink is not None
        assert placed["x"] - 1 <= ink[0] and placed["y"] - 1 <= ink[1]
        assert ink[2] <= placed["x"] + placed["width"] + 1
        assert ink[3] <= placed["y"] + placed["height"] + 1
        assert ink[2] - ink[0] > placed["width"] * 0.9
        canvas = manifest["canvas"]
        if canvas["width"] != canvas["height"]:
            assert placed["width"] < max(canvas["width"], canvas["height"])
        green = _ink_box(png_bytes, lambda px: px[1] > 150 and px[0] < 40 and px[3] > 0)
        assert green is not None
        assert green[3] - green[1] < canvas["height"] * 0.5
        assert png_bytes
        text = next(layer for layer in manifest["layers"] if layer["role"] == "text")
        assert "".join(line["text"] for line in text["lines"]) == "月圆人团圆"
        assert "\n" not in "".join(line["text"] for line in text["lines"])
        assert text["text"] == "月圆\n人团圆"


def test_replay_and_text_materialization():
    manifest = {"schema": "composition-layers-v1", "canvas": {"width": 100, "height": 100}, "fit": "contain",
                "layers": [
                    {"id": "background", "role": "background", "kind": "color", "color": "#FFFFFF",
                     "transform": {"x": 0, "y": 0, "width": 100, "height": 100}},
                    {"id": "text-0", "role": "text", "kind": "text", "text": COPY, "font_size": 20,
                     "font_id": "noto-sans-sc", "color": "#111111", "align": "center", "region": "title",
                     "transform": {"x": 8, "y": 6, "width": 84, "height": 22},
                     "lines": [{"text": COPY, "x": 10, "y": 8, "width": 40, "height": 12}]},
                ]}
    asset = type("A", (), {"id": "preview", "ext": "png"})()
    data = {"objects": {}, "order": []}
    settings = WorkerSettings(ticket_key="x" * 32)
    place_layered(data, "task-1", asset, {"mode": "right"}, settings, manifest)
    once = list(data["order"])
    place_layered(data, "task-1", asset, {"mode": "right"}, settings, manifest)
    assert data["order"] == once
    assert data["objects"]["generated-task-1:text-0"]["lineBoxes"][0]["text"] == COPY
    assert canvas_insert_allowed(cancel_requested=True, run_cancelled=False, task_cancelled=False, permitted=True) is False
    blob = json.dumps({"copy": COPY, "font_id": "noto-sans-sc"}).encode()
    trusted = materialize_text_reference({"copy": "被篡改", "font_id": "other", "lines": []},
        storage_key="text-layouts/a.json", sha256=hashlib.sha256(blob).hexdigest(), asset_id="asset",
        blob=blob, core_asset_id="core", core_asset_version_id="ver")
    assert trusted["copy"] == COPY
    assert trusted["font_id"] == "noto-sans-sc"
    assert "lines" not in trusted or trusted.get("lines") != []


def test_font_route_and_type_gate():
    app = FastAPI()
    install(app)
    client = TestClient(app)
    listed = client.get("/api/fonts")
    assert listed.status_code == 200
    assert listed.json()["fonts"][0]["sha256"] == FONT_SHA256
    assert "path" not in listed.text
    good = client.get("/api/fonts/noto-sans-sc", params={"sha256": FONT_SHA256})
    assert good.status_code == 200
    assert hashlib.sha256(good.content).hexdigest() == FONT_SHA256
    assert client.get("/api/fonts/noto-sans-sc", params={"sha256": "0" * 64}).status_code == 404
    assert client.get("/api/fonts/missing", params={"sha256": FONT_SHA256}).status_code == 404
    catalog = build_catalog(Catalog(CatalogConfig(version="catalog-unit", environment="development", enabled=True,
        capabilities=[])), "catalog-unit")
    tasks = [
        TypedTask(id="title", tool="text_layout", depends_on=[], parameters={
            "copy": COPY, "font_id": "noto-sans-sc", "size": 64, "color": "#111111",
            "alignment": "center", "region": "title", "preset": "feed_square"}),
        TypedTask(id="poster", tool="compose_layers", depends_on=["cut", "title"], parameters={
            "preset": "feed_square", "fit": "contain",
            "background": {"kind": "image", "image": "$cut.output"},
            "subject": "$cut.output", "text_layers": ["$title.output"]}),
    ]
    cut = TypedTask(id="cut", tool="cutout", depends_on=[], parameters={
        "image": {"asset_id": "img", "media_type": "image"},
        "mask": {"handle": "h", "media_type": "mask", "source_asset_id": "img",
                 "source_sha256": "a" * 64, "width": 8, "height": 8, "operation_id": "op"}})
    review = review_tool_types([cut, *tasks], catalog, catalog.lock)
    assert review.valid, review.issues
    plain = TypedTask(id="poster2", tool="compose_layers", depends_on=["title"], parameters={
        "preset": "story_portrait", "fit": "contain", "background": {"kind": "color", "color": "#112233"},
        "text_layers": ["$title.output"]})
    assert review_tool_types([tasks[0], plain], catalog, catalog.lock).valid
    priced = ToolSettings(text_layout_credits=2, compose_credits=3)
    priced_catalog = build_catalog(Catalog(CatalogConfig(version="priced", environment="development", enabled=True,
        capabilities=[])), "priced", priced)
    assert priced_catalog.get_tool("compose_layers", priced_catalog.lock.version).price.credits == 3
    binding = catalog.get_tool("text_layout", catalog.lock.version).execution.binding["composition"]
    assert binding["fonts"][0]["id"] == "noto-sans-sc"
    assert binding["fonts"][0]["sha256"] == FONT_SHA256
    assert "path" not in json.dumps(binding)


def test_published_binding_ignores_later_settings(monkeypatch):
    first = build_catalog(Catalog(CatalogConfig(version="v1", environment="development", enabled=True,
        capabilities=[])), "v1", ToolSettings(text_line_height=1.3))
    monkeypatch.setenv("AIVERO_TOOLS_TEXT_LINE_HEIGHT", "2.6")
    tool = first.get_tool("text_layout", first.lock.version)
    adapter = LocalAdapter(tool, None, Path("."))
    result = asyncio.run(adapter.compute("reg", {
        "copy": COPY, "font_id": "noto-sans-sc", "size": 48, "color": "#112233",
        "alignment": "left", "region": "title", "preset": "feed_square"}))
    assert result.status == "succeeded"
    assert result.output["lines"][0]["height"] == round(48 * 1.3)
    pinned = dict(tool.execution.binding["composition"])
    pinned["fonts"] = [{**pinned["fonts"][0], "sha256": "b" * 64}]
    broken = tool.model_copy(update={"execution": tool.execution.model_copy(update={"binding": {"composition": pinned}})})
    mismatch = asyncio.run(LocalAdapter(broken, None, Path(".")).compute("reg", {
        "copy": COPY, "font_id": "noto-sans-sc", "size": 48, "color": "#112233",
        "alignment": "left", "region": "title", "preset": "feed_square"}))
    assert mismatch.status == "failed"
    assert mismatch.failure == "param_invalid"


def test_nonzero_face_index_is_rejected():
    face = FontFace("noto-sans-sc", "v", FONT_SHA256, FONT_PATH, 1)
    try:
        face.load(16)
    except CompositionError as exc:
        assert exc.code == "unsupported_collection"
    else:
        raise AssertionError("face index was accepted")


def test_custom_registry_route_does_not_publish_a_path(monkeypatch, tmp_path):
    registry = {"root": str(tmp_path), "fonts": [{"id": "custom", "version": "1", "sha256": "a" * 64, "path": "a.otf"}]}
    path = tmp_path / "fonts.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setenv("AIVERO_TOOLS_FONT_REGISTRY_PATH", str(path))
    app = FastAPI()
    install(app)
    client = TestClient(app)
    response = client.get("/api/fonts")
    assert response.status_code == 401
    assert str(path) not in response.text
    assert "a.otf" not in response.text


def test_font_size_limit_is_configurable_and_frozen():
    import pytest
    from backend.core.composition.fonts import load_registry, FONT_ID
    from backend.core.composition.snapshot import composition_snapshot
    from backend.core.tools.contracts import ToolSettings
    from backend.core.composition.errors import CompositionError
    with pytest.raises(CompositionError):
        load_registry("", max_bytes=1).get(FONT_ID)
    configured = ToolSettings(max_font_bytes=20 * 1024 * 1024)
    assert composition_snapshot(configured)["max_font_bytes"] == configured.max_font_bytes
