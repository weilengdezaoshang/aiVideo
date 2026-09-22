"""FastAPI HTTP/SSE entry point. Existing browser endpoints and data files stay compatible."""

from __future__ import annotations

import asyncio
import hashlib
import copy
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .capabilities import generation_capabilities
from .common import now, read_json, write_json
from .cutout import Recognition
from .errors import (
    INTERNAL_RESPONSE,
    AppError,
    ErrorCode,
    Recovery,
    status_contract,
)
from .evaluation.api import build_eval_router
from .metrics_api import router as metrics_runtime_router
from .evaluation.tracing import TraceRecorder, prompt_fingerprint, start_trace_id
from .frontend import FrontendFiles
from .observability import get_request_id, log_access, set_request_id
from .config import Config, load_config, patch_config, public_config
from .jobs import Jobs
from .exports import Exports, export_router
from .models import InitImage, parse_image, parse_params, with_reference_weight
from .planning import directions, plan_agent
from .storyboard import plan_storyboard
from .storyboard_jobs import StoryboardJobs
from .storyboard_runs import StoryboardRuns
from .providers.cloud import CloudProvider, chat, resolve_model
from .providers.comfyui import ComfyProvider
from .providers.mock import MockProvider
from .storage import Assets, Conflict, Documents, History, apply_mask
from .traces import Traces

ROOT = Path(__file__).resolve().parents[3]


def build_provider(config, client):
    if config.provider == "cloud":
        return CloudProvider(config, client)
    if config.provider == "comfyui":
        return ComfyProvider(config, client)
    return MockProvider()


async def body(request: Request, limit=12 * 1024**2):
    data = await raw_body(request, limit)
    try:
        result = json.loads(data or b"{}")
    except ValueError:
        raise HTTPException(400, "请求体不是合法 JSON") from None
    if not isinstance(result, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    return result


async def raw_body(request: Request, limit):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, "上传内容超过大小限制")
    return bytes(data)


def require(value, message="记录不存在"):
    if value is None:
        raise HTTPException(404, message)
    return value


def bounded(value, default, maximum):
    try:
        return min(maximum, max(1, int(value)))
    except (ValueError, TypeError):
        return default


