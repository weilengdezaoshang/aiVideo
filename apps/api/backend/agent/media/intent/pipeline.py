"""结构门 → 门闩 → 批量例句 RAG → 槽位 → 澄清；不执行生成。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .decide import decide_from_hits
from .embedder import EmbedError, HTTPEmbedder
from .feasibility import build_feasibility
from .gates import apply_gates, WEAK_MOTION
from .retriever import DEFAULT_LIBRARY, Retriever
from .sequential import detect_sequence
from .slots import fill_slots
from .types import ClarifyOption, ClarifyTicket, IntentDecision, Step
from ..catalog import HostError

logger = logging.getLogger(__name__)


def flag(name):
    return os.environ.get(name, 'false').lower() in {'1', 'true'}


@dataclass(frozen=True)
class IntentSettings:
    enabled: bool = False
    multi_step: bool = False
    direct_i2v: bool = False
    shadow: bool = False
    library: Path = DEFAULT_LIBRARY
    index: Path | None = None
    embed_url: str = 'http://127.0.0.1:8091/v1/embeddings'
    model_id: str = 'pending'
    timeout: float = 0.8
    strong: float = 0.82
    margin: float = 0.12
    minimum: float = 0.55
    ticket_ttl: float = 600

    def __post_init__(self):
        if not (0 <= self.minimum <= self.strong <= 1 and 0 <= self.margin <= 2
                and 0 < self.timeout <= 30 and 0 < self.ticket_ttl <= 3600):
            raise ValueError('意图阈值或超时配置超出允许范围')

    @classmethod
    def from_env(cls):
        return cls(enabled=flag('INTENT_RAG'), multi_step=flag('INTENT_MULTI_STEP'),
            direct_i2v=flag('INTENT_DIRECT_I2V'), shadow=flag('INTENT_RAG_SHADOW'),
            library=Path(os.environ.get('INTENT_EXEMPLARS', str(DEFAULT_LIBRARY))),
            index=Path(os.environ['INTENT_INDEX']) if os.environ.get('INTENT_INDEX') else None,
            embed_url=os.environ.get('INTENT_EMBED_URL', 'http://127.0.0.1:8091/v1/embeddings'),
            model_id=os.environ.get('INTENT_EMBED_MODEL_ID', 'pending'),
            strong=float(os.environ.get('INTENT_T_STRONG', '0.82')),
            margin=float(os.environ.get('INTENT_T_MARGIN', '0.12')),
            minimum=float(os.environ.get('INTENT_T_MIN', '0.55')))


class IntentPipeline:
    def __init__(self, catalog, settings=None, *, retriever=None, embedder=None):
        self.catalog = catalog
        self.settings = settings or IntentSettings.from_env()
        self.retriever, self.embedder = retriever, embedder

    def _trace(self, message, request_id, feasibility):
        return {'request_id': request_id, 'message_hash': hashlib.sha256(message.encode()).hexdigest(),
            'possibleL2': feasibility.possibleL2, 'topK': [], 'certainty': 'none', 'decided_l2': None,
            'path': None, 'clarify_id': None, 'embed_model_id': self.settings.model_id,
            'embed_degraded': False, 'rule_version': 'layered-1.2-sequential-1',
            'exemplars_version': 'unloaded', 'gates_hit': [],
            'executable': {'capabilityOk': feasibility.capabilityOk},
            'intent_multi_step_enabled': self.settings.enabled and self.settings.multi_step,
            'multi_step': False, 'steps': [], 'step_results': [],
            'thresholds': {'strong': self.settings.strong, 'margin': self.settings.margin, 'min': self.settings.minimum}}

    def _finish(self, decision, trace):
        trace.update(certainty=decision.certainty, decided_l2=decision.l2, path=decision.path,
            clarify_id=decision.clarify.id if decision.clarify else None,
            multi_step=bool(decision.steps and len(decision.steps) > 1),
            steps=[{'id': step.id, 'l2': step.l2, 'path': step.path, 'input_from': step.input_from,
                'depends_on': step.depends_on, 'slots_digest': hashlib.sha256(json.dumps(step.slots, sort_keys=True).encode()).hexdigest()}
                for step in decision.steps or []])
        trace['exemplar_id'] = next((hit['id'] for hit in trace.get('topK', []) if hit['l2'] == decision.l2), None)
        # 不记录原话或资产地址；失败前也有结构化决策日志。
        logger.info('host.intent_decision %s', json.dumps(trace, ensure_ascii=False))
        return decision.model_copy(update={'trace': trace})

    def _clarify(self, message, references, request_id, snapshot, trace, *, question=None, options=None):
        if options is None:
            image_l2 = 'image_edit' if references else 'text_to_image'
            video_l2 = 'image_to_multiframe' if references else 'text_to_video'
            options = [ClarifyOption(id=image_l2, label='改图' if references else '生成图片', l2=image_l2),
                ClarifyOption(id=video_l2, label='做成视频', l2=video_l2)]
        ticket = ClarifyTicket(id=str(uuid.uuid4()), request_id=request_id, snapshot=snapshot,
            references=list(references), message=message, expires_at=time.time() + self.settings.ticket_ttl,
            question=question or '希望改成一张图片，还是做成视频？', options=options)
        return self._finish(IntentDecision(certainty='weak', clarify=ticket), trace)

    async def _single(self, message, feasibility, trace):
        l2, gates = apply_gates(message, feasibility, direct_i2v=self.settings.direct_i2v)
        trace['gates_hit'].extend(gates)
        if l2:
            return l2, 'strong'
        # 弱动态也探测侧车，故障始终可观测，但不能因命中而静默出图。
        try:
            if self.retriever is None:
                if self.settings.index is None:
                    raise EmbedError('未配置已批准例句索引')
                self.retriever = Retriever.from_cache(self.settings.library, self.settings.index, self.settings.model_id)
            trace['exemplars_version'] = self.retriever.library['exemplars_version']
            embedder = self.embedder or HTTPEmbedder(self.settings.embed_url, self.settings.model_id, self.settings.timeout)
            query = f"{message}\n[has_image={str(feasibility.has_image).lower()}]"
            vectors = await asyncio.wait_for(embedder.embed([query]), timeout=self.settings.timeout)
            hits = self.retriever.search(vectors[0], feasibility.possibleL2)
            trace['topK'] = hits
            if gates:
                return None, 'weak'
            if hits and set(hits[0].get('tags', [])) & {'multi_step_hint', 'sequential_candidate', 'weak_motion', 'conflict'}:
                return None, 'weak'
            return decide_from_hits(hits, self.settings.strong, self.settings.margin, self.settings.minimum)
        except (EmbedError, OSError, ValueError, KeyError, TypeError, TimeoutError):
            trace['embed_degraded'] = True
            return None, 'none'

    async def run(self, message, references, *, request_id, snapshot, input_from='selection'):
        feasibility = build_feasibility(bool(references), self.catalog, direct_i2v=self.settings.direct_i2v)
        trace = self._trace(message, request_id, feasibility)
        try:
            from .retriever import load_library
            trace['exemplars_version'] = (self.retriever.library if self.retriever else load_library(self.settings.library))['exemplars_version']
        except (OSError, ValueError, KeyError, EmbedError):
            trace['embed_degraded'] = True
        sequence = detect_sequence(message)
        if sequence:
            trace['gates_hit'].append('sequential')
            if len(sequence.parts) != 2 or not all(sequence.parts):
                return self._clarify(message, references, request_id, snapshot, trace,
                    question='一次最多支持两步，请拆成两次发送。')
            first, second = sequence.parts
            first_l2, certainty = await self._single(first, feasibility, trace)
            # 强顺位只允许显式动作，不能把缺图的改图偷换为文生图。
            if first_l2 is None and re.search(r'抠|改成|改为|修脸|去.*背景|cut out|remove.*(?:bg|background)|edit to', first, re.I):
                first_l2, certainty = ('image_edit', 'strong') if references else (None, 'none')
            if first_l2 is None and re.search(r'^(?:画|文生|生成一张|draw\b|generate an? image)', first, re.I):
                first_l2, certainty = 'text_to_image', 'strong'
            next_feas = build_feasibility(True, self.catalog)
            second_trace = self._trace(second, request_id, next_feas)
            second_l2, second_certainty = await self._single(second, next_feas, second_trace)
            trace['embed_degraded'] |= second_trace['embed_degraded']
            trace['gates_hit'] += second_trace['gates_hit']
            trace['step_decisions'] = [{'l2': first_l2, 'certainty': certainty,
                'possibleL2': feasibility.possibleL2, 'topK': trace['topK'], 'capabilityOk': feasibility.capabilityOk},
                {'l2': second_l2, 'certainty': second_certainty, 'possibleL2': next_feas.possibleL2,
                    'topK': second_trace['topK'], 'capabilityOk': next_feas.capabilityOk}]
            # 后段弱动态只形成待确认候选，确认前不得将它锁为可执行视频。
            motion_candidate = second_l2 is None and re.search(WEAK_MOTION + r'|会动|转起来|出片', second, re.I)
            if motion_candidate:
                second_l2, second_certainty = 'image_to_multiframe', 'weak'
            if first_l2 not in {'image_edit', 'text_to_image'} or second_l2 != 'image_to_multiframe':
                return self._clarify(message, references, request_id, snapshot, trace,
                    question='仅支持先生图或改图，再生成视频。请明确两步目标，或拆成两次发送。')
            steps = [Step(id='s1', l2=first_l2, path=first_l2, slots=fill_slots(first),
                needs_image=first_l2 == 'image_edit', input_from=input_from),
                Step(id='s2', l2='image_to_multiframe', path='image_to_multiframe', slots=fill_slots(second),
                    needs_image=True, input_from='step:s1', depends_on=['s1'])]
            options = [ClarifyOption(id='step_first_only', label='先做第一步', l2=first_l2, steps=[steps[0]])]
            if self.settings.enabled and self.settings.multi_step:
                if sequence.strong and certainty == second_certainty == 'strong':
                    return self._finish(IntentDecision(certainty='strong', l2=first_l2, path=first_l2,
                        slots=steps[0].slots, steps=steps), trace)
                options.append(ClarifyOption(id='sequence_all', label='一次做完', steps=steps))
            return self._clarify(message, references, request_id, snapshot, trace, options=options,
                question='先做第一步，还是一次做完？' if len(options) > 1 else '当前未开启多步，请先做第一步，或拆成两次发送。')
        l2, certainty = await self._single(message, feasibility, trace)
        if l2 is None:
            return self._clarify(message, references, request_id, snapshot, trace)
        slots = fill_slots(message)
        step = Step(id='s1', l2=l2, path=l2, slots=slots, needs_image=l2 in {'image_edit', 'image_to_multiframe', 'image_to_video'}, input_from=input_from)
        return self._finish(IntentDecision(certainty=certainty, l2=l2, path=l2, slots=slots, steps=[step]), trace)

    def resume(self, ticket, reply, *, snapshot, option_id=None, trace=None):
        """只匹配已签发的选项，不重新检索；原图变化即失效。"""
        if time.time() >= ticket.expires_at:
            raise HostError('confirmation_expired', '澄清已过期，请重新发送原需求', status=409)
        if ticket.snapshot != snapshot or any(not node.get('assetId') for node in snapshot.get('nodes', {}).values()):
            raise HostError('confirmation_expired', '原参考节点已变化，请重新发送原需求', status=409)
        aliases = {'改图': 'image_edit', '图片': 'text_to_image', '生成图片': 'text_to_image',
            '做成视频': 'image_to_multiframe' if ticket.references else 'text_to_video',
            '视频': 'image_to_multiframe' if ticket.references else 'text_to_video',
            '一次做完': 'sequence_all', '先做第一步': 'step_first_only'}
        ident = option_id or aliases.get(reply.strip(), reply.strip())
        options = [option for option in ticket.options if option.id == ident or option.label == reply.strip()]
        current_trace = {**(trace or {}), 'gates_hit': ['pending_clarify'], 'topK': [],
            'request_id': ticket.request_id, 'clarify_id': ticket.id}
        if len(options) != 1:
            return IntentDecision(certainty='weak', clarify=ticket, trace=current_trace)
        option = options[0]
        steps = option.steps
        if steps and len(steps) > 1 and not (self.settings.enabled and self.settings.multi_step):
            raise HostError('confirmation_expired', '多步开关已关闭，请拆成两次发送', status=409)
        l2 = steps[0].l2 if steps else option.l2
        slots = steps[0].slots if steps else fill_slots(ticket.message)
        return self._finish(IntentDecision(certainty='strong', l2=l2, path=l2, slots=slots,
            steps=steps or [Step(id='s1', l2=l2, path=l2, slots=slots,
                needs_image=l2 in {'image_edit', 'image_to_multiframe', 'image_to_video'},
                input_from='upload' if ticket.references and not ticket.snapshot.get('nodes') else 'selection')]), current_trace)
