"""Database-only production entrypoint: uvicorn backend.production:create_app --factory.

The legacy application's in-process generation scheduler is never constructed here.
"""
from contextlib import asynccontextmanager, suppress
import asyncio
import hashlib
import base64
import json
import os
from pathlib import Path
import random
import re
import uuid
import time

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.config import Config
from backend.cutout import Recognition
from backend.capabilities import generation_capabilities
from backend.errors import AppError
from backend.frontend import FrontendFiles
from backend.infrastructure.database import DatabaseSettings, create_async_database_engine, create_async_session_factory
from backend.infrastructure.orm import Asset, AssetReference, BrowserSession, Document, DocumentRequest, GenerationRequest, Job, MediaAlias, RequestTombstone
from backend.models import parse_image, parse_params
from backend.providers.cloud import CloudProvider
from backend.production_settings import ProductionSettings
from backend.services.acceptance import accept_generation
from backend.services.documents import DatabaseDocuments, project_document
from backend.services.execution import ExecutionStore, utcnow
from backend.services.identity import CSRF_COOKIE, SESSION_COOKIE, Identity, authenticated, digest, issue_session, principal_id
from backend.services.public_projection import project_job, AcceptedJobResponse, JobEnvelope, JobsResponse
from backend.services.media import save_media
from backend.storage import apply_mask
from backend.timeline import validate_timeline

ROOT = Path(__file__).resolve().parents[3]


async def read_body(request: Request, limit: int = 12 * 1024**2) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, "请求体超限")
    return bytes(data)


async def read_json(request: Request):
    try:
        value = json.loads(await read_body(request))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(400, "请求体必须是 JSON 对象") from None


def load_routes(path: Path) -> dict:
    routes = json.loads(path.read_text())
    if not isinstance(routes, dict) or not routes:
        raise ValueError("Explicit provider routes are required")
    for kind, snapshot in routes.items():
        if kind not in {"image", "video"} or not isinstance(snapshot, dict):
            raise ValueError("Invalid provider route")
        if any("key" in k.lower() or "secret" in k.lower() or "password" in k.lower() for k in snapshot):
            raise ValueError("Provider routes must contain references, never credentials")
        config = Config.model_validate(snapshot)
        if not snapshot.get("configVersion"):
            raise ValueError("Provider routes require configVersion")
        if config.provider == "cloud" and not (config.cloudBaseUrl and snapshot.get("credentialRef")):
            raise ValueError("Cloud routes require endpoint and credentialRef")
        if not snapshot.get("videoModel" if kind == "video" else "cloudModel"):
            raise ValueError("Routes must pin the actual model")
    return routes