def clamp_value(value, low, high):
    """可选整数元数据:缺失或非法返回 None,超范围报错而非静默取整。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if not low <= number <= high:
        raise ValueError(f"元数据数值超出允许范围({low}–{high})")
    return number


def create_app(
    root: Path = ROOT,
    data_dir: Path | None = None,
    transport=None,
    cutout_infer=None,
    config: Config | None = None,
    step_tracer: TraceRecorder | None = None,
) -> FastAPI:
    data = data_dir or Path(os.environ.get("SWARMUI_DATA_DIR", str(root / "data")))

    @asynccontextmanager
    async def lifespan(app):
        data.mkdir(parents=True, exist_ok=True)
        # File persistence + in-memory queue have one process owner. Fail clearly on duplicate workers.
        import fcntl

        lock = (data / ".server.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("数据目录已被另一个服务进程占用；当前部署仅支持单 worker") from None
        client = httpx.AsyncClient(transport=transport, follow_redirects=True)
        s = app.state
        s.client = client
        # 显式配置注入:评测/测试用它避免修改进程环境变量(技术方案 §7.3)。
        s.config = config if config is not None else load_config(root)
        s.provider = build_provider(s.config, client)
        s.assets, s.documents, s.history = (
            Assets(data / "assets"),
            Documents(data / "documents"),
            History(data),
        )
        s.exports = Exports(data)
        s.storyboard_jobs = StoryboardJobs(data)
        s.traces = Traces(data / "traces.jsonl")
        # §9 步骤追踪:API 入口建 trace,覆盖建任务前的校验与翻译路径。
        s.step_tracer = step_tracer or TraceRecorder(data / "trace-steps.jsonl")
        s.jobs = Jobs(
            s.provider, s.history, data / "jobs.json", s.traces, step_tracer=s.step_tracer
        )
        s.sessions = {
            x["id"]: x
            for x in read_json(data / "agent-sessions.json", {}).get("sessions", [])
            if isinstance(x, dict) and x.get("id")
        }
        for session in s.sessions.values():
            if session["status"] == "preparing":
                session.update(status="planned", error="上次识别被中断,可重新准备蒙版")
        s.submit_lock = asyncio.Lock()
        s.cancelled_requests = read_json(data / "cancelled-requests.json", {})
        s.agent_lock = asyncio.Lock()
        s.asset_lock = asyncio.Lock()
        s.recognition = Recognition(
            os.environ.get("CUTOUT_MODEL", "birefnet-portrait"), cutout_infer
        )
        s.storyboard_runs = StoryboardRuns(data, s.documents, s.jobs, submit_node_generation)
        s.backend = await s.provider.status()
        s.jobs.flush()

        async def health_loop():
            while True:
                await asyncio.sleep(10)
                provider = s.provider
                status = await provider.status()
                if provider is s.provider:
                    s.backend = status

        health_task = asyncio.create_task(health_loop())
        try:
            yield
        finally:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
            s.recognition.close()
            await s.storyboard_runs.close()
            await s.storyboard_jobs.close()
            await s.exports.close()
            await s.jobs.close()
            save_sessions()
            await client.aclose()
            lock.close()

    app = FastAPI(title="FRAYUNE API", version="0.4.0", lifespan=lifespan)
    s = app.state
    app.include_router(export_router())

    @app.middleware("http")
    async def request_context(request, call_next):
        # 请求 ID:优先沿用网关/客户端带来的,缺失才生成;与业务幂等 ID 相互独立。
        request_id = request.headers.get("x-request-id") or str(uuid4())
        set_request_id(request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            log_access(
                request.method,
                request.url.path,
                getattr(response, "status_code", 500),
                int((time.monotonic() - started) * 1000),
                request_id,
            )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        payload = exc.to_payload()
        payload["traceId"] = get_request_id()
        return JSONResponse(payload, status_code=exc.http_status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        code, recovery = status_contract(exc.status_code)
        return JSONResponse(
            {
                "error": str(exc.detail),
                "code": code.value,
                "recovery": recovery.value,
                "details": {},
                "traceId": get_request_id(),
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": "请求参数格式不正确",
                "code": ErrorCode.INVALID_PARAM.value,
                "recovery": Recovery.EDIT_INPUT.value,
                "details": {},
                "traceId": get_request_id(),
            },
            status_code=400,
        )

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse(
            {
                "error": str(exc),
                "code": ErrorCode.INVALID_PARAM.value,
                "recovery": Recovery.EDIT_INPUT.value,
                "details": {},
                "traceId": get_request_id(),
            },
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        # 最终负责边界:堆栈在此记录一次(含内部细节),响应保持安全兜底。
        logging.exception("请求失败: %s %s", request.method, request.url.path)
        return JSONResponse({**INTERNAL_RESPONSE, "traceId": get_request_id()}, status_code=500)

    def save_sessions():
        entries = sorted(s.sessions.values(), key=lambda x: x["createdAt"], reverse=True)[:300]
        s.sessions = {x["id"]: x for x in entries}
        write_json(data / "agent-sessions.json", {"sessions": entries})

    def update_session(session, **patch):
        session.update(**patch, updatedAt=now())
        save_sessions()

    async def save_asset(content, ext, kind="image", name=None):
        async with s.asset_lock:
            return await asyncio.to_thread(s.assets.save, content, ext, kind, name)

    async def detect(ident, operation_id=None):
        operation_id = operation_id or str(uuid4())
        if operation_id in s.recognition.active:
            raise HTTPException(409, "识别请求已存在")
        record, source = require(s.assets.read(ident), "原图资产不存在")
        if record["kind"] != "image":
            raise HTTPException(400, "原图必须是图片")
        try:
            content = await s.recognition.detect(operation_id, source)
            s.recognition.check(operation_id)
            async with s.asset_lock:
                s.recognition.check(operation_id)
                # Shield persistence so cancellation cannot leave an untracked partial write.
                saving = asyncio.create_task(asyncio.to_thread(s.assets.save, content, "png"))
                mask = None
                try:
                    mask = await asyncio.shield(saving)
                    s.recognition.check(operation_id)
                except BaseException:
                    mask = mask or await saving
                    s.assets.remove(mask["id"])
                    raise
            return dict(
                mask=mask,
                urls=s.assets.urls(mask["id"]),
                provider="rembg",
                model=s.recognition.model,
                operationId=operation_id,
            )
        finally:
            s.recognition.finish(operation_id)

    @app.get("/api/health")
    async def health():
        return dict(ok=True, provider=s.provider.name, backend=s.backend)

    @app.get("/api/health/ready")
    async def health_ready():
        """readiness:数据库不可用 → 503(拒绝新的可靠受理,§十三.3);
        broker 不可用 → 200 + degraded(broker 不可用仍可落库受理,§十三.2)。"""
        from backend.infrastructure.database import DatabaseSettings, create_async_database_engine
        from sqlalchemy import text as sql_text

        components: dict[str, str] = {}
        try:
            engine = create_async_database_engine(DatabaseSettings())
            try:
                async with engine.connect() as conn:
                    await conn.execute(sql_text("SELECT 1"))
                components["database"] = "up"
            finally:
                await engine.dispose()
        except Exception:
            components["database"] = "down"

        broker_url = os.environ.get("AIVERO_BROKER_URL", "memory://")
        try:
            from kombu import Connection

            with Connection(broker_url, transport_options={"connect_timeout": 2}) as conn:
                conn.ensure_connection(max_retries=1, timeout=2)
            components["broker"] = "up"
        except Exception:
            components["broker"] = "degraded"

        if components["database"] != "up":
            return JSONResponse(
                {
                    "error": "依赖数据库不可用,暂不接受新的可靠受理",
                    "code": ErrorCode.UPSTREAM_UNAVAILABLE.value,
                    "recovery": Recovery.RETRY.value,
                    "components": components,
                    "traceId": get_request_id(),
                },
                status_code=503,
            )
        return {"status": "ready", "components": components}

    @app.get("/api/models")
    async def models():
        try:
            return {"models": await s.provider.models()}
        except Exception:
            raise HTTPException(502, "无法获取模型列表") from None

    @app.get("/api/samplers")
    async def samplers():
        try:
            return await s.provider.samplers()
        except Exception:
            raise HTTPException(502, "无法获取采样器列表") from None

    def require_dev_settings():
        if os.environ.get("SWARMUI_ENV") != "development":
            raise HTTPException(403, "生产环境不开放生成设置")

    @app.get("/api/runtime")
    async def runtime():
        return {"settingsEnabled": os.environ.get("SWARMUI_ENV") == "development"}

    @app.get("/api/config")
    async def config_get():
        require_dev_settings()
        return {"config": public_config(s.config)}

    @app.post("/api/config")
    async def config_update(request: Request):
        require_dev_settings()
        previous = s.provider.name
        s.config, overridden = patch_config(s.config, await body(request), root)
        s.provider = build_provider(s.config, s.client)
        s.jobs.provider = s.provider
        s.jobs.pump()
        s.backend = await s.provider.status()
        return dict(
            config=public_config(s.config),
            previousProvider=previous,
            providerSwitched=True,
            envOverridden=overridden,
        )

    @app.post("/api/config/test")
    async def config_test(request: Request):
        require_dev_settings()
        candidate, _ = patch_config(s.config, await body(request))
        probe = build_provider(candidate, s.client)
        try:
            result = await asyncio.wait_for(probe.status(), 5)
        except TimeoutError:
            result = dict(ok=False, detail="连接超时(5 秒)")
        return {**result, "provider": probe.name}

    @app.get("/api/generation-capabilities")
    async def capabilities():
        try:
            models = await s.provider.models()
        except Exception:
            models = []
        return generation_capabilities(s.config, models)

    def request_key(document_id, request_id):
        return f"{document_id}:{request_id}"

    def find_request(document_id, request_id):
        return next(
            (
                j
                for j in s.jobs.jobs.values()
                if j.get("documentId") == document_id and j.get("requestId") == request_id
            ),
            None,
        )

    @app.get("/api/generation-requests/{request_id}")
    async def request_get(request_id: str, documentId: str):
        job = find_request(documentId, request_id)
        if job:
            return {"job": job}
        if request_key(documentId, request_id) in s.cancelled_requests:
            return {"cancelled": True}
        raise HTTPException(404, "未找到该次生成请求")

    @app.delete("/api/generation-requests/{request_id}")
    async def request_cancel(request_id: str, documentId: str):
        if not re.fullmatch(r"[a-f0-9-]{36}", request_id):
            raise ValueError("非法请求标识")
        require(s.documents.get(documentId), "画布不存在")
        async with s.submit_lock:
            key = request_key(documentId, request_id)
            s.cancelled_requests[key] = now()
            write_json(data / "cancelled-requests.json", s.cancelled_requests)
            job = find_request(documentId, request_id)
            if job:
                s.jobs.cancel(job["id"])
            return {"cancelled": True}

    @app.post("/api/generate", status_code=202)
    async def generate(request: Request):
        return await submit_node_generation(await body(request))

    async def submit_node_generation(raw):
        if "requestId" not in raw:
            return await submit_generation(raw)
        rid, did = raw.get("requestId"), raw.get("documentId")
        if (
            not isinstance(rid, str)
            or not re.fullmatch(r"[a-f0-9-]{36}", rid)
            or not isinstance(did, str)
        ):
            raise ValueError("缺少合法的画布和请求标识")
        require(s.documents.get(did), "画布不存在")
        if (
            not isinstance(raw.get("clientRef"), str)
            or not raw["clientRef"]
            or len(raw["clientRef"]) > 64
        ):
            raise ValueError("缺少节点标识")
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        async with s.submit_lock:
            if request_key(did, rid) in s.cancelled_requests:
                raise HTTPException(409, "该次生成已取消")
            existing = find_request(did, rid)
            if existing:
                if existing.get("requestHash") != fingerprint:
                    raise HTTPException(409, "同一请求标识不能更改生成参数")
                return {"jobId": existing["id"], "job": existing}
            caps = generation_capabilities(s.config, await s.provider.models())
            kind = "video" if raw.get("kind") == "video" else "image"
            cap = caps[kind]
            if not cap["supported"]:
                raise ValueError(cap["reason"])
            if raw.get("referenceAssetIds") and not cap["referenceLimit"]:
                raise ValueError("当前模型不支持参考图")
            if raw.get("batchCount", 1) != 1:
                raise ValueError("节点生成每次只输出一张")
            if raw.get("model") not in {model["id"] for model in cap["models"]}:
                raise ValueError("请选择当前后端支持的模型")
            return await submit_generation(
                raw, {"documentId": did, "requestId": rid, "requestHash": fingerprint}
            )

    async def submit_generation(raw, binding=None):
        trace_id = start_trace_id()
        tracer = s.step_tracer
        tracer.trace_started(
            trace_id,
            origin="production",
            requestId=(binding or {}).get("requestId"),
            documentId=(binding or {}).get("documentId"),
            clientRef=raw.get("clientRef") if isinstance(raw.get("clientRef"), str) else None,
        )
        try:
            params = parse_params(raw)
            image, mask = parse_image(raw.get("initImage")), parse_image(raw.get("maskImage"), True)
            tracer.step(trace_id, "input.validate")
        except ValueError as exc:
            tracer.step(trace_id, "input.validate", status="failed", error=str(exc)[:200])
            raise
        refs = raw.get("referenceAssetIds", [])
        if not isinstance(refs, list) or len(refs) > 1 or any(not isinstance(x, str) for x in refs):
            raise ValueError("当前仅支持一张参考图")
        if refs:
            if image:
                raise ValueError("参考图只能使用一种传递方式")
            record, content = require(s.assets.read(refs[0]), "参考图资产不存在")
            if record["kind"] != "image" or len(content) > 8 * 1024**2:
                raise ValueError("参考图必须是小于 8MB 的图片")
            image = InitImage(content, record["ext"])
            tracer.step(trace_id, "references.resolve", assetId=record["id"])
        if mask and not image:
            raise ValueError("局部重绘需要同时携带参考图(initImage)")
        if params.referenceWeight and not image:
            raise ValueError("参考强度需要同时携带参考图")
        config = s.config
        if config.provider == "cloud":
            if params.referenceWeight and image:
                raise ValueError("当前云端模型不支持参考强度,请切换 Mock 或 ComfyUI 后端")
            if (
                config.cloudVendor != "aliyun"
                and config.cloudTextModel
                and re.search("[\u4e00-\u9fff]", params.prompt)
            ):
                tracer.step(
                    trace_id,
                    "prompt.translate",
                    status="started",
                    **prompt_fingerprint(params.prompt),
                )
                try:
                    translated = await chat(
                        s.client,
                        config,
                        params.prompt,
                        "Translate the image prompt to English literally. Preserve names, counts and constraints. Output only the translation.",
                        0.1,
                    )
                    params.sourcePrompt, params.prompt = params.prompt, translated
                except Exception as exc:
                    logging.warning("提示词翻译失败，使用原文")
                    tracer.step(
                        trace_id,
                        "prompt.translate",
                        status="failed_fallback",
                        error=str(exc)[:200],
                        **prompt_fingerprint(params.prompt),
                    )
                else:
                    tracer.step(
                        trace_id,
                        "prompt.translate",
                        **prompt_fingerprint(params.prompt),
                        sourceFingerprint=prompt_fingerprint(params.sourcePrompt or ""),
                    )
            if params.kind == "image":
                params.model = resolve_model(config, bool(image and not mask))
        if mask:
            params.inpaint = True
        params = with_reference_weight(params, bool(image))
        if len(s.jobs.list()) >= 50:
            raise HTTPException(429, "队列已满")
        ref = raw.get("clientRef")
        ref = ref.strip()[:64] if isinstance(ref, str) else None
        job = s.jobs.create(params, image, mask, ref, binding=binding, trace_id=trace_id)
        return {"jobId": job["id"], "job": job}

    @app.post("/api/generate/group", status_code=202)
    async def group_generate(request: Request):
        raw = await body(request)
        refs = raw.get("clientRefs")
        if (
            not isinstance(refs, list)
            or len(refs) != 4
            or any(not isinstance(x, str) or not x.strip() or len(x) > 64 for x in refs)
        ):
            raise ValueError("clientRefs 必须是 4 个非空字符串(每个 ≤64 字符)")
        base = parse_params(raw)
        ref_assets = raw.get("referenceAssetIds", [])
        ref_image = None
        if ref_assets:
            if (
                not isinstance(ref_assets, list)
                or len(ref_assets) != 1
                or not isinstance(ref_assets[0], str)
            ):
                raise ValueError("组图生图当前仅支持一张参考图")
            record, content = require(s.assets.read(ref_assets[0]), "参考图资产不存在")
            if record["kind"] != "image" or len(content) > 8 * 1024**2:
                raise ValueError("参考图必须是小于 8MB 的图片")
            ref_image = InitImage(content, record["ext"])
        if base.referenceWeight and not ref_image:
            raise ValueError("参考强度需要同时携带参考图")
        config = s.config
        if ref_image and config.provider == "cloud":
            if config.cloudVendor not in {"aliyun", "siliconflow"}:
                raise ValueError(f"当前厂商({config.cloudVendor})暂不支持参考图")
            if base.referenceWeight:
                raise ValueError("当前云端模型不支持参考强度,请切换 Mock 或 ComfyUI 后端")
        plan = directions(base.prompt)
        prepared = []
        for d in plan["directions"]:
            params = parse_params(
                {**raw, "prompt": d["prompt"], "kind": "image", "batchCount": 1, "seed": -1}
            )
            prepared.append(with_reference_weight(params, ref_image is not None))
        if len(s.jobs.list()) + 4 > 50:
            raise HTTPException(429, "队列已满")
        group_id, slots = str(uuid4()), []
        for d, params in zip(plan["directions"], prepared):
            if config.provider == "cloud":
                params.model = resolve_model(config, ref_image is not None)
            ref = refs[d["slot"]].strip()
            job = s.jobs.create(
                params,
                image=ref_image,
                client_ref=ref,
                group=dict(groupId=group_id, slot=d["slot"], directionTitle=d["title"]),
            )
            slots.append({**d, "jobId": job["id"], "clientRef": ref})
        return dict(
            groupId=group_id, subject=plan["subject"], constraints=plan["constraints"], slots=slots
        )

    @app.get("/api/jobs")
    async def jobs(request: Request):
        return {
            "jobs": s.jobs.list(
                bool(request.query_params.get("all")),
                bounded(request.query_params.get("limit"), 30, 100),
            )
        }

    @app.get("/api/jobs/{ident}")
    async def job_get(ident: str):
        return {"job": require(s.jobs.jobs.get(ident), "任务不存在")}

    @app.post("/api/jobs/{ident}/reconcile", status_code=202)
    async def job_reconcile(ident: str):
        """对账重启后结果未知的任务:只查询上游既有任务,不重新提交(技术方案 v0.1 §5.4)。"""
        job = require(s.jobs.jobs.get(ident), "任务不存在")
        if job["status"] != "unknown":
            raise HTTPException(409, "只有结果未知的任务可以查询上游")
        resumed = s.jobs.resume(ident)
        return {"job": resumed}

    @app.delete("/api/jobs/{ident}")
    async def job_cancel(ident: str):
        return {"job": require(s.jobs.cancel(ident), "任务不存在或已结束")}

    @app.get("/api/history")
    async def history(request: Request):
        q = request.query_params
        return {
            "images": s.history.list(
                bounded(q.get("limit"), 100, 500),
                q.get("q"),
                q.get("model"),
                q.get("kind"),
                True if q.get("starred") == "1" else False if q.get("starred") == "0" else None,
            )
        }

    @app.delete("/api/images/{ident}")
    async def image_delete(ident: str):
        return {"removed": require(s.history.remove(ident), "图片不存在")}

    @app.put("/api/images/{ident}/star")
    async def image_star(ident: str, request: Request):
        return {
            "image": require(
                s.history.star(ident, (await body(request)).get("starred") is not False),
                "图片不存在",
            )
        }

    @app.get("/api/assets")
    async def assets_list(request: Request):
        q = request.query_params
        return {
            "assets": [
                {"asset": x, "urls": s.assets.urls(x["id"])}
                for x in s.assets.list(q.get("kind"), bounded(q.get("limit"), 200, 500))
            ]
        }

    @app.post("/api/assets", status_code=201)
    async def assets_upload(request: Request):
        content = await raw_body(request, 40 * 1024**2)
        if not content:
            raise ValueError("请求体必须是非空二进制内容")
        kind = request.query_params.get("kind", "image")
        if kind not in {"image", "video", "audio"}:
            raise ValueError("不支持的素材类型")
        ext = (
            request.query_params.get("ext")
            or request.headers.get("content-type", "").split(";")[0].split("/")[-1]
        )
        asset = await save_asset(content, ext, kind, request.query_params.get("name"))
        return {"asset": asset, "urls": s.assets.urls(asset["id"])}

    @app.get("/api/assets/{ident}")
    async def assets_get(ident: str):
        return {"asset": require(s.assets.get(ident), "资产不存在"), "urls": s.assets.urls(ident)}

    def require_video_asset(ident):
        record = require(s.assets.get(ident), "资产不存在")
        if record["kind"] != "video":
            raise ValueError("只有视频资产需要补充元数据")
        return record

    @app.post("/api/assets/{ident}/metadata")
    async def asset_metadata(ident: str, request: Request):
        require_video_asset(ident)
        raw = await body(request)
        fields = {}
        duration_ms = clamp_value(raw.get("durationMs"), 100, 3600000)
        width = clamp_value(raw.get("width"), 16, 8192)
        height = clamp_value(raw.get("height"), 16, 8192)
        if duration_ms is not None:
            fields["durationMs"] = duration_ms
        if width is not None:
            fields["width"] = width
        if height is not None:
            fields["height"] = height
        if not fields:
            raise ValueError("需要 durationMs/width/height 中的至少一项")
        async with s.asset_lock:
            record = s.assets.update_meta(ident, **fields)
        return {"asset": record, "urls": s.assets.urls(ident)}

    @app.post("/api/assets/{ident}/poster", status_code=201)
    async def asset_poster(ident: str, request: Request):
        require_video_asset(ident)
        content = await raw_body(request, 8 * 1024**2)
        if not content:
            raise ValueError("请求体必须是非空图片内容")
        ext = request.headers.get("content-type", "").split(";")[0].split("/")[-1]
        poster = await save_asset(content, ext or "jpeg", "image")
        async with s.asset_lock:
            record = s.assets.update_meta(ident, posterAssetId=poster["id"])
        return {"asset": record, "poster": poster, "urls": s.assets.urls(ident)}

    @app.post("/api/cutout/detect/{ident}")
    async def cutout_detect(ident: str, request: Request):
        operation_id = request.query_params.get("operationId") or str(uuid4())
        validate_operation(operation_id)

        async def watch_disconnect():
            while True:
                if await request.is_disconnected():
                    s.recognition.cancel(operation_id)
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            return await detect(ident, operation_id)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def validate_operation(ident):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", ident):
            raise HTTPException(400, "识别操作 ID 格式不正确")

    @app.delete("/api/cutout/operations/{operation_id}")
    async def cancel_cutout(operation_id: str):
        validate_operation(operation_id)
        s.recognition.cancel(operation_id)
        return {"cancelled": True, "operationId": operation_id}

    @app.post("/api/cutout/apply/{ident}", status_code=201)
    async def cutout_apply(ident: str, request: Request):
        record, source = require(s.assets.read(ident), "原图资产不存在")
        if record["kind"] != "image":
            raise ValueError("原图必须是图片")
        mask = await raw_body(request, 20 * 1024**2)
        if not mask:
            raise ValueError("请求体必须是 PNG Mask")
        async with s.agent_lock:
            session_id = request.query_params.get("sessionId")
            session = (
                require(s.sessions.get(session_id), "Agent 会话不存在") if session_id else None
            )
            if session and (session["status"] != "waiting_mask" or session["assetId"] != ident):
                raise HTTPException(409, "Agent 会话不在等待蒙版确认状态")
            output = await asyncio.to_thread(apply_mask, source, mask)
            asset = await save_asset(output, "png")
            saved_mask = await save_asset(mask, "png")
            if session:
                update_session(
                    session, status="completed", resultAssetId=asset["id"], resultExt=asset["ext"]
                )
            return dict(asset=asset, mask=saved_mask, urls=s.assets.urls(asset["id"]))

    @app.post("/api/storyboards/runs", status_code=202)
    async def storyboard_run_create(request: Request):
        raw = await body(request)
        return dict(
            run=s.storyboard_runs.start(raw.get("id"), raw.get("documentId"), raw.get("steps"))
        )

    @app.get("/api/storyboards/runs")
    async def storyboard_run_list(documentId: str):
        require(s.documents.get(documentId), "项目不存在")
        return dict(runs=s.storyboard_runs.list_for_document(documentId))

    @app.get("/api/storyboards/runs/{ident}")
    async def storyboard_run_get(ident: str):
        return dict(run=s.storyboard_runs.get(ident))

    @app.post("/api/storyboards/runs/{ident}/{action}")
    async def storyboard_run_control(ident: str, action: str):
        return dict(run=await s.storyboard_runs.control(ident, action))

    @app.post("/api/storyboards/jobs", status_code=202)
    async def storyboard_job_create(request: Request):
        raw = await body(request)
        require(s.documents.get(raw.get("documentId", "")), "项目不存在")
        job = s.storyboard_jobs.submit(
            raw.get("id"),
            raw["documentId"],
            raw.get("prompt"),
            raw.get("workflow", "general"),
            s.config,
            s.client,
        )
        return dict(job=job)

    @app.get("/api/storyboards/jobs")
    async def storyboard_job_list(documentId: str):
        require(s.documents.get(documentId), "项目不存在")
        return dict(jobs=s.storyboard_jobs.list_for_document(documentId))

    @app.get("/api/storyboards/jobs/{ident}")
    async def storyboard_job_get(ident: str):
        return dict(job=s.storyboard_jobs.get(ident))

    @app.delete("/api/storyboards/jobs/{ident}")
    async def storyboard_job_cancel(ident: str):
        return dict(job=s.storyboard_jobs.cancel(ident))

    @app.post("/api/storyboards/plan")
    async def storyboard_plan(request: Request):
        raw = await body(request)
        require(s.documents.get(raw.get("documentId", "")), "项目不存在")
        try:
            result = await plan_storyboard(
                raw.get("prompt"), s.config, s.client, raw.get("workflow", "general")
            )
        except ValueError:
            raise
        except Exception:
            raise HTTPException(502, "分镜规划请求失败；未自动重试，请检查模型服务") from None
        return dict(storyboard=result, plannedBy="model")

    @app.post("/api/agent/sessions", status_code=201)
    async def agent_create(request: Request):
        raw = await body(request)
        for field in ("docId", "request"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise ValueError(f"缺少 {field}")
        if not raw.get("assetId"):
            return JSONResponse(
                dict(error="请先选择图片并添加到对话", reason="need_reference"), status_code=422
            )
        asset = require(s.assets.get(raw["assetId"]), "引用的图片资产不存在")
        if asset["kind"] != "image":
            raise ValueError("引用必须是图片")
        text = raw["request"].strip()[:4000]
        result = await plan_agent(text, s.config, s.client)
        if not result["ok"]:
            return JSONResponse(
                dict(error=result["message"], reason=result["reason"]), status_code=422
            )
        session = dict(
            id=str(uuid4()),
            docId=raw["docId"],
            assetId=asset["id"],
            assetExt=asset["ext"],
            request=text,
            status="planned",
            plan=result["plan"],
            plannedBy=result["plannedBy"],
            createdAt=now(),
            updatedAt=now(),
        )
        s.sessions[session["id"]] = session
        save_sessions()
        return {"session": session}

    @app.get("/api/agent/sessions")
    async def agent_list(request: Request):
        doc_id = request.query_params.get("docId")
        if not doc_id:
            raise ValueError("缺少 docId 查询参数")
        return {
            "sessions": sorted(
                [x for x in s.sessions.values() if x["docId"] == doc_id],
                key=lambda x: x["createdAt"],
                reverse=True,
            )
        }

    @app.get("/api/agent/sessions/{ident}")
    async def agent_get(ident: str):
        return {"session": require(s.sessions.get(ident), "Agent 会话不存在")}

    @app.post("/api/agent/sessions/{ident}/prepare")
    async def agent_prepare(ident: str):
        session = require(s.sessions.get(ident), "Agent 会话不存在")
        if session["status"] in {"waiting_mask", "completed"}:
            return {"session": session}
        if session["status"] != "planned":
            return JSONResponse(
                dict(error="当前状态不允许准备蒙版", session=session), status_code=409
            )
        update_session(session, status="preparing")
        try:
            result = await detect(session["assetId"])
            update_session(session, status="waiting_mask", maskAssetId=result["mask"]["id"])
            return {"session": session, **result}
        except Exception as exc:
            message = str(exc.detail) if isinstance(exc, HTTPException) else "主体识别失败"
            update_session(session, status="failed", error=message)
            return JSONResponse(dict(error=message, session=session), status_code=502)

    @app.get("/api/documents")
    async def documents_list():
        return {"documents": s.documents.list()}

    @app.post("/api/documents", status_code=201)
    async def documents_create(request: Request):
        key = request.headers.get("idempotency-key", "").strip()
        if len(key) > 128:
            raise ValueError("Idempotency-Key 过长(上限 128 字符)")
        return {"document": s.documents.create((await body(request)).get("name"), key or None)}

    @app.get("/api/documents/{ident}")
    async def documents_get(ident: str):
        return {"document": require(s.documents.get(ident), "文档不存在")}

    @app.post("/api/documents/{ident}")
    async def documents_save(ident: str, request: Request):
        try:
            return {"document": require(s.documents.save(ident, await body(request)), "文档不存在")}
        except Conflict as exc:
            return JSONResponse(dict(error=str(exc), document=exc.document), status_code=409)

    @app.put("/api/documents/{ident}/rename")
    async def documents_rename(ident: str, request: Request):
        return {
            "document": require(
                s.documents.rename(ident, (await body(request)).get("name")), "文档不存在"
            )
        }

    @app.delete("/api/documents/{ident}")
    async def documents_remove(ident: str):
        if not s.documents.remove(ident):
            raise HTTPException(404, "文档不存在")
        return {"removed": True}

    @app.get("/api/metrics")
    async def metrics(request: Request):
        return {
            "metrics": await asyncio.to_thread(
                s.traces.aggregate, bounded(request.query_params.get("days"), 7, 90)
            )
        }

    @app.post("/api/feedback")
    async def feedback(request: Request):
        raw = await body(request)
        if (
            not isinstance(raw.get("imageId"), str)
            or not raw["imageId"]
            or len(raw["imageId"]) > 64
            or raw.get("action") != "deleted"
        ):
            raise ValueError("需要 imageId 和 deleted action")
        s.traces.append(
            dict(
                ts=now(),
                kind="feedback",
                action="deleted",
                imageId=raw["imageId"],
                model=str(raw.get("model", ""))[:200],
            )
        )
        return {"ok": True}

    @app.get("/api/events")
    async def events():
        queue = asyncio.Queue(maxsize=256)
        s.jobs.listeners.add(queue)
        snapshot = copy.deepcopy(
            dict(
                jobs=s.jobs.list(),
                history=s.history.list(),
                provider=s.provider.name,
                backend=s.backend,
            )
        )

        async def stream():
            try:
                yield "retry: 3000\n\n"
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                while True:
                    try:
                        event, payload = await asyncio.wait_for(queue.get(), 15)
                        if event == "overflow":
                            break
                        yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                    except TimeoutError:
                        yield ": ping\n\n"
            finally:
                s.jobs.listeners.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    pages = {
        "/": "landing/index.html",
        "/workspace": "workspace/index.html",
        "/canvas": "canvas-next/index.html",
        "/canvas-next": "canvas-next/index.html",
        "/evals": "evaluation/index.html",
    }

    def page_endpoint(file):
        async def endpoint():
            return FileResponse(root / "apps" / "web" / file, headers={"Cache-Control": "no-cache"})

        return endpoint

    for url, file in pages.items():
        app.add_api_route(url, page_endpoint(file), methods=["GET"], include_in_schema=False)

    # Preserve directory-style URLs used by launch scripts.
    @app.get("/canvas/", include_in_schema=False)
    async def canvas_slash():
        return FileResponse(
            root / "apps/web/canvas-next/index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/canvas-next/", include_in_schema=False)
    async def canvas_next_slash():
        return FileResponse(
            root / "apps/web/canvas-next/index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/workspace/", include_in_schema=False)
    async def workspace_slash():
        return FileResponse(
            root / "apps/web/workspace/index.html", headers={"Cache-Control": "no-cache"}
        )

    app.mount("/images", StaticFiles(directory=data / "images", check_dir=False), name="images")
    app.mount("/assets", StaticFiles(directory=data / "assets", check_dir=False), name="assets")
    # §14 评测 API:独立命名空间,不影响既有端点与 data 格式。
    app.include_router(build_eval_router(data))
    app.include_router(metrics_runtime_router)
    app.mount("/", FrontendFiles(root), name="web")
    return app
