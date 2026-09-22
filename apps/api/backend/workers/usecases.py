"""Worker 应用用例(§四:Celery task 保持轻量,业务规则在此,API/CLI 可复用)。

本阶段(§十五.2)只验证执行契约;生成管线本体在 §十五.4 扩展:
提交 → 保存 externalTaskId → 到期查询 → 下载 → 资产 → 终态,每阶段之间
经检查点尊重业务取消(recovery=abandon,结构化标记,非文案判断)。
"""

import uuid

from backend.infrastructure.database import DatabaseSettings


async def execute_generation_step(job_id: uuid.UUID, settings: DatabaseSettings) -> dict:
    """执行一个任务阶段;取消检查点优先于一切派发动作。

    返回契约:{cancelled, missing?, upstream_cancelled?, errorCode?}。
    upstream_cancelled 仅在真正调用了 provider.cancel_external 后才有值;
    未派发上游时必须为 None(不宣称上游已取消)。
    """
    from backend.infrastructure.uow import AsyncUnitOfWork

    async with AsyncUnitOfWork(settings) as uow:
        job = await uow.jobs.get(job_id)
        if job is None:
            return {"cancelled": False, "missing": True}
        if job.recovery == "abandon":
            return {
                "cancelled": True,
                "upstream_cancelled": None,
                "errorCode": job.error_code,
            }
        if job.status != "queued":
            # 幂等守卫:重复消息(或迟到的重复投递)不得重复执行业务动作(§五.6/§八)。
            return {"cancelled": False, "skipped": True, "status": job.status}
        # stub 管线(§十五.4 替换为 Provider 分阶段执行):
        # queued → running(领取) → completed(无上游依赖时直接终态)。
        assert await uow.jobs.transition(job_id, {"queued"}, "running") is True
        await uow.commit()
    async with AsyncUnitOfWork(settings) as uow:
        assert await uow.jobs.transition(job_id, {"running"}, "completed") is True
        await uow.commit()
    return {"cancelled": False, "executed": True}
