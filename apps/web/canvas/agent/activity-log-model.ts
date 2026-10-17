import type { AgentModelCall, AgentRun, AgentToolCall } from './types.js'

export type ToolCategory = 'read' | 'control' | 'search' | 'generate' | 'other'
export const toolCategories: Record<ToolCategory, { label: string; icon: string }> = {
  read: { label: '读取', icon: '≡' },
  control: { label: '画布操作', icon: '◇' },
  search: { label: '搜索', icon: '⌕' },
  generate: { label: '生成', icon: '✦' },
  other: { label: '其他', icon: '⋯' },
}
// Extend these rules when a provider introduces another tool family.
const categoryRules: readonly {
  pattern: RegExp
  category: ToolCategory
  keys: readonly string[]
}[] = [
  { pattern: /search|browse|find/i, category: 'search', keys: ['query', 'url'] },
  {
    pattern: /text_to_image|image_edit|generate|gen_|render|video|image_to|frames_to/i,
    category: 'generate',
    keys: ['title', 'prompt', 'nodeId'],
  },
  { pattern: /read|get|list/i, category: 'read', keys: ['path', 'nodeId', 'nodeIds', 'assetId'] },
  {
    pattern: /create|update|move|delete|set|add/i,
    category: 'control',
    keys: ['nodeId', 'title', 'path'],
  },
]
export const toolStatusLabels: Record<AgentToolCall['status'], string> = {
  pending: '等待中',
  running: '执行中',
  completed: '已返回',
  failed: '失败',
  cancelled: '已取消',
  late: '迟到结果已归档',
  waiting_approval: '等待审批',
}
export function toolCategory(call: AgentToolCall): ToolCategory {
  if (call.kind === 'read' || /read|get|list/i.test(call.name)) {
    return 'read'
  }
  if (call.kind === 'control') {
    return 'control'
  }
  return categoryRules.find((rule) => rule.pattern.test(call.name))?.category ?? 'other'
}
function argumentText(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) {
    return value
  }
  if (typeof value === 'number') {
    return String(value)
  }
  if (Array.isArray(value) && value.length && value.every((item) => typeof item === 'string')) {
    return value.join('、')
  }
  return undefined
}
export function toolSummary(call: AgentToolCall): string {
  const preferred = categoryRules.find((rule) => rule.pattern.test(call.name))?.keys ?? []
  for (const key of [...preferred, 'query', 'nodeId', 'title', 'path', 'prompt', 'assetId']) {
    const text = argumentText(call.arguments[key])
    if (text) {
      return text
    }
  }
  return call.name
}
export function stepText(key?: string | null): string | undefined {
  if (!key) {
    return undefined
  }
  const number = /^(?:step[-_ ]?|步骤\s*)(\d+)$/i.exec(key)?.[1]
  return number ? `步骤 ${number}` : key
}
export type ToolGroup = { id: string; category: ToolCategory; calls: AgentToolCall[] }
export function groupTools(calls: readonly AgentToolCall[]): ToolGroup[] {
  const groups: ToolGroup[] = []
  for (const call of calls) {
    const category = toolCategory(call)
    const previous = groups.at(-1)
    if (previous?.category === category) {
      previous.calls.push(call)
    } else {
      groups.push({ id: `group:${call.id}`, category, calls: [call] })
    }
  }
  return groups
}
/** Running work first; otherwise surface actions/errors before queued or terminal results. */
export function groupStatus(group: ToolGroup): AgentToolCall['status'] {
  for (const status of ['running', 'waiting_approval', 'failed', 'pending', 'late'] as const) {
    if (group.calls.some((call) => call.status === status)) {
      return status
    }
  }
  return group.calls.every((call) => call.status === 'cancelled') ? 'cancelled' : 'completed'
}
export function groupSummary(group: ToolGroup): string {
  const failed = group.calls.filter((call) => call.status === 'failed').length
  const cancelled = group.calls.filter((call) => call.status === 'cancelled').length
  const issues = [failed ? `${failed} 失败` : '', cancelled ? `${cancelled} 已取消` : ''].filter(
    Boolean,
  )
  return `${group.calls.length} 次调用${issues.length ? `（${issues.join('，')}）` : ''}`
}
export function modelTokenText(usage: AgentModelCall['usage']): string | undefined {
  if (!usage) {
    return undefined
  }
  const total = usage.total_tokens ?? usage.totalTokens
  const input = usage.input_tokens ?? usage.prompt_tokens ?? usage.inputTokens
  const output = usage.output_tokens ?? usage.completion_tokens ?? usage.outputTokens
  const count =
    typeof total === 'number'
      ? total
      : typeof input === 'number' && typeof output === 'number'
        ? input + output
        : undefined
  if (count === undefined || !Number.isFinite(count) || count < 0) {
    return undefined
  }
  return `${count >= 1000 ? `${(count / 1000).toFixed(1)}k` : count} tokens`
}

/** Media Host persists executions as DAG children, not AgentToolCall records.
 * Adapt only the public projection for display; never invent model reasoning or
 * write these rows back to the store. Stable child IDs preserve manual folding.
 */
export function activityTools(
  run: Pick<AgentRun, 'id' | 'toolCalls' | 'steps'> & {
    media?: Pick<NonNullable<AgentRun['media']>, 'goalType' | 'children'>
  },
): AgentToolCall[] {
  if (!run.media || run.toolCalls.length) {
    return run.toolCalls
  }
  return run.media.children.map((child) => {
    const step = run.steps?.find((item) => item.id === child.stepId || item.key === child.stepId)
    const peers = run.media!.children.filter((item) => item.stepId === child.stepId)
    const status: AgentToolCall['status'] =
      child.status === 'completed'
        ? 'completed'
        : child.status === 'failed' || child.status === 'blocked'
          ? 'failed'
          : child.status === 'cancelled' || child.status === 'skipped'
            ? 'cancelled'
            : child.status === 'running' || child.status === 'queued'
              ? 'running'
              : 'pending'
    return {
      id: `media:${run.id}:${child.stepId}:${child.index}`,
      name:
        step?.tools[0] ?? (run.media!.goalType === 'video' ? 'video_generate' : 'image_generate'),
      status,
      stepKey: step ? `步骤 ${step.ordinal + 1}` : undefined,
      arguments: {
        title: peers.length > 1 ? `${child.title} ${child.index + 1}/${peers.length}` : child.title,
        ...(step?.intent ? { prompt: step.intent } : {}),
        targetNode: child.targetNode,
      },
      ...(child.assetId || child.errorCode
        ? {
            output: {
              status: child.status,
              assetId: child.assetId,
              targetNode: child.targetNode,
              attempt: child.attempt,
              errorCode: child.errorCode,
            },
          }
        : {}),
      error: child.errorCode,
    }
  })
}
