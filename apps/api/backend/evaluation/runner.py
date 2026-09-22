"""Run/Trial 调度与执行(技术方案 §7)。

边界与诚实性约束:
- mock 禁止外部调用;live 必须显式正预算与密钥;replay 只消费录制、禁止真实出口。
- run manifest 创建即冻结;重跑创建新 run;恢复只继续 pending 状态的 trial。
- live 恢复绝不盲目重提:externalTaskId 未对账前,相关 trial 保持未知状态。
- 判分只读 run 目录内冻结的用例快照与素材,不回头读源文件(DATA-01)。
- 执行失败不评分,进入报告覆盖缺口;裁判整体失败记 error,不冒充零分或通过。
- 裁判、修复重试、生成、轮询等一切真实外部调用统一经预算网关(§13.2)。
- 回放完整性(未消费交互/新增出口)使 run 失败,影响报告、门禁与 CLI 退出码,
  不再只记日志(§8.3)。
- 取消语义:停止派发新 trial;已发出的外部请求保留预算预留并按 unknown 对账,
  绝不提前释放并发槽位(执行线程真正退出后才归还)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ..common import atomic_write_text, now
from ..config import Config
from ..traces import classify, timestamp as parse_iso
from .artifacts import ObjectStore
from .budgets import BudgetExhausted, BudgetLedger, BudgetPolicy, PriceUnknown
from .datasets import DatasetStore, load_cases, validate_source_params
from .gateway import BudgetedTransport, CallContext
from .gradecache import GradeCache, grade_cache_key
from .graders.judge import StubJudge, VlmJudge
from .graders.rules import Answer, grade_case
from .models import (
    Attempt,
    CostRecord,
    EvalEvent,
    Grade,
    RunManifest,
    RunState,
    RunStatus,
    Trial,
    TrialPlanLine,
    TrialStatus,
    UsageRecord,
    canonical_hash,
    trial_identity,
)
from .outbox import Outbox
from .recorder import RecordingTransport, RecordingWriter
from .registry import (
    capabilities_for,
    check_case_supported,
    compress_prompt,
    extract_video_frames,
)
from .replay import ReplayTransport

MAX_REPETITIONS = 10
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# live 沙盒配置白名单(非密钥);密钥只从环境读取(§16)。
SANDBOX_CONFIG_KEYS = {
    "provider",
    "cloudVendor",
    "cloudBaseUrl",
    "cloudModel",
    "cloudTextModel",
    "videoModel",
}
# 裁判配置白名单:冻结进 manifest.grading,任何一项变化都是新评分配置。
JUDGE_CONFIG_KEYS = {
    "model",
    "baseUrl",
    "temperature",
    "maxAttempts",
    "maxTokens",
    "promptVersion",
    "preprocessingVersion",
}
VIDEO_KINDS = {"text_to_video", "image_to_video"}
REFERENCE_TASKS = {"image_to_image", "image_edit", "image_to_video"}


class ReplayIntegrityFailed(RuntimeError):
    """回放完整性校验失败:未消费交互或观测到新增出口(影响退出码与门禁)。"""


class RunCancelled(RuntimeError):
    """run 被用户取消;执行线程正常退出后由调度器回收槽位。"""


class EventLog:
    """run 内只追加事件账本(§14.3);sequence 单调,eventId 唯一。

    outbox 提供时,每条事件同步落副本进入观测导出缓冲;导出失败不反压本账本。
    """

    def __init__(self, run_dir: Path, outbox: Outbox | None = None, trace_id: str | None = None):
        self.file = run_dir / "events.jsonl"
        self.outbox = outbox
        self.trace_id = trace_id
        self._lock = threading.Lock()
        self._sequence = 0
        if self.file.is_file():
            with self.file.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        self._sequence += 1

    def append(
        self, event_type: str, run_id: str, trialId=None, stepId=None, **payload
    ) -> EvalEvent:
        with self._lock:
            self._sequence += 1
            event = EvalEvent(
                eventId=f"ev-{self._sequence:06d}-{hashlib.sha256(f'{run_id}:{self._sequence}'.encode()).hexdigest()[:8]}",
                sequence=self._sequence,
                type=event_type,
                runId=run_id,
                trialId=trialId,
                traceId=self.trace_id,
                stepId=stepId,
                timestamp=now(),
                payload=payload,
            )
            with self.file.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
            if self.outbox is not None:
                self.outbox.push(event.model_dump())
            return event


def deterministic_seed(run_id: str, case_id: str, repetition: int) -> int:
    digest = hashlib.sha256(f"{run_id}|{case_id}|{repetition}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (2**31 - 1)


def code_fingerprint(root: Path) -> dict:
    """尽力记录代码版本(§5.3);非 git 环境保持 None,不伪造。"""
    fingerprint: dict = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10
        )
        if commit.returncode == 0:
            fingerprint["commit"] = commit.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=30
        )
        if status.returncode == 0:
            fingerprint["dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        logging.warning("无法获取 git 版本信息,code 字段保持 null")
    return fingerprint


def _budget_rejection(exc: Exception) -> str:
    if isinstance(exc, PriceUnknown):
        return f"[PRICE_UNKNOWN] {exc}"
    if isinstance(exc, BudgetExhausted):
        return f"[BUDGET_EXHAUSTED] {exc}"
    return f"[BUDGET_REJECTED] {exc}"


def make_judge(
    name: str,
    grading: dict | None = None,
    client: httpx.Client | None = None,
    ledger: BudgetLedger | None = None,
    context: CallContext | None = None,
):
    """构造判分器。

    - stub:合成裁判,无外部调用。
    - vlm:配置必须已冻结进 manifest.grading;提供 ledger 时所有请求(含修复重试)
      经同步预算网关预留/结算;价格未登记或预算不足 → 抛 PriceUnknown/BudgetExhausted,
      由调用方记为裁判失败,不静默转免费。
    """
    grading = grading or {}
    if name == "stub":
        return StubJudge()
    if name == "vlm":
        from .config import load_eval_config

        eval_config = load_eval_config()
        api_key = eval_config.judge_api_key
        if not api_key:
            raise ValueError("缺少裁判 API Key:请设置 EVAL_JUDGE_API_KEY")
        inner = httpx.HTTPTransport() if client is None else None
        if client is None:
            if ledger is None:
                raise ValueError("vlm 裁判必须挂载预算账本(§13.2:真实调用先预留)")
            from .gateway import BudgetedSyncTransport

            judge_client = httpx.Client(
                transport=BudgetedSyncTransport(
                    inner=inner, ledger=ledger, context=context, call_site="judge"
                )
            )
        else:
            judge_client = client
        return VlmJudge(
            judge_client,
            grading.get("baseUrl") or eval_config.judge_base_url,
            api_key,
            grading.get("model") or eval_config.judge_model,
            rubric_version=str(grading.get("rubricVersion", "1")),
            temperature=float(grading.get("temperature", 0)),
            max_attempts=int(grading.get("maxAttempts", 2)),
            max_tokens=int(grading.get("maxTokens", 2000)),
        )
    raise ValueError(f"未知判分器:{name}")


class EvaluationRunner:
    def __init__(self, root: Path, store: Path | None = None):
        self.root = root
        self.store = store or root / "data" / "evaluation"
        self.datasets = DatasetStore(self.store)
        self.objects = ObjectStore(self.store, project="frayune")

    # ---------- 路径 ----------

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("runId 只允许字母/数字/点/下划线/连字符,长度 1-64")
        return self.store / "runs" / run_id

    # ---------- 数据集 ----------

    def freeze_dataset(self, source: Path, dataset_id: str, split: str = "dev") -> dict:
        cases = load_cases(Path(source))
        problems = validate_source_params(cases)
        if problems:
            raise ValueError("数据集预检失败:" + ";".join(problems))
        manifest = self.datasets.freeze(cases, dataset_id, split, source_path=Path(source))
        return manifest.model_dump()

    # ---------- Run 生命周期 ----------

    def create_run(
        self,
        run_id: str,
        dataset_id: str,
        version: int | None = None,
        *,
        provider: str = "mock",
        mode: str = "mock",
        repetitions: int = 1,
        purpose: str = "",
        judge: str = "stub",
        judge_config: dict | None = None,
        budget: dict | None = None,
        recording_id: str | None = None,
        source_run_id: str | None = None,
        sandbox_config: dict | None = None,
        compression: dict | None = None,
        use_grade_cache: bool = True,
        model: str | None = None,
    ) -> RunManifest:
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 128):
            raise ValueError("model 须为非空字符串(≤128 字符)")
        model = model.strip() if isinstance(model, str) else None
        if judge not in {"stub", "vlm"}:
            raise ValueError(f"未知判分器:{judge}")
        if judge == "vlm":
            if mode != "live":
                raise ValueError("vlm 裁判产生真实调用,只允许在 live 模式使用")
            if not budget or budget.get("maxCost") in (None, 0, "0", "0.00"):
                raise ValueError("vlm 裁判必须配置正预算(§13.2)")
            if judge_config and not isinstance(judge_config, dict):
                raise ValueError("judgeConfig 必须是对象")
        if mode == "mock":
            if judge != "stub":
                raise ValueError("mock 模式只允许 stub 判分(禁止外部调用)")
            if provider != "mock":
                raise ValueError("mock 模式只能使用 mock Provider(§7.1:mock 禁止外部模型调用)")
            return self._create_standard_run(
                target=None,
                run_id=run_id,
                dataset_id=dataset_id,
                version=version,
                provider="mock",
                mode="mock",
                repetitions=repetitions,
                purpose=purpose,
                judge=judge,
                compression=compression,
                use_grade_cache=use_grade_cache,
                model=model,
            )
        if mode == "live" and model:
            # 实验变量固定到 run:cloud 平台经沙盒配置下发(Provider 的请求体模型
            # 取自 config.cloudModel),mock 平台经 manifest.model 选择请求体模型。
            sandbox_config = dict(sandbox_config or {})
            sandbox_config["cloudModel"] = model
        if mode == "live":
            if provider == "mock":
                raise ValueError("live 模式不能使用 mock Provider")
            if not budget or budget.get("maxCost") in (None, 0, "0", "0.00"):
                raise ValueError(
                    "live 运行必须显式配置正预算(§13.2:真实调用需本次明确启用);默认预算为 0"
                )
            BudgetPolicy(**budget)  # 预算参数在创建期即校验:负数/非有限值直接拒绝
            if not recording_id:
                recording_id = f"rec-{run_id}"
            return self._create_standard_run(
                target=None,
                run_id=run_id,
                dataset_id=dataset_id,
                version=version,
                provider=provider,
                mode="live",
                repetitions=repetitions,
                purpose=purpose,
                judge=judge,
                judge_config=judge_config,
                budget=budget,
                recording_id=recording_id,
                sandbox_config=sandbox_config,
                compression=compression,
                use_grade_cache=use_grade_cache,
                model=model,
            )
        if mode == "replay":
            return self._create_replay_run(
                self.run_dir(run_id), run_id, source_run_id, recording_id, purpose
            )
        raise ValueError(f"未知执行模式:{mode}")

    def _create_standard_run(
        self,
        target,
        run_id,
        dataset_id,
        version,
        *,
        provider,
        mode,
        repetitions,
        purpose,
        judge,
        judge_config=None,
        budget=None,
        recording_id=None,
        sandbox_config=None,
        compression=None,
        use_grade_cache=True,
        model: str | None = None,
    ) -> RunManifest:
        if mode != "mock":
            if not 1 <= repetitions <= MAX_REPETITIONS:
                raise ValueError(f"重复次数须在 1-{MAX_REPETITIONS} 之间")
        frozen = self.datasets.load(dataset_id, version)
        problems = validate_source_params(frozen.cases)
        if problems:
            raise ValueError("数据集快照与执行链不兼容:" + ";".join(problems))
        target = self.run_dir(run_id)
        if target.exists():
            raise ValueError(f"runId 已存在:{run_id}(每次运行使用新的 runId,§19.4)")
        # 能力预检(P4,§1.2):不支持的组合显式拒绝,不降级、不静默改型。
        caps = capabilities_for({"provider": provider, **(sandbox_config or {})})
        references: dict[str, list[str]] = {}
        for case in frozen.cases:
            check_case_supported(case, caps)
            if case.input.referenceArtifactIds:
                refs = self._archive_references(run_id, case)
                references[case.caseId] = refs
        target.mkdir(parents=True, exist_ok=True)  # references 归档可能已提前创建
        dataset_ref = self.datasets.snapshot_into(dataset_id, frozen.manifest.version, target)
        plan = []
        for case in frozen.cases:
            for repetition in range(repetitions):
                raw_seed = case.input.params.get("seed")
                requested = (
                    int(raw_seed)
                    if isinstance(raw_seed, (int, float)) and not isinstance(raw_seed, bool) and raw_seed >= 0
                    else deterministic_seed(run_id, case.caseId, repetition)
                )
                plan.append(
                    TrialPlanLine(
                        caseId=case.caseId,
                        caseVersion=case.version,
                        caseContentHash=case.contentHash,
                        repetitionIndex=repetition,
                        requestedSeed=requested,
                    )
                )
        grading = {
            "judge": judge,
            "rubricVersion": "1",
            "promptVersion": "1",
            "preprocessingVersion": "1",
            "cacheBypass": not use_grade_cache,
        }
        if judge == "vlm":
            config = {
                key: value
                for key, value in (judge_config or {}).items()
                if key in JUDGE_CONFIG_KEYS
            }
            config.setdefault("promptVersion", "1")
            grading.update(config)
        manifest = RunManifest(
            runId=run_id,
            createdAt=now(),
            purpose=purpose,
            mode=mode,
            provider=provider,
            dataset=dataset_ref,
            plan=plan,
            grading=grading,
            budget=budget
            or {"currency": "CNY", "maxCost": "0.00", "maxExternalCalls": 0},
            code=code_fingerprint(self.root),
            model=model,
            sandboxConfig=self._sandbox_config_snapshot(provider, sandbox_config),
            recordingId=recording_id,
            compression=self._compression_snapshot(compression),
        )
        self._persist_manifest_and_trials(target, manifest)
        return manifest

    @staticmethod
    def _compression_snapshot(compression: dict | None) -> dict:
        """压缩实验配置快照:默认关闭;启用时必须声明压缩器版本(§13.5)。"""
        if not compression or not compression.get("enabled"):
            return {"enabled": False}
        version = compression.get("compressorVersion", "whitespace-v1")
        return {"enabled": True, "compressorVersion": version}

    def _archive_references(self, run_id: str, case) -> list[str]:
        """把用例引用素材复制进 run 目录(校验哈希),保证 run 自包含(DATA-01)。"""
        refs_dir = self.run_dir(run_id) / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        archived = []
        for artifact_id in case.input.referenceArtifactIds:
            record, data = self.objects.get(artifact_id)  # 缺失/损坏在这里显式失败
            if case.taskType in REFERENCE_TASKS and not record.mime.startswith("image/"):
                raise ValueError(f"用例 {case.caseId} 的参考素材 {artifact_id} 不是图片")
            target = refs_dir / f"{artifact_id}.{record.mime.rsplit('/', 1)[-1]}"
            target.write_bytes(data)
            if hashlib.sha256(target.read_bytes()).hexdigest() != record.sha256:
                raise ValueError(f"参考素材复制后哈希不一致:{artifact_id}")
            archived.append(artifact_id)
        return archived

    def _create_replay_run(self, target, run_id, source_run_id, recording_id, purpose) -> RunManifest:
        """严格回放 run:计划来自源 run 已完成 trial,seed 采用录制时的 resolvedSeed(§8.4)。"""
        if not source_run_id:
            raise ValueError("replay 模式必须提供 source_run_id(被回放的 run)")
        if not recording_id:
            raise ValueError("replay 模式必须提供 recording_id(源 run 的录制包)")
        target = self.run_dir(run_id)
        if target.exists():
            raise ValueError(f"runId 已存在:{run_id}(每次运行使用新的 runId,§19.4)")
        source = self.load_run(source_run_id)
        if source.manifest.mode == "mock":
            raise ValueError("mock run 没有外部交互,无需回放")
        source_plan = {
            (line.caseId, line.repetitionIndex): line for line in source.manifest.plan
        }
        replayable = [t for t in source.trials if t.status == "completed" and t.resolvedSeed is not None]
        if not replayable:
            raise ValueError("源 run 没有可回放的已完成 trial")
        target.mkdir(parents=True)
        # 复制源 run 的数据集快照,保证与录制时输入一致(§8.1)
        shutil.copytree(self.run_dir(source_run_id) / "dataset", target / "dataset")
        source_refs = self.run_dir(source_run_id) / "references"
        if source_refs.is_dir():
            shutil.copytree(source_refs, target / "references")
        dataset_ref = dict(source.manifest.dataset)
        plan = []
        for trial in replayable:
            line = source_plan.get((trial.caseId, trial.repetitionIndex))
            plan.append(
                TrialPlanLine(
                    caseId=trial.caseId,
                    caseVersion=trial.caseVersion,
                    caseContentHash=line.caseContentHash if line else "",
                    repetitionIndex=trial.repetitionIndex,
                    requestedSeed=trial.resolvedSeed,
                )
            )
        manifest = RunManifest(
            runId=run_id,
            createdAt=now(),
            purpose=purpose or f"严格回放 {source_run_id}",
            mode="replay",
            provider=source.manifest.provider,
            dataset=dataset_ref,
            plan=plan,
            grading={"judge": "stub", "rubricVersion": "1"},
            budget={"currency": "CNY", "maxCost": "0.00", "maxExternalCalls": 0},
            code=code_fingerprint(self.root),
            sandboxConfig=source.manifest.sandboxConfig,
            recordingId=recording_id,
            seedPolicy="recorded",
            parentRunId=source_run_id,
        )
        self._persist_manifest_and_trials(target, manifest)
        return manifest

    def _persist_manifest_and_trials(self, target: Path, manifest: RunManifest) -> None:
        atomic_write_text(
            target / "manifest.json",
            json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2),
        )
        (target / "trials").mkdir(exist_ok=True)
        for line in manifest.plan:
            trial = Trial(
                trialId=trial_identity(manifest.runId, line.caseId, line.caseVersion, line.repetitionIndex),
                runId=manifest.runId,
                caseId=line.caseId,
                caseVersion=line.caseVersion,
                repetitionIndex=line.repetitionIndex,
                requestedSeed=line.requestedSeed,
            )
            self._trial_dir(manifest.runId, trial.trialId).mkdir(parents=True, exist_ok=True)
            self._write_trial(trial)
        self._write_state(manifest.runId, status="queued")
        EventLog(target).append("run.created", manifest.runId, planCount=len(manifest.plan))

    @staticmethod
    def _sandbox_config_snapshot(provider: str, sandbox_config: dict | None = None) -> dict:
        """live 运行的非密钥配置快照;密钥在 execute 时从环境读取,不落盘(§16)。"""
        snapshot = {
            key: value
            for key, value in (sandbox_config or {}).items()
            if key in SANDBOX_CONFIG_KEYS and isinstance(value, str)
        }
        snapshot.setdefault("provider", provider)
        if snapshot["provider"] == "cloud":
            snapshot.setdefault("cloudVendor", "zhipu")
        return snapshot

    def load_run(self, run_id: str) -> RunState:
        directory = self.run_dir(run_id)
        try:
            manifest = RunManifest.model_validate(
                json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"run 不存在:{run_id}") from exc
        except ValidationError as exc:
            raise ValueError(f"run manifest 损坏:{run_id}") from exc
        trials, grades, attempts = [], [], []
        trials_root = directory / "trials"
        for trial_dir in sorted(trials_root.iterdir()) if trials_root.is_dir() else []:
            trial_file = trial_dir / "trial.json"
            if not trial_file.is_file():
                continue
            trials.append(Trial.model_validate(json.loads(trial_file.read_text(encoding="utf-8"))))
            grade_file = trial_dir / "grades.json"
            if grade_file.is_file():
                grades.extend(json.loads(grade_file.read_text(encoding="utf-8"))["grades"])
            attempt_file = trial_dir / "attempts.json"
            if attempt_file.is_file():
                attempts.extend(
                    json.loads(attempt_file.read_text(encoding="utf-8")).get("attempts", [])
                )
        parsed_grades = [Grade.model_validate(item) for item in grades]
        parsed_attempts = [Attempt.model_validate(item) for item in attempts]
        state_file = directory / "state.json"
        status: RunStatus = "queued"
        if state_file.is_file():
            status = json.loads(state_file.read_text(encoding="utf-8")).get("status", "queued")
        return RunState(
            manifest=manifest,
            trials=trials,
            grades=parsed_grades,
            attempts=parsed_attempts,
            status=status,
        )

    def read_state(self, run_id: str) -> dict:
        state_file = self.run_dir(run_id) / "state.json"
        if not state_file.is_file():
            return {}
        return json.loads(state_file.read_text(encoding="utf-8"))

    def execute_run(
        self,
        run_id: str,
        transport=None,
        poll_interval: float = 0.05,
        job_timeout_sec: float = 300,
        use_grade_cache: bool | None = None,
        cancel_check=None,
        project_ledger: BudgetLedger | None = None,
    ):
        """执行 run。transport 仅测试/沙盒注入(如 stub 上游);生产 live 走真实网络。

        - mock:无外部调用,transport 必须为 None。
        - live:预算网关(预留先行、标记 sent)→ 录制 → 真实/stub 上游;结束后封存录制。
        - replay:严格回放 transport,未知请求立即失败;结束后校验计划内交互全部消费,
          未消费/新增出口 → run 失败(ReplayIntegrityFailed)。
        - cancel_check:调度器注入的取消探测;取消后停止派发,已发出的请求保留预留。
        """
        state = self.load_run(run_id)
        if state.status not in {"queued", "running", "interrupted"}:
            raise ValueError(f"run 状态为 {state.status},不能执行;重跑请创建新 run")
        run_id = state.manifest.runId
        manifest = state.manifest
        trace_id = f"evaltrace-{hashlib.sha256(f'{run_id}:{now()}'.encode()).hexdigest()[:16]}"
        outbox = Outbox(self.store / "outbox" / "events.jsonl")
        self._write_state(
            run_id, status="running", startedAt=state.manifest.createdAt, traceId=trace_id
        )
        log = EventLog(self.run_dir(run_id), outbox=outbox, trace_id=trace_id)
        log.append("run.started", run_id, mode=manifest.mode, provider=manifest.provider)
        if manifest.mode == "mock" or manifest.mode == "replay":
            if transport is not None and manifest.mode == "mock":
                raise ValueError("mock 模式禁止注入网络 transport(§7.1:mock 禁止外部调用)")
            for trial in state.trials:
                if trial.status == "running":
                    # mock/replay 无外部副作用,崩溃残留的 running trial 可安全重跑;
                    # live 模式必须先对账 externalTaskId,绝不盲目重提。
                    trial.status = "pending"
                    self._write_trial(trial)
                    log.append(
                        "trial.reset",
                        run_id,
                        trialId=trial.trialId,
                        reason="恢复:无外部副作用,重新执行",
                    )
        run_dir = self.run_dir(run_id)
        context = CallContext()
        context.run_id = run_id
        source_trial_key: dict = {}
        ledger = None
        recording_writer = None
        replay_transport = None
        app_transport = None
        if manifest.mode == "live":
            ledger = BudgetLedger.open(
                self.store,
                scope_id=run_id,
                policy=BudgetPolicy(**manifest.budget),
            )
            recording_writer = RecordingWriter(self.store, manifest.recordingId, source_run_id=run_id)
            inner = transport or httpx.AsyncHTTPTransport()
            app_transport = BudgetedTransport(
                inner=RecordingTransport(inner=inner, writer=recording_writer, objects=self.objects, context=context),
                ledger=ledger,
                context=context,
            )
        elif manifest.mode == "replay":
            from .replay import RecordingReader

            replay_transport = ReplayTransport(
                RecordingReader(self.store, manifest.recordingId, objects=self.objects),
                context=context,
            )
            app_transport = replay_transport
            # 回放 trialId 与源 run 不同:按 (caseId, repetitionIndex) 映射回源 trialId,
            # 供回放 transport 按"源 trial 分桶"严格匹配,避免同形请求跨 trial 错配。
            source_trial_key = {
                (line.caseId, line.repetitionIndex): trial_identity(
                    manifest.parentRunId or "", line.caseId, line.caseVersion, line.repetitionIndex
                )
                for line in manifest.plan
            }
        cases = {case.caseId: case for case in DatasetStore.load_run_snapshot(run_dir)}
        config = self._sandbox_runtime_config(manifest)
        sandbox = run_dir / "sandbox"
        cancelled = False
        try:
            with TestClient(
                create_sandbox_app(sandbox, config, transport=app_transport)
            ) as client:
                models = client.get("/api/models").json().get("models", [])
                model_ids = [item["id"] for item in models]
                if manifest.model:
                    if manifest.provider != "mock" and model_ids and manifest.model not in model_ids:
                        # 云端沙盒未提供该模型:显式失败,不静默回落默认模型(mock 是
                        # 协议夹具,模型名仅透传,不做白名单限制)
                        raise ValueError(
                            f"模型 {manifest.model} 不在沙盒可用列表 {model_ids[:5]} 中,"
                            "请检查 sandboxConfig.cloudModel 或平台支持"
                        )
                    default_model = manifest.model
                else:
                    default_model = model_ids[0] if model_ids else manifest.provider
                for trial in state.trials:
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        break
                    if trial.status != "pending":
                        continue  # 恢复语义:仅继续未开始的 trial(§5.4 interrupted)
                    case = cases.get(trial.caseId)
                    if case is None:  # 快照与计划不一致属于执行器级故障
                        raise ValueError(f"快照缺少用例:{trial.caseId}")
                    context.trial_id = trial.trialId
                    context.match_trial_id = source_trial_key.get(
                        (trial.caseId, trial.repetitionIndex)
                    )
                    context.attempt_index = 0
                    self._execute_trial(
                        client, trial, case, default_model, log, poll_interval, job_timeout_sec,
                        cancel_check=cancel_check, manifest=manifest,
                    )
                    context.match_trial_id = None
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        break
        except Exception as exc:
            self._write_state(run_id, status="interrupted", error=str(exc)[:500])
            log.append("run.interrupted", run_id, error=str(exc)[:500])
            raise
        if cancelled:
            self._cancel_remaining(run_id, state, log)
            self._write_state(run_id, status="cancelled", finishedAt=now())
            log.append("run.cancelled", run_id)
            return self.load_run(run_id)
        self._grade_pending(
            run_id, manifest, cases, log, use_cache=use_grade_cache, ledger=ledger,
            context=context,
        )
        finished = self.load_run(run_id)
        state_extra = {}
        if manifest.mode == "live":
            meta = recording_writer.seal()
            summary = ledger.summary()
            state_extra = {"recordingId": manifest.recordingId, "recordingSealed": meta["sealed"], "budget": summary.model_dump()}
            log.append(
                "recording.sealed",
                run_id,
                recordingId=manifest.recordingId,
                interactionCount=meta.get("interactionCount", 0),
            )
            log.append(
                "budget.summary",
                run_id,
                settled=summary.settled,
                outstandingReserved=summary.outstandingReserved,
                unknownCount=summary.unknownCount,
            )
        if manifest.mode == "replay":
            replay_extra = self._replay_verification(replay_transport, finished)
            state_extra = {"replay": replay_extra}
            if replay_extra["verification"] == "failed":
                reasons = []
                if replay_extra["unconsumedInPlan"]:
                    reasons.append(f"未消费交互 {len(replay_extra['unconsumedInPlan'])} 条")
                if replay_extra["newExternalCalls"]:
                    reasons.append(f"观测到 {len(replay_extra['newExternalCalls'])} 个录制外出口")
                detail = ";".join(reasons)
                self._write_state(
                    run_id, status="failed", finishedAt=now(),
                    error=f"回放完整性校验失败:{detail}", **state_extra,
                )
                log.append("run.failed", run_id, reason="replay_integrity", detail=detail)
                raise ReplayIntegrityFailed(detail)
        self._write_state(run_id, status="completed", finishedAt=now(), **state_extra)
        log.append(
            "run.finished",
            run_id,
            trials=len(finished.trials),
            completed=sum(1 for t in finished.trials if t.status == "completed"),
        )
        return self.load_run(run_id)

    # ---------- 取消 ----------

    def _cancel_remaining(self, run_id: str, state: RunState, log: EventLog) -> None:
        """取消后:停止派发;未开始的 trial 标记 cancelled,不冒充完成或失败。"""
        for trial in self.load_run(run_id).trials:
            if trial.status == "pending":
                trial.status = "cancelled"
                trial.finishedAt = now()
                trial.error = "run 已取消,未派发"
                self._write_trial(trial)
                log.append(
                    "trial.finished", run_id, trialId=trial.trialId, status="cancelled"
                )

    def request_cancel(self, run_id: str) -> dict:
        """持久化取消请求;执行中的 run 进入 stopping,由执行线程实际停止。"""
        state = self.read_state(run_id)
        status = state.get("status")
        if status in {"completed", "failed", "cancelled", "budget_exhausted"}:
            return {"runId": run_id, "status": status, "cancelRequested": False}
        self._write_state(run_id, status="stopping", cancelRequested=True)
        return {"runId": run_id, "status": "stopping", "cancelRequested": True}

    # ---------- 回放完整性 ----------

    def _replay_verification(self, transport: ReplayTransport, state: RunState) -> dict:
        """计划内交互必须全部消费;新增出口按实际观测计数,不硬编码 0。

        in-plan 以源 run 的 trialId 计(录制时的身份),plan 之外的交互单独列出。
        """
        manifest = state.manifest
        source_trial_ids = {
            trial_identity(
                manifest.parentRunId or manifest.runId,
                line.caseId,
                line.caseVersion,
                line.repetitionIndex,
            )
            for line in manifest.plan
        }
        unconsumed = transport.unconsumed()
        in_plan = [
            item.index
            for item in unconsumed
            if item.trialId is None or item.trialId in source_trial_ids
        ]
        out_of_plan = [
            item.index
            for item in unconsumed
            if item.trialId is not None and item.trialId not in source_trial_ids
        ]
        return {
            "matchProtocolVersion": transport.protocol_version,
            "matched": len(transport.matched),
            "unconsumedInPlan": in_plan,
            "unconsumedOutOfPlan": out_of_plan,
            "newExternalCalls": len(transport.observed_new_calls),
            "newExternalCallDetail": [
                {"method": call[0], "url": call[1]} for call in transport.observed_new_calls[:20]
            ],
            "verification": "passed" if not in_plan and not transport.observed_new_calls else "failed",
        }

    @staticmethod
    def _sandbox_runtime_config(manifest: RunManifest) -> Config:
        """沙盒被测实例配置:live 从环境读密钥,密钥不进 manifest(§16)。

        replay 不需要任何密钥:出口被录制回放拦截,Provider 只提供协议形状。
        """
        if manifest.mode == "mock":
            return Config(provider="mock")
        base = dict(manifest.sandboxConfig)
        if not base.get("provider"):
            base["provider"] = manifest.provider
        if manifest.mode == "replay":
            # 回放禁网(一切出口被录制回放拦截),Provider 构造期的密钥校验
            # 使用显式占位值;占位 Key 永远不会发往真实网络(REPLAY-06 护栏)。
            base["imageApiKey"] = base.get("imageApiKey") or "replay-placeholder-not-a-credential"
            from ..config import normalize

            return normalize(base)
        base.setdefault("imageApiKey", os.environ.get("SWARMUI_IMAGE_API_KEY", ""))
        if not base.get("imageApiKey"):
            raise ValueError("live 运行需要 SWARMUI_IMAGE_API_KEY(或沙盒配置注入),密钥不从 manifest 读取")
        from ..config import normalize

        return normalize(base)

    # ---------- Trial 执行 ----------

    def _trial_dir(self, run_id: str, trial_id: str) -> Path:
        return self.run_dir(run_id) / "trials" / trial_id

    def _write_trial(self, trial: Trial) -> None:
        path = self._trial_dir(trial.runId, trial.trialId) / "trial.json"
        atomic_write_text(
            path, json.dumps(trial.model_dump(), ensure_ascii=False, indent=2)
        )

    def _write_state(self, run_id: str, status: RunStatus, **fields) -> None:
        path = self.run_dir(run_id) / "state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        state.update(status=status, **fields)
        atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))

    def _reference_data_urls(self, run_id: str, case) -> list[tuple[str, str]]:
        """解析 run 目录内归档的参考素材 → [(mime, data_url)];缺失即显式失败。"""
        refs = []
        refs_dir = self.run_dir(run_id) / "references"
        for artifact_id in case.input.referenceArtifactIds:
            matches = sorted(refs_dir.glob(f"{artifact_id}.*")) if refs_dir.is_dir() else []
            if not matches:
                raise ValueError(f"参考素材未归档:{artifact_id}")
            data = matches[0].read_bytes()
            record = self.objects.record(artifact_id)
            refs.append((record.mime, f"data:{record.mime};base64,{base64.b64encode(data).decode()}"))
        return refs

    def _generation_body(self, run_id: str, case, model: str, seed: int) -> tuple[dict, str | None]:
        """按 taskType 构造被测请求:素材正确传递,绝不静默退化为文生图(§7 适配)。"""
        task = case.taskType
        params = dict(case.input.params)
        params.pop("seed", None)
        params.pop("model", None)
        prompt = case.input.prompt
        body = {**params, "prompt": prompt, "model": model, "seed": seed}
        if task in REFERENCE_TASKS:
            refs = self._reference_data_urls(run_id, case)
            if not refs:
                raise ValueError(f"{task} 用例 {case.caseId} 缺少参考素材,拒绝降级为文生图")
            body["initImage"] = refs[0][1]
            if task == "image_edit":
                if len(refs) < 2:
                    raise ValueError(f"image_edit 用例 {case.caseId} 缺少蒙版素材(第二参考)")
                if refs[1][0] != "image/png":
                    raise ValueError("局部重绘蒙版必须是 PNG")
                body["maskImage"] = refs[1][1]
        if task in VIDEO_KINDS:
            body["kind"] = "video"
        if task == "text_to_image" and case.input.referenceArtifactIds:
            raise ValueError("text_to_image 用例不应携带参考素材")
        return body

    def _execute_trial(
        self,
        client: TestClient,
        trial: Trial,
        case,
        default_model: str,
        log: EventLog,
        poll_interval: float,
        timeout_sec: float,
        cancel_check=None,
        manifest: RunManifest | None = None,
    ) -> None:
        run_id = trial.runId
        manifest = manifest or self.load_run(run_id).manifest
        trial.status = "running"
        trial.startedAt = now()
        self._write_trial(trial)
        log.append("trial.started", run_id, trialId=trial.trialId, caseId=trial.caseId, taskType=case.taskType)
        attempt = Attempt(
            attemptId=f"{trial.trialId}-gen-0",
            trialId=trial.trialId,
            callSite="generation",
            status="not_started",
            startedAt=now(),
        )
        body = self._generation_body(run_id, case, default_model, trial.requestedSeed)
        compression = manifest.compression or {}
        if compression.get("enabled"):
            compressed = compress_prompt(body["prompt"])
            body["prompt"] = compressed["compressed"]
            log.append(
                "compression.applied",
                run_id,
                trialId=trial.trialId,
                compressorVersion=compressed["compressorVersion"],
                originalChars=compressed["originalChars"],
                compressedChars=compressed["compressedChars"],
            )
        log.append(
            "step.started", run_id, trialId=trial.trialId, stepId="provider.submit", prompt=body["prompt"]
        )
        started = time.monotonic()
        response = client.post("/api/generate", json=body)
        if response.status_code != 202:
            detail = str(response.json().get("error", "提交失败"))
            self._fail_trial(trial, attempt, "failed", detail, log)
            return
        job_id = response.json()["jobId"]
        trial.jobId = job_id
        attempt.externalTaskId = None
        while True:
            if cancel_check is not None and cancel_check():
                # 取消:尽力停止沙盒内已派发任务;上游是否已停止未知,预留不释放。
                client.delete(f"/api/jobs/{job_id}")
                trial.status = "cancelled"
                trial.error = "用户取消"
                trial.latencyMs = int((time.monotonic() - started) * 1000)
                trial.finishedAt = now()
                attempt.status = "cancelled"
                attempt.error = trial.error
                self._persist_attempt(trial, attempt)
                self._write_trial(trial)
                log.append("step.failed", run_id, trialId=trial.trialId, stepId="provider.poll", code="CANCELLED")
                log.append("trial.finished", run_id, trialId=trial.trialId, status="cancelled")
                return
            job = client.get(f"/api/jobs/{job_id}").json()["job"]
            if job["status"] in {"completed", "failed"}:
                break
            if time.monotonic() - started > timeout_sec:
                trial.status = "timed_out"
                trial.error = f"任务超时(>{timeout_sec:.0f}s),已停止等待"
                trial.latencyMs = int((time.monotonic() - started) * 1000)
                trial.finishedAt = now()
                attempt.status = "unknown_external"
                attempt.error = trial.error
                self._persist_attempt(trial, attempt)
                self._write_trial(trial)
                log.append("step.failed", run_id, trialId=trial.trialId, stepId="provider.poll", code="TIMEOUT")
                log.append("trial.finished", run_id, trialId=trial.trialId, status="timed_out")
                return
            time.sleep(poll_interval)
        trial.latencyMs = int((time.monotonic() - started) * 1000)
        created = job.get("createdAt")
        started_at = job.get("startedAt") or created
        if created and started_at:
            trial.queueMs = max(
                0, int((parse_iso(started_at) - parse_iso(created)).total_seconds() * 1000)
            )
        if job["status"] != "completed" or not job.get("images"):
            detail = job.get("error") or "任务未完成"
            self._fail_trial(trial, attempt, "failed", detail, log, category=classify(detail))
            return
        # 保存全部产物(多图/视频),不再只取第一张。
        is_video = case.taskType in VIDEO_KINDS
        artifact_ids = []
        for image in job["images"]:
            content = client.get(image["url"]).content
            if not content:
                self._fail_trial(trial, attempt, "failed", "生成产物为空", log)
                return
            ext = image["file"].rsplit(".", 1)[-1]
            artifact = self.objects.put(
                content, ext, kind="video" if is_video else "image", origin_run_id=run_id
            )
            artifact_ids.append(artifact.artifactId)
            log.append(
                "artifact.created",
                run_id,
                trialId=trial.trialId,
                artifactId=artifact.artifactId,
                sha256=artifact.sha256,
                mime=artifact.mime,
            )
        if is_video:
            self._extract_video_evidence(run_id, trial, artifact_ids[0], log)
        attempt.status = "succeeded"
        attempt.cost = None  # mock 生成无外部费用;live 费用由预算账本按调用记账
        self._persist_attempt(trial, attempt)
        trial.status = "completed"
        trial.resolvedSeed = job["images"][0].get("params", {}).get("seed")
        trial.artifactIds = artifact_ids
        trial.finishedAt = now()
        self._write_trial(trial)
        log.append("step.finished", run_id, trialId=trial.trialId, stepId="generation.request")
        log.append(
            "trial.finished",
            run_id,
            trialId=trial.trialId,
            status="completed",
            artifactId=artifact_ids[0],
        )

    def _extract_video_evidence(
        self, run_id: str, trial: Trial, video_artifact_id: str, log: EventLog
    ) -> None:
        """视频任务可复现抽帧(§7):帧图入库为证据;环境不可得时如实标记缺失,不冒充时序指标。"""
        import tempfile

        try:
            record, data = self.objects.get(video_artifact_id)
            suffix = record.mime.rsplit("/", 1)[-1] or "bin"
            with tempfile.NamedTemporaryFile(prefix="eval-frame-", suffix=f".{suffix}") as temp:
                temp.write(data)
                temp.flush()
                extraction = extract_video_frames(Path(temp.name))
        except Exception as exc:  # 抽帧失败不拖垮已成功的生成:如实记录缺失原因
            log.append(
                "video.frames.unavailable",
                run_id,
                trialId=trial.trialId,
                note=f"抽帧异常:{type(exc).__name__}: {exc}"[:200],
            )
            return
        if not extraction.get("available"):
            log.append(
                "video.frames.unavailable",
                run_id,
                trialId=trial.trialId,
                version=extraction.get("version"),
                note=extraction.get("note") or "抽帧不可得,时序质量指标缺失",
            )
            return
        frame_ids = []
        for frame in extraction.get("frames", []):
            frame_artifact = self.objects.put(
                frame["png"], "png", kind="image", origin_run_id=run_id
            )
            frame_ids.append(frame_artifact.artifactId)
            log.append(
                "artifact.created",
                run_id,
                trialId=trial.trialId,
                artifactId=frame_artifact.artifactId,
                sha256=frame_artifact.sha256,
                mime=frame_artifact.mime,
                frameTimeMs=int(frame["timeSec"] * 1000),
            )
        trial.frameArtifactIds = frame_ids
        log.append(
            "video.frames.extracted",
            run_id,
            trialId=trial.trialId,
            version=extraction.get("version"),
            durationSec=extraction.get("durationSec"),
            frameArtifactIds=frame_ids,
        )

    def _fail_trial(self, trial: Trial, attempt: Attempt, status: TrialStatus, detail: str, log: EventLog, category=None) -> None:
        trial.status = status
        trial.error = detail
        trial.errorCategory = category or classify(detail)
        trial.finishedAt = now()
        attempt.status = "failed"
        attempt.error = detail
        self._persist_attempt(trial, attempt)
        self._write_trial(trial)
        log.append(
            "step.failed",
            trial.runId,
            trialId=trial.trialId,
            stepId="generation.request",
            code=trial.errorCategory,
            message=detail[:500],
        )
        log.append("trial.finished", trial.runId, trialId=trial.trialId, status=status)

    def _persist_attempt(self, trial: Trial, attempt: Attempt) -> None:
        attempt.finishedAt = now()
        directory = self._trial_dir(trial.runId, trial.trialId)
        path = directory / "attempts.json"
        attempts = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"attempts": []}
        attempts["attempts"] = [
            item for item in attempts["attempts"] if item["attemptId"] != attempt.attemptId
        ] + [attempt.model_dump()]
        trial.attemptIds = [item["attemptId"] for item in attempts["attempts"]]
        atomic_write_text(path, json.dumps(attempts, ensure_ascii=False, indent=2))

    # ---------- 评分 ----------

    def _grade_pending(
        self,
        run_id: str,
        manifest: RunManifest,
        cases: dict,
        log: EventLog,
        use_cache: bool | None = None,
        ledger: BudgetLedger | None = None,
        context: CallContext | None = None,
    ) -> None:
        grading = manifest.grading
        judge_name = grading.get("judge", "stub")
        if use_cache is None:
            use_cache = not grading.get("cacheBypass", False)
        context = context or CallContext()
        context.run_id = run_id
        try:
            judge = make_judge(judge_name, grading=grading, ledger=ledger, context=context)
        except (ValueError, PriceUnknown) as exc:
            self._fail_all_pending_grading(run_id, cases, log, f"裁判不可用:{exc}")
            return
        cache = GradeCache(self.store) if use_cache else None
        for trial in self.load_run(run_id).trials:
            if trial.status != "completed" or trial.qualityVerdict is not None:
                continue
            context.trial_id = trial.trialId  # 裁判费用按 trial 归账
            case = cases[trial.caseId]
            artifact_id = trial.artifactIds[0]
            record, data = self.objects.get(artifact_id)
            judge_error = None
            usage = None
            answers_raw = None
            cache_hit = False
            cache_entry = None
            # 评估器按任务类型分派(§10.6):vlm 只能读取图片;视频任务改用帧级证据,
            # 无帧证据时按"能力不支持"明确失败,绝不把视频产物伪装成图片送裁判(§7)。
            grading_artifact_id = artifact_id
            grading_note = None
            if judge_name != "stub" and case.taskType in VIDEO_KINDS:
                if not trial.frameArtifactIds:
                    self._persist_grading(
                        trial, case, {},
                        "[CAPABILITY_UNSUPPORTED] vlm 裁判无法读取视频产物;需要帧级证据(§7),"
                        "本次未抽帧,视频检查项记为裁判失败而非通过",
                        None, judge_name, artifact_id, False, log,
                    )
                    continue
                frame_record, frame_data = self.objects.get(trial.frameArtifactIds[0])
                data, record = frame_data, frame_record
                grading_artifact_id = frame_record.artifactId
                grading_note = "视频帧级评估:使用首帧证据;时序质量指标未实现(§7)"
            key = grade_cache_key(
                record.sha256,
                [check.model_dump() for check in case.checks],
                grading.get("rubricVersion", "1"),
                judge_name,
                grading.get("model") if judge_name != "stub" else "synthetic",
                preprocessing_version=str(grading.get("preprocessingVersion", "1")),
                locale="zh-CN",
                judge_params={
                    k: grading.get(k)
                    for k in ("temperature", "maxAttempts", "maxTokens", "promptVersion")
                },
            )
            if cache is not None:
                cache_entry, cache_hit = cache.singleflight(
                    key,
                    lambda: self._compute_judging(judge, judge_name, data, record, case, ledger, context),
                )
                if not cache_hit:
                    # 预算拒绝/异常发生在 compute 内:不写缓存条目,记为裁判失败
                    if cache_entry.get("budgetRejected"):
                        cache_entry = {"answers": {}, "judgeError": cache_entry["judgeError"]}
            else:
                cache_entry = self._compute_judging(judge, judge_name, data, record, case, ledger, context)
                if cache_entry.get("budgetRejected"):
                    cache_entry = {"answers": {}, "judgeError": cache_entry["judgeError"]}
            answers_raw = cache_entry.get("answers") or {}
            judge_error = cache_entry.get("judgeError")
            if cache_hit:
                usage = None  # 缓存命中:本次无新调用,不重复记账(§13.4)
            else:
                usage = cache_entry.get("usage")
                usage = UsageRecord(**usage) if isinstance(usage, dict) else usage
            answers = {
                cid: Answer(
                    checkId=cid,
                    observed=item["observed"],
                    evidence=item.get("evidence"),
                    source=item.get("source", "rules"),
                )
                for cid, item in answers_raw.items()
            }
            self._persist_grading(
                trial, case, answers, judge_error, usage, judge_name, grading_artifact_id,
                cache_hit, log, cache_entry=cache_entry, grading_note=grading_note,
            )

    @staticmethod
    def _compute_judging(judge, judge_name, data, record, case, ledger, context) -> dict:
        """真实调用裁判并组织缓存条目;usage/费用口径一并保留(修复首次计算丢 usage)。"""
        if judge_name == "stub":
            result = judge.grade_artifact(data, record.mime, case.checks)
            return {
                "answers": {
                    cid: {"observed": answer.observed, "evidence": answer.evidence, "source": answer.source}
                    for cid, answer in result.answers.items()
                },
                "judgeError": result.error,
                "usage": None,
                "costBasis": None,
            }
        assert ledger is not None, "vlm 裁判必须挂载账本"
        context.attempt_index = context.next_judge_attempt()
        try:
            result = judge.grade_artifact(data, record.mime, case.checks)
        except (PriceUnknown, BudgetExhausted) as exc:
            return {"answers": {}, "judgeError": _budget_rejection(exc), "budgetRejected": True}
        except Exception as exc:  # 裁判适配器异常按裁判失败处理,不冒充零分
            return {"answers": {}, "judgeError": f"裁判异常:{type(exc).__name__}: {exc}"[:300]}
        usage = result.usage
        charges = [c for c in context.charges if c.get("trialId") == context.trial_id and c.get("callSite") == "judge"]
        basis = "provider_reported" if usage and usage.source == "provider_reported" else "unknown"
        return {
            "answers": {
                cid: {"observed": answer.observed, "evidence": answer.evidence, "source": answer.source}
                for cid, answer in result.answers.items()
            },
            "judgeError": result.error,
            "usage": usage.model_dump() if usage else None,
            "costBasis": basis,
            "charges": charges,
            "attempts": result.attempts,
        }

    def _fail_all_pending_grading(self, run_id: str, cases: dict, log: EventLog, error: str) -> None:
        for trial in self.load_run(run_id).trials:
            if trial.status != "completed" or trial.qualityVerdict is not None:
                continue
            self._persist_grading(
                trial, cases[trial.caseId], {}, error, None,
                "unavailable", trial.artifactIds[0], False, log,
            )

    def _persist_grading(
        self,
        trial: Trial,
        case,
        answers: dict,
        judge_error: str | None,
        usage,
        judge_name: str,
        artifact_id: str,
        cache_hit: bool,
        log: EventLog,
        cache_entry: dict | None = None,
        grading_note: str | None = None,
    ) -> None:
        grading = grade_case(trial.trialId, case.checks, answers)
        for grade in grading.grades:
            for evidence in grade.evidence:
                evidence.artifactId = evidence.artifactId or artifact_id
                if grading_note:
                    evidence.note = grading_note if not evidence.note else f"{grading_note};{evidence.note}"
                grade.cacheHit = cache_hit
            if judge_error:
                evidence = grade.evidence[0]
                evidence.note = f"裁判整体失败:{judge_error}"
        charges = (cache_entry or {}).get("charges") if not cache_hit else None
        judge_attempt = Attempt(
            attemptId=f"{trial.trialId}-judge-0",
            trialId=trial.trialId,
            callSite="judge",
            status="failed" if judge_error else "succeeded",
            usage=usage,
            cost=self._judge_cost(charges) if (judge_name != "stub" and not cache_hit) else None,
            error=judge_error,
            startedAt=now(),
            finishedAt=now(),
        )
        self._persist_attempt(trial, judge_attempt)
        trial.qualityVerdict = grading.verdict
        trial.weightedScore = grading.score
        self._write_trial(trial)
        directory = self._trial_dir(trial.runId, trial.trialId)
        grades_payload = {
            "judge": judge_name,
            "judgedAt": now(),
            "judgeError": judge_error,
            "usage": usage.model_dump() if usage else None,
            "cacheHit": cache_hit,
            "verdict": grading.verdict,
            "weightedScore": grading.score,
            "grades": [grade.model_dump() for grade in grading.grades],
        }
        if cache_hit and cache_entry is not None:
            # 保留原始评分来源引用:命中方不重复计费,但可追溯首次计算(§13.4)
            grades_payload["cacheSource"] = {
                "cachedAt": cache_entry.get("cachedAt"),
                "costBasis": cache_entry.get("costBasis"),
                "usage": cache_entry.get("usage"),
            }
        if charges:
            grades_payload["charges"] = charges
        atomic_write_text(
            directory / "grades.json",
            json.dumps(grades_payload, ensure_ascii=False, indent=2),
        )
        for grade in grading.grades:
            log.append(
                "grade.created",
                trial.runId,
                trialId=trial.trialId,
                stepId=f"grade:{grade.checkId}",
                status=grade.status,
                observed=grade.observed,
                verdict=grading.verdict,
                cacheHit=cache_hit,
            )

    @staticmethod
    def _judge_cost(charges: list[dict] | None) -> CostRecord | None:
        """裁判费用来自账本实际结算记录(估算口径),未知保持 None。"""
        if not charges:
            return None
        known = [item for item in charges if item.get("amount") is not None]
        if not known:
            return None
        return CostRecord(
            currency=known[0].get("currency", "CNY"),
            amount=sum(item["amount"] for item in known),
            basis="estimated",
        )

    def list_runs(self) -> list[dict]:
        runs_root = self.store / "runs"
        if not runs_root.is_dir():
            return []
        result = []
        for directory in sorted(runs_root.iterdir()):
            manifest_file = directory / "manifest.json"
            state_file = directory / "state.json"
            if not manifest_file.is_file():
                continue
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
            result.append(
                {
                    "runId": manifest["runId"],
                    "createdAt": manifest["createdAt"],
                    "mode": manifest["mode"],
                    "provider": manifest["provider"],
                    "purpose": manifest.get("purpose", ""),
                    "legacy": manifest.get("legacy", False),
                    "status": state.get("status"),
                    "planCount": len(manifest.get("plan", [])),
                    "planHash": manifest.get("planHash", "")[:12],
                }
            )
        return result


def create_sandbox_app(sandbox: Path, config: Config, transport=None):
    """隔离被测实例(§7.2-3):业务数据写 run 沙箱,不读线上 data。

    transport 是评测的外部调用边界:live 注入预算网关+录制,回放注入严格匹配。
    """
    from ..app import create_app

    return create_app(sandbox, data_dir=sandbox / "data", config=config, transport=transport)


def plan_hash(plan: list[TrialPlanLine]) -> str:
    return canonical_hash([line.model_dump() for line in plan])
