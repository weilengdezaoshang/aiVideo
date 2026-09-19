"""指标暴露路由(补充要求 §十三):独立 router,由 app include。

- /api/metrics/runtime:渲染进程内 counter/gauge,并采集数据库 gauge
  (Outbox 待发送/最老年龄、任务队列长度,§十三 至少采集项);
- 采集失败降级为仅进程指标,不泄漏内部细节;高基数字段在注册表层已拒绝。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import metrics
from .observability import get_request_id

router = APIRouter()


@router.get("/api/metrics/runtime")
def runtime_metrics():
    """同步端点:采集内部用 asyncio.run,须在无运行循环的线程池线程执行。"""
    payload = {"counters": {}, "gauges": {}, "traceId": get_request_id()}
    try:
        from .infrastructure.database import DatabaseSettings

        settings = DatabaseSettings()
        metrics.collect_outbox_gauges(settings)
        metrics.collect_job_gauges(settings)
    except Exception:
        pass  # 采集失败降级:进程指标仍可用,内部细节不进响应(§十三/§八.7)
    try:
        rendered = metrics.render()
        payload["counters"] = rendered["counters"]
        payload["gauges"] = rendered["gauges"]
    except Exception:
        pass
    return JSONResponse(payload)
