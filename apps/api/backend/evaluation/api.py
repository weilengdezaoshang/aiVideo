"""/api/evals/v1 评测 API(技术方案 §14):现有生成/任务/SSE 端点零改动,独立挂载。

幂等与错误语义(§14.4):runId 已存在→409;参数/校验错误→422/400;
审核冲突→409;预算不足为结构化业务错误,不滥用 500。
执行模型:创建 run 持久化后立即返回 202,由受控后台执行器调度(§14.1);
轮询、进度、取消与恢复都可在本命名空间完成,页面刷新后仍可追踪。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .budgets import BudgetLedger
from .calibration import CalibrationStore
from .compare import build_comparison
from .compression import apply_cost, apply_promotion_gate, compression_summary
from .datasets import DatasetStore
from .gates import GateStore, evaluate_gate
from .integrations import LangfuseExporter, make_outbox_exporter
from .outbox import Outbox
from .reports import ReportWriter, build_report
from .reviews import ReviewSamplingPolicy, ReviewConflict, ReviewStore, publish_case_draft
from .runner import EvaluationRunner
from .scheduler import EvaluationScheduler


def _runner(data_dir: Path) -> EvaluationRunner:
    root = Path(__file__).resolve().parents[4]  # 仓库代码与证据目录独立定位
    return EvaluationRunner(root, store=data_dir / "evaluation")


def _store(data_dir: Path) -> Path:
    return data_dir / "evaluation"


def _scheduler(data_dir: Path) -> EvaluationScheduler:
    return EvaluationScheduler.instance(_store(data_dir))


def _wait_terminal(data_dir: Path, run_id: str, timeout: float = 60.0):
    """等待 run 进入终态;供契约测试与 CLI 使用,生产前端应轮询列表/详情。"""
    import time

    runner = _runner(data_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = runner.read_state(run_id)
        if state.get("status") in {"completed", "failed", "cancelled", "interrupted", "budget_exhausted"}:
            return state
        time.sleep(0.05)
    return runner.read_state(run_id)


def _state_or_404(data_dir: Path, run_id: str):
    try:
        return _runner(data_dir).load_run(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def build_eval_router(data_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api/evals/v1", tags=["evals"])
    store = _store(data_dir)

    def _shutdown_scheduler():  # pragma: no cover - FastAPI 生命周期
        _scheduler(data_dir).shutdown(wait=False)

    router.on_shutdown.append(_shutdown_scheduler)

    def save_sampling(runner, run_id, raw):
        from ..common import atomic_write_text
        policy = ReviewSamplingPolicy.model_validate(raw["reviewSampling"]) if raw.get("reviewSampling") is not None else ReviewSamplingPolicy.from_env()
        state = runner.load_run(run_id)
        state.manifest.reviewSampling = policy.model_dump()
        atomic_write_text(runner.run_dir(run_id) / "manifest.json", state.manifest.model_dump_json())

    def review_evidence(task):
        state = _state_or_404(data_dir, task.runId)
        trial = next((t for t in state.trials if t.trialId == task.trialId), None)
        return {"suite": trial.flowLayer if trial else None,
                "reasons": [g.checkId for g in state.grades if g.trialId == task.trialId and g.status in {"fail", "error", "inconclusive", "dependency_failed"}],
                "tracePath": trial.tracePath if trial else None,
                "traceUrl": f"/api/evals/v1/runs/{task.runId}/trials/{task.trialId}/trace" if trial and trial.tracePath else None}

    # ---------- 数据集 ----------

    @router.get("/datasets")
    async def datasets():
        dataset_store = DatasetStore(store)
        items = []
        for directory in sorted((store / "manifests").iterdir()) if (store / "manifests").is_dir() else []:
            if not directory.is_dir():
                continue
            versions = dataset_store.list_versions(directory.name)
            if versions:
                items.append(
                    {
                        "datasetId": directory.name,
                        "versions": [
                            {
                                "version": m.version,
                                "split": m.split,
                                "caseCount": m.caseCount,
                                "contentHash": m.contentHash[:12],
                                "frozenAt": m.frozenAt,
                            }
                            for m in versions
                        ],
                    }
                )
        return {"datasets": items}

    @router.get("/datasets/{dataset_id}/cases")
    async def dataset_cases(dataset_id: str, version: int | None = None):
        if Path(dataset_id).name != dataset_id or dataset_id in {".", ".."}:
            raise HTTPException(400, "数据集标识不合法")
        try:
            dataset_store = DatasetStore(store)
            if version is None:
                versions = dataset_store.list_versions(dataset_id)
                version = max((item.version for item in versions), default=None)
            frozen = dataset_store.load(dataset_id, version)
            return {"cases": [case.model_dump() for case in frozen.cases]}
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/runs/{run_id}/cases")
    async def run_cases(run_id: str):
        _state_or_404(data_dir, run_id)
        try:
            cases = DatasetStore.load_run_snapshot(_runner(data_dir).run_dir(run_id))
            return {"cases": [case.model_dump() for case in cases]}
        except FileNotFoundError as exc:
            raise HTTPException(404, "此运行缺少用例快照，无法查看定义") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/agent-flow/cases")
    async def preview_flow_cases(suite: str = "agent_flow"):
        from .agent_flow import load_flow_sources, select_flow_suite
        try:
            loaded = load_flow_sources(_runner(data_dir).root)
            cases = select_flow_suite([case for _, case in loaded], suite)
            return {"cases": [case.model_dump() for case in cases]}
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.post("/agent-flow", status_code=202)
    async def agent_flow(request: Request):
        from uuid import uuid4
        from .agent_flow import load_flow_sources, select_flow_suite
        raw = await request.json()
        try:
            ReviewSamplingPolicy.model_validate(raw.get("reviewSampling") or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        suite = raw.get("suite", "agent_flow")
        runner = _runner(data_dir)
        try:
            loaded = load_flow_sources(runner.root)
            selected = select_flow_suite([case for _, case in loaded], suite)
            layers = raw.get("layers")
            if layers is not None:
                if not isinstance(layers, list) or not layers or any(not isinstance(layer, str) or layer not in {"L0", "L1", "L2", "L3", "L4", "L5", "L7"} for layer in layers):
                    raise ValueError("请选择有效的 L0～L5、L7 层级")
                selected = [case for case in selected if any(layer in case.suites for layer in layers)]
            run_id = f"af-{uuid4().hex[:12]}"
            source = store / "staging" / f"{run_id}.jsonl"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("\n".join(case.model_dump_json() for case in selected) + "\n")
            scope = "-".join(sorted(set(layers))) if layers is not None else suite
            dataset_id = f"agent-flow-{scope.lower()}"
            runner.freeze_dataset(source, dataset_id, split="dev")
            runner.create_run(run_id, dataset_id, purpose=f"Agent 流程 · {scope}")
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        save_sampling(runner, run_id, raw)
        _scheduler(data_dir).submit(run_id)
        return {"runId": run_id}

    # ---------- Run ----------

    @router.post("/runs", status_code=202)
    async def create_run(request: Request):
        """创建 run 并入队;立即返回 202,不阻塞等待执行完成(§14.1)。"""
        raw = await request.json()
        try:
            ReviewSamplingPolicy.model_validate(raw.get("reviewSampling") or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        run_id = raw.get("runId")
        if not isinstance(run_id, str) or not run_id.strip():
            raise HTTPException(422, "缺少 runId")
        runner = _runner(data_dir)
        try:
            if raw.get("mode") == "replay":
                manifest = runner.create_run(
                    run_id.strip(),
                    "",
                    mode="replay",
                    source_run_id=raw.get("sourceRunId"),
                    recording_id=raw.get("recordingId"),
                    purpose=str(raw.get("purpose", "")),
                )
            else:
                manifest = runner.create_run(
                    run_id.strip(),
                    str(raw.get("datasetId", "")),
                    raw.get("version"),
                    repetitions=int(raw.get("repetitions", 1)),
                    purpose=str(raw.get("purpose", "")),
                    mode=str(raw.get("mode", "mock")),
                    provider=str(raw.get("provider", "mock")),
                    judge=str(raw.get("judge", "stub")),
                    judge_config=raw.get("judgeConfig"),
                    budget=raw.get("budget"),
                    sandbox_config=raw.get("sandboxConfig"),
                    compression=raw.get("compression"),
                    use_grade_cache=bool(raw.get("useGradeCache", True)),
                    model=raw.get("model"),
                )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            message = str(exc)
            raise HTTPException(409 if "已存在" in message else 422, message) from None
        save_sampling(runner, manifest.runId, raw)
        queued = _scheduler(data_dir).submit(manifest.runId)
        return {
            "runId": manifest.runId,
            "status": runner.read_state(manifest.runId).get("status", "queued"),
            "planCount": len(manifest.plan),
            "planHash": manifest.planHash[:12],
            "queued": queued["queued"],
            "queueReason": queued["reason"],
            "note": "执行由后台调度器接管;轮询 GET /runs/{runId} 获取进度",
        }

    @router.get("/runs")
    async def list_runs():
        return {"runs": _runner(data_dir).list_runs()}

    @router.get("/runs/{run_id}")
    async def run_detail(run_id: str):
        runner = _runner(data_dir)
        state = _state_or_404(data_dir, run_id)
        try:
            cases = DatasetStore.load_run_snapshot(runner.run_dir(run_id))
        except (FileNotFoundError, ValueError):
            cases = []
        report = build_report(state, cases)
        state_file = runner.run_dir(run_id) / "state.json"
        extra = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
        progress = {
            "total": len(state.trials),
            "pending": sum(1 for t in state.trials if t.status == "pending"),
            "running": sum(1 for t in state.trials if t.status == "running"),
            "completed": sum(1 for t in state.trials if t.status == "completed"),
            "failed": sum(1 for t in state.trials if t.status == "failed"),
            "cancelled": sum(1 for t in state.trials if t.status == "cancelled"),
            "timedOut": sum(1 for t in state.trials if t.status == "timed_out"),
        }
        return {
            "runId": run_id,
            "manifest": {
                "purpose": state.manifest.purpose,
                "mode": state.manifest.mode,
                "provider": state.manifest.provider,
                "dataset": state.manifest.dataset,
                "planCount": len(state.manifest.plan),
                "grading": state.manifest.grading,
                "legacy": state.manifest.legacy,
                "parentRunId": state.manifest.parentRunId,
                "recordingId": state.manifest.recordingId,
                "compression": state.manifest.compression,
            },
            "status": state.status,
            "progress": progress,
            "extra": {k: v for k, v in extra.items() if k != "status"},
            "denominators": report["denominators"],
            "metrics": report["metrics"],
            "annotations": report["annotations"],
            "coverageGaps": report["coverageGaps"],
            "flow": report.get("flow"),
        }

    @router.get("/runs/{run_id}/trials")
    async def run_trials(run_id: str):
        state = _state_or_404(data_dir, run_id)
        grades_by_trial: dict[str, list] = {}
        for grade in state.grades:
            grades_by_trial.setdefault(grade.trialId, []).append(grade.model_dump())
        return {
            "trials": [
                {
                    "trialId": trial.trialId,
                    "caseId": trial.caseId,
                    "repetitionIndex": trial.repetitionIndex,
                    "status": trial.status,
                    "qualityVerdict": trial.qualityVerdict,
                    "weightedScore": trial.weightedScore,
                    "requestedSeed": trial.requestedSeed,
                    "resolvedSeed": trial.resolvedSeed,
                    "error": trial.error,
                    "artifactIds": trial.artifactIds,
                    "tracePath": trial.tracePath,
                    "grades": grades_by_trial.get(trial.trialId, []),
                }
                for trial in state.trials
            ]
        }

    @router.get("/runs/{run_id}/trials/{trial_id}/trace")
    async def trial_trace(run_id: str, trial_id: str):
        runner = _runner(data_dir)
        state = _state_or_404(data_dir, run_id)
        trial = next((t for t in state.trials if t.trialId == trial_id), None)
        if not trial or not trial.tracePath:
            raise HTTPException(404, "该用例未生成 Trace")
        path = Path(trial.tracePath).resolve()
        if not (path.is_relative_to(runner.run_dir(run_id).resolve()) or path.is_relative_to((runner.root / "eval/runs").resolve())):
            raise HTTPException(403, "Trace 不在本次运行目录")
        if not path.is_file():
            raise HTTPException(404, "Trace 文件不存在")
        return json.loads(path.read_text())

    @router.get("/runs/{run_id}/events")
    async def run_events(run_id: str, after: int = 0):
        _state_or_404(data_dir, run_id)
        events_file = _runner(data_dir).run_dir(run_id) / "events.jsonl"
        events = []
        if events_file.is_file():
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    if item.get("sequence", 0) > after:
                        events.append(item)
        return {"events": events}

    @router.get("/runs/{run_id}/report")
    async def run_report(run_id: str):
        runner = _runner(data_dir)
        _state_or_404(data_dir, run_id)
        reports_dir = runner.run_dir(run_id) / "reports"
        latest = (
            max((int(p.name) for p in reports_dir.iterdir() if p.name.isdigit()), default=None)
            if reports_dir.is_dir()
            else None
        )
        if latest is None:
            raise HTTPException(404, "尚无报告;POST 本端点生成")
        report_file = reports_dir / str(latest) / "report.json"
        return json.loads(report_file.read_text(encoding="utf-8"))

    @router.post("/runs/{run_id}/report", status_code=201)
    async def run_report_create(run_id: str):
        runner = _runner(data_dir)
        state = _state_or_404(data_dir, run_id)
        try:
            cases = DatasetStore.load_run_snapshot(runner.run_dir(run_id))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        report = build_report(state, cases)
        _, markdown = ReportWriter(runner.store).write(report)
        return {"reportVersion": report["reportVersion"], "markdown": str(markdown)}

    @router.post("/runs/{run_id}/gate", status_code=201)
    async def run_gate(run_id: str):
        runner = _runner(data_dir)
        state = _state_or_404(data_dir, run_id)
        try:
            cases = DatasetStore.load_run_snapshot(runner.run_dir(run_id))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        state_extra = runner.read_state(run_id)
        decision = evaluate_gate(state, cases, store=store, state_extra=state_extra)
        GateStore(store).save(decision)
        return decision.model_dump()

    @router.get("/runs/{run_id}/gates")
    async def run_gates(run_id: str):
        return {"gates": [d.model_dump() for d in GateStore(store).list_for_run(run_id)]}

    @router.get("/runs/{run_id}/budget")
    async def run_budget(run_id: str):
        """预算页数据:上限、已结算(分口径)、未结预留、未知与估算/实际口径。"""
        _state_or_404(data_dir, run_id)
        ledger = BudgetLedger.open(store, run_id)
        summary = ledger.summary()
        payload = summary.model_dump()
        payload["reservations"] = [
            {
                "reservationId": r.reservationId,
                "callSite": r.callSite,
                "status": r.status,
                "reservedAmount": r.reservedAmount,
                "settledAmount": r.settledAmount,
                "settledBasis": r.settledBasis,
                "trialId": r.trialId,
                "note": r.note,
            }
            for r in ledger.reservations()
        ]
        scheduler = EvaluationScheduler._instances.get(str(store.resolve()))
        if scheduler is not None and scheduler.project_ledger is not None:
            payload["project"] = scheduler.project_ledger.summary().model_dump()
        return payload

    @router.post("/runs/{run_id}/cancel", status_code=202)
    async def run_cancel(run_id: str):
        """取消:排队中直接取消;执行中协作式停止派发,已发出的请求保留预留对账。"""
        _state_or_404(data_dir, run_id)
        return _scheduler(data_dir).cancel(run_id)

    @router.post("/runs/{run_id}/wait")
    async def run_wait(run_id: str):
        """阻塞等待终态(仅测试/CLI 使用;页面应轮询)。"""
        _state_or_404(data_dir, run_id)
        state = _wait_terminal(data_dir, run_id)
        return {"runId": run_id, "status": state.get("status"), "state": state}

    # ---------- Trial 详情(步骤树 + 调用记录) ----------

    @router.get("/trials/{trial_id}")
    async def trial_detail(trial_id: str):
        runner = _runner(data_dir)
        found = None
        for directory in sorted((store / "runs").iterdir()) if (store / "runs").is_dir() else []:
            manifest_file = directory / "manifest.json"
            if not manifest_file.is_file():
                continue
            try:
                state = runner.load_run(directory.name)
            except (FileNotFoundError, ValueError):
                continue
            trial = next((t for t in state.trials if t.trialId == trial_id), None)
            if trial:
                found = (state, trial, directory.name)
                break
        if not found:
            raise HTTPException(404, "trial 不存在")
        state, trial, run_id = found
        events_file = runner.run_dir(run_id) / "events.jsonl"
        steps = []
        if events_file.is_file():
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    if item.get("trialId") == trial_id:
                        steps.append(item)
        grades = [grade.model_dump() for grade in state.grades if grade.trialId == trial_id]
        # 调用记录:每次外部请求尝试的调用点/外部任务/用量/费用与错误(§9 问题定位)。
        attempts = [attempt.model_dump() for attempt in state.attempts if attempt.trialId == trial_id]
        artifacts = []
        for artifact_id in trial.artifactIds:
            try:
                record, _ = runner.objects.get(artifact_id)
                artifacts.append(record.model_dump())
            except (FileNotFoundError, ValueError):
                artifacts.append({"artifactId": artifact_id, "missing": True})
        return {
            "runId": run_id,
            "trial": trial.model_dump(),
            "steps": steps,
            "grades": grades,
            "attempts": attempts,
            "artifacts": artifacts,
        }

    # ---------- 素材内容(内容寻址只读;审核与详情页查看,不提供目录列举) ----------

    @router.get("/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str):
        runner = _runner(data_dir)
        try:
            record, data = runner.objects.get(artifact_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(410, str(exc)) from None  # 损坏/已撤销:不再提供内容
        from fastapi.responses import Response

        return Response(
            content=data,
            media_type=record.mime,
            headers={"Cache-Control": "no-store"},
        )

    # ---------- 比较 ----------

    @router.get("/suggest-baseline")
    async def baseline_suggestion(run_id: str | None = None):
        from .baselines import suggest_baseline
        if run_id:
            _state_or_404(data_dir, run_id)
        return suggest_baseline(_runner(data_dir), run_id)

    # ---------- 成本与压缩配对(硬条件 + 重跑引导) ----------

    def _pair_reasons(baseline, candidate) -> list[dict] | None:
        """压缩对比硬校验;不合法时返回中文 reasons[](不生成全未知报告)。"""
        from .pairing import validate_pair

        reasons = validate_pair(store, baseline, candidate)
        return reasons or None

    def _invalid_pair_response(reasons: list[dict]):
        """4xx + 中文 reasons[];直接 JSONResponse,形状不随宿主 app 的异常处理器变化。"""
        from fastapi.responses import JSONResponse

        summary = "无法对比:" + ";".join(item["message"] for item in reasons)
        return JSONResponse({"error": summary, "reasons": reasons}, status_code=422)

    @router.get("/suggest-pair")
    async def pair_suggestion(run_id: str | None = None):
        from .pairing import suggest_pair
        if run_id:
            _state_or_404(data_dir, run_id)
        return suggest_pair(_runner(data_dir), store, run_id)

    @router.post("/validate-pair")
    async def validate_pair_endpoint(request: Request):
        from .pairing import pair_coverage
        raw = await request.json()
        baseline = _state_or_404(data_dir, str(raw.get("baselineRunId", "")))
        candidate = _state_or_404(data_dir, str(raw.get("candidateRunId", "")))
        reasons = _pair_reasons(baseline, candidate)
        if reasons:
            return _invalid_pair_response(reasons)
        return {
            "valid": True,
            "baselineRunId": baseline.manifest.runId,
            "candidateRunId": candidate.manifest.runId,
            "coverage": pair_coverage(baseline, candidate),
        }

    def resolve_baseline(raw, candidate_key):
        from .baselines import suggest_baseline
        explicit = raw.get("baselineRunId")
        if explicit:
            return str(explicit)
        candidate = str(raw.get(candidate_key, ""))
        _state_or_404(data_dir, candidate)
        suggestion = suggest_baseline(_runner(data_dir), candidate)
        if not suggestion["baselineRunId"]:
            raise HTTPException(422, suggestion["reason"])
        return suggestion["baselineRunId"]

    @router.post("/comparisons")
    async def comparisons(request: Request):
        raw = await request.json()
        runner = _runner(data_dir)
        baseline = _state_or_404(data_dir, resolve_baseline(raw, "candidateRunId"))
        candidate = _state_or_404(data_dir, str(raw.get("candidateRunId", "")))

        def snapshot_cases(state):
            try:
                return DatasetStore.load_run_snapshot(runner.run_dir(state.manifest.runId))
            except (FileNotFoundError, ValueError):
                return []

        return build_comparison(
            baseline,
            candidate,
            baseline_cases=snapshot_cases(baseline),
            candidate_cases=snapshot_cases(candidate),
        )

    # ---------- 压缩实验报告 ----------

    @router.post("/compression-report")
    async def compression_report(request: Request):
        raw = await request.json()
        baseline = _state_or_404(data_dir, resolve_baseline(raw, "compressedRunId"))
        compressed = _state_or_404(data_dir, str(raw.get("compressedRunId", "")))
        # 非法配对(快照不一致/缺费用/token/非压缩对)直接 4xx + reasons,不生成全未知报告(§12.3)。
        reasons = _pair_reasons(baseline, compressed)
        if reasons:
            return _invalid_pair_response(reasons)
        summary = compression_summary(baseline, compressed)
        budgets = {}
        for label, state in (("baseline", baseline), ("compressed", compressed)):
            ledger_file = store / "budgets" / f"{state.manifest.runId}.jsonl"
            if ledger_file.is_file():
                budgets[label] = BudgetLedger.open(store, state.manifest.runId).summary().model_dump()
        summary = apply_cost(summary, budgets.get("baseline"), budgets.get("compressed"))
        summary = apply_promotion_gate(summary, max_quality_drop=float(raw.get("maxQualityDrop", 0.02)))
        return summary

    # ---------- 校准(§11.3) ----------

    @router.get("/calibrations")
    async def calibrations():
        return {"calibrations": CalibrationStore(store).list_all()}

    @router.post("/calibrations", status_code=201)
    async def import_calibration(request: Request):
        raw = await request.json()
        try:
            record = CalibrationStore(store).import_samples(
                raw.get("samples") or [],
                judge_model=str(raw.get("judgeModel", "")),
                rubric_version=str(raw.get("rubricVersion", "1")),
                prompt_version=str(raw.get("promptVersion", "1")),
                labeler_version=str(raw.get("labelerVersion", "1")),
                dataset_ref=str(raw.get("datasetRef", "")),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return record.model_dump()

    # ---------- 观测导出 Outbox(§9.3) ----------

    @router.get("/outbox/status")
    async def outbox_status():
        outbox = Outbox(store / "outbox" / "events.jsonl")
        pending = outbox.pending()
        exporter = LangfuseExporter()
        return {
            "pending": len(pending),
            "langfuseConfigured": bool(
                exporter.public_key and exporter.secret_key and exporter.host
            ),
            "note": "未配置凭据时导出不可用,界面必须显示'未配置/未验证'(§3.3)",
        }

    @router.post("/outbox/flush")
    async def outbox_flush(request: Request):
        """人工触发导出重试;Langfuse 未配置时返回明确状态,不假装已导出。"""
        await request.json()
        outbox = Outbox(store / "outbox" / "events.jsonl")
        exporter = LangfuseExporter()
        if not (exporter.public_key and exporter.secret_key and exporter.host):
            return {
                "flushed": 0,
                "remaining": len(outbox.pending()),
                "configured": False,
                "lastError": "Langfuse 未配置(LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST)",
            }
        result = make_outbox_exporter(exporter)(outbox)
        result["configured"] = True
        return result

    # ---------- 运维健康(§17.4) ----------

    @router.get("/health")
    async def health():
        from .health import evaluation_health

        return evaluation_health(store)

    # ---------- 告警条件(§17.4,可配置阈值;不发外部通知) ----------

    @router.get("/alerts")
    async def alerts(threshold: str | None = None):
        from .health import evaluate_alerts

        rules = json.loads(threshold) if threshold else None
        return evaluate_alerts(store, rules)

    # ---------- 人工审核 ----------

    @router.get("/review-tasks")
    async def review_tasks(status: str | None = None, run_id: str | None = None):
        tasks = ReviewStore(store).list(status=status)
        if run_id:
            tasks = [task for task in tasks if task.runId == run_id]
        return {"tasks": [{**t.model_dump(), "evidence": review_evidence(t)} for t in tasks]}

    @router.get("/review-tasks/{task_id}")
    async def review_task_detail(task_id: str):
        try:
            detail = ReviewStore(store).task_detail(task_id)
            task = ReviewStore(store).get(task_id)
            detail["task"]["evidence"] = review_evidence(task)
            return detail
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.post("/review-tasks", status_code=201)
    async def create_review_task(request: Request):
        raw = await request.json()
        state = _state_or_404(data_dir, str(raw.get("runId", "")))
        trial = next((t for t in state.trials if t.trialId == str(raw.get("trialId", ""))), None)
        if trial is not None:
            cases = DatasetStore.load_run_snapshot(store / "runs" / state.manifest.runId)
            case = next((item for item in cases if item.caseId == trial.caseId), None)
            flow = bool(trial.flowLayer) or bool(case and case.taskType == "agent_flow")
            grades = [grade for grade in state.grades if grade.trialId == trial.trialId]
            rules_only = bool(grades) and all(grade.source == "rules" and grade.status in {"pass", "fail", "dependency_failed"} for grade in grades)
            policy = ReviewSamplingPolicy.model_validate(state.manifest.reviewSampling or {})
            flagged = bool(case and case.needsHumanReview)
            attribution = flow and trial.qualityVerdict == "fail" and (policy.reviewAgentFlowFails or policy.agentFailures)
            if (flow or rules_only) and not flagged and not attribution:
                raise HTTPException(422, "此用例由程序规则判定，无需人工重判；流程失败请查看 Trace 与失败原因。失败归因抽检需在运行配置中显式开启。")
        artifact_ids = raw.get("artifactIds") or []
        grade_refs = raw.get("gradeRefs") or []
        # 未显式提供时自动补全绑定:从 run 的 trial 取素材与评分引用,
        # 保证审核页"查看素材/自动证据"闭环对手动创建的任务同样成立(§11)。
        if not artifact_ids or not grade_refs:
            try:
                state = _state_or_404(data_dir, str(raw.get("runId", "")))
                trial = next((t for t in state.trials if t.trialId == str(raw.get("trialId", ""))), None)
                if trial is not None:
                    artifact_ids = artifact_ids or list(trial.artifactIds)
                    if not grade_refs:
                        grade_refs = [
                            {"gradeId": grade.gradeId, "evaluator": grade.evaluator}
                            for grade in state.grades
                            if grade.trialId == trial.trialId
                        ]
            except (FileNotFoundError, ValueError):
                pass  # run 缺失/损坏由 create 内部校验报错
        task = ReviewStore(store).create(
            str(raw.get("runId", "")),
            str(raw.get("trialId", "")),
            str(raw.get("caseId", "")),
            artifact_ids=artifact_ids,
            grade_refs=grade_refs,
            priority=str(raw.get("priority", "normal")),
        )
        return {"task": {**task.model_dump(), "evidence": review_evidence(task)}}

    def _guard_conflict(func):
        try:
            return func()
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.post("/review-tasks/{task_id}/claim")
    async def claim_review(task_id: str, request: Request):
        raw = await request.json()
        return {
            "task": _guard_conflict(
                lambda: ReviewStore(store)
                .claim(task_id, str(raw.get("reviewer", "")), int(raw.get("expectedRevision", 0)))
                .model_dump()
            )
        }

    @router.post("/review-tasks/{task_id}/reviews", status_code=201)
    async def submit_review(task_id: str, request: Request):
        raw = await request.json()
        opinion = _guard_conflict(
            lambda: ReviewStore(store).submit_opinion(
                task_id,
                str(raw.get("reviewer", "")),
                raw.get("verdicts") or [],
                int(raw.get("expectedRevision", 0)),
                agree_with_auto=raw.get("agreeWithAuto"),
                category=raw.get("category"),
                note=raw.get("note"),
                uncertain=bool(raw.get("uncertain", False)),
            )
        )
        return {"opinion": opinion.model_dump()}

    @router.post("/review-tasks/{task_id}/dispute", status_code=200)
    async def dispute_review(task_id: str, request: Request):
        raw = await request.json()
        task = _guard_conflict(
            lambda: ReviewStore(store).dispute(
                task_id, str(raw.get("by", "")), str(raw.get("reason", ""))
            )
        )
        return {"task": {**task.model_dump(), "evidence": review_evidence(task)}}

    @router.post("/review-tasks/{task_id}/adjudications", status_code=201)
    async def adjudicate_review(task_id: str, request: Request):
        raw = await request.json()
        adjudication = _guard_conflict(
            lambda: ReviewStore(store).adjudicate(
                task_id,
                raw.get("boundOpinionIds") or [],
                str(raw.get("finalVerdict", "")),
                str(raw.get("reason", "")),
                str(raw.get("decidedBy", "")),
            )
        )
        return {"adjudication": adjudication.model_dump()}

    @router.post("/review-tasks/{task_id}/case-drafts", status_code=201)
    async def review_to_case_draft(task_id: str, request: Request):
        raw = await request.json()
        path = _guard_conflict(lambda: ReviewStore(store).create_case_draft(task_id, raw.get("case") or {}))
        return {"draftId": json.loads(path.read_text(encoding="utf-8"))["draftId"]}

    @router.post("/dataset-drafts/{draft_id}/publish", status_code=201)
    async def publish_draft(draft_id: str, request: Request):
        raw = await request.json()
        try:
            manifest = publish_case_draft(
                store, draft_id, str(raw.get("datasetId", "")), split=str(raw.get("split", "regression"))
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return manifest

    return router
