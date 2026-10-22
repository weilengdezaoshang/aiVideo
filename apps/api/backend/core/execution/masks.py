"""Task-bound execution intermediates; never the manual editor's selection cache."""
import hashlib
import io
from PIL import Image, ImageChops
from backend.infrastructure.artifacts import ArtifactSpool


def mask_path(root, handle):
    return root / "agent-task-masks" / (hashlib.sha256(handle.encode()).hexdigest() + ".png")


def save_mask(root, handle, data):
    path = mask_path(root, handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    ArtifactSpool._atomic(path, data)


def selected_mask(root, reference):
    handles = reference.get("_members") or [reference["handle"]]
    merged = None
    for handle in handles:
        with Image.open(mask_path(root, handle)) as image:
            if image.mode != "L" or image.size != (reference["width"], reference["height"]):
                raise ValueError("mask source dimensions mismatch")
            merged = image.copy() if merged is None else ImageChops.lighter(merged, image)
    output = io.BytesIO()
    merged.save(output, format="PNG")
    return output.getvalue()
