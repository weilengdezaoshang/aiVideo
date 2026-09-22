"""Deterministic direction planning and constrained cutout assistant planning."""

import json
import re

from .providers.cloud import chat

STYLES = [
    "水墨",
    "水彩",
    "油画",
    "素描",
    "线稿",
    "像素",
    "赛博朋克",
    "蒸汽波",
    "国风",
    "浮世绘",
    "极简",
    "写实",
    "卡通",
    "动漫",
    "插画",
    "3d",
    "厚涂",
    "胶片",
    "黑白",
    "低多边形",
    "扁平",
]
DIMENSIONS = [
    ("远景氛围", "构图", "远景构图,主体完整入画,大面积留白交代环境氛围"),
    ("光影刻画", "光线", "近景构图,用强烈的方向光与阴影刻画主体的立体感"),
    ("场景叙事", "场景", "中景构图,把主体放进一个能讲故事的场景,加入环境细节"),
    ("特写情绪", "表达", "特写镜头,聚焦主体最具辨识度的细节,强化情绪张力"),
]
TOOLS = ["detect_mask", "wait_user_check_mask", "apply_cutout_place_right"]
LABELS = ["识别图片主体,准备蒙版", "请你检查蒙版", "生成透明图片并放到原图右侧"]


def directions(prompt):
    constraints, plain = [], []
    for clause in filter(None, (x.strip() for x in re.split(r"[,，、;；。\n]+", prompt.strip()))):
        if any(word in clause.lower() for word in STYLES) or re.search(
            r"不要|不能|避免|禁止|必须|一定要|无水|无人", clause
        ):
            if clause not in constraints:
                constraints.append(clause)
        else:
            plain.append(clause)
    subject = plain[0] if plain else prompt.strip()
    return dict(
        subject=subject,
        constraints=constraints,
        directions=[
            dict(
                slot=i,
                title=title,
                dimension=dim,
                prompt=",".join([subject, *plain[1:], *constraints, desc]),
            )
            for i, (title, dim, desc) in enumerate(DIMENSIONS)
        ],
    )


def cutout_plan():
    return dict(
        summary="识别主体 → 请你检查蒙版 → 生成透明图片并放到右侧",
        steps=[dict(key=k, label=v) for k, v in zip(TOOLS, LABELS)],
        meta=dict(sourceImages=1, outputImages=1, needManualConfirm=True),
    )


def rule_plan(request):
    if re.search(
        r"视频|配音|分镜|拼接|时间线|网页|搜索|批量|多张图|全部图片|上色|扩图|超清", request
    ):
        return dict(
            ok=False,
            reason="out_of_scope",
            message="当前助手只支持把一张图的主体抠出并放到原图旁，请拆开请求。",
        )
    if not re.search(r"抠|主体|透明|背景|剪影|分离|去背", request):
        return dict(
            ok=False, reason="unclear", message="请明确描述抠图需求，例如把图片主体抠出并放到旁边。"
        )
    return dict(ok=True, plan=cutout_plan(), plannedBy="rules")


async def plan_agent(request, config, client):
    if (
        config.provider == "cloud"
        and config.cloudVendor != "aliyun"
        and config.cloudTextModel
        and config.imageApiKey
    ):
        try:
            system = (
                "你是画布助手规划器。仅支持引用图片抠图，必须经过人工确认蒙版。"
                "混合其他能力应 capable=false。只输出 JSON: "
                '{"capable":true,"reason":"unclear","message":"",'
                '"plan":{"summary":"说明","steps":[{"key":"detect_mask","label":"识别"},'
                '{"key":"wait_user_check_mask","label":"确认"},'
                '{"key":"apply_cutout_place_right","label":"抠图"}]}}。'
                "步骤只允许以上三个键，且全部保留。"
            )
            text = await chat(client, config, request, system)
            payload = json.loads(re.sub(r"^```(?:json)?\s*|```\s*$", "", text))
            if not payload.get("capable"):
                reason = payload.get("reason")
                return dict(
                    ok=False,
                    reason=reason
                    if reason in {"need_reference", "out_of_scope", "unclear"}
                    else "unclear",
                    message=str(payload.get("message") or "请求需要先澄清")[:500],
                )
            steps = payload["plan"]["steps"]
            if sorted(s["key"] for s in steps) != sorted(TOOLS):
                raise ValueError("不合法的模型计划")
            plan = cutout_plan()
            plan["summary"] = str(payload["plan"].get("summary") or plan["summary"])[:200]
            return dict(ok=True, plan=plan, plannedBy="model")
        except Exception:
            pass
    return rule_plan(request)