def create_app(settings: ProductionSettings | None = None, database: DatabaseSettings | None = None,
               root: Path = ROOT, cutout_infer=None) -> FastAPI:
    settings = settings or ProductionSettings()
    if database is None and not os.environ.get("AIVERO_DB_URL"):
        raise RuntimeError("Production requires explicit AIVERO_DB_URL")
    database = database or DatabaseSettings()
    routes = load_routes(settings.provider_routes_file)

    @asynccontextmanager
    async def lifespan(app):
        engine = create_async_database_engine(database)
        app.state.factory = create_async_session_factory(engine)
        app.state.documents = DatabaseDocuments(app.state.factory)
        app.state.recognition = Recognition(os.environ.get("CUTOUT_MODEL", "birefnet-portrait"), cutout_infer)
        app.state.redis = Redis.from_url(os.environ.get("AIVERO_REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_timeout=2, socket_connect_timeout=2)
        settings.media_root.mkdir(parents=True, exist_ok=True)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM browser_sessions LIMIT 1"))
            yield
        finally:
            app.state.recognition.close()
            await app.state.redis.aclose()
            await engine.dispose()

    app = FastAPI(title="FRAYUNE production API", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret.get_secret_value(),
        session_cookie="__Host-aivideo-oidc", https_only=True, same_site="lax", max_age=600)
    oauth = OAuth()
    oauth.register("identity", client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid", "code_challenge_method": "S256", "timeout": 10})
    app.state.oauth = oauth

    @app.middleware("http")
    async def request_context(request, call_next):
        from backend.observability import set_request_id, log_access
        supplied = request.headers.get("x-request-id", "")
        trace = supplied if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", supplied) else uuid.uuid4().hex
        request.state.trace_id = trace
        set_request_id(trace)
        started = time.monotonic()
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = trace
            log_access(request.method, request.url.path, response.status_code,
                int((time.monotonic() - started) * 1000), trace)
            return response
        finally:
            set_request_id("")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        codes = {400: "INVALID_PARAM", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                 409: "CONFLICT", 413: "INVALID_PARAM", 428: "CONFLICT", 429: "QUEUE_FULL"}
        return JSONResponse({"error": str(exc.detail), "code": codes.get(exc.status_code, "INTERNAL"),
            "recovery": "sign_in" if exc.status_code == 401 else "contact"}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "请求参数格式无效", "code": "INVALID_PARAM", "recovery": "edit_input"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({"error": "服务暂时不可用", "code": "INTERNAL", "recovery": "contact"}, status_code=500)

    @app.exception_handler(AppError)
    async def business_error(request, exc):
        return JSONResponse(exc.to_payload(), status_code=exc.http_status)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": "请求参数无效", "code": "INVALID_PARAM", "recovery": "edit_input"}, status_code=400)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, exc):
        return JSONResponse({"error": "持久化服务暂不可用", "code": "STORAGE_FAILURE", "recovery": "retry"}, status_code=503)

    @app.exception_handler(RedisError)
    async def coordination_unavailable(request, exc):
        return JSONResponse({"error": "协调服务暂不可用", "code": "COORDINATION_UNAVAILABLE", "recovery": "retry"}, status_code=503)

    @app.get("/auth/login")
    async def login(request: Request):
        return await oauth.identity.authorize_redirect(request, settings.public_origin + "/auth/callback")

    @app.get("/auth/callback")
    async def callback(request: Request):
        try:
            token = await oauth.identity.authorize_access_token(request)
            claims = token["userinfo"]
            if claims.get("iss") != settings.oidc_issuer or not claims.get("sub"):
                raise ValueError("Invalid OIDC identity")
        except Exception:
            request.session.clear()
            raise HTTPException(401, "身份验证失败") from None
        old_token = request.cookies.get(SESSION_COOKIE)
        if old_token:
            async with app.state.factory.begin() as session:
                await session.execute(delete(BrowserSession).where(BrowserSession.token_hash == digest(old_token)))
        session_token, csrf = await issue_session(app.state.factory,
            principal_id(settings.oidc_issuer, claims["sub"]), settings.session_lifetime_s)
        request.session.clear()
        response = RedirectResponse("/workspace", status_code=303)
        for name, value, httponly in ((SESSION_COOKIE, session_token, True), (CSRF_COOKIE, csrf, False)):
            response.set_cookie(name, value, secure=True, httponly=httponly, samesite="lax",
                max_age=settings.session_lifetime_s, path="/")
        return response

    @app.get("/api/session")
    async def session_get(identity: Identity = Depends(authenticated)):
        return {"workspaceId": str(identity.workspace_id), "role": identity.role}

    @app.post("/auth/logout")
    async def logout(request: Request, identity: Identity = Depends(authenticated)):
        async with app.state.factory.begin() as session:
            await session.execute(delete(BrowserSession).where(
                BrowserSession.token_hash == digest(request.cookies[SESSION_COOKIE])))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, secure=True)
        response.delete_cookie(CSRF_COOKIE, secure=True)
        return response

    @app.get("/api/health")
    async def health():
        return {"ok": True, "mode": "production", "provider": routes.get("image", {}).get("provider", "unknown"),
                "backend": {"ok": True, "detail": "持久化受理服务就绪；供应商连通性由执行器判定"}}

    @app.get("/api/runtime")
    async def runtime(identity: Identity = Depends(authenticated)):
        return {"settingsEnabled": False, "mode": "production"}

    @app.get("/api/models")
    async def models(identity: Identity = Depends(authenticated)):
        model = routes.get("image", {}).get("cloudModel")
        return {"models": [{"id": model, "name": model}] if model else [], "source": "pinned-configuration", "stale": False}

    @app.get("/api/generation-capabilities")
    async def capabilities(identity: Identity = Depends(authenticated)):
        config = Config.model_validate(routes.get("image") or routes["video"])
        result = generation_capabilities(config, (await models(identity))["models"])
        for kind in ("image", "video"):
            if kind not in routes:
                result[kind].update(supported=False, reason="此部署未配置该任务类型", models=[])
            elif kind == "video":
                result[kind] = generation_capabilities(Config.model_validate(routes[kind]), [])[kind]
        return result

    @app.get("/api/samplers")
    async def samplers(identity: Identity = Depends(authenticated)):
        return {"samplers": ["euler"], "schedulers": ["normal"]}

    @app.api_route("/api/config", methods=["GET", "POST"])
    @app.post("/api/config/test")
    async def disabled_settings(identity: Identity = Depends(authenticated)):
        raise HTTPException(403, "生产环境不开放生成设置")

    @app.get("/api/health/ready")
    async def ready():
        async with app.state.factory() as session:
            await session.execute(text("SELECT 1"))
        return {"ready": True}

    @app.get("/internal/metrics", include_in_schema=False)
    async def metrics(request: Request):
        import secrets
        expected = settings.metrics_token.get_secret_value() if settings.metrics_token else ""
        if not expected or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected):
            raise HTTPException(403, "指标访问未授权")
        from backend.services.production_metrics import render_metrics
        return PlainTextResponse(await render_metrics(app.state.factory), media_type="text/plain; version=0.0.4")

    async def job_view(session, row):
        result = project_job(row)
        if row.kind == "batch":
            children = list((await session.scalars(select(Job).where(Job.parent_id == row.id)
                .order_by(Job.created_at, Job.id))).all())
            result["images"] = [image for child in children for image in project_job(child)["images"]]
            result["batchCount"] = len(children)
            result["children"] = [project_job(child) for child in children]
            result["kind"] = row.params.get("kind", "image")
        return result

    async def scoped_job(ident, identity):
        async with app.state.factory() as session:
            row = await session.get(Job, ident)
            if row is None or row.workspace_id != identity.workspace_id:
                raise HTTPException(404, "任务不存在")
            return await job_view(session, row)

    async def job_list(identity, limit=100):
        async with app.state.factory() as session:
            base = select(Job).where(Job.workspace_id == identity.workspace_id, Job.parent_id.is_(None))
            active = list(await session.scalars(base.where(Job.status.in_(["queued", "running", "unknown"]))
                .order_by(Job.created_at.desc())))
            finished = list(await session.scalars(base.where(Job.status.in_(["completed", "failed"]))
                .order_by(Job.created_at.desc()).limit(limit)))
            rows = active + finished
            results = []
            for row in rows:
                view = await job_view(session, row)
                results.extend(view["children"] if row.params.get("directionGroup") else [view])
            return results

    async def acceptance_view(ident, identity, grouped=False):
        job = await scoped_job(ident, identity)
        if not grouped:
            return {"jobId": str(ident), "job": job}
        from backend.planning import directions
        plan = directions(job["params"]["prompt"])
        children = sorted(job["children"], key=lambda child: child["slot"])
        return {"groupId": str(ident), "subject": plan["subject"], "constraints": plan["constraints"],
            "slots": [{**direction, "jobId": child["id"], "clientRef": child["clientRef"]}
                for direction, child in zip(plan["directions"], children)]}

    @app.post("/api/generate", status_code=202, response_model=AcceptedJobResponse)
    @app.post("/api/generate/group", status_code=202)
    async def generate(request: Request, identity: Identity = Depends(authenticated)):
        raw = await read_json(request)
        grouped = request.url.path.endswith("/group")
        request_id = raw.get("requestId") or request.headers.get("idempotency-key") or str(uuid.uuid4())
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise HTTPException(400, "请求标识无效")
        fingerprint = hashlib.sha256(json.dumps({"group": raw} if grouped else raw, sort_keys=True).encode()).hexdigest()
        async with app.state.factory() as session:
            previous = (await session.scalars(select(GenerationRequest).where(
                GenerationRequest.workspace_id == identity.workspace_id,
                GenerationRequest.request_id == request_id))).one_or_none()
        if previous:
            if previous.request_hash != fingerprint:
                raise HTTPException(409, "同一请求标识不能更改参数")
            return await acceptance_view(previous.job_id, identity, grouped)
        params = parse_params(raw)
        snapshot = routes.get(params.kind)
        if snapshot is None:
            raise HTTPException(400, "当前部署不支持此任务类型")
        pinned_model = snapshot["videoModel" if params.kind == "video" else "cloudModel"]
        if params.model != pinned_model:
            raise HTTPException(400, "模型不属于当前固定路由")
        stored = params.model_dump()
        variants = None
        if grouped:
            from backend.planning import directions
            client_refs = raw.get("clientRefs")
            if params.kind != "image" or not isinstance(client_refs, list) or len(client_refs) != 4 or any(
                    not isinstance(ref, str) or not 1 <= len(ref.strip()) <= 64 for ref in client_refs):
                raise HTTPException(400, "clientRefs 必须是 4 个非空字符串，组图只支持图像")
            if len(set(client_refs)) != 4:
                raise HTTPException(400, "clientRefs 不能重复")
            stored.update(batchCount=4, directionGroup=True)
            variants = [{"prompt": direction["prompt"], "clientRef": client_refs[direction["slot"]].strip(),
                "slot": direction["slot"], "directionTitle": direction["title"], "seed": -1}
                for direction in directions(params.prompt)["directions"]]
        for key in ("documentId", "clientRef", "referenceAssetIds"):
            if key in raw:
                stored[key] = raw[key]
        refs = stored.get("referenceAssetIds", [])
        if not isinstance(refs, list) or len(refs) > 1:
            raise HTTPException(400, "当前适配器支持最多一张参考图")
        if refs:
            async with app.state.factory() as session:
                asset = await session.get(Asset, uuid.UUID(refs[0]))
                if asset is None or asset.workspace_id != identity.workspace_id or asset.kind != "image":
                    raise HTTPException(404, "参考素材不可用")
                stored["referenceStorage"] = {"key": asset.storage_key, "ext": asset.ext}
        for input_name, storage_name in (("initImage", "referenceStorage"), ("maskImage", "maskStorage")):
            image = parse_image(raw.get(input_name), input_name == "maskImage")
            if image:
                if input_name == "initImage" and refs:
                    raise HTTPException(400, "参考输入不能重复指定")
                saved = await save_media(app.state.factory, settings.media_root, identity.workspace_id, image.data)
                stored[storage_name] = {"key": saved.storage_key, "ext": saved.ext}
                stored.setdefault("referenceAssetIds", []).append(str(saved.id))
        stored["requestId"] = request_id
        stored["traceId"] = request.state.trace_id
        if stored.get("maskStorage") and not stored.get("referenceStorage"):
            raise HTTPException(400, "蒙版必须同时提供参考图")
        if params.referenceWeight and (not stored.get("referenceStorage") or snapshot.get("provider") == "cloud"):
            raise HTTPException(400, "参考强度需要参考图且仅支持 Mock 或 ComfyUI")
        snapshot = dict(snapshot)
        if snapshot.get("provider") == "cloud":
            # Resolve the actual billed model at admission, without a network call.
            # request() only serializes references; no media bytes enter the snapshot.
            from backend.models import InitImage
            marker = InitImage(b"", "png")
            provider = CloudProvider(Config.model_validate(snapshot), None)
            if params.kind == "video":
                if stored.get("referenceStorage"):
                    raise HTTPException(400, "云端图生视频暂未开放")
                _, body = provider.video_request(params)
            else:
                _, body = provider.request(params, params.seed,
                    marker if stored.get("referenceStorage") else None,
                    marker if stored.get("maskStorage") else None)
            actual_model = body["model"]
            snapshot["videoModel" if params.kind == "video" else "cloudModel"] = actual_model
            stored["model"] = actual_model
        result = await accept_generation(app.state.factory, workspace_id=identity.workspace_id,
            request_id=request_id, request_hash=fingerprint, params=stored, kind=params.kind,
            provider_snapshot=snapshot, principal=identity.principal, child_overrides=variants)
        return await acceptance_view(result["jobId"], identity, grouped)

    @app.get("/api/generation-requests/{request_id}")
    async def request_get(request_id: str, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as session:
            record = (await session.scalars(select(GenerationRequest).where(
                GenerationRequest.workspace_id == identity.workspace_id,
                GenerationRequest.request_id == request_id))).one_or_none()
            if record is None:
                if await session.get(RequestTombstone, (identity.workspace_id, request_id)):
                    return {"cancelled": True}
                raise HTTPException(404, "未找到该次生成请求")
        return {"job": await scoped_job(record.job_id, identity)}

    @app.delete("/api/generation-requests/{request_id}")
    async def cancel_request(request_id: str, request: Request, identity: Identity = Depends(authenticated)):
        if not 1 <= len(request_id) <= 128:
            raise HTTPException(400, "请求标识无效")
        document_id = request.query_params.get("documentId")
        if document_id:
            await app.state.documents.get(identity.workspace_id, uuid.UUID(document_id))
        async with app.state.factory.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(72140901)"))
            if await session.get(RequestTombstone, (identity.workspace_id, request_id)) is None:
                session.add(RequestTombstone(workspace_id=identity.workspace_id, request_id=request_id))
            previous = (await session.scalars(select(GenerationRequest).where(
                GenerationRequest.workspace_id == identity.workspace_id, GenerationRequest.request_id == request_id))).one_or_none()
            if previous:
                targets = list(await session.scalars(select(Job).where(
                    (Job.id == previous.job_id) | (Job.parent_id == previous.job_id))
                    .order_by(Job.parent_id.is_(None), Job.id).with_for_update()))
                for target in targets:
                    target.cancel_requested_at = utcnow()
        if previous:
            await cancel_job(previous.job_id, identity)
        return {"cancelled": True}

    @app.get("/api/jobs", response_model=JobsResponse)
    async def jobs(identity: Identity = Depends(authenticated)):
        return {"jobs": await job_list(identity)}

    @app.get("/api/jobs/{ident}", response_model=JobEnvelope)
    async def get_job(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        return {"job": await scoped_job(ident, identity)}

    @app.delete("/api/jobs/{ident}", response_model=JobEnvelope)
    async def cancel_job(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        job = await scoped_job(ident, identity)
        targets = [uuid.UUID(child["id"]) for child in job.get("children", [])] or [ident]
        for target in targets:
            await ExecutionStore(app.state.factory).request_cancel(target, identity.workspace_id)
        return {"job": await scoped_job(ident, identity)}

    @app.post("/api/jobs/{ident}/reconcile", status_code=202)
    async def reconcile(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        # Only a wakeup; cannot reset deadlines, clear uncertainty or submit again.
        async with app.state.factory.begin() as session:
            row = await session.get(Job, ident, with_for_update=True)
            if row is None or row.workspace_id != identity.workspace_id:
                raise HTTPException(404, "任务不存在")
            if row.status != "unknown" or row.phase != "reconcile":
                raise HTTPException(409, "此任务需要管理员对账，不能重新提交")
            row.next_poll_at = utcnow()
        return {"job": await scoped_job(ident, identity)}

    @app.get("/api/documents")
    async def documents(identity: Identity = Depends(authenticated)):
        return {"documents": await app.state.documents.list(identity.workspace_id)}

    @app.post("/api/documents", status_code=201)
    async def create_document(request: Request, identity: Identity = Depends(authenticated)):
        raw = await read_json(request)
        return {"document": await app.state.documents.create(identity.workspace_id, raw.get("name"),
            request.headers.get("idempotency-key"))}

    @app.get("/api/documents/{ident}")
    async def get_document(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        return {"document": await app.state.documents.get(identity.workspace_id, ident)}

    @app.post("/api/documents/{ident}")
    async def save_document(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        saved, conflict = await app.state.documents.save(identity.workspace_id, ident, await read_json(request))
        if conflict:
            return JSONResponse({"error": "文档已被其他会话更新", "code": "CONFLICT", "document": conflict}, status_code=409)
        return {"document": saved}

    @app.put("/api/documents/{ident}/rename")
    async def rename_document(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        raw = await read_json(request)
        async with app.state.factory.begin() as session:
            row = await session.get(Document, ident, with_for_update=True)
            if row is None or row.workspace_id != identity.workspace_id:
                raise HTTPException(404, "文档不存在")
            row.name = str(raw.get("name") or "未命名画布").strip()[:100]
            row.revision += 1
            row.updated_at = utcnow()
            return {"document": project_document(row)}

    @app.delete("/api/documents/{ident}")
    async def delete_document(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory.begin() as session:
            row = await session.get(Document, ident, with_for_update=True)
            if row is None or row.workspace_id != identity.workspace_id:
                raise HTTPException(404, "文档不存在")
            await session.execute(delete(AssetReference).where(AssetReference.owner_type == "document",
                AssetReference.owner_id == str(ident)))
            await session.execute(delete(DocumentRequest).where(DocumentRequest.document_id == ident))
            await session.delete(row)
        return {"removed": True}

    def asset_view(row):
        return {"asset": {**row.details, "id": str(row.id), "kind": row.kind, "ext": row.ext,
            "width": row.width, "height": row.height, "bytes": row.bytes, "hasThumbs": False,
            "createdAt": row.created_at.isoformat()}, "urls": {
                "original": f"/api/assets/{row.id}/content", "thumb256": None, "thumb1024": None,
                "poster": f"/api/assets/{row.details['posterAssetId']}/content" if row.details.get("posterAssetId") else None}}

    async def owned_asset(ident, identity):
        async with app.state.factory() as session:
            row = await session.get(Asset, ident)
            if row is None or row.workspace_id != identity.workspace_id:
                raise HTTPException(404, "资产不存在")
            return row

    @app.get("/api/assets")
    async def assets(identity: Identity = Depends(authenticated)):
        async with app.state.factory() as session:
            rows = await session.scalars(select(Asset).where(Asset.workspace_id == identity.workspace_id)
                .order_by(Asset.created_at.desc()).limit(500))
            return {"assets": [asset_view(row) for row in rows]}

    @app.post("/api/assets", status_code=201)
    async def upload(request: Request, identity: Identity = Depends(authenticated)):
        data = await read_body(request, 40 * 1024**2)
        kind = request.query_params.get("kind", "image")
        ext = request.query_params.get("ext") or request.headers.get("content-type", "").split(";")[0].split("/")[-1]
        row = await save_media(app.state.factory, settings.media_root, identity.workspace_id, data,
            kind, ext, request.query_params.get("name", ""))
        return asset_view(row)

    @app.post("/api/assets/{ident}/metadata")
    async def metadata(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        raw = await read_json(request)
        async with app.state.factory.begin() as session:
            row = await session.get(Asset, ident, with_for_update=True)
            if row is None or row.workspace_id != identity.workspace_id:
                raise HTTPException(404, "资产不存在")
            if row.kind != "video":
                raise HTTPException(400, "只允许补充视频元数据")
            details = dict(row.details)
            for key, low, high in (("durationMs", 100, 3600000), ("width", 16, 8192), ("height", 16, 8192)):
                if key in raw:
                    value = raw[key]
                    if type(value) is not int or not low <= value <= high:
                        raise HTTPException(400, "视频元数据越界")
                    if key in {"width", "height"}:
                        setattr(row, key, value)
                    else:
                        details[key] = value
            row.details = details
            return asset_view(row)

    @app.post("/api/assets/{ident}/poster", status_code=201)
    async def poster(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        row = await owned_asset(ident, identity)
        if row.kind != "video":
            raise HTTPException(400, "只有视频需要海报")
        saved = await save_media(app.state.factory, settings.media_root, identity.workspace_id,
            await read_body(request, 8 * 1024**2))
        async with app.state.factory.begin() as session:
            row = await session.get(Asset, ident, with_for_update=True)
            row.details = {**row.details, "posterAssetId": str(saved.id)}
            session.add(AssetReference(asset_id=saved.id, owner_type="poster", owner_id=str(ident)))
            return {**asset_view(row), "poster": asset_view(saved)["asset"]}

    async def image_bytes(ident, identity):
        row = await owned_asset(ident, identity)
        path = (settings.media_root / row.storage_key).resolve()
        if row.kind != "image" or not path.is_relative_to(settings.media_root.resolve()):
            raise HTTPException(400, "原图不可用")
        return await asyncio.to_thread(path.read_bytes)

    def operation_key(operation_id, identity):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", operation_id):
            raise HTTPException(400, "识别操作 ID 无效")
        return digest(f"{identity.workspace_id}:{identity.principal}:{operation_id}")

    @app.post("/api/cutout/detect/{ident}")
    async def detect(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        operation_id = request.query_params.get("operationId") or str(uuid.uuid4())
        key = operation_key(operation_id, identity)
        recognition = app.state.recognition
        source = await image_bytes(ident, identity)
        # No await between the duplicate guard and detect acquiring its local slot.
        # A duplicate request must never finish() another request's operation.
        if await app.state.redis.get("aivideo:cutout:cancel:" + key):
            recognition.cancel(key)
        recognition.check(key)
        if key in recognition.active:
            raise HTTPException(409, "识别请求已存在")
        async def watch():
            while True:
                cancelled = False
                with suppress(RedisError):
                    cancelled = bool(await app.state.redis.get("aivideo:cutout:cancel:" + key))
                if cancelled or await request.is_disconnected():
                    recognition.cancel(key)
                    return
                await asyncio.sleep(.1)
        watcher = asyncio.create_task(watch())
        try:
            result = await recognition.detect(key, source)
            if await app.state.redis.get("aivideo:cutout:cancel:" + key):
                recognition.cancel(key)
            recognition.check(key)
            return {"operationId": operation_id, "provider": "rembg", "model": recognition.model,
                "urls": {"original": "data:image/png;base64," + base64.b64encode(result).decode()}}
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            recognition.finish(key)

    @app.delete("/api/cutout/operations/{operation_id}")
    async def cancel_cutout(operation_id: str, identity: Identity = Depends(authenticated)):
        key = operation_key(operation_id, identity)
        app.state.recognition.cancel(key)
        with suppress(RedisError):
            await app.state.redis.set("aivideo:cutout:cancel:" + key, "1", ex=300)
        return {"cancelled": True, "operationId": operation_id}

    @app.post("/api/cutout/apply/{ident}", status_code=201)
    async def cutout_apply(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        source = await image_bytes(ident, identity)
        mask = await read_body(request, 20 * 1024**2)
        result = await asyncio.to_thread(apply_mask, source, mask)
        saved = await save_media(app.state.factory, settings.media_root, identity.workspace_id, result)
        return {**asset_view(saved), "mask": {"id": "", "transient": True}}

    @app.get("/api/assets/{ident}")
    async def get_asset(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        return asset_view(await owned_asset(ident, identity))

    @app.get("/api/assets/{ident}/content")
    async def content(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        row = await owned_asset(ident, identity)
        path = (settings.media_root / row.storage_key).resolve()
        if not path.is_relative_to(settings.media_root.resolve()) or not path.is_file():
            raise HTTPException(503, "媒体暂不可读取")
        return FileResponse(path, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/history")
    async def history(request: Request, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as session:
            rows = list(await session.scalars(select(Asset).where(Asset.workspace_id == identity.workspace_id,
                Asset.details["history"].as_boolean().is_(True)).order_by(Asset.created_at.desc()).limit(500)))
            result = []
            for row in rows:
                if row.details.get("hidden"):
                    continue
                params = row.details.get("params", {})
                if request.query_params.get("q", "").lower() not in str(params.get("prompt", "")).lower():
                    continue
                if request.query_params.get("kind") and request.query_params["kind"] != row.kind:
                    continue
                if request.query_params.get("model") and request.query_params["model"] != params.get("model"):
                    continue
                starred = request.query_params.get("starred")
                if starred in {"0", "1"} and bool(row.details.get("starred")) != (starred == "1"):
                    continue
                result.append({"id": str(row.id), "file": f"{row.id}.{row.ext}", "jobId": row.details.get("jobId"),
                    "url": f"/api/assets/{row.id}/content", "params": params, "starred": bool(row.details.get("starred")),
                    "provider": row.details.get("provider", "unknown"), "createdAt": row.created_at.isoformat()})
            return {"images": result}

    @app.delete("/api/images/{ident}")
    async def history_delete(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        async with app.state.factory.begin() as session:
            row = await session.get(Asset, ident, with_for_update=True)
            if row is None or row.workspace_id != identity.workspace_id or not row.details.get("history"):
                raise HTTPException(404, "历史记录不存在")
            row.details = {**row.details, "hidden": True}
        # Retention never deletes referenced bytes as a side effect of hiding history.
        return {"removed": True}

    @app.put("/api/images/{ident}/star")
    async def history_star(ident: uuid.UUID, request: Request, identity: Identity = Depends(authenticated)):
        raw = await read_json(request)
        async with app.state.factory.begin() as session:
            row = await session.get(Asset, ident, with_for_update=True)
            if row is None or row.workspace_id != identity.workspace_id or not row.details.get("history"):
                raise HTTPException(404, "历史记录不存在")
            row.details = {**row.details, "starred": raw.get("starred") is not False}
            return {"image": {"id": str(ident), "starred": row.details["starred"]}}

    @app.get("/assets/{ident}/{filename}")
    async def legacy_asset(ident: uuid.UUID, filename: str, identity: Identity = Depends(authenticated)):
        asset = await owned_asset(ident, identity)
        if filename not in {f"original.{asset.ext}", "t256.webp", "t1024.webp"}:
            raise HTTPException(404, "资产文件不存在")
        return await content(ident, identity)

    @app.get("/images/{filename}")
    async def legacy_history(filename: str, identity: Identity = Depends(authenticated)):
        async with app.state.factory() as session:
            alias = await session.get(MediaAlias, (identity.workspace_id, "/images/" + filename))
            if alias is None:
                raise HTTPException(404, "历史文件不存在")
            return await content(alias.asset_id, identity)

    def export_view(job):
        return {"id": job["id"], "documentId": job["documentId"], "status": job["status"],
            "progress": job["progress"] * 100, "createdAt": job["createdAt"],
            "error": job["message"] if job["status"] == "failed" else None,
            "code": job["code"], "recovery": job["recovery"], "stateVersion": job["stateVersion"]}

    @app.post("/api/exports", status_code=202)
    async def export_create(request: Request, identity: Identity = Depends(authenticated)):
        raw = await read_json(request)
        timeline = validate_timeline(raw.get("timeline"))
        if not timeline["clips"] or len(timeline["clips"]) > 200 or any(timeline[k] % 2 for k in ("width", "height")):
            raise HTTPException(400, "导出时间线为空、片段超限或画幅无效")
        if sum(c["outFrame"] - c["inFrame"] for c in timeline["clips"]) > 108000:
            raise HTTPException(400, "导出不能超过一小时")
        sources, refs = {}, set()
        for clip in timeline["clips"] + timeline.get("audioClips", []):
            url = clip["source"]["url"]
            match = re.fullmatch(r"/api/assets/([a-f0-9-]{36})/content", url)
            if not match:
                raise HTTPException(400, "导出仅允许已授权的资产库素材")
            asset = await owned_asset(uuid.UUID(match[1]), identity)
            if clip["source"]["kind"] != asset.kind:
                raise HTTPException(400, "素材类型不一致")
            sources[url] = asset.storage_key
            refs.add(str(asset.id))
        request_id = str(uuid.UUID(raw["requestId"]))
        params = {"timeline": timeline, "sources": sources, "referenceAssetIds": sorted(refs),
            "documentId": raw["documentId"], "requestId": request_id}
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        accepted = await accept_generation(app.state.factory, workspace_id=identity.workspace_id,
            request_id=request_id, request_hash=fingerprint, params=params, kind="export",
            provider_snapshot={"provider": "ffmpeg", "configVersion": "ffmpeg-v1"}, principal=identity.principal)
        return {"export": export_view(await scoped_job(accepted["jobId"], identity))}

    @app.get("/api/exports/{ident}")
    async def export_get(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        job = await scoped_job(ident, identity)
        if job["kind"] != "export":
            raise HTTPException(404, "导出不存在")
        return {"export": export_view(job)}

    @app.delete("/api/exports/{ident}")
    async def export_cancel(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        await export_get(ident, identity)
        return {"export": export_view((await cancel_job(ident, identity))["job"])}

    @app.get("/api/exports/{ident}/download")
    async def export_download(ident: uuid.UUID, identity: Identity = Depends(authenticated)):
        job = await scoped_job(ident, identity)
        if job["kind"] != "export" or job["status"] != "completed" or not job["images"]:
            raise HTTPException(404, "成片尚未就绪")
        return await content(uuid.UUID(job["images"][0]["id"]), identity)

    @app.get("/api/events")
    async def events(request: Request, identity: Identity = Depends(authenticated)):
        async def stream():
            versions = {}
            images = set()
            pubsub = app.state.redis.pubsub()
            subscribed = False
            try:
                try:
                    await pubsub.subscribe("aivideo:notify:" + str(identity.workspace_id))
                    async with asyncio.timeout(3):
                        while not subscribed:
                            ack = await pubsub.get_message(timeout=1)
                            subscribed = bool(ack and ack["type"] == "subscribe")
                except (RedisError, TimeoutError):
                    pass
                snapshot = await job_list(identity)
                versions.update({job["id"]: job["stateVersion"] for job in snapshot})
                images.update(image["id"] for job in snapshot for image in job["images"])
                yield "retry: 3000\n\nevent: snapshot\ndata: " + json.dumps({"jobs": snapshot,
                    "history": [image for job in snapshot for image in job["images"]]}) + "\n\n"
                while not await request.is_disconnected():
                    delay = settings.sse_refresh_s * random.uniform(.8, 1.2)
                    try:
                        if subscribed:
                            await pubsub.get_message(ignore_subscribe_messages=True, timeout=delay)
                        else:
                            await asyncio.sleep(delay)
                    except RedisError:
                        subscribed = False
                        await asyncio.sleep(delay)
                    current_identity = await authenticated(request)
                    if current_identity.workspace_id != identity.workspace_id:
                        break
                    current_jobs = await job_list(identity)
                    for job in current_jobs:
                        if job["stateVersion"] > versions.get(job["id"], -1):
                            versions[job["id"]] = job["stateVersion"]
                            yield "event: job\ndata: " + json.dumps(job) + "\n\n"
                            for image in job["images"]:
                                if image["id"] not in images:
                                    images.add(image["id"])
                                    yield "event: image\ndata: " + json.dumps(image) + "\n\n"
                    visible = {job["id"] for job in current_jobs}
                    versions = {ident: version for ident, version in versions.items() if ident in visible}
                    images = {image["id"] for job in current_jobs for image in job["images"]}
                    yield ": ping\n\n"
                    await asyncio.sleep(.1)
            except (HTTPException, SQLAlchemyError):
                return
            finally:
                with suppress(RedisError):
                    await pubsub.aclose()
        return StreamingResponse(stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    for url, file in {"/": "landing", "/workspace": "workspace", "/canvas": "canvas-next", "/canvas-next": "canvas-next"}.items():
        def endpoint_factory(name):
            async def page(request: Request):
                if name != "landing":
                    try:
                        await authenticated(request)
                    except HTTPException as exc:
                        if exc.status_code == 401:
                            return RedirectResponse("/auth/login", status_code=303)
                        raise
                return FileResponse(root / "apps/web" / name / "index.html", headers={"Cache-Control": "no-cache"})
            return page
        app.add_api_route(url, endpoint_factory(file), methods=["GET"], include_in_schema=False)
    app.mount("/", FrontendFiles(root), name="web")
    return app
