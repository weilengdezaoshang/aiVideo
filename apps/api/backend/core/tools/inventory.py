"""Human-readable inventory is another export, not another source of tool truth."""
from .catalog import ToolCatalog


def inventory_markdown(catalog: ToolCatalog) -> str:
    lines = ["| 名称 | 输入 → 输出 | 积分 | 可取消 | 服务商幂等 | 结果链接有效期 | 可用 |",
             "|---|---|---|---|---|---|---|"]
    for tool in catalog.snapshot.tools:
        inputs = ", ".join(dict.fromkeys(port.media_type for port in tool.inputs)) or "—"
        outputs = ", ".join(dict.fromkeys(port.media_type for port in tool.outputs))
        price = "未定价" if tool.price.kind == "unpriced" else str(tool.price.credits)
        if tool.price.kind == "per_count":
            price += " × " + tool.price.count_parameter
        execution = tool.execution
        expiry = str(execution.result_url_ttl_seconds) + " 秒" if execution.result_url_ttl_seconds else {
            "authenticated_asset": "无固定到期；受鉴权及资产状态约束", "transient": "仅当前识别操作；不持久化",
            "provider_unspecified": "服务商未声明"}[execution.result_url_policy]
        state = "是" if tool.available else "否：" + tool.unavailable_reason
        lines.append(f"| {tool.name} | {inputs} → {outputs} | {price} | "
                     f"{'是（' + execution.cancellation + '）' if execution.cancellable else '否'} | "
                     f"{'是' if execution.provider_idempotent else '否'} | {expiry} | {state} |")
    return "\n".join(lines) + "\n"
