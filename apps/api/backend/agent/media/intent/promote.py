"""明确 Approve 才写新版本；旧版本保留，以版本化文件对回滚。"""
import time
from copy import deepcopy
from .wash import wash


def promote(library, records, ids, approved_by, heldout):
    if not approved_by.strip() or not ids:
        raise ValueError('必须指定审校人和明确批准的候选 ID')
    selected = [record for record in records if record['id'] in ids]
    if len(selected) != len(set(ids)):
        raise ValueError('批准 ID 缺失或重复')
    accepted, rejected = wash(selected, heldout)
    if rejected or any(record.get('wash_status') != 'ready' for record in selected):
        raise ValueError('候选未通过清洗，不能批准')
    result = deepcopy(library)
    for record in accepted:
        if record['id'] in {item['id'] for item in result['items']}:
            raise ValueError('候选已存在')
        result['items'].append({'id': record['id'], 'text': record['utterance'], 'l2': record['l2'],
            'approval': 'approved', 'approved_by': approved_by, 'tags': record.get('tags', []),
            'provenance': record, 'needs_image': record['l2'] in {'image_edit', 'image_to_multiframe', 'image_to_video'}})
    version = str(result['exemplars_version']).split('.')
    if len(version) != 3 or not all(part.isdigit() for part in version):
        raise ValueError('例句版本必须是三段数字')
    version[2] = str(int(version[2]) + 1)
    result['exemplars_version'] = '.'.join(version)
    return result


def approve_seeds(library, ids, approved_by, model_id, heldout):
    """只批准操作者逐条勾选的种子；仍受清洗、硬负例和评测泄漏门约束。"""
    from .retriever import library_digest
    if not ids or not approved_by.strip() or not model_id.strip() or model_id == 'pending':
        raise ValueError('须明确指定种子 ID、审校人和固定模型版本')
    selected = [item for item in library['items'] if item['id'] in ids]
    if len(selected) != len(set(ids)) or any(item.get('approval') == 'approved' for item in selected):
        raise ValueError('种子 ID 缺失、重复或已批准')
    records = [{'id': item['id'], 'utterance': item['text'], 'l2': item.get('l2'),
        'source': 'manual_review', 'request_id': 'seed:' + item['id'],
        'snapshot_ref': {'seed_digest': library_digest(library)}, 'ts': time.time(),
        'user_action': 'Approve:' + approved_by, 'tags': item.get('tags', [])} for item in selected]
    accepted, rejected = wash(records, heldout)
    if rejected or len(accepted) != len(selected):
        raise ValueError('种子未通过清洗、歧义或泄漏检查')
    result = deepcopy(library)
    for item in result['items']:
        if item['id'] in ids:
            item.update(approval='approved', approved_by=approved_by)
    version = str(result['exemplars_version']).split('.')
    version[2] = str(int(version[2]) + 1)
    result['exemplars_version'], result['embed_model_id'] = '.'.join(version), model_id
    return result
