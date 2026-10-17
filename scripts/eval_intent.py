"""记录固定安全集的旧路径基线/新路径 shadow；不冒充生产 embedding 标定。"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps/api'))
from backend.agent.media.catalog import Catalog, HostError  # noqa: E402
from backend.agent.media.contracts import CatalogConfig  # noqa: E402
from backend.agent.media.intent import IntentPipeline, IntentSettings  # noqa: E402
from backend.agent.media.planner import plan_intent, plan_from_decision, plan_from_multi  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def safety_catalog(video=True):
    """只构造能力契约；评测不入队，不接触厂商或资产。"""
    caps = []
    for name, kind, refs in [('text_to_image', 'image', 0), ('image_edit', 'image', 1),
                             ('text_to_video', 'video', 0), ('frames_to_video', 'video', 4)]:
        caps.append({'id': name, 'enabled': video or kind != 'video', 'outputType': kind,
            'maxRefImages': refs, 'tiers': {'preview': {'credits': 1}}, 'binding': {'version': 'safety',
                'engine': 'frames' if name == 'frames_to_video' else 'provider',
                'providerSnapshot': {} if name == 'frames_to_video' else {'provider': 'mock',
                    'configVersion': 'safety', 'cloudModel': 'mock', 'videoModel': 'mock'}}})
    return Catalog(CatalogConfig.model_validate({'version': 'safety', 'environment': 'development',
        'enabled': True, 'capabilities': caps}))


async def evaluate():
    rows = json.loads((ROOT / 'evals/intent/gates.json').read_text())
    results = []
    for case in rows:
        catalog = safety_catalog(case.get('video_enabled', True))
        references = ['fixture-image'] if case['image'] else []
        try:
            baseline = plan_intent(case['message'], references, catalog).goalType
        except HostError as exc:
            baseline = exc.code
        pipe = IntentPipeline(catalog, replace(IntentSettings.from_env(), enabled=True,
            multi_step=case.get('multi', False)))
        decision = await pipe.run(case['message'], references, request_id=case['id'],
            snapshot={'document_id': 'fixture', 'nodes': {}})
        actual = 'clarify'
        if not decision.clarify:
            try:
                plan = plan_from_multi(decision, references, catalog) if len(decision.steps or []) > 1 else plan_from_decision(decision, references, catalog)
                actual = 'multi' if len(decision.steps or []) > 1 else plan.goalType
            except HostError as exc:
                actual = exc.code
        results.append({'id': case['id'], 'expected': case['expected'], 'baseline': baseline,
            'shadow': actual, 'passed': actual == case['expected'], 'trace': decision.trace})
    cost_cases = [row for row in results if row['expected'] in {'image', 'video'}]
    def costly(value, expected):
        return value in {'image', 'video'} and value != expected
    return {'scope': 'fixed_safety_gates', 'production_calibrated': False,
        'high_cost_denominator': len(cost_cases),
        'baseline_high_cost_errors': sum(costly(row['baseline'], row['expected']) for row in cost_cases),
        'shadow_high_cost_errors': sum(costly(row['shadow'], row['expected']) for row in cost_cases),
        'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    with Path(args.output).open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    sys.exit(0 if all(row['passed'] for row in report['results']) and
        report['shadow_high_cost_errors'] <= report['baseline_high_cost_errors'] else 1)
