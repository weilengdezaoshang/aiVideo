"""User-facing supervisor copy. Numbers come from the caller, never from a model."""


def english(language: str | None) -> bool:
    return str(language or "").lower().startswith("en")


def opening(language: str | None, quoted: int, *, confirm: bool, goal: str = "") -> str:
    prefix = (("Plan: " + goal.strip() + ". ") if english(language) else ("准备执行：" + goal.strip() + "。")) if goal.strip() else ""
    if english(language):
        if confirm:
            return prefix + f"Reviewed. Estimated cost {quoted} credits. Execution starts after you confirm."
        return prefix + f"Reviewed. Estimated cost {quoted} credits. Execution has started."
    if confirm:
        return prefix + f"已审核。预估花费 {quoted} 积分。确认后才会执行。"
    return prefix + f"已审核。预估花费 {quoted} 积分，开始执行。"


def completed(language: str | None, places: list[str]) -> str:
    if english(language):
        if not places:
            return "Done. Nothing new was placed on the canvas."
        return "Done. Placement: " + "; ".join(places) + "."
    if not places:
        return "已完成。画布上没有新的放置位置。"
    return "已完成。放置位置：" + "；".join(places) + "。"


def failed(language: str | None, refunded: int, code: str | None = None) -> str:
    reason = failure_reason(language, code) if code else ""
    if english(language):
        return reason + f"A step failed. {refunded} credits were refunded. Successful results were kept."
    return reason + f"有步骤失败。已退回 {refunded} 积分，成功产物已保留。"


def blocked(language: str | None, refunded: int) -> str:
    if english(language):
        return f"Cannot generate this. {refunded} credits were refunded."
    return f"无法生成。已退回 {refunded} 积分。"


def ask_text(parameters: dict, language: str | None) -> str:
    question = parameters.get("question")
    if isinstance(question, str) and question.strip():
        return question
    form = parameters.get("form") or {}
    labels = []
    for field in form.get("fields") or []:
        if isinstance(field, dict):
            labels.append(str(field.get("label") or field.get("id") or ""))
    labels = [item for item in labels if item]
    if english(language):
        return "Please answer: " + ", ".join(labels)
    return "请补充：" + "、".join(labels)


def place_label(rect: dict) -> str:
    return "x={x} y={y} width={width} height={height}".format(
        x=rect.get("x"), y=rect.get("y"), width=rect.get("width"), height=rect.get("height"))


def failure_reason(language: str | None, code: str | None) -> str:
    reasons = {
        "param_invalid": ("生成参数无效。", "The generation parameters were invalid. "),
        "provider_down": ("生成服务暂不可用。", "The generation service was unavailable. "),
        "quota": ("生成服务额度不足。", "The generation service quota was insufficient. "),
        "retryable": ("生成服务暂时失败，重试已达上限。", "The generation service failed after the allowed retries. "),
        "queue_timeout": ("任务排队超时。", "The task timed out in the queue. "),
        "timeout": ("生成服务响应超时。", "The generation service timed out. "),
        "no_subject": ("未识别到可分离的主体。", "No separable subject was detected. "),
    }
    pair = reasons.get(str(code), ("该步骤未能完成，原因暂不明确。", "The step failed for an unknown reason. "))
    return pair[1 if english(language) else 0]


def read_summary(language: str | None, tool: str, output: dict) -> str:
    result = output.get("result") if isinstance(output, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return ""
    en = english(language)
    count = data.get("objectCount")
    if tool == "read_canvas" and isinstance(count, int) and not isinstance(count, bool):
        return f"The canvas has {count} readable objects." if en else f"画布中有 {count} 个可读对象。"
    width, height = data.get("width"), data.get("height")
    if tool in {"inspect_image", "read_asset"} and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height)):
        return f"Image dimensions: {width}×{height} pixels." if en else f"图片尺寸：{width}×{height} 像素。"
    elements = data.get("elements")
    if tool in {"list_canvas_elements", "get_canvas_elements"} and isinstance(elements, list):
        texts = [str(item["text"]) for item in elements if isinstance(item, dict) and isinstance(item.get("text"), str)]
        intro = f"Read {len(elements)} objects." if en else f"已读取 {len(elements)} 个对象。"
        return intro + ("\n" + "\n".join(texts) if texts else "")
    return "Asset information was read." if en else "已读取素材信息。"
