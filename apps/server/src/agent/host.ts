import { CapabilityCatalog, CapabilityError, developmentCatalog } from './capabilities.js'
import {
  IntentGate,
  IntentValidator,
  RoutingError,
  SchemaIntentLLM,
  type RoutingOptions,
  type ClarificationReply,
} from './intent.js'
import { executionDag, readySteps, BindingSlots } from './scheduler.js'
import { PlanValidator, videoTemplate, fourDirectionTemplate } from './plan.js'
import {
  evidenceSchema,
  finalizeGeneration,
  StubReviewerProvider,
  type ReflectionOptions,
} from './reflection.js'
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
const argsSchema = z.object({
  prompt: z.string(),
  approved: z.boolean().optional(),
  imageRefs: z.array(z.string()).optional(),
  frameCallIds: z.array(z.string()).optional(),
  frameUrls: z.array(z.string()).optional(),
})
const resultSchema = z.union([
  z.object({
    artifact: z.object({
      kind: z.literal('artifact'),
      id: z.string(),
      type: z.enum(['image', 'video']),
      url: z.string(),
      status: z.enum(['success', 'failed']),
      qualityIssue: z.string().optional(),
    }),
    credits: z.number().nonnegative(),
    evidence: evidenceSchema.optional(),
    metadata: z
      .object({
        width: z.number(),
        height: z.number(),
        bytes: z.number(),
        expectedWidth: z.number().optional(),
        expectedHeight: z.number().optional(),
        expectedRatio: z.number().optional(),
        reviewPass: z.boolean().optional(),
        localPath: z.string().optional(),
        fixtureId: z.string().optional(),
      })
      .optional(),
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
  private catalog: CapabilityCatalog
  private slots = new BindingSlots()
  publicCapabilities() {
    return this.catalog.public()
  }
  constructor(
    private ports: Pick<Ports, 'llm' | 'queue' | 'sessions' | 'reviewer'>,
    private tools: readonly Tool[],
    private streamCharDelayMs = 0,
    private creditThreshold = 50,
    private reflection: ReflectionOptions = {},
    private routing: RoutingOptions = {},
    catalog?: CapabilityCatalog,
  ) {
    this.catalog = catalog ?? developmentCatalog(tools)
  }
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
        title: step.title,
        text: step.title,
        status: step.status,
        dependsOn: state.calls.find((call) => call.id === step.id)?.dag?.dependsOn,
        parallelGroup: state.calls.find((call) => call.id === step.id)?.dag?.parallelGroup,
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
      interrupt_kind: reason === 'clarify' ? 'clarify' : 'credit',
      title: reason === 'clarify' ? '确认创作需求' : '确认本次创作积分',
      estimateCredits,
      summary,
      state: 'pending',
    })
    return {
      confirmId,
      status: 'awaiting_user',
      interrupt_kind: reason === 'clarify' ? 'clarify' : 'credit',
      reason,
      estimateCredits,
      summary,
      activeAgent: state.activeAgent,
      pendingToolCall: { id: call.id, name: call.name, args: call.args },
      savedAt: new Date().toISOString(),
      continuation: structuredClone(state),
    }
  }
  async resolveClarification(
    state: ExecutionState,
    reply?: ClarificationReply,
  ): Promise<ExecutionState> {
    const next = structuredClone(state)
    const intent = next.intent
    const input = next.intentInput
    if (!intent || !input) {
      throw new RoutingError('intent_invalid', '澄清快照缺少意图')
    }
    if (reply?.imageRefs) {
      input.imageRefs = reply.imageRefs
      input.canvas = undefined
    } else if (
      intent.clarifySlots?.includes('reference_image') &&
      intent.referenceAssetIds.length >= 1
    ) {
      input.imageRefs = intent.referenceAssetIds.slice(0, 1)
      input.canvas = undefined
    }
    if (reply?.goal) {
      input.requestedGoal = reply.goal
      next.intent = await new IntentGate(this.routing).resolve(input)
    } else {
      if (!intent.proposedPath) {
        throw new RoutingError('intent_invalid', '请明确图片或视频目标')
      }
      intent.goal = intent.proposedPath === 'text_to_image' ? 'image' : 'video'
      intent.path = intent.proposedPath
      intent.needsClarification = false
      intent.confidence = 1
      intent.clarifySlots = undefined
      intent.referenceAssetIds = input.imageRefs ?? input.canvas?.assetUrls ?? []
      intent.hasImage = intent.referenceAssetIds.length > 0 || input.canvas?.hasImage === true
      input.prompt = input.prompt.replace(/局部|区域修改|部分修改/g, '完整创作')
      next.intent = new IntentValidator().validate(intent, input, this.routing)
    }
    if (next.intent.needsClarification || next.intent.path === 'clarify') {
      throw new RoutingError('intent_invalid', next.intent.clarifyQuestion ?? '请补充澄清信息')
    }
    next.clarificationAccepted = true
    return next
  }
  async run(turn: Turn, signal: AbortSignal, publish: Publish): Promise<InterruptSnapshot | null> {
    const state = (turn.execution ??= initialExecution())
    state.intentInput ??= { prompt: turn.prompt, imageRefs: turn.imageRefs, ...turn.intentContext }
    state.imageRefs ??= state.intentInput.imageRefs ?? state.intentInput.canvas?.assetUrls ?? []
    state.intent ??= await new IntentGate({
      ...this.routing,
      intentLlm: this.routing.intentLlm ?? new SchemaIntentLLM(this.ports.llm),
    }).resolve(state.intentInput)
    state.intent = new IntentValidator().validate(state.intent, state.intentInput, this.routing)
    signal.throwIfAborted()
    if (state.intent.needsClarification && !state.clarificationAccepted) {
      return this.interrupt(
        turn,
        { id: randomUUID(), name: 'intent_clarify', args: { prompt: turn.prompt } },
        0,
        `${state.intent.clarification} 确认后按 ${state.intent.proposedPath ?? state.intent.path} 执行。`,
        'clarify',
      )
    }
    if (state.intent.path === 'clarify') {
      throw new RoutingError('intent_invalid', '澄清后没有可执行的路径')
    }
    state.imageRefs = state.intent.referenceAssetIds
    const validator = new PlanValidator()
    const validate = () => {
      state.planValidation = validator.validate(
        state.intent!,
        state.calls,
        this.routing,
        this.tools.map((t) => t.name),
      )
      validator.enforce(state.planValidation)
      try {
        for (const step of executionDag(state.calls)) {
          const args = argsSchema.parse(state.calls.find((call) => call.id === step.id)!.args)
          this.catalog.resolve(step, (args.imageRefs ?? state.imageRefs ?? []).length)
        }
      } catch (error) {
        if (error instanceof CapabilityError) {
          state.planValidation = { valid: false, code: error.code, errors: [error.message] }
        }
        throw error
      }
    }
    // Validate restored plans too: an old image-only continuation cannot bypass the gate.
    if (state.phase === 'generating' || state.phase === 'summary') {
      validate()
    }
    const directions =
      state.intent.goal === 'image'
        ? fourDirectionTemplate(
            turn.prompt,
            state.imageRefs,
            this.tools.map((tool) => tool.name),
          )
        : undefined
    if (
      (state.intent.goal === 'video' || directions) &&
      (state.phase === 'host' || state.phase === 'planner')
    ) {
      const template = directions ?? videoTemplate(state.intent, turn.prompt, state.imageRefs)
      state.calls = template
      validate()
      const totalEstimate = template.reduce(
        (sum, call) =>
          sum + (this.tools.find((tool) => tool.name === call.name)?.estimateCredits ?? 40),
        0,
      )
      if (
        state.intentInput.quota?.creditsLeft !== undefined &&
        state.intentInput.quota.creditsLeft < totalEstimate
      ) {
        throw new RoutingError('quota_exceeded', '当前积分不足以完成所选路径')
      }
      // Planner supplies prompt slots only; its tool choices never change the path.
      state.agents.planner++
      const filled = await this.ports.llm.act({
        agent: 'planner',
        messages: [
          { role: 'user', content: turn.prompt },
          {
            role: 'system',
            content: directions
              ? '四方向创作计划已由模板确定，保持正面、背面、左侧、右侧四个独立输出。'
              : `只填写路径 ${state.intent.path} 的 prompt，禁止以静图代替视频。`,
          },
        ],
        tools: template.map((c) => ({ name: c.name })),
      })
      signal.throwIfAborted()
      state.calls = template.map((call, index) => {
        const args = argsSchema.safeParse(filled.toolCalls?.[index]?.args)
        return args.success && !directions
          ? { ...call, args: { ...argsSchema.parse(call.args), prompt: args.data.prompt } }
          : call
      })
      validate()
      state.index = 0
      state.phase = 'generating'
      state.plan = {
        explanation: directions ? '生成四个方向的画面' : `视频路径：${state.intent.path}`,
        steps: state.calls.map((call, index) => ({
          id: call.id,
          title:
            call.dag?.title ??
            (call.name === 'subject_cutout'
              ? '提取参考图主体'
              : call.name === 'image_generate'
                ? `生成关键帧 ${index + 1 - (state.intent?.needSubjectCutout ? 1 : 0)}`
                : '生成并交付视频'),
          status: 'pending',
        })),
      }
      await this.plan(turn, publish)
    }
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
        if (agent === 'host') {
          await this.text(turn, result.text, signal, publish)
        } else {
          turn.message.parts.push({ kind: 'thinking', summary: '规划创作步骤', text: result.text })
          await publish({ kind: 'message', message: turn.message })
        }
      }
      return result.toolCalls ?? []
    }
    const intent = state.intent
    const intentInput = state.intentInput
    const executeCall = async (index: number): Promise<InterruptSnapshot | null> => {
      for (;;) {
        signal.throwIfAborted()
        const call = state.calls[index]
        if (!call) {
          return null
        }
        const step = state.plan?.steps[index]
        if (step && step.status !== 'in_progress') {
          step.status = 'in_progress'
          await this.plan(turn, publish)
        }
        const binding = this.catalog.resolve(
          executionDag(state.calls)[index]!,
          (state.imageRefs ?? []).length,
        )
        const tool = this.tools.find((t) => t.name === call.name)
        if (!tool) {
          throw new Error(`未注册工具：${call.name}`)
        }
        const attempt = state.generationAttempts?.[call.id] ?? 1
        const approvalKey = attempt === 1 ? call.id : `${call.id}:${attempt}`
        const idempotencyKey = attempt === 1 ? call.id : `${call.id}:${attempt}`
        const charged = attempt === 1 || this.reflection.retryCredits !== false
        const estimate = state.calls
          .slice(index)
          .filter((c) => !state.approvedCallIds.includes(c.id === call.id ? approvalKey : c.id))
          .reduce(
            (sum, c) =>
              sum +
              (c.id === call.id && !charged
                ? 0
                : (this.tools.find((t) => t.name === c.name)?.estimateCredits ?? 40)),
            0,
          )
        if (
          intentInput.quota?.creditsLeft !== undefined &&
          intentInput.quota.creditsLeft < state.credits + estimate
        ) {
          throw new RoutingError('quota_exceeded', '当前积分不足以完成所选路径')
        }
        if (!state.approvedCallIds.includes(approvalKey) && estimate > this.creditThreshold) {
          if (step) {
            step.status = 'pending'
          }
          state.index = index
          return this.interrupt(
            turn,
            call,
            estimate,
            intent.goal === 'video'
              ? `将完成视频创作${intent.frameCount ? `（${intent.frameCount} 帧 + 视频合成）` : ''}，预计消耗 ${estimate} 积分。`
              : state.plan
                ? `将生成 ${state.calls.length - index} 张镜头画面，预计消耗 ${estimate} 积分。`
                : `生成一张图片，预计消耗 ${estimate} 积分。`,
          )
        }
        validate()
        const args = argsSchema.parse(call.args)
        if (args.frameCallIds) {
          args.frameUrls = args.frameCallIds.map((id) => {
            const artifact = turn.message.parts.find((p) => p.kind === 'artifact' && p.id === id)
            if (
              artifact?.kind !== 'artifact' ||
              artifact.type !== 'image' ||
              artifact.status !== 'success'
            ) {
              throw new RoutingError('plan_invalid', '视频合成缺少已完成关键帧')
            }
            return artifact.url
          })
        }
        let entry = state.toolCalls.find((t) => t.id === call.id)
        if (!entry) {
          entry = {
            id: call.id,
            agent: state.activeAgent,
            name: tool.name,
            args,
            resultSummary: intent.goal === 'video' ? '正在执行视频创作步骤' : '正在生成图片',
            status: 'invoking',
            credits: 0,
          }
          state.toolCalls.push(entry)
        }
        entry.capabilityId = binding.id
        entry.tier = call.dag?.tier ?? 'preview'
        entry.bindingVersion = binding.binding.version
        entry.adapter = binding.binding.adapter
        entry.modelId = binding.binding.modelId
        entry.dependsOn = executionDag(state.calls)[index]!.dependsOn
        entry.targetNodes = executionDag(state.calls)[index]!.targetNodes
        entry.parallelGroup = call.dag?.parallelGroup
        entry.status = 'invoking'
        state.agents[state.activeAgent]++
        const status: Extract<Part, { kind: 'tool_status' }> = {
          kind: 'tool_status',
          callId: call.id,
          toolName: tool.name,
          label: intent.goal === 'video' ? '正在执行视频创作步骤' : '正在生成图片',
          state: 'loading',
        }
        if (attempt > 1) {
          status.label = `质量检查未通过，正在重新生成（第 ${attempt} 次）`
        }
        const previousStatus = turn.message.parts.findIndex(
          (part) => part.kind === 'tool_status' && part.callId === call.id,
        )
        if (previousStatus >= 0) {
          turn.message.parts[previousStatus] = status
        } else {
          turn.message.parts.push(status)
        }
        await publish({ kind: 'message', message: turn.message })
        try {
          const job = await this.slots.execute(
            `${binding.id}:${binding.binding.version}`,
            binding.maxConcurrency,
            signal,
            () =>
              this.ports.queue.enqueue({
                name: tool.name,
                payload: {
                  ...args,
                  imageRefs: args.imageRefs ?? state.imageRefs,
                  tier: call.dag?.tier ?? 'preview',
                  attempt,
                },
                idempotencyKey,
                signal,
              }),
          )
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
            state.calls[index] = { ...continuation, dag: call.dag }
            for (const dependent of state.calls) {
              if (dependent.dag) {
                dependent.dag.dependsOn = dependent.dag.dependsOn.map((id) =>
                  id === call.id ? continuation.id : id,
                )
              }
              const dependentArgs = argsSchema.parse(dependent.args)
              if (dependentArgs.frameCallIds?.includes(call.id)) {
                dependent.args = {
                  ...dependentArgs,
                  frameCallIds: dependentArgs.frameCallIds.map((id) =>
                    id === call.id ? continuation.id : id,
                  ),
                }
              }
            }
            if (step) {
              step.id = continuation.id
              step.status = 'pending'
            }
            state.index = index
            return this.interrupt(
              turn,
              continuation,
              result.estimateCredits,
              result.summary,
              'approval',
            )
          }
          if (!state.creditedGenerationKeys?.includes(idempotencyKey)) {
            state.credits += charged ? result.credits : 0
            entry.credits = (entry.credits ?? 0) + (charged ? result.credits : 0)
            ;(state.creditedGenerationKeys ??= []).push(idempotencyKey)
          }
          if (
            result.artifact.type !== binding.outputType ||
            result.artifact.status !== 'success' ||
            !result.artifact.url
          ) {
            throw new RoutingError('plan_invalid', '生成工具产物不符合能力目录的媒体契约')
          }
          if (
            ['video_generate', 'frames_to_video', 'image_to_video'].includes(call.name) &&
            (result.artifact.type !== 'video' || result.artifact.status !== 'success')
          ) {
            throw new RoutingError('plan_invalid', '视频工具没有返回成功的视频产物')
          }
          const final = await finalizeGeneration(
            result,
            attempt,
            this.ports.reviewer ?? new StubReviewerProvider(),
            this.reflection,
            signal,
            args.prompt,
            { conversationId: turn.conversationId, turnId: turn.turnId, toolCallId: call.id },
          )
          if (final.evidence) {
            entry.evidence = final.evidence
            entry.reviewHistory = [...(entry.reviewHistory ?? []), final.evidence]
            entry.reviewAttempts = attempt - 1
            entry.reviewOutcome = final.outcome
          }
          if (final.outcome === 'retried') {
            ;(state.generationAttempts ??= {})[call.id] = attempt + 1
            entry.reviewAttempts = attempt
            entry.resultSummary = '质量检查未通过，准备重试'
            status.label = `质量检查未通过，正在重新生成（第 ${attempt + 1} 次）`
            await publish({ kind: 'message', message: turn.message })
            continue
          }
          if (final.outcome === 'escalated') {
            if (intent.goal === 'video') {
              throw new RoutingError('plan_invalid', '视频创作步骤未通过质量检查，已停止后续合成')
            }
            const reason = final.evidence?.review?.issues.join('；') ?? '审核未达标'
            result.artifact.status = 'failed'
            result.artifact.qualityIssue = '未通过质量检查'
            result.artifact.tier = call.dag?.tier ?? 'preview'
            result.artifact.createdByStepId = call.id
            turn.message.parts.push(result.artifact)
            entry.status = 'failed'
            entry.resultSummary = `未通过质量检查：${reason}`
            status.state = 'failed'
            status.label = '未通过质量检查'
            if (step) {
              step.status = 'failed'
            }
            await this.text(
              turn,
              `\n未通过质量检查：${reason}。已达到自动重试上限，请调整需求后重试。`,
              signal,
              publish,
            )
            await this.plan(turn, publish)
            await publish({ kind: 'message', message: turn.message })
            return null
          }
          entry.status = 'finished'
          entry.resultSummary = '生成完成'
          status.state = 'completed'
          status.label = '生成完成'
          if (
            args.frameCallIds ||
            (intent.path === 'image_to_multiframe' && result.artifact.type === 'image')
          ) {
            result.artifact.id = call.id
          }
          result.artifact.tier = call.dag?.tier ?? 'preview'
          result.artifact.createdByStepId = call.id
          turn.message.parts.push(result.artifact)
          if (step) {
            step.status = 'completed'
          }
          await this.plan(turn, publish)
          await publish({ kind: 'message', message: turn.message })
          return null
        } catch (error: unknown) {
          const failedJob = await this.ports.queue.getByIdempotencyKey(idempotencyKey)
          if (failedJob) {
            entry.jobId = failedJob.id
            // A cancelled native provider can still finish and charge. Keep its receipt without publishing its asset.
            const receipt = resultSchema.safeParse(failedJob.result)
            if (
              receipt.success &&
              !receipt.data.need_user &&
              !state.creditedGenerationKeys?.includes(idempotencyKey)
            ) {
              const credits = charged ? receipt.data.credits : 0
              state.credits += credits
              entry.credits = (entry.credits ?? 0) + credits
              ;(state.creditedGenerationKeys ??= []).push(idempotencyKey)
            }
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
    let executionFailure: unknown
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
        if (state.intent.goal === 'video') {
          await this.text(turn, '\n视频创作已完成。', signal, publish)
          state.phase = 'done'
          continue
        }
        if (state.toolCalls.some((call) => call.reviewOutcome === 'escalated')) {
          state.phase = 'done'
          continue
        }
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
        validate()
        if (!state.plan) {
          state.plan = {
            steps: state.calls.map((call, index) => ({
              id: call.id,
              title: `创作步骤 ${index + 1}`,
              status: 'pending',
            })),
          }
          await this.plan(turn, publish)
        }
        const dag = executionDag(state.calls)
        const ready = readySteps(
          dag,
          state.plan.steps.map((step, index) => ({ ...step, id: state.calls[index]!.id })),
        )
        if (!ready.length) {
          if (executionFailure) {
            throw executionFailure
          }
          if (state.plan.steps.some((step) => step.status === 'pending')) {
            throw new RoutingError('plan_invalid', '计划依赖未完成，已阻断后续步骤')
          }
          state.phase = 'summary'
          continue
        }
        const first = ready[0]!
        const parallel = this.tools.find((tool) => tool.name === first.toolName)?.parallelSafe
        const layerLimit = Math.min(
          this.routing.maxParallelSteps ?? 4,
          ...ready.map(
            (step) => this.catalog.resolve(step, (state.imageRefs ?? []).length).maxConcurrency,
          ),
        )
        const layer = parallel
          ? ready
              .filter(
                (step) => this.tools.find((tool) => tool.name === step.toolName)?.parallelSafe,
              )
              .slice(0, layerLimit)
          : [first]
        const approvalKeyFor = (call: ToolCall) => {
          const attempt = state.generationAttempts?.[call.id] ?? 1
          return attempt === 1 ? call.id : `${call.id}:${attempt}`
        }
        const remaining = state.calls.filter(
          (call, index) =>
            state.plan!.steps[index]?.status === 'pending' &&
            !state.approvedCallIds.includes(approvalKeyFor(call)),
        )
        const estimate = remaining.reduce(
          (total, call) =>
            total + (this.tools.find((tool) => tool.name === call.name)?.estimateCredits ?? 40),
          0,
        )
        if (
          state.intentInput.quota?.creditsLeft !== undefined &&
          state.credits + estimate > state.intentInput.quota.creditsLeft
        ) {
          throw new RoutingError('quota_exceeded', '当前积分不足以完成本批任务')
        }
        if (estimate > this.creditThreshold) {
          state.index = state.calls.findIndex((call) => call.id === first.id)
          await this.plan(turn, publish)
          return this.interrupt(
            turn,
            state.calls[state.index]!,
            estimate,
            `将执行剩余 ${remaining.length} 个创作步骤，预计消耗 ${estimate} 积分。`,
          )
        }
        // Approval covers the entire batch before any sibling can enqueue.
        state.approvedCallIds.push(...remaining.map(approvalKeyFor))
        state.index = state.calls.findIndex((call) => call.id === first.id)
        const results = await Promise.allSettled(
          layer.map((step) => executeCall(state.calls.findIndex((call) => call.id === step.id))),
        )
        // Drain all siblings before reporting failure/cancellation or releasing the run.
        const failed = results.find((result) => result.status === 'rejected')
        state.index = state.plan.steps.findIndex((step) => step.status === 'pending')
        if (state.index < 0) {
          state.index = state.calls.length
        }
        if (failed?.status === 'rejected') {
          if (signal.aborted) {
            throw failed.reason
          }
          executionFailure ??= failed.reason
          // Fail only dependent descendants; independent pending steps can still finish.
          let changed = true
          while (changed) {
            changed = false
            for (let index = 0; index < dag.length; index++) {
              const step = state.plan.steps[index]!
              if (
                step.status === 'pending' &&
                dag[index]!.dependsOn.some((id) => {
                  const dependentIndex = state.calls.findIndex((call) => call.id === id)
                  return ['failed', 'skipped', 'cancelled'].includes(
                    state.plan!.steps[dependentIndex]?.status ?? '',
                  )
                })
              ) {
                step.status = 'skipped'
                changed = true
              }
            }
          }
          await this.plan(turn, publish)
          continue
        }
        const interrupts = results.flatMap((result) =>
          result.status === 'fulfilled' && result.value ? [result.value] : [],
        )
        if (interrupts.length > 1) {
          throw new RoutingError('plan_invalid', '并行工具不得同时要求交互确认')
        }
        if (interrupts[0]) {
          // Capture after all siblings settle, so resume keeps their receipts and progress.
          interrupts[0].continuation = structuredClone(state)
          return interrupts[0]
        }
      }
    }
    if (state.intent.goal === 'video') {
      validate()
      if (
        !turn.message.parts.some(
          (part) => part.kind === 'artifact' && part.type === 'video' && part.status === 'success',
        )
      ) {
        throw new RoutingError('plan_invalid', '视频目标未交付成功视频')
      }
    }
    return null
  }
}
