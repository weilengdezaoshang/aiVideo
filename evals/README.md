# 生成质量评测体系

对[技术方案 v0.1](./技术方案-图生图文生视频与时间线-v0.1.md) §15 评测实验的落地:固定参数批量生成 → VLM 判分 → 对比报告。当前覆盖**文生图**,文生视频与图生视频桶待真实后端验收后扩充。

## 组成

| 文件 | 职责 |
| --- | --- |
| `golden-set.jsonl` | 中文评测集,每行一条:id、bucket(桶)、prompt、固定 params(seed!)、questions(原子判分问题 + expect 接受答案 + weight) |
| `../scripts/eval.py` | 三个子命令:`run`(批量生成)→ `judge`(判分)→ `report`(摘要/对比) |

## 用法(.venv/bin/python)

```bash
# ① 管线自测(完全离线,mock 生成 + stub 判分)
python3 scripts/eval.py run   --provider mock --run-id demo
python3 scripts/eval.py judge data/evals/demo --judge stub
python3 scripts/eval.py report data/evals/demo                # 单次摘要
python3 scripts/eval.py report data/evals/<新> data/evals/<旧>  # 换模型前后对比

# ② 真实评测(cloud 生成 + Qwen-VL 判分;产生真实费用,仅换厂商/换模型/大版本升级时跑)
python3 scripts/eval.py run   --provider cloud --run-id qwen-a
python3 scripts/eval.py judge data/evals/qwen-a --judge qwen-vl
```

qwen-vl 判分器默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen-vl-max-latest`,可用 `EVAL_JUDGE_BASE_URL / EVAL_JUDGE_API_KEY / EVAL_JUDGE_MODEL` 覆盖;Key 缺省回退 `SWARMUI_IMAGE_API_KEY`。协议形状以实测为准。

## 边界与纪律

1. **mock 运行的分数不代表模型质量**,报告会强制标注;它的用途是验证管线与可复现性(同 seed 产物逐字节一致,有测试锁定)。
2. 评测产生真实调用费用:**只在换厂商、换模型、模型大版本升级时跑**;费用随每次调用进入 traces,不与额度混淆。
3. 扩集规则:全部中文、来自真实使用措辞(不写"基准考题腔");每个桶 ≥15 条才算可对比;改集必须换 run 对比,不允许用旧分数混排。
4. 管线测试(`tests/backend/test_eval_pipeline.py`)已纳入 `npm run verify`,评测代码自身的回归是零成本的。
