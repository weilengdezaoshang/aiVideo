"""Explicitly synthetic assets for local development; no external model calls."""

import asyncio
import io
import random

from PIL import Image, ImageDraw

from ..models import with_reference_weight
from .base import Generated, Provider


class MockProvider(Provider):
    name = "mock"
    capacity = 2

    async def status(self):
        return {"ok": True, "detail": "演示模式(Mock),无需 GPU"}

    async def models(self):
        return [
            {"id": ident, "name": name}
            for ident, name in [
                ("mock-diffusion-xl", "Mock Diffusion XL(内置演示)"),
                ("mock-anime-v3", "Mock Anime v3(内置演示)"),
                ("mock-photo-real", "Mock PhotoReal(内置演示)"),
            ]
        ]

    async def samplers(self):
        return {
            "samplers": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "ddim", "uni_pc"],
            "schedulers": ["normal", "karras", "exponential", "sgm_uniform", "beta"],
        }

    async def generate(
        self, params, seed, progress, image=None, mask=None, external=None, external_task_id=None
    ):
        params = with_reference_weight(params, image is not None)
        for i in range(5):
            await asyncio.sleep(0.05)
            progress((i + 1) / 6, "演示生成中")
        return await asyncio.to_thread(self.render, params, seed, image, mask)

    @staticmethod
    def render(p, seed, ref, mask):
        rng = random.Random(seed)
        result = Image.new(
            "RGB", (p.width, p.height), tuple(rng.randrange(40, 180) for _ in range(3))
        )
        draw = ImageDraw.Draw(result)
        for _ in range(12):
            x, y = rng.randrange(p.width), rng.randrange(p.height)
            radius = rng.randrange(10, max(11, min(p.width, p.height) // 3))
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=tuple(rng.randrange(256) for _ in range(3)),
            )
        if ref:
            source = Image.open(io.BytesIO(ref.data)).convert("RGB").resize(result.size)
            result = Image.blend(source, result, p.denoise)
            if mask:
                alpha = Image.open(io.BytesIO(mask.data)).convert("L").resize(result.size)
                result = Image.composite(result, source, alpha)
        ImageDraw.Draw(result).text((12, 12), f"MOCK / seed {seed}", fill="white")
        output = io.BytesIO()
        if p.kind == "video":
            # Same animated-preview contract as the previous Mock, explicitly marked MOCK.
            frames = [result, result.transpose(Image.Transpose.FLIP_LEFT_RIGHT)]
            frames[0].save(
                output,
                "WEBP",
                save_all=True,
                append_images=frames[1:],
                duration=max(100, int(p.durationSec * 500)),
                loop=0,
            )
            return Generated(output.getvalue(), "webp")
        result.save(output, "PNG")
        return Generated(output.getvalue(), "png")
