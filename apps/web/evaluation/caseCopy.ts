/** 用例文案词典：新增协议字段必须在这里翻译，未知值保留证据。 */
export const labels: Record<string, string> = {
  L0: 'L0 · 协议与事件',
  L1: 'L1 · 任务路由',
  L2: 'L2 · 计划推进',
  L3: 'L3 · 确认与取消',
  L4: 'L4 · 跨轮继续',
  L5: 'L5 · 排队与限流',
  L7: 'L7 · 生成后反思',
  agent_flow: 'Agent 流程',
  text_to_image: '文生图',
  t2i: '文生图',
  smoke: '基础冒烟',
  regression: '回归',
  protocol: '协议',
  error: '错误处理',
  routing: '任务路由',
  plan: '计划',
  interrupt: '确认与中断',
  continue: '继续执行',
  queue: '队列',
  reflection: '生成后反思',
  accepted: '审核通过',
  retried: '自动重试',
  escalated: '交回用户处理',
  not_run: '未运行审核',
  reviewOutcome: '审核结果',
  route: '任务路由',
  tools: '工具调用',
  direct: '直接生成',
  handoff_planner: '交给策划',
  handoff_roundtrip: '交给策划后返回执行',
  handoff_to_planner: '转交策划工具',
  handoff_to_host: '返回执行工具',
  creative_agent_mcp_gen_text2image_v3: '文生图工具',
  stream_complete: '正常结束',
  stream_error: '错误结束',
  interrupt_approval: '等待积分确认',
  success: '成功',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
  completed: '已完成',
  timed_out: '已超时',
  timeout: '超时',
  pending: '等待中',
  stub: '固定返回的模拟模型',
  mock: '模拟数据',
  rate_limit: '限流',
  concurrency: '并发上限',
  idempotency: '重复请求只执行一次',
  cancel: '取消任务',
  backpressure: '队列满时拒绝新任务',
  expiry: '过期回收',
  bullmq: '未实现驱动拒绝',
}
export const conditions: Record<string, string> = {
  reviewRun: '运行生成后审核',
  escalationText: '超限说明包含审核问题',
  sseStart: '事件流以开始事件启动',
  sseMonotonic: '事件序号严格递增',
  hasConversationId: '事件携带会话标识',
  hasTurnId: '事件携带轮次标识',
  traceV1: '记录符合第一版追踪协议',
  mustAskUser: '出现积分确认，等待用户批准',
  noHandoff: '不转交策划',
  errorProtocol: '错误响应包含错误说明',
  planUnique: '同一条回复只有一份计划',
  planAdvanced: '计划状态向前推进',
  planCompleted: '计划所有步骤完成',
  planIdsStable: '计划步骤标识保持不变',
  completedKept: '保留已完成步骤',
  duplicateIdempotent: '重复确认不能重跑生成',
  draftCleared: '新需求清除续跑草稿',
  hasPlan: '再次展示计划',
  cursorKept: '保留执行位置和步骤标识',
  notRedone: '不能重做已完成步骤',
  rateLimit429: '超限请求返回 429',
  hasRetryAfter: '告知用户多久后可重试',
  concurrencyRejected: '超过并发上限时拒绝请求',
  singleExecution: '相同幂等键只执行一次',
  backpressure: '队列满时拒绝新任务',
  expired: '回收过期收据并允许重新执行',
  bullmqRefused: '明确拒绝尚未实现的队列驱动',
}
const numbers: Record<string, string> = {
  reviewAttempts: '自动重试次数为',
  maxSteps: '步骤数不超过',
  maxCredits: '积分不超过',
  httpStatus: 'HTTP 状态码为',
  planStepCount: '计划步骤数为',
  successArtifacts: '成功作品数为',
  conflictStatus: '相反确认的 HTTP 状态码为',
  newArtifacts: '新增作品数为',
  newCredits: '新增积分为',
  minArtifacts: '成功作品数至少为',
  minCredits: '积分至少为',
  minCompletedBeforeStop: '停止前已完成步骤至少为',
}
export function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}
export function humanValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '未提供'
  }
  if (typeof value === 'boolean') {
    return value ? '是' : '否'
  }
  if (Array.isArray(value)) {
    return value.map(humanValue).join('、')
  }
  if (typeof value === 'object') {
    return Object.entries(record(value))
      .map(([key, item]) => `${label(key)}：${humanValue(item)}`)
      .join('；')
  }
  if (typeof value === 'number') {
    return String(value)
  }
  return (
    labels[String(value)] ??
    (/[\u3400-\u9fff]/u.test(String(value)) ? String(value) : `未知字段：${String(value)}`)
  )
}
export function label(code: string): string {
  return labels[code] ?? conditions[code] ?? numbers[code] ?? `未知字段：${code}`
}
export function expectationLines(expect: Record<string, unknown>, prefix = ''): string[] {
  const groups: Record<string, string> = {
    afterCancel: '取消后，',
    afterApprove: '批准后，',
    afterContinue: '继续后，',
    queue: '队列中，',
  }
  return Object.entries(expect).flatMap(([key, value]) => {
    if (groups[key]) {
      return expectationLines(record(value), prefix + groups[key])
    }
    if (key === 'mustAskUser' && value === false) {
      return [`${prefix}不得出现积分确认。`]
    }
    if (key === 'noHandoff' && value === false) {
      return [`${prefix}必须转交策划。`]
    }
    if (conditions[key]) {
      return [`${prefix}${value === false ? '不得满足：' : '必须满足：'}${conditions[key]}。`]
    }
    if (numbers[key]) {
      return [`${prefix}${numbers[key]} ${humanValue(value)}。`]
    }
    const names: Record<string, string> = {
      route: '必须',
      terminal: '事件流最终为',
      outcome: '执行结果必须为',
      toolsAny: '必须调用工具',
      cancelStatus: '取消后的任务状态为',
      timeoutStatus: '超时后的任务状态为',
    }
    return [`${prefix}${names[key] ?? label(key)}：${humanValue(value)}。`]
  })
}
export const reasonLabels: Record<string, string> = {
  review_outcome: '审核结果不符合预期',
  review_not_run: '审核运行状态不符合预期',
  unexpected_retry: '自动重试次数不符合预期',
  missing_escalation: '缺少超限说明或审核问题',
  pending: '尚未执行，等待调度',
  running: '正在执行，尚无最终判定',
  execution_failed: '执行失败，未完成评分',
  sse_start: '事件流开始协议不符合预期',
  sse_monotonic: '事件序号未严格递增',
  sse_terminal: '事件流结束方式不符合预期',
  conversation_id: '会话标识不符合预期',
  turn_id: '轮次标识不符合预期',
  trace_v1: '追踪协议校验未通过',
  outcome: '执行结果不符合预期',
  route: '任务未按预期路由',
  must_ask_user: '用户确认行为不符合预期',
  no_handoff: '出现了不应发生的策划转交',
  steps_within_max: '步骤数超出上限',
  credits_within_max: '积分超出上限',
  http_status: 'HTTP 状态码不符合预期',
  error_protocol: '错误响应协议不符合预期',
  plan_unique: '计划不唯一',
  plan_advanced: '计划未向前推进',
  plan_completed: '计划未全部完成',
  plan_step_count: '计划步骤数不符合预期',
  plan_ids_stable: '步骤标识发生变化',
  completed_kept: '已完成步骤回退',
  success_artifacts: '成功作品数量不符合预期',
  cancel_new_artifacts: '取消后新增作品数不符合预期',
  cancel_new_credits: '取消后积分变化不符合预期',
  approve_min_artifacts: '批准后作品不足',
  approve_min_credits: '批准后积分不足',
  duplicate_idempotent: '重复确认重跑了生成',
  confirm_conflict: '相反确认未按预期处理',
  continue_has_plan: '继续后未展示计划',
  continue_cursor_kept: '继续后执行位置丢失',
  continue_completed_kept: '继续后已完成步骤丢失',
  continue_not_redone: '继续后重做了已完成步骤',
  continue_min_completed: '停止前完成步骤不足',
  draft_cleared: '新需求未清除续跑草稿',
  rate_limit_429: '限流响应不符合预期',
  retry_after: '缺少重试等待时间',
  concurrency_cap: '并发上限未生效',
  queue_idempotent: '队列重复执行',
  queue_cancel: '队列取消状态不符合预期',
  queue_timeout: '队列超时状态不符合预期',
  queue_backpressure: '队列满时未拒绝新任务',
  queue_expiry: '过期收据未正确回收',
  bullmq_refused: '未拒绝尚未实现的驱动',
}
export function failureReason(code: string, question?: string): string {
  return (
    reasonLabels[code] ??
    (code.startsWith('tool_')
      ? `工具调用不符合预期：${label(code.slice(5))}`
      : question
        ? `未满足判分问题：${question}`
        : `未知字段：${code}`)
  )
}

