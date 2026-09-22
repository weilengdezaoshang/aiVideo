"""评估器注册契约(技术方案 §10.6)与能力预检(P4)。

每个评估器声明:输入/结果 schema、适用 taskType、依赖、实现版本、模型与预处理
版本、缓存策略和已知限制。更换视觉模型、rubric 或帧采样 = 更换版本,必须重新
登记;未登记的评估器拒绝进入评分流程。
能力未支持的组合在预检阶段拒绝并给出覆盖缺口(§1.2),不让它静默落入另一个任务类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .models import CaseVersion, TaskType


@dataclass
class EvaluatorDeclaration:
    id: str
    version: str
    rubric_version: str
    applies_to: set[TaskType]
    inputs: dict[str, Any]  # 输入 schema(摘要)
    results: dict[str, Any]  # 结果 schema(摘要)
    dependencies: list[str] = field(default_factory=list)
    model: str | None = None
    preprocessing_version: str = "1"
    max_usage: dict[str, Any] = field(default_factory=dict)
    timeout_sec: float = 60
    cache_policy: str = "typed-rules:无外部调用,不缓存"
    known_limits: list[str] = field(default_factory=list)
    # 校准记录:P4 校准集上的人工一致性结果;未校准时为 None(不冒充可信)。
    calibration: dict[str, Any] | None = None
    callable: Callable | None = None


_REGISTRY: dict[str, EvaluatorDeclaration] = {}


def register(declaration: EvaluatorDeclaration, replace: bool = False) -> None:
    key = f"{declaration.id}@{declaration.version}"
    if key in _REGISTRY and not replace:
        raise ValueError(f"评估器版本已登记:{key}(更换实现必须更换版本,§10.6)")
    _REGISTRY[key] = declaration


def get(evaluator_id: str, version: str) -> EvaluatorDeclaration:
    key = f"{evaluator_id}@{version}"
    if key not in _REGISTRY:
        raise ValueError(f"评估器未登记:{key}")
    return _REGISTRY[key]


def all_evaluators() -> list[EvaluatorDeclaration]:
    return list(_REGISTRY.values())


def register_builtin() -> None:
    """登记 P0/P1 内置评估器(idempotent)。"""
    builtin = [
        EvaluatorDeclaration(
            id="typed-rules",
            version="1",
            rubric_version="1",
            applies_to={"text_to_image", "image_to_image", "image_edit", "text_to_video", "image_to_video"},
            inputs={"observed": "typed(bool/int/float/str)"},
            results={"status": "pass|fail|dependency_failed|inconclusive|error|not_applicable"},
            dependencies=["judge 结构化回答"],
            cache_policy="纯函数,无外部调用,无需缓存",
            known_limits=["只能判定结构化 observed 与 expected 的匹配", "不做任何文本挖掘"],
        ),
        EvaluatorDeclaration(
            id="stub-judge",
            version="1",
            rubric_version="1",
            applies_to={"text_to_image"},
            inputs={"artifact": "bytes+mime(不使用)"},
            results={"answers": "observed=expected 的合成回答"},
            model="synthetic",
            known_limits=["合成回答仅验证管线,不代表模型质量(§1.3)", "禁止进入任何质量排行"],
        ),
        EvaluatorDeclaration(
            id="vlm-judge",
            version="1",
            rubric_version="1",
            applies_to={"text_to_image", "image_to_image", "image_edit"},
            inputs={"artifact": "bytes+mime", "checks": "结构化检查项列表"},
            results={"answers": "checkId→typed observed+evidence"},
            dependencies=["OpenAI 兼容 chat/completions 端点"],
            model=None,  # 模型由运行配置决定;更换模型必须更新声明并重新校准
            max_usage={"max_attempts": 2},
            cache_policy="P4:按 §13.4 缓存键(素材/问题/规则/模型修订)",
            known_limits=["中文视觉裁判未经独立人工校准前,结论按 inconclusive 对待(§11.3)"],
        ),
    ]
    for item in builtin:
        if f"{item.id}@{item.version}" not in _REGISTRY:
            register(item)


# ---------- 能力预检(P4:§1.2/§10.3) ----------


class CapabilityUnsupported(ValueError):
    code = "CAPABILITY_UNSUPPORTED"


@dataclass
class ProviderCapabilities:
    """来自 Provider 能力快照(冻结于 run manifest.sandboxConfig),不是动态猜测。

    supported_tasks 是唯一权威;video/image_edit/image_to_image 布尔字段由
    tasks 派生,仅为兼容既有调用方与 manifest 快照而保留。
    """

    provider: str
    video: bool = False
    image_edit: bool = False
    image_to_image: bool = False
    max_duration_sec: float | None = None
    supported_tasks: frozenset = field(default_factory=frozenset)

    def supports(self, task_type: str) -> bool:
        return task_type in self.supported_tasks


# 平台×厂商能力矩阵(§10.3):每格与 providers/ 实际实现的协议严格对齐,来源:
#   t2i   四厂商 images 端点 + mock 合成 + comfyui 工作流(cloud.py request/mock.py/comfyui.py)
#   i2i   aliyun multimodal-generation 携带 image / siliconflow FLUX.1-Kontext
#   edit  仅 siliconflow FLUX.1-Fill(蒙版局部重绘)
#   t2v   aliyun video-synthesis 异步协议(需 videoModel,5s 档) / comfyui wan 工作流 / mock 合成
#   i2v   各平台均未实现(aliyun 视频请求体只含 prompt,参考图在 generate 分支被显式拒绝)
# 扩展新平台 = Provider 适配器实现 + 本矩阵登记 + 契约测试,三者缺一不可;
# 未知组合保守按仅 t2i 处理,其余任务由预检显式拒绝,绝不静默降级。
_ALL_TASKS = frozenset(
    {"text_to_image", "image_to_image", "image_edit", "text_to_video", "image_to_video"}
)
CAPABILITY_MATRIX: dict[tuple[str, str | None], frozenset] = {
    ("mock", None): _ALL_TASKS,
    ("comfyui", None): frozenset({"text_to_image", "text_to_video"}),
    ("cloud", "aliyun"): frozenset({"text_to_image", "image_to_image", "text_to_video"}),
    ("cloud", "siliconflow"): frozenset({"text_to_image", "image_to_image", "image_edit"}),
    ("cloud", "zhipu"): frozenset({"text_to_image"}),
    ("cloud", "openai"): frozenset({"text_to_image"}),
}


def capabilities_for(config: dict) -> ProviderCapabilities:
    provider = config.get("provider", "mock")
    vendor = config.get("cloudVendor") if provider == "cloud" else None
    tasks = set(CAPABILITY_MATRIX.get((provider, vendor), frozenset({"text_to_image"})))
    max_duration: float | None = None
    if provider == "cloud" and vendor == "aliyun":
        if config.get("videoModel"):
            max_duration = 5  # VIDEO_DURATIONS 约束(§ 视频协议)
        else:
            tasks -= {"text_to_video"}
    if provider == "comfyui" and config.get("videoBackend") in {None, "", "none"}:
        tasks -= {"text_to_video"}
    tasks = frozenset(tasks)
    return ProviderCapabilities(
        provider=provider,
        video="text_to_video" in tasks,
        image_edit="image_edit" in tasks,
        image_to_image="image_to_image" in tasks,
        max_duration_sec=max_duration,
        supported_tasks=tasks,
    )


def check_case_supported(case: CaseVersion, caps: ProviderCapabilities) -> None:
    """能力不支持的组合必须显式拒绝(§10.3):不让它静默落入另一个任务类型。"""
    if case.taskType == "text_to_image":
        return
    if not caps.supports(case.taskType):
        labels = {
            "text_to_video": "视频生成",
            "image_to_video": "图生视频(参考图驱动)",
            "image_to_image": "图像参考生成",
            "image_edit": "蒙版局部重绘",
        }
        reason = labels.get(case.taskType, case.taskType)
        raise CapabilityUnsupported(
            f"[CAPABILITY_UNSUPPORTED] 平台 {caps.provider} 未开通{reason},"
            f"用例 {case.caseId}({case.taskType})被拒绝而非降级执行"
        )
    if case.taskType in {"image_to_image", "image_edit"}:
        if not case.input.referenceArtifactIds:
            raise CapabilityUnsupported(
                f"[CAPABILITY_UNSUPPORTED] {case.taskType} 需要参考素材,用例 {case.caseId} 缺失"
            )


# ---------- 裁判校准报告(§11.3) ----------


def judge_calibration_report(
    samples: list[dict], defect_category: str = "全部"
) -> dict:
    """以“是否存在指定缺陷”为阳性,计算裁判混淆矩阵。

    samples: [{auto_defect: bool|None, human_defect: bool|None, category?: str}]
    - auto: 裁判判定(check 级 fail=缺陷);human: 人工独立标注。
    - 任一侧 None(未定/证据不足)不计入分母,单独计数,不强行充当确定标签。
    """
    tp = fn = fp = tn = 0
    excluded_auto_unknown = 0
    excluded_human_unknown = 0
    for sample in samples:
        if sample.get("category") not in (None, defect_category) and defect_category != "全部":
            continue
        auto, human = sample.get("auto_defect"), sample.get("human_defect")
        if auto is None:
            excluded_auto_unknown += 1
            continue
        if human is None:
            excluded_human_unknown += 1
            continue
        if human and auto:
            tp += 1
        elif human and not auto:
            fn += 1
        elif not human and auto:
            fp += 1
        else:
            tn += 1

    def rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "category": defect_category,
        "confusion": {"TP": tp, "FN": fn, "FP": fp, "TN": tn},
        "defectRecall": rate(tp, tp + fn),  # TP/(TP+FN)
        "falsePositiveRate": rate(fp, fp + tn),  # FP/(FP+TN)
        "excluded": {"autoUnknown": excluded_auto_unknown, "humanUnknown": excluded_human_unknown},
        "note": "分母含各分组数量;自报 confidence 不当置信区间(§11.3);分母 0 显示 N/A",
    }


# ---------- 提示词压缩实验(§13.5) ----------


COMPRESSOR_VERSION = "whitespace-v1"


def compress_prompt(prompt: str) -> dict:
    """确定性轻量压缩:折叠空白与重复标点,不触碰数量/否定/实体/待渲染文字。

    LLMLingua-2 等模型压缩属独立实验,启用前必须在本任务校准(§13.5);
    本实现只做无损级压缩,作为压缩实验管线的第一档。
    """
    import re

    compressed = re.sub(r"\s+", " ", prompt).strip()
    compressed = re.sub(r"([,，。.!?！?])\1+", r"\1", compressed)
    compressed = re.sub(r"([,，。.!?！?；;：:、]) +", r"\1", compressed)
    return {
        "compressorVersion": COMPRESSOR_VERSION,
        "original": prompt,
        "compressed": compressed,
        "originalChars": len(prompt),
        "compressedChars": len(compressed),
        "tokenDeltaEstimate": None,  # token 估算需要 tokenizer,未知保持 None(§13.1)
    }


# ---------- 视频抽帧(§7 多模态适配) ----------

FRAME_EXTRACTION_VERSION = "ffmpeg-uniform-v1"


def extract_video_frames(video_path: Path, count: int = 3, timeout: float = 30) -> dict:
    """可复现抽帧:ffmpeg 按均匀时间点取帧;环境无 ffmpeg 时明确标记缺失。

    返回 {available, frames: [{timeSec, png}], version};未实现时不冒充时序质量,
    依赖帧的检查项由调用方按 available=False 标记 inconclusive/缺失。
    """
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return {
            "available": False,
            "frames": [],
            "version": FRAME_EXTRACTION_VERSION,
            "note": "环境无 ffmpeg,帧级与时序质量指标缺失,不冒充可得(§7)",
        }
    import json as _json

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    duration = 0.0
    metadata: dict = {}
    if probe.returncode == 0:
        try:
            metadata = _json.loads(probe.stdout or "{}")
            duration = float((metadata.get("format") or {}).get("duration") or 0)
        except ValueError:
            duration = 0.0
    stamps = [duration * (i + 1) / (count + 1) for i in range(count)] if duration else []
    frames = []
    with tempfile.TemporaryDirectory() as workdir:
        for index, stamp in enumerate(stamps):
            out = Path(workdir) / f"frame-{index}.png"
            result = subprocess.run(
                [ffmpeg, "-y", "-ss", f"{stamp:.3f}", "-i", str(video_path),
                 "-frames:v", "1", str(out)],
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode == 0 and out.is_file():
                frames.append({"timeSec": round(stamp, 3), "png": out.read_bytes()})
    return {
        "available": bool(frames),
        "frames": frames,
        "durationSec": duration or None,
        "version": FRAME_EXTRACTION_VERSION,
        "metadata": {"format": (metadata.get("format") or {}).get("format_name")},
        "note": None if frames else "抽帧失败:视频损坏或编解码不受支持",
    }
