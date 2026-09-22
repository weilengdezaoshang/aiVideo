"""评分缓存验收(§21 CACHE-01/02/03;§13.4 缓存键与失效)。"""


from backend.evaluation.gradecache import GradeCache, grade_cache_key
from backend.evaluation.runner import EvaluationRunner

from eval_helpers import make_case, write_jsonl


def test_cache01_singleflight_shares_one_computation(tmp_path):
    cache = GradeCache(tmp_path)
    calls = []

    def compute():
        calls.append(1)
        return {
            "answers": {"has_cat": {"observed": True, "source": "vlm"}},
            "judgeError": None,
            "usage": {"source": "provider_reported", "textTokens": 42},
            "costBasis": "provider_reported",
        }

    from concurrent.futures import ThreadPoolExecutor

    def worker():
        return cache.singleflight("k1", compute)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [f.result() for _ in range(6) for f in [pool.submit(worker)]]

    assert len(calls) == 1  # 只有一次真实计算
    for entry, cache_hit in results:
        assert entry["judgeError"] is None
        assert entry["answers"]["has_cat"]["observed"] is True
        # 首次计算的 usage 保留在条目里,命中方拿到同一份来源引用(§13.4)
        assert entry["usage"]["textTokens"] == 42
    assert sum(1 for _, hit in results if not hit) == 1  # 恰好一次未命中
    assert all(hit for _, hit in results[1:])


def test_cache02_key_changes_invalidate(tmp_path):
    """改 rubric、改裁判模型修订、改检查项、改素材 → 缓存键变化(可查询原因)。"""
    checks = [{"id": "chk-a", "question": "a?"}, {"id": "chk-b", "question": "b?"}]
    base = grade_cache_key("sha-A", checks, "1", "vlm-judge", "model-1")
    assert base == grade_cache_key("sha-A", checks, "1", "vlm-judge", "model-1")  # 稳定
    assert base != grade_cache_key("sha-B", checks, "1", "vlm-judge", "model-1")  # 素材变
    assert base != grade_cache_key("sha-A", checks, "2", "vlm-judge", "model-1")  # rubric 变
    assert base != grade_cache_key("sha-A", checks, "1", "vlm-judge", "model-2")  # 模型修订变
    # 问题顺序必须保留:顺序变化=不同的裁判输入(§8.3:数组/消息顺序不可重排)
    assert base != grade_cache_key("sha-A", list(reversed(checks)), "1", "vlm-judge", "model-1")
    assert base != grade_cache_key("sha-A", [{"id": "chk-a", "question": "b?"}], "1", "vlm-judge", "model-1")


def test_cache03_live_stability_replay_bypasses_cache(tmp_path):
    """稳定性试验:use_grade_cache=False 时不短路,trial 数等于新调用定义(执行层验证)。"""
    runner = EvaluationRunner(tmp_path, store=tmp_path / "store")
    source = write_jsonl(tmp_path / "ds.jsonl", [make_case("t2i-cache-01")])
    runner.freeze_dataset(source, "ds-zh")
    runner.create_run("cache-run-a", "ds-zh")
    runner.execute_run("cache-run-a", use_grade_cache=True)
    grades_file = runner._trial_dir("cache-run-a", runner.load_run("cache-run-a").trials[0].trialId) / "grades.json"
    first = grades_file.read_text(encoding="utf-8")
    # 相同素材第二次判分:命中缓存
    runner.create_run("cache-run-b", "ds-zh")
    runner.execute_run("cache-run-b", use_grade_cache=True)
    second = (runner._trial_dir("cache-run-b", runner.load_run("cache-run-b").trials[0].trialId) / "grades.json").read_text(encoding="utf-8")
    assert '"cacheHit": true' in second
    assert '"cacheHit": false' in first
    # 稳定性试验绕过缓存:不产生 cacheHit 标记
    runner.create_run("cache-run-c", "ds-zh")
    runner.execute_run("cache-run-c", use_grade_cache=False)
    third = (runner._trial_dir("cache-run-c", runner.load_run("cache-run-c").trials[0].trialId) / "grades.json").read_text(encoding="utf-8")
    assert '"cacheHit": false' in third
