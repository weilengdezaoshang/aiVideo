"""UI options reflect adapters actually implemented in this application."""


def generation_capabilities(config, models):
    cloud = config.provider == "cloud"
    vendor = config.cloudVendor
    image = dict(
        supported=True,
        models=models,
        referenceLimit=1 if not cloud or vendor in {"aliyun", "siliconflow"} else 0,
        referenceWeightAdjustable=not cloud,
        ratios=["1:1", "16:9", "9:16", "4:3", "3:4"],
        resolutions=[512, 1024, 2048],
        denoise=not cloud,
        countMax=1,
    )
    if cloud and vendor == "openai":
        image.update(ratios=["1:1"], resolutions=[1024])
    if cloud and vendor == "aliyun":
        image["resolutions"] = [1024, 2048]
    if not cloud:
        video = dict(
            supported=True,
            reason="",
            models=[dict(id=config.videoModel or "auto", name=config.videoModel or "自动选择视频模型")],
            referenceLimit=1,
            referenceModes=["firstFrame"],
            sizes=[],
            ratios=["16:9", "9:16", "1:1"],
            resolutions=[512, 768],
            durations=[4, 6, 8, 12],
            countMax=1,
        )
    elif vendor == "aliyun":
        # 通义万相 t2v:DashScope 异步任务,档位与协议形状以官方文档与实测为准(技术方案 v0.1 §5.3)。
        # sizes 为可直接下发的精确画幅;i2v 未开放,故 referenceLimit=0。
        video = dict(
            supported=bool(config.videoModel),
            reason="" if config.videoModel else "请先在设置中配置视频模型(如 wanx2.1-t2v-turbo)",
            models=[dict(id=config.videoModel or "auto", name=config.videoModel or "自动选择视频模型")],
            referenceLimit=0,
            sizes=["1280x720", "720x1280", "960x960"],
            ratios=["16:9", "9:16", "1:1"],
            resolutions=[1280, 960],
            durations=[5],
            countMax=1,
        )
    else:
        video = dict(
            supported=False,
            reason=f"当前厂商({vendor})的视频接入尚未验证,请切换阿里云百炼、Mock 或 ComfyUI",
            models=[dict(id=config.videoModel or "auto", name=config.videoModel or "自动选择视频模型")],
            referenceLimit=0,
            sizes=[],
            ratios=["16:9", "9:16", "1:1"],
            resolutions=[],
            durations=[],
            countMax=1,
        )
    return dict(provider=config.provider, image=image, video=video)
