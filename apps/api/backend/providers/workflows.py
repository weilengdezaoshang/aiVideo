"""ComfyUI node graphs, preserving image, LTXV and Wan (single/dual UNet) paths."""

import math


def node(kind, **inputs):
    return {"class_type": kind, "inputs": inputs}


def aligned(value, multiple):
    return max(multiple, math.floor(value / multiple + 0.5) * multiple)


def image_workflow(p, seed, image=None, mask=None):
    w = {
        "4": node("CheckpointLoaderSimple", ckpt_name=p.model),
        "6": node("CLIPTextEncode", text=p.prompt, clip=["4", 1]),
        "7": node("CLIPTextEncode", text=p.negativePrompt, clip=["4", 1]),
        "3": node(
            "KSampler",
            seed=seed,
            steps=p.steps,
            cfg=p.cfgScale,
            sampler_name=p.sampler,
            scheduler=p.scheduler,
            denoise=p.denoise if image else 1,
            model=["4", 0],
            positive=["6", 0],
            negative=["7", 0],
            latent_image=["14" if image and mask else "11" if image else "5", 0],
        ),
        "8": node("VAEDecode", samples=["3", 0], vae=["4", 2]),
        "9": node("SaveImage", filename_prefix="FRAYUNE", images=["8", 0]),
    }
    if image:
        w["10"] = node("LoadImage", image=image)
        w["11"] = node("VAEEncode", pixels=["10", 0], vae=["4", 2])
        if mask:
            w["12"] = node("LoadImage", image=mask)
            w["13"] = node("ImageToMask", image=["12", 0], channel="red")
            w["14"] = node("SetLatentNoiseMask", samples=["11", 0], mask=["13", 0])
    else:
        w["5"] = node("EmptyLatentImage", width=p.width, height=p.height, batch_size=1)
    return w


def video_output(w, source, fps):
    w["24"] = node("CreateVideo", images=[source, 0], fps=fps)
    w["25"] = node("SaveVideo", video=["24", 0], filename_prefix="FRAYUNE_video", format="mp4")


def ltx_workflow(p, seed, model, image=None):
    dims = dict(
        width=aligned(p.width, 32),
        height=aligned(p.height, 32),
        length=8 * max(1, math.floor(math.floor(p.durationSec * p.fps + 0.5) / 8 + 0.5)) + 1,
        batch_size=1,
    )
    w = {
        "4": node("CheckpointLoaderSimple", ckpt_name=model),
        "6": node("CLIPTextEncode", text=p.prompt, clip=["4", 1]),
        "7": node("CLIPTextEncode", text=p.negativePrompt, clip=["4", 1]),
        "3": node(
            "KSampler",
            seed=seed,
            steps=p.steps,
            cfg=p.cfgScale,
            sampler_name=p.sampler,
            scheduler=p.scheduler,
            denoise=1,
            model=["4", 0],
            positive=["13", 0],
            negative=["13", 1],
            latent_image=["12", 2 if image else 0],
        ),
        "8": node("VAEDecode", samples=["3", 0], vae=["4", 2]),
    }
    if image:
        w["10"] = node("LoadImage", image=image)
        w["12"] = node(
            "LTXVImgToVideo",
            positive=["6", 0],
            negative=["7", 0],
            vae=["4", 2],
            image=["10", 0],
            **dims,
        )
    else:
        w["12"] = node("EmptyLTXVLatentVideo", **dims)
    w["13"] = node(
        "LTXVConditioning",
        frame_rate=p.fps,
        positive=["12", 0] if image else ["6", 0],
        negative=["12", 1] if image else ["7", 0],
    )
    video_output(w, "8", p.fps)
    return w


def wan_workflow(p, seed, model, clip, vae, image=None, low_noise=None):
    dims = dict(
        width=aligned(p.width, 16),
        height=aligned(p.height, 16),
        length=max(1, math.floor(math.floor(p.durationSec * p.fps + 0.5) / 4 + 0.5) * 4),
        batch_size=1,
    )
    cond = dict(
        positive=["12", 0] if image else ["6", 0], negative=["12", 1] if image else ["7", 0]
    )
    w = {
        "1": node("UNETLoader", unet_name=model, weight_dtype="default"),
        "2": node("CLIPLoader", clip_name=clip, type="wan"),
        "3": node("VAELoader", vae_name=vae),
        "6": node("CLIPTextEncode", text=p.prompt, clip=["2", 0]),
        "7": node("CLIPTextEncode", text=p.negativePrompt, clip=["2", 0]),
        "14": node("ModelSamplingSD3", model=["1", 0], shift=8),
    }
    if image:
        w["10"] = node("LoadImage", image=image)
        w["12"] = node(
            "WanImageToVideo",
            positive=["6", 0],
            negative=["7", 0],
            vae=["3", 0],
            start_image=["10", 0],
            **dims,
        )
    else:
        w["12"] = node("EmptyHunyuanLatentVideo", **dims)
    if low_noise:
        w["15"] = node("UNETLoader", unet_name=low_noise, weight_dtype="default")
        w["16"] = node("ModelSamplingSD3", model=["15", 0], shift=8)
        w["17"] = node(
            "BasicScheduler", model=["14", 0], scheduler=p.scheduler, steps=p.steps, denoise=1
        )
        w["18"] = node("SplitSigmas", sigmas=["17", 0], step=max(1, math.ceil(p.steps / 2)))
        w["19"] = node("KSamplerSelect", sampler_name=p.sampler)
        w["20"] = node(
            "SamplerCustom",
            model=["14", 0],
            add_noise=True,
            noise_seed=seed,
            cfg=p.cfgScale,
            sampler=["19", 0],
            sigmas=["18", 0],
            latent_image=["12", 2 if image else 0],
            **cond,
        )
        w["21"] = node(
            "SamplerCustom",
            model=["16", 0],
            add_noise=False,
            noise_seed=0,
            cfg=p.cfgScale,
            sampler=["19", 0],
            sigmas=["18", 1],
            latent_image=["20", 0],
            **cond,
        )
        output = "21"
    else:
        w["22"] = node(
            "KSampler",
            seed=seed,
            steps=p.steps,
            cfg=p.cfgScale,
            sampler_name=p.sampler,
            scheduler=p.scheduler,
            denoise=1,
            model=["14", 0],
            latent_image=["12", 2 if image else 0],
            **cond,
        )
        output = "22"
    w["23"] = node("VAEDecode", samples=[output, 0], vae=["3", 0])
    video_output(w, "23", p.fps)
    return w
