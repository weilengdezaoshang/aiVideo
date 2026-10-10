import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import type {
  EventData,
  ExecutionState,
  InterruptSnapshot,
  Part,
  ToolCall,
  ToolResult,
  Turn,
} from '../domain/model.js'
import type { Ports, Tool } from '../ports/index.js'
export type Publish = (event: EventData) => Promise<void>
const argsSchema = z.object({ prompt: z.string(), approved: z.boolean().optional() })
const resultSchema = z.union([
  z.object({
    artifact: z.object({
      kind: z.literal('artifact'),
      id: z.string(),
      type: z.enum(['image', 'video']),
      url: z.string(),
      status: z.enum(['success', 'failed']),
    }),
    credits: z.number().nonnegative(),
    need_user: z.literal(false).optional(),
  }),
  z.object({
    need_user: z.literal(true),
    estimateCredits: z.number().nonnegative(),
    summary: z.string(),
    pendingToolCall: z.object({ id: z.string(), name: z.string(), args: z.unknown() }).optional(),
  }),
])
export function initialExecution(): ExecutionState {
  return {
    phase: 'host',
    activeAgent: 'host',
    calls: [],
    index: 0,
    approvedCallIds: [],
    agents: { host: 0, planner: 0 },
    toolCalls: [],
    interrupts: [],
    credits: 0,
  }
}
export class AgentLoop {
  constructor(
    private ports: Pick<Ports, 'llm' | 'queue' | 'sessions'>,
    private tools: readonly Tool[],
    private streamCharDelayMs = 0,
    private creditThreshold = 50,
  ) {}
  private async text(turn: Turn, text: string, signal: AbortSignal, publish: Publish) {
    for (const char of text) {
      signal.throwIfAborted()
      if (this.streamCharDelayMs > 0) {
        await sleep(this.streamCharDelayMs, undefined, { signal })
      }
      const part = turn.message.parts.find((p) => p.kind === 'text')
      if (part?.kind === 'text') {
        part.text += char
      } else {
        turn.message.parts.push({ kind: 'text', text: char })
      }
      await publish({ kind: 'text_delta', text: char })
    }
  }
  private async handoff(turn: Turn, call: ToolCall, target: 'host' | 'planner', publish: Publish) {
    const state = turn.execution!
    state.agents[state.activeAgent]++
    state.toolCalls.push({
      id: call.id,
      agent: state.activeAgent,
      name: call.name,
      args: call.args,
      resultSummary: target === 'planner' ? '已交给策划' : '已交回创作助手',
      status: 'finished',
      credits: 0,
    })
    turn.message.parts.push({
      kind: 'tool_status',
      callId: call.id,
      toolName: call.name,
      label: target === 'planner' ? '已交给策划' : '已交回创作助手',
      state: 'completed',
    })
    state.activeAgent = target
    const conversation = await this.ports.sessions.getConversation(turn.conversationId)
    if (conversation) {
      conversation.activeAgent = target
      conversation.updatedAt = new Date().toISOString()
      await this.ports.sessions.saveConversation(conversation)
    }
    await publish({ kind: 'message', message: turn.message })
  }
  private async plan(turn: Turn, publish: Publish) {
    const state = turn.execution!
    if (!state.plan) {
      return
    }
    const part: Extract<Part, { kind: 'plan' }> = {
      kind: 'plan',
      explanation: state.plan.explanation,
      steps: state.plan.steps.map((step) => ({
        id: step.id,
        text: step.title,
        status: step.status,
      })),
    }
    const index = turn.message.parts.findIndex((p) => p.kind === 'plan')
    if (index < 0) {
      turn.message.parts.push(part)
    } else {
      turn.message.parts[index] = part
    }
    await publish({
      kind: 'system',
      type: 'todo_plan',
      explanation: part.explanation,
      steps: part.steps,
    })
    await publish({ kind: 'message', message: turn.message })
  }
  private interrupt(
    turn: Turn,
    call: ToolCall,
    estimateCredits: number,
    summary: string,
    reason = 'credits',
  ): InterruptSnapshot {
    const state = turn.execution!
    const confirmId = randomUUID()
    state.interrupts.push({ confirmId, reason, resumed: false })
    let entry = state.toolCalls.find((t) => t.id === call.id)
    if (!entry) {
      entry = {
        id: call.id,
        agent: state.activeAgent,
        name: call.name,
        args: call.args,
        resultSummary: '等待积分确认',
        status: 'wait_user_response',
        credits: 0,
      }
      state.toolCalls.push(entry)
    }
    entry.status = 'wait_user_response'
    turn.message.parts.push({
      kind: 'confirm',
      confirmId,
      title: '确认本次创作积分',
      estimateCredits,
      summary,
      state: 'pending',
    })
    return {
      confirmId,
      reason,
      estimateCredits,
      summary,
      activeAgent: state.activeAgent,
      pendingToolCall: { id: call.id, name: call.name, args: call.args },
      savedAt: new Date().toISOString(),
      continuation: structuredClone(state),
    }
  }
  async run(turn: Turn, signal: AbortSignal, publish: Publish): Promise<InterruptSnapshot | null> {
    const state = (turn.execution ??= initialExecution())
    const act = async (agent: 'host' | 'planner', phase?: string) => {
      state.agents[agent]++
      const result = await this.ports.llm.act({
        agent,
        messages: [
          { role: 'user', content: turn.prompt },
          ...(phase ? [{ role: 'system', content: phase }] : []),
        ],
        tools: [
          ...this.tools.map((t) => ({ name: t.name })),
          { name: 'handoff_to_planner' },
          { name: 'handoff_to_host' },
        ],
      })
      signal.throwIfAborted()
      if (result.text) {
        await this.text(turn, result.text, signal, publish)
      }
      return result.toolCalls ?? []
    }
    while (state.phase !== 'done') {
      signal.throwIfAborted()
      if (state.phase === 'host') {
        const calls = await act('host')
        const handoff = calls.find((c) => c.name === 'handoff_to_planner')
        if (handoff) {
          await this.handoff(turn, handoff, 'planner', publish)
          state.phase = 'planner'
          continue
        }
        state.calls = calls
        state.index = 0
        state.phase = 'generating'
      } else if (state.phase === 'planner') {
        state.calls = await act('planner')
        state.index = 0
        state.plan = {
          explanation: '按镜头逐步生成与交付',
          steps: state.calls.map((call, index) => ({
            id: call.id,
            title: `镜头 ${index + 1}：${argsSchema.parse(call.args).prompt.split('：').at(-1)}`,
            status: 'pending',
          })),
        }
        state.phase = 'generating'
        await this.plan(turn, publish)
      } else if (state.phase === 'summary') {
        if (state.activeAgent === 'planner') {
          const calls = await act('planner', 'finish')
          const handoff = calls.find((c) => c.name === 'handoff_to_host')
          if (!handoff) {
            throw new Error('策划未返回 handoff_to_host')
          }
          await this.handoff(turn, handoff, 'host', publish)
        }
        await act('host', 'summary')
        state.phase = 'done'
      } else {
        const call = state.calls[state.index]
        if (!call) {
          state.phase = state.plan ? 'summary' : 'done'
          continue
        }
        const step = state.plan?.steps[state.index]
        if (step && step.status !== 'in_progress') {
          step.status = 'in_progress'
          await this.plan(turn, publish)
        }
        const tool = this.tools.find((t) => t.name === call.name)
        if (!tool) {
          throw new Error(`未注册工具：${call.name}`)
        }
        const estimate = state.calls
          .slice(state.index)
          .filter((c) => !state.approvedCallIds.includes(c.id))
          .reduce(
            (sum, c) => sum + (this.tools.find((t) => t.name === c.name)?.estimateCredits ?? 40),
            0,
          )
        if (!state.approvedCallIds.includes(call.id) && estimate > this.creditThreshold) {
          return this.interrupt(
            turn,
            call,
            estimate,
            state.plan
              ? `将生成 ${state.calls.length - state.index} 张镜头画面，预计消耗 ${estimate} 积分。`
              : `生成一张图片，预计消耗 ${estimate} 积分。`,
          )
        }
        const args = argsSchema.parse(call.args)
        let entry = state.toolCalls.find((t) => t.id === call.id)
        if (!entry) {
          entry = {
            id: call.id,
            agent: state.activeAgent,
            name: tool.name,
            args,
            resultSummary: '正在生成图片',
            status: 'invoking',
            credits: 0,
          }
          state.toolCalls.push(entry)
        }
        entry.status = 'invoking'
        state.agents[state.activeAgent]++
        const status: Extract<Part, { kind: 'tool_status' }> = {
          kind: 'tool_status',
          callId: call.id,
          toolName: tool.name,
          label: '正在生成图片',
          state: 'loading',
        }
        turn.message.parts.push(status)
        await publish({ kind: 'message', message: turn.message })
        try {
          const job = await this.ports.queue.enqueue({
            name: tool.name,
            payload: args,
            idempotencyKey: call.id,
            signal,
          })
          entry.jobId = job.id
          if (job.status !== 'completed') {
            throw new Error(job.error?.message ?? '生图任务未完成')
          }
          const result: ToolResult = resultSchema.parse(job.result)
          signal.throwIfAborted()
          if (result.need_user) {
            status.state = 'completed'
            status.label = '等待用户确认'
            entry.status = 'wait_user_response'
            entry.resultSummary = result.summary
            const continuation = result.pendingToolCall ?? {
              id: randomUUID(),
              name: call.name,
              args: { ...args, approved: true },
            }
            if (continuation.id === call.id) {
              throw new Error('need_user 续工具调用必须使用新 ID，避免重复使用已完成的询问任务')
            }
            state.calls[state.index] = continuation
            if (step) {
              step.id = continuation.id
            }
            return this.interrupt(
              turn,
              continuation,
              result.estimateCredits,
              result.summary,
              'approval',
            )
          }
          state.credits += result.credits
          entry.credits = result.credits
          entry.status = 'finished'
          entry.resultSummary = '生成完成'
          status.state = 'completed'
          status.label = '生成完成'
          turn.message.parts.push(result.artifact)
          if (step) {
            step.status = 'completed'
          }
          state.index++
          await this.plan(turn, publish)
          await publish({ kind: 'message', message: turn.message })
        } catch (error: unknown) {
          const failedJob = await this.ports.queue.getByIdempotencyKey(call.id)
          if (failedJob) {
            entry.jobId = failedJob.id
          }
          status.state = signal.aborted ? 'cancelled' : 'failed'
          status.label = signal.aborted ? '已停止' : '生成失败'
          entry.status = signal.aborted ? 'cancelled' : 'failed'
          entry.resultSummary = status.label
          if (step) {
            step.status = signal.aborted ? 'cancelled' : 'failed'
            await this.plan(turn, publish).catch(() => {})
          }
          throw error
        }
      }
    }
    return null
  }
}
