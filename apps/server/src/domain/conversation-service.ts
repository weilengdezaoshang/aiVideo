import { randomUUID } from 'node:crypto'
import type { AgentLoop } from '../agent/host.js'
import type { Ports } from '../ports/index.js'
import type {
  AgentEvent,
  EventData,
  InterruptSnapshot,
  LoggedEvent,
  Turn,
  TurnRecord,
} from './model.js'
import type { AgentTraceV1 } from '../trace/schema.js'
export function isContinueIntent(prompt: string): boolean {
  const text = prompt.trim()
  return (
    text.length <= 20 &&
    /^(继续|接着做|接着|继续执行|继续生成|继续上次|resume)([。.!！]?)$/i.test(text)
  )
}
export class RateLimitError extends Error {
  constructor(
    message: string,
    readonly retryAfterMs = 1000,
  ) {
    super(message)
  }
}
export class ResumeError extends Error {
  constructor(
    message: string,
    readonly status: 404 | 409,
  ) {
    super(message)
  }
}
export interface ResumeContext {
  approved: boolean
  snapshot?: InterruptSnapshot
  confirmId: string
  replay: boolean
}
function eventKindToPort(kind: EventData['kind']): AgentEvent['event'] {
  return kind === 'text_delta' ? 'delta' : kind
}
function restore(record: TurnRecord): Turn {
  if (!record.message || !record.runId || !record.prompt) {
    throw new ResumeError('轮次缺少恢复数据', 409)
  }
  return {
    conversationId: record.conversationId,
    turnId: record.id,
    runId: record.runId,
    prompt: record.prompt,
    createdAt: record.createdAt,
    status: record.status,
    message: structuredClone(record.message),
    execution: record.execution ? structuredClone(record.execution) : undefined,
  }
}
function terminalParts(turn: Turn) {
  for (const part of turn.message.parts) {
    if (part.kind === 'tool_status' && part.state === 'loading') {
      part.state = turn.status === 'cancelled' ? 'cancelled' : 'failed'
      part.label = turn.status === 'cancelled' ? '已停止' : '生成失败'
    }
    if (part.kind === 'confirm' && part.state === 'pending') {
      part.state = 'expired'
    }
    if (part.kind === 'plan') {
      for (const step of part.steps) {
        if (step.status === 'in_progress') {
          step.status = turn.status === 'cancelled' ? 'cancelled' : 'failed'
        }
      }
    }
  }
  if (turn.execution) {
    for (const entry of turn.execution.toolCalls) {
      if (entry.status === 'invoking' || entry.status === 'wait_user_response') {
        entry.status = turn.status === 'cancelled' ? 'cancelled' : 'failed'
        entry.resultSummary = turn.status === 'cancelled' ? '已取消' : '执行失败'
      }
    }
    for (const step of turn.execution.plan?.steps ?? []) {
      if (step.status === 'in_progress') {
        step.status = turn.status === 'cancelled' ? 'cancelled' : 'failed'
      }
    }
  }
}
export class ConversationService {
  constructor(
    private ports: Ports,
    private host: AgentLoop,
  ) {}
  private async allow(key: string) {
    const result = await this.ports.limiter.allow(key)
    if (!result.ok) {
      throw new RateLimitError('请求过于频繁', result.retryAfterMs)
    }
  }
  private async admit(key: string) {
    const result = await this.ports.limiter.acquire?.(key)
    if (result && !result.ok) {
      throw new RateLimitError('同时执行的任务过多', result.retryAfterMs)
    }
  }
  async prepare(prompt: string, conversationId?: string) {
    const key = conversationId ?? 'anonymous'
    await this.admit(key)
    try {
      const turn = await this.prepareNew(prompt, conversationId)
      turn.admissionKey = key
      return turn
    } catch (error) {
      this.ports.limiter.release?.(key)
      throw error
    }
  }
  private async prepareNew(prompt: string, conversationId?: string) {
    await this.allow(conversationId ?? 'anonymous')
    const draft = conversationId
      ? await this.ports.sessions.getContinuationDraft(conversationId)
      : null
    if (!draft || !isContinueIntent(prompt)) {
      if (draft && conversationId) {
        await this.ports.sessions.clearContinuationDraft(conversationId)
      }
      return this.ports.sessions.beginTurn(prompt, conversationId)
    }
    const turn = await this.ports.sessions.beginTurn(draft.originalPrompt, conversationId)
    turn.execution = structuredClone(draft.execution)
    // 被取消/失败的尝试不能复用队列终态收据；保留计划步骤 ID，仅更换执行 ID。
    for (let index = turn.execution.index; index < turn.execution.calls.length; index++) {
      const call = turn.execution.calls[index]!
      const job = await this.ports.queue.getByIdempotencyKey(call.id)
      if (job?.status === 'cancelled' || job?.status === 'failed') {
        turn.execution.calls[index] = { ...call, id: randomUUID() }
      }
    }
    // 续用工具 ID 与游标，已完成任务不重复 enqueue；积分审批重新进行。
    turn.execution.approvedCallIds = turn.execution.calls
      .slice(0, turn.execution.index)
      .map((c) => c.id)
    // 新 run 的 Trace 只记录本轮行为，旧轮保留在原 Trace 文件。
    turn.execution.agents = { host: 0, planner: 0 }
    turn.execution.toolCalls = []
    turn.execution.interrupts = []
    turn.execution.credits = 0
    for (const step of turn.execution.plan?.steps ?? []) {
      if (
        step.status === 'cancelled' ||
        step.status === 'failed' ||
        step.status === 'in_progress'
      ) {
        step.status = 'pending'
      }
    }
    turn.message.parts = [{ kind: 'text', text: '接着上次计划继续。\n' }]
    if (turn.execution.plan) {
      turn.message.parts.push({
        kind: 'plan',
        explanation: turn.execution.plan.explanation,
        steps: turn.execution.plan.steps.map((s) => ({
          id: s.id,
          text: s.title,
          status: s.status,
        })),
      })
    }
    await this.ports.sessions.saveRuntimeTurn(turn)
    await this.ports.sessions.clearContinuationDraft(turn.conversationId)
    const conversation = await this.ports.sessions.getConversation(turn.conversationId)
    if (conversation) {
      conversation.activeAgent = turn.execution.activeAgent
      await this.ports.sessions.saveConversation(conversation)
    }
    return turn
  }
  async prepareResume(conversationId: string, confirmId: string, approved: boolean) {
    await this.admit(conversationId)
    try {
      const prepared = await this.resume(conversationId, confirmId, approved)
      prepared.turn.admissionKey = conversationId
      return prepared
    } catch (error) {
      this.ports.limiter.release?.(conversationId)
      throw error
    }
  }
  private async resume(
    conversationId: string,
    confirmId: string,
    approved: boolean,
  ): Promise<{ turn: Turn; resume: ResumeContext }> {
    await this.allow(conversationId)
    for (const record of await this.ports.sessions.listTurns(conversationId)) {
      const snapshot = await this.ports.sessions.getInterruptSnapshot(record.id)
      const receipt = record.resolutions?.find((r) => r.confirmId === confirmId)
      if (snapshot?.confirmId !== confirmId && !receipt) {
        continue
      }
      if (receipt?.state === 'settled' && receipt.approved === approved) {
        return { turn: restore(record), resume: { approved, confirmId, replay: true } }
      }
      if (!snapshot?.continuation || snapshot.confirmId !== confirmId) {
        throw new ResumeError('确认已处理或已失效', 409)
      }
      const claim = await this.ports.sessions.claimInterrupt(record.id, confirmId, approved)
      if (claim !== 'claimed') {
        throw new ResumeError(
          claim === 'running' ? '确认已受理，正在执行' : '确认已处理或已失效',
          409,
        )
      }
      const turn = restore(record)
      // 唯一恢复来源：SessionStore 的可序列化 snapshot。
      turn.execution = structuredClone(snapshot.continuation)
      turn.status = approved ? 'running' : 'cancelled'
      turn.message.status = approved ? 'streaming' : 'cancelled'
      const card = turn.message.parts.find((p) => p.kind === 'confirm' && p.confirmId === confirmId)
      if (card?.kind === 'confirm') {
        card.state = approved ? 'accepted' : 'rejected'
      }
      const interrupt = turn.execution.interrupts.find((i) => i.confirmId === confirmId)
      if (interrupt) {
        interrupt.resumed = approved
      }
      if (approved) {
        turn.execution.approvedCallIds.push(
          ...turn.execution.calls.slice(turn.execution.index).map((c) => c.id),
        )
        for (const call of turn.execution.toolCalls) {
          if (call.status === 'wait_user_response' && call.id !== snapshot.pendingToolCall?.id) {
            call.status = 'finished'
            call.resultSummary = '已确认，继续执行'
          }
        }
      } else {
        await this.ports.sessions.saveContinuationDraft(conversationId, {
          sourceTurnId: turn.turnId,
          originalPrompt: turn.prompt,
          execution: structuredClone(turn.execution),
          savedAt: new Date().toISOString(),
        })
        const text = turn.message.parts.find((p) => p.kind === 'text')
        const cancelText =
          turn.execution.credits === 0
            ? '\n已取消，未消耗积分。'
            : '\n已取消后续生成，已完成作品保留，未新增积分消耗。'
        if (text?.kind === 'text') {
          text.text += cancelText
        } else {
          turn.message.parts.push({ kind: 'text', text: cancelText.trim() })
        }
        terminalParts(turn)
      }
      await this.ports.sessions.activateTurn(turn)
      await this.ports.sessions.clearInterruptSnapshot(record.id)
      return { turn, resume: { approved, snapshot, confirmId, replay: false } }
    }
    throw new ResumeError('确认不存在或已失效', 404)
  }
  async disconnect(conversationId: string, turnId: string) {
    const record = await this.ports.sessions.getTurn(turnId)
    if (record?.status === 'running') {
      await this.stop(conversationId, turnId)
    }
  }
  async stop(conversationId: string, turnId: string) {
    const previous = await this.ports.sessions.getTurn(turnId)
    const turn = await this.ports.sessions.cancel(conversationId, turnId)
    if (
      turn?.execution?.plan &&
      (previous?.status === 'interrupted' || previous?.status === 'running')
    ) {
      await this.ports.sessions.saveContinuationDraft(conversationId, {
        sourceTurnId: turn.turnId,
        originalPrompt: turn.prompt,
        execution: structuredClone(turn.execution),
        savedAt: new Date().toISOString(),
      })
    }
    if (turn && previous?.status === 'interrupted') {
      terminalParts(turn)
      await this.ports.sessions.clearInterruptSnapshot(turnId)
      await this.ports.sessions.saveRuntimeTurn(turn)
      await this.ports.events.append({
        conversationId,
        turnId,
        event: 'message',
        data: { kind: 'message', message: turn.message },
      })
      await this.writeTrace(turn)
    }
    return turn
  }
  private async writeTrace(turn: Turn) {
    const state = turn.execution
    const events = await this.ports.events.readFrom(turn.conversationId, 0, { turnId: turn.turnId })
    const finishedAt = new Date().toISOString()
    const steps = state ? state.agents.host + state.agents.planner : 0
    const trace: AgentTraceV1 = {
      version: '1',
      runId: turn.runId,
      conversationId: turn.conversationId,
      startedAt: turn.createdAt,
      finishedAt,
      turns: [
        {
          turnId: turn.turnId,
          agents: (['host', 'planner'] as const)
            .filter((name) => (state?.agents[name] ?? 0) > 0)
            .map((name) => ({ name, steps: state!.agents[name] })),
          events: events.map((e) => ({
            seq: e.seq,
            at: e.at,
            type:
              e.data && typeof e.data === 'object' && 'type' in e.data
                ? String(e.data.type)
                : e.event === 'delta'
                  ? 'text_delta'
                  : e.event,
          })),
          toolCalls: state?.toolCalls ?? [],
          interrupts: state?.interrupts ?? [],
          outcome:
            turn.status === 'completed'
              ? 'success'
              : turn.status === 'interrupted'
                ? 'interrupted'
                : turn.status === 'cancelled'
                  ? 'cancelled'
                  : 'failed',
          metrics: {
            steps,
            credits: state?.credits ?? 0,
            latencyMs: Math.max(0, Date.parse(finishedAt) - Date.parse(turn.createdAt)),
          },
        },
      ],
    }
    await this.ports.traces.write(trace)
  }
  async run(turn: Turn, sink: (event: LoggedEvent) => Promise<void>, resume?: ResumeContext) {
    try {
      await this.executeRun(turn, sink, resume)
    } finally {
      if (turn.admissionKey) {
        this.ports.limiter.release?.(turn.admissionKey)
      }
    }
  }
  private async executeRun(
    turn: Turn,
    sink: (event: LoggedEvent) => Promise<void>,
    resume?: ResumeContext,
  ) {
    const signal = resume?.replay
      ? new AbortController().signal
      : this.ports.sessions.signal(turn.turnId)
    let writable = true
    const publish = async (data: EventData, terminal = false) => {
      if (!terminal) {
        signal.throwIfAborted()
      }
      if (!resume?.replay) {
        await this.ports.sessions.saveRuntimeTurn(turn)
      }
      const event = await this.ports.events.append({
        conversationId: turn.conversationId,
        turnId: turn.turnId,
        event: eventKindToPort(data.kind),
        data,
      })
      if (writable) {
        try {
          await sink({
            ...data,
            conversation_id: turn.conversationId,
            turn_uuid: turn.turnId,
            runId: turn.runId,
            sequence: event.seq,
            timestamp: event.at,
          })
        } catch (error: unknown) {
          writable = false
          throw error
        }
      }
    }
    let snapshot: InterruptSnapshot | null = null
    let failure: string | undefined
    try {
      await publish({ kind: 'system', type: 'stream_start' }, true)
      if (!resume?.replay && !(resume && !resume.approved)) {
        if (resume || turn.execution) {
          await publish({ kind: 'message', message: turn.message })
        }
        snapshot = await this.host.run(turn, signal, publish)
        signal.throwIfAborted()
        turn.status = snapshot ? 'interrupted' : 'completed'
        turn.message.status = snapshot ? 'streaming' : 'completed'
      }
    } catch (error: unknown) {
      snapshot = null
      turn.status = signal.aborted ? 'cancelled' : 'failed'
      turn.message.status = turn.status
      failure = signal.aborted ? undefined : error instanceof Error ? error.message : '执行失败'
      if (failure) {
        turn.message.parts.push({ kind: 'error', message: failure })
      }
      terminalParts(turn)
    }
    try {
      await publish({ kind: 'message', message: turn.message }, true)
    } catch {
      writable = false
    }
    try {
      if (snapshot) {
        snapshot.cursor = {
          lastEventSeq: (await this.ports.events.getLastSeq(turn.conversationId)) + 1,
          messageId: turn.message.id,
        }
        await this.ports.sessions.saveInterruptSnapshot(turn.turnId, snapshot)
      }
      if (!resume?.replay) {
        await this.writeTrace(turn)
      }
    } catch (error: unknown) {
      failure = `Trace/快照写入失败：${error instanceof Error ? error.message : '未知错误'}`
      snapshot = null
      turn.status = 'failed'
      turn.message.status = 'failed'
      turn.message.parts.push({ kind: 'error', message: failure })
      terminalParts(turn)
      await this.ports.sessions.clearInterruptSnapshot(turn.turnId)
      try {
        await publish({ kind: 'message', message: turn.message }, true)
      } catch {
        writable = false
      }
    }
    // 保存 Trace 期间也可能收到 stop；取消不能被迟到的 interrupted 终态覆盖。
    const current = await this.ports.sessions.getTurn(turn.turnId)
    if (!resume?.replay && current?.status === 'cancelled' && turn.status !== 'cancelled') {
      snapshot = null
      turn.status = 'cancelled'
      turn.message.status = 'cancelled'
      terminalParts(turn)
      await this.ports.sessions.clearInterruptSnapshot(turn.turnId)
      try {
        await publish({ kind: 'message', message: turn.message }, true)
        await this.writeTrace(turn)
      } catch (error: unknown) {
        failure = error instanceof Error ? error.message : '取消状态记录失败'
      }
    }
    if (resume && !resume.replay) {
      await this.ports.sessions.settleInterrupt(turn.turnId, resume.confirmId)
    }
    if (!resume?.replay) {
      this.ports.sessions.release(turn.turnId)
    }
    try {
      const card = turn.message.parts.find((p) => p.kind === 'confirm' && p.state === 'pending')
      if (!failure && turn.status === 'interrupted' && card?.kind === 'confirm') {
        await publish(
          {
            kind: 'system',
            type: 'interrupt_approval',
            confirmId: card.confirmId,
            title: card.title,
            estimateCredits: card.estimateCredits,
            summary: card.summary,
          },
          true,
        )
      } else {
        await publish(
          failure
            ? { kind: 'system', type: 'stream_error', message: failure, status: turn.status }
            : { kind: 'system', type: 'stream_complete', status: turn.status },
          true,
        )
      }
    } catch {
      /* SSE 已断开，记录和 Trace 已完成。 */
    }
  }
}
