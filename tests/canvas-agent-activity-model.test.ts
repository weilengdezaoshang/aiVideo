import test from 'node:test'
import assert from 'node:assert/strict'
import {
  activityTools,
  groupTools,
  groupSummary,
  groupStatus,
  modelTokenText,
  stepText,
  toolCategory,
  toolSummary,
  toolStatusLabels,
} from '../apps/web/canvas/agent/activity-log-model.js'
import type { AgentToolCall } from '../apps/web/canvas/agent/types.js'
const call = (
  id: string,
  name = 'read_canvas',
  extra: Partial<AgentToolCall> = {},
): AgentToolCall => ({ id, name, status: 'completed', arguments: {}, ...extra })
test('工具分类优先读取与显式画布控制，并支持搜索生成与未知工具', () => {
  assert.equal(toolCategory(call('a', 'get_canvas', { kind: 'control' })), 'read')
  assert.equal(toolCategory(call('b', 'custom', { kind: 'control' })), 'control')
  assert.equal(toolCategory(call('c', 'web_search')), 'search')
  assert.equal(toolCategory(call('d', 'image_generate')), 'generate')
  assert.equal(toolCategory(call('e', 'custom')), 'other')
})
test('摘要按工具选择参数，忽略对象和空值，保留原始长文本', () => {
  assert.equal(
    toolSummary(call('a', 'web_search', { arguments: { query: '咖啡', title: '标题' } })),
    '咖啡',
  )
  assert.equal(
    toolSummary(call('b', 'get_node', { arguments: { nodeId: 'n1', query: 'q' } })),
    'n1',
  )
  assert.equal(
    toolSummary(
      call('c', 'custom', { arguments: { query: {}, nodeId: '', title: '长'.repeat(300) } }),
    ),
    '长'.repeat(300),
  )
  assert.equal(toolSummary(call('d')), 'read_canvas')
  assert.equal(stepText('step_2'), '步骤 2')
  assert.equal(stepText('策划'), '策划')
})
test('只分组连续同类工具，保持顺序并分别统计失败和取消', () => {
  const calls = [
    call('a'),
    call('b'),
    call('c', 'get_node', { status: 'failed' }),
    call('d', 'get_node', { status: 'cancelled' }),
    call('e', 'image_generate'),
    call('f'),
  ]
  const groups = groupTools(calls)
  assert.deepEqual(
    groups.map((group) => group.calls.map((item) => item.id)),
    [['a', 'b', 'c', 'd'], ['e'], ['f']],
  )
  assert.equal(groupSummary(groups[0]), '4 次调用（1 失败，1 已取消）')
  assert.equal(calls.length, 6)
})
test('token格式与全量状态标签', () => {
  assert.equal(modelTokenText({ total_tokens: 1200 }), '1.2k tokens')
  assert.equal(modelTokenText({ prompt_tokens: 100, completion_tokens: 52 }), '152 tokens')
  assert.equal(modelTokenText({ total_tokens: 'unknown' }), undefined)
  assert.equal(modelTokenText({ total_tokens: -1 }), undefined)
  assert.equal(toolStatusLabels.waiting_approval, '等待审批')
  assert.equal(toolStatusLabels.late, '迟到结果已归档')
  assert.equal(toolStatusLabels.pending, '等待中')
})

test('媒体任务适配覆盖真实状态、稳定ID、分组与已有工具记录去重', () => {
  const mediaRun: Parameters<typeof activityTools>[0] = {
    id: 'media-run',
    toolCalls: [],
    media: {
      goalType: 'image',
      children: [
        {
          stepId: 'image',
          title: '生成画面',
          retryCredits: 10,
          index: 0,
          status: 'running',
          targetNode: 'a',
          attempt: 1,
          assetId: null,
          errorCode: null,
        },
        {
          stepId: 'image',
          title: '生成画面',
          retryCredits: 10,
          index: 1,
          status: 'blocked',
          targetNode: 'b',
          attempt: 1,
          assetId: null,
          errorCode: 'CONTENT_BLOCKED',
        },
        {
          stepId: 'image',
          title: '生成画面',
          retryCredits: 10,
          index: 2,
          status: 'skipped',
          targetNode: 'c',
          attempt: 1,
          assetId: null,
          errorCode: null,
        },
      ],
    },
  }
  const rows = activityTools(mediaRun)
  assert.deepEqual(
    rows.map((item) => item.status),
    ['running', 'failed', 'cancelled'],
  )
  assert.equal(toolCategory(rows[0]), 'generate')
  assert.equal(groupTools(rows)[0].calls.length, 3)
  assert.equal(toolSummary(rows[1]), '生成画面 2/3')
  assert.equal(activityTools(structuredClone(mediaRun))[0].id, rows[0].id)
  const existing = [call('real-tool')]
  assert.equal(activityTools({ ...mediaRun, toolCalls: existing }), existing)
})

test('混合分组状态按运行、审批、失败、等待和迟到排序，不受调用顺序影响', () => {
  const status = (states: AgentToolCall['status'][]) =>
    groupStatus(
      groupTools(
        states.map((state, index) => call(String(index), 'read_canvas', { status: state })),
      )[0],
    )
  assert.equal(status(['failed', 'waiting_approval', 'completed']), 'waiting_approval')
  assert.equal(status(['waiting_approval', 'running', 'failed']), 'running')
  assert.equal(status(['late', 'pending', 'completed']), 'pending')
  assert.equal(status(['completed', 'cancelled', 'late']), 'late')
  assert.equal(status(['cancelled', 'cancelled', 'cancelled']), 'cancelled')
  assert.equal(status(['completed', 'cancelled', 'completed']), 'completed')
})