export const checkLabels: Record<string, string> = {
  sse_start: '开始事件',
  sse_monotonic: '事件序号',
  sse_terminal: '结束事件',
  conversation_id: '会话标识',
  turn_id: '轮次标识',
  trace_v1: '追踪协议',
  outcome: '执行结果',
  route: '任务路由',
  must_ask_user: '积分确认',
  no_handoff: '策划转交',
  steps_within_max: '步骤上限',
  credits_within_max: '积分上限',
  http_status: 'HTTP 状态码',
  error_protocol: '错误协议',
  plan_unique: '计划唯一性',
  plan_advanced: '计划推进',
  plan_completed: '计划完成',
  plan_step_count: '计划步骤数',
  plan_ids_stable: '步骤标识稳定性',
  completed_kept: '已完成步骤保留',
  success_artifacts: '成功作品数',
  cancel_new_artifacts: '取消后新增作品数',
  cancel_new_credits: '取消后新增积分',
  approve_min_artifacts: '批准后作品下限',
  approve_min_credits: '批准后积分下限',
  duplicate_idempotent: '重复确认幂等性',
  confirm_conflict: '相反确认冲突',
  continue_has_plan: '继续后计划展示',
  continue_cursor_kept: '继续后执行位置',
  continue_completed_kept: '继续后完成状态',
  continue_not_redone: '继续后避免重复生成',
  continue_min_completed: '停止前完成数量',
  draft_cleared: '续跑草稿清除',
  rate_limit_429: '请求限流',
  retry_after: '重试等待时间',
  concurrency_cap: '并发上限',
  queue_idempotent: '队列幂等性',
  queue_cancel: '队列取消',
  queue_timeout: '队列超时',
  queue_backpressure: '队列容量限制',
  queue_expiry: '队列过期回收',
  bullmq_refused: '未实现驱动拒绝',
}
export function checkLabel(code: string): string {
  return (
    checkLabels[code] ??
    (code.startsWith('tool_') ? `工具调用：${label(code.slice(5))}` : label(code))
  )
}
