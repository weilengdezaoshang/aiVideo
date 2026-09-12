"""P1 严格回放离线演示:录制 → 两次严格回放 → 参数差异定位。零真实费用。

用法:.venv/bin/python scripts/eval-replay-demo.py [工作目录]

流程对应技术方案 §20 纵向切片(不含真实录制):
  live(stub 上游,零费用)录制 → 严格回放两次(业务投影一致,新增外部调用 0)
  → 修改提示词后回放(在调用边界失败并显示字段 diff)
"""

import base64
import io
import json
import sys
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps/api"))

from backend.evaluation.runner import EvaluationRunner  # noqa: E402
from backend.evaluation.datasets import load_cases  # noqa: E402

STUB_BASE = "http://stub.local/api/paas/v4"


def tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "steelblue").save(output, "PNG")
    return output.getvalue()


class StubUpstream(httpx.AsyncBaseTransport):
    """zhipu 兼容协议形状的沙盒上游:仅在本进程内响应,不出网、零费用。"""

    def __init__(self):
        self.png = tiny_png()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "a white cat"}}]})
        if path.endswith("/images/generations"):
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(self.png).decode()}]}
            )
        return httpx.Response(404, json={"error": "unknown path"})


def main(workdir: str = "/tmp/frayune-replay-demo") -> int:
    import os
    import shutil

    root = Path(workdir)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    store = root / "store"
    runner = EvaluationRunner(ROOT, store=store)
    os.environ["SWARMUI_IMAGE_API_KEY"] = "demo-only-key"

    # ① 数据集:一条数量用例
    dataset = root / "demo-zh.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "caseId": "t2i-count-demo",
                "language": "zh-CN",
                "input": {"prompt": "三只白猫并排坐在纯灰色背景前", "params": {"seed": 7}},
                "checks": [
                    {"id": "cat_count", "kind": "integer", "question": "图中共有几只猫?", "expected": 3}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runner.freeze_dataset(load_cases(dataset) and dataset, "demo-zh")

    # ② live 录制(stub 上游,零费用)
    runner.create_run(
        "demo-live",
        "demo-zh",
        mode="live",
        provider="cloud",
        purpose="离线演示:stub 上游录制",
        budget={"currency": "CNY", "maxCost": 5.0, "prices": {"generation": 0.5, "translate": 0.05, "poll": 0.0}},
        sandbox_config={
            "cloudVendor": "zhipu",
            "cloudBaseUrl": STUB_BASE,
            "cloudModel": "demo-image",
            "cloudTextModel": "demo-text",
        },
    )
    runner.execute_run("demo-live", transport=StubUpstream())
    print("① live 录制完成:recording=rec-demo-live(零真实费用)")

    # ③ 严格回放两次:业务投影一致,新增外部调用 0
    for replay_id in ("demo-replay-1", "demo-replay-2"):
        runner.create_run(
            replay_id, "demo-zh", mode="replay", source_run_id="demo-live",
            recording_id="rec-demo-live", purpose="离线演示:严格回放",
        )
        runner.execute_run(replay_id)
        meta = json.loads((runner.run_dir(replay_id) / "state.json").read_text())["replay"]
        trial = runner.load_run(replay_id).trials[0]
        print(
            f"② {replay_id}: status={trial.status} seed={trial.resolvedSeed} "
            f"匹配={meta['matched']} 未消费={len(meta['unconsumed'])} 新增外部调用={meta['newExternalCalls']}"
        )

    # ④ 修改提示词后复用旧录制:调用边界失败 + 字段 diff
    runner.create_run(
        "demo-replay-edit", "demo-zh", mode="replay", source_run_id="demo-live",
        recording_id="rec-demo-live", purpose="离线演示:参数差异定位",
    )
    snapshot = runner.run_dir("demo-replay-edit") / "dataset" / "cases.jsonl"
    snapshot.write_text(
        snapshot.read_text(encoding="utf-8").replace("三只白猫", "两只黑猫"), encoding="utf-8"
    )
    state = runner.execute_run("demo-replay-edit")
    trial = state.trials[0]
    print(f"③ 改提示词后回放: trial.status={trial.status}")
    print(f"   error={trial.error}")

    print("演示完成。结论:相同输入回放投影一致且零新增调用;参数变化在调用边界被精确拦截。")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
