"""离线候选清洗；可疑、歧义、泄漏样本进入拒绝队列。"""
import json
import re
from pathlib import Path
import time
import unicodedata
from difflib import SequenceMatcher

from .types import Step
from pydantic import ValidationError
from .gates import NEGATIVE_VIDEO


def normalize(text):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text).lower())


def wash(records, heldout):
    accepted, rejected, seen = [], [], []
    builtin = json.loads(Path(__file__).with_name('heldout.json').read_text(encoding='utf-8'))
    blocked = {normalize(text) for text in [*builtin, *heldout]}
    labels = {}
    for record in records:
        labels.setdefault(normalize(record.get('utterance', '')), set()).add(record.get('l2'))
    for record in records:
        text = record.get('utterance', '').strip()
        key = normalize(text)
        reason = None
        if any(not record.get(field) for field in ('id', 'source', 'l2', 'request_id', 'snapshot_ref', 'ts', 'user_action')):
            reason = 'provenance_missing'
        elif record.get('l2') not in {'text_to_image', 'image_edit', 'text_to_video', 'image_to_multiframe', 'image_to_video', 'other', 'unsupported'}:
            reason = 'label_invalid'
        elif record.get('source') not in {'clarify_click', 'clarify_text', 'task_success', 'manual_review'}:
            reason = 'source_invalid'
        elif record.get('eligible_after', 0) > time.time() or record.get('revoked'):
            reason = 'not_stable'
        elif len(key) < 3 or len(text) > 500 or re.fullmatch(r'好的|谢谢|随便|不知道|你决定|ok|thanks', text, re.I):
            reason = 'noise'
        elif re.search(r'https?://|[\w.+-]+@[\w.-]+|\b1[3-9]\d{9}\b|(?:ignore|忽略).*(?:instructions|指令)|系统提示', text, re.I):
            reason = 'privacy_or_adversarial'
        elif key in blocked or any(SequenceMatcher(None, key, item).ratio() >= 0.92 for item in blocked):
            reason = 'evaluation_leak'
        elif len(labels[key]) > 1:
            reason = 'label_conflict'
        elif record['l2'] in {'image_to_multiframe', 'image_to_video', 'text_to_video'} and re.search(NEGATIVE_VIDEO, text, re.I):
            reason = 'hard_negative_conflict'
        elif record['source'] != 'manual_review' and record['l2'] in {'image_edit', 'image_to_multiframe', 'image_to_video'} and not record['snapshot_ref'].get('nodes'):
            # 文生图→视频的后步不是独立的 image_edit 候选。
            reason = 'reference_missing'
        elif record.get('steps') and len(record['steps']) > 1:
            try:
                from .types import IntentDecision
                IntentDecision(certainty='strong', steps=[Step.model_validate(step) for step in record['steps']])
            except (ValidationError, TypeError):
                reason = 'sequence_invalid'
        if reason is None and any(SequenceMatcher(None, key, item).ratio() >= 0.92 for item in seen):
            reason = 'near_duplicate'
        if reason:
            rejected.append({**record, 'rejection': reason})
        else:
            seen.append(key)
            accepted.append({**record, 'utterance': text, 'wash_status': 'ready', 'approval': 'pending'})
    return accepted, rejected
