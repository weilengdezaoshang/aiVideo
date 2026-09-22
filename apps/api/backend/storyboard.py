"""Editable shot plans; generated media and timeline edits remain separate."""

from copy import deepcopy


def validate_storyboard(raw):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("分镜版本无效")
    if not isinstance(raw.get("outline"), str) or len(raw["outline"]) > 20000:
        raise ValueError("分镜大纲无效")
    shots = raw.get("shots")
    if not isinstance(shots, list) or len(shots) > 200:
        raise ValueError("分镜数量不能超过 200")
    ids = set()
    total = 0
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("镜头格式无效")
        ident = shot.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 100 or ident in ids:
            raise ValueError("镜头标识无效或重复")
        ids.add(ident)
        for key, limit in (("title", 500), ("visual", 4000), ("dialogue", 4000), ("camera", 1000)):
            if not isinstance(shot.get(key), str) or len(shot[key]) > limit:
                raise ValueError("镜头文本无效")
        frames = shot.get("durationFrames")
        if type(frames) is not int or not 1 <= frames <= 108000:
            raise ValueError("镜头时长无效")
        total += frames
        node = shot.get("nodeId")
        if node is not None and (not isinstance(node, str) or not node or len(node) > 100):
            raise ValueError("镜头关联节点无效")
        versions = shot.get("versions", [])
        if (
            not isinstance(versions, list)
            or len(versions) > 100
            or any(not isinstance(v, str) or not v or len(v) > 100 for v in versions)
            or len(set(versions)) != len(versions)
        ):
            raise ValueError("镜头版本列表无效")
        if type(shot.get("locked")) is not bool:
            raise ValueError("镜头锁定状态无效")
    if total > 108000:
        raise ValueError("分镜总时长不能超过一小时")
    return deepcopy(raw)


PLANNING_WORKFLOWS = {
    "general": "遵循用户给出的创作结构；信息不足时采用简洁、可拍摄的叙事。",
    "product": (
        "按产品广告规划：开场吸引注意、展示使用场景、演示用户提供的卖点、结尾行动引导。"
        "保持产品外观一致；只使用用户提供的品牌、参数和功效，不编造性能、认证或价格。"
        "把产品展示和人物动作分别写清，避免单镜头包含无法同时完成的动作。"
    ),
    "story": (
        "按故事短片规划：建立人物与目标、推进冲突、呈现转折、收束结局。"
        "在大纲明确主要角色的外观和场景，在各镜头保持服装、道具和空间连续性。"
        "台词与动作对应，镜头之间写清因果，避免无理由更换角色外观。"
    ),
    "explainer": (
        "按知识讲解规划：提出问题、逐步解释、用画面示例说明、归纳关键结论。"
        "每个镜头聚焦一个信息点，旁白与画面同步，给阅读和理解留出时间。"
        "不编造统计或引用；用户未提供的关键事实应在大纲标记为待核实。"
    ),
}


async def plan_storyboard(prompt, config, client, workflow="general"):
    import json
    import re
    from uuid import uuid4
    from .providers.cloud import chat

    if not isinstance(workflow, str) or workflow not in PLANNING_WORKFLOWS:
        raise ValueError("不支持的创作工作流")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("请提供不超过 12000 字的创作需求或剧本")
    if (
        config.provider != "cloud"
        or config.cloudVendor == "aliyun"
        or not config.cloudTextModel
        or not config.imageApiKey
    ):
        raise ValueError("请先配置支持 Chat Completions 的云端文本模型与密钥")
    system = (
        "你是视频分镜规划师。根据用户需求输出连贯的镜头序列，不调用工具，不声称已生成媒体。"
        "只输出 JSON 对象，格式为 {outline:字符串,shots:[{title:字符串,visual:画面描述字符串,"
        "dialogue:台词字符串,camera:运镜字符串,durationFrames:整数}]}。"
        "按30fps计算时长，遵守用户总时长，最多30镜头，总长最多一小时。"
        "每镜头必须包含全部字段；没有台词或运镜时填空字符串。"
    )
    system += "创作工作流要求：" + PLANNING_WORKFLOWS[workflow]
    text = await chat(client, config, prompt, system, max_tokens=6000, timeout=60)
    raw = json.loads(re.sub(r"^```(?:json)?\s*|```\s*$", "", text.strip()))
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("shots"), list)
        or not 1 <= len(raw["shots"]) <= 30
    ):
        raise ValueError("文本模型未返回有效分镜，请调整需求后重试")
    shots = []
    for shot in raw["shots"]:
        if not isinstance(shot, dict):
            raise ValueError("文本模型镜头格式无效")
        shots.append(
            {
                key: shot.get(key)
                for key in ("title", "visual", "dialogue", "camera", "durationFrames")
            }
        )
        shots[-1].update(id=str(uuid4()), nodeId=None, locked=False)
    return validate_storyboard(dict(version=1, outline=raw.get("outline"), shots=shots))
