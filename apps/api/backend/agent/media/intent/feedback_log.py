"""候选随 AgentRun 事务持久化；任何来源都不代表已 Approve。"""
import time
import uuid


def candidate(source, message, l2, request_id, snapshot, *, user_action, steps=None):
    if source not in {'clarify_click', 'clarify_text', 'task_success'} or not l2:
        raise ValueError('候选必须来自明确确认或成功任务')
    return {'id': str(uuid.uuid4()), 'source': source, 'utterance': message, 'l2': l2,
        'request_id': request_id, 'snapshot_ref': snapshot, 'ts': time.time(),
        'user_action': user_action, 'approval': 'pending',
        'tags': ['sequential_candidate'] if steps and len(steps) > 1 else [],
        'steps': steps or [], 'eligible_after': time.time() + (60 if source == 'task_success' else 0)}


def reference_snapshot(doc, selected):
    """不绑定当前选区或整文档版本，只锁定原参考节点的内容身份。"""
    objects = doc.data.get('objects', {})
    return {'document_id': str(doc.id), 'nodes': {node: {
        key: objects.get(node, {}).get(key) for key in ('kind', 'assetId', 'assetVersion')}
        for node in selected}}


def success_stable(run, document):
    """成功候选导出时再次核对作品仍在画布，避免立刻撤销的弱标签。"""
    if run.status not in {'awaiting_user_final', 'completed'} or document is None:
        return False
    objects = document.data.get('objects', {})
    nodes = [node for step in (run.execution.get('plan') or {}).get('steps', []) for node in step['targetNodes']]
    return bool(nodes) and all(objects.get(node, {}).get('assetId') and
        objects[node].get('createdByRunId') == str(run.id) for node in nodes)
