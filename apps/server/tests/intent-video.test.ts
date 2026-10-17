import assert from 'node:assert/strict'
import { test } from 'node:test'
import { IntentGate, IntentValidator, routingOptionsFromEnv } from '../src/agent/intent.js'
import { PlanValidator, videoTemplate } from '../src/agent/plan.js'
import { createApplication } from '../src/app/create-app.js'
import { MemoryTraceWriter } from '../src/adapters/memory/index.js'
import type { Tool } from '../src/ports/index.js'

const flags = { videoToolEnabled: true, multiframeEnabled: true, directI2vEnabled: true }
const registered = ['video_generate', 'image_generate', 'frames_to_video', 'image_to_video']
const fixtures: Tool[] = registered.map((name) => ({
  name,
  estimateCredits: 1,
  async execute(input, signal) {
    signal.throwIfAborted()
    if (name === 'frames_to_video') {
      assert.equal(input.frameUrls?.length, 4)
    }
    return {
      artifact: {
        kind: 'artifact',
        id: name,
        type: name === 'image_generate' ? 'image' : 'video',
        url: `/stub/${name}`,
        status: 'success',
      },
      credits: 1,
    }
  },
}))
test('rules: 无图文生视频，有图默认多帧，明确只要图', async () => {
  const gate = new IntentGate(flags)
  assert.equal((await gate.resolve({ prompt: '生成咖啡视频' })).path, 'text_to_video')
  assert.equal(
    (await gate.resolve({ prompt: '生成咖啡视频', imageRefs: ['asset:coffee'] })).path,
    'image_to_multiframe',
  )
  assert.equal((await gate.resolve({ prompt: '只要一张关键帧，不要视频' })).goal, 'image')
  assert.equal((await gate.resolve({ prompt: '只要一张图片' })).path, 'text_to_image')
  assert.equal(
    (await gate.resolve({ prompt: '直接图生视频', imageRefs: ['asset:a'] })).path,
    'image_to_multiframe',
  )
  assert.equal(
    (
      await new IntentGate({ ...flags, imageVideoStrategy: 'allow_direct_i2v' }).resolve({
        prompt: '直接图生视频',
        imageRefs: ['asset:a'],
      })
    ).path,
    'image_to_video',
  )
})
test('PlanValidator 拒绝视频以一张关键帧结束；禁用路径不降级', async () => {
  const intent = await new IntentGate().resolve({ prompt: '生成视频', imageRefs: ['asset:a'] })
  const validator = new PlanValidator()
  const invalid = validator.validate(
    intent,
    [{ id: 'single', name: 'image_generate', args: { prompt: '只要一张关键帧' } }],
    flags,
    registered,
  )
  assert.equal(invalid.code, 'plan_invalid')
  assert.throws(() => validator.enforce(invalid), /plan_invalid/)
  const calls = videoTemplate(intent, '生成视频', ['asset:a'])
  assert.equal(validator.validate(intent, calls, flags, registered).valid, true)
  assert.equal(
    validator.validate(intent, calls, { ...flags, multiframeEnabled: false }, registered).code,
    'video_unavailable',
  )
  assert.equal(validator.validate(intent, calls, flags, []).code, 'video_unavailable')
})
test('IntentLLM 使用 JSON schema；Validator 拒绝越权路径', async () => {
  let seen = false
  const intent = await new IntentGate({
    intentMode: 'rules_plus_llm',
    intentLlm: {
      async classify(_input, schema) {
        seen = Boolean(schema.properties)
        return {
          goal: 'video',
          path: 'text_to_video',
          hasImage: false,
          explicitDirectI2v: false,
          source: 'llm',
          needsClarification: true,
          clarification: '是否制作视频？',
        }
      },
    },
  }).resolve({ prompt: '让它动起来' })
  assert.equal(seen, true)
  assert.equal(intent.needsClarification, true)
  assert.equal(intent.path, 'clarify')
  assert.throws(
    () =>
      new IntentValidator().validate(
        { ...intent, path: 'text_to_image', proposedPath: undefined },
        { prompt: '让它动起来' },
      ),
    /intent_invalid/,
  )
  assert.throws(() => routingOptionsFromEnv({ PLAN_VALIDATOR: 'off' }), /不合法/)
})
async function run(prompt: string, imageRefs?: string[], tools?: Tool[], enabled = true) {
  const traces = new MemoryTraceWriter()
  const app = createApplication({
    ...flags,
    videoToolEnabled: enabled,
    tools: tools ?? fixtures,
    ports: { traces },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    creditThreshold: 100,
    reviewEnabled: false,
  })
  const turn = await app.service.prepare(prompt, undefined, imageRefs)
  await app.service.run(turn, async () => {})
  return { app, turn, trace: traces.get(turn.runId)! }
}
test('Host stub 完成正确路径且 trace 保留完整意图/门禁', async () => {
  for (const refs of [undefined, ['asset:a']]) {
    const { turn, trace } = await run('做咖啡视频', refs)
    assert.equal(turn.status, 'completed')
    assert.equal(trace.turns[0]?.intent?.path, refs ? 'image_to_multiframe' : 'text_to_video')
    assert.equal(trace.turns[0]?.planValidation?.valid, true)
    assert.equal(
      turn.message.parts.some((p) => p.kind === 'artifact' && p.type === 'video'),
      true,
    )
    const plan = turn.message.parts.find((p) => p.kind === 'plan')
    assert.ok(plan?.kind === 'plan' && plan.steps.every((step) => step.title))
  }
})
test('视频未配置显式失败；零生图调用', async () => {
  const { turn, trace } = await run('做视频', ['asset:a'], undefined, false)
  assert.equal(turn.status, 'failed')
  assert.equal(trace.turns[0]?.planValidation?.code, 'video_unavailable')
  assert.equal(trace.turns[0]?.toolCalls.length, 0)
  assert.ok(turn.message.parts.some((p) => p.kind === 'error' && p.code === 'video_unavailable'))
})
test('视频工具冒充静图不得成功写回', async () => {
  const tools: Tool[] = [
    {
      name: 'video_generate',
      estimateCredits: 0,
      async execute() {
        return {
          artifact: {
            kind: 'artifact',
            id: 'fake',
            type: 'image',
            url: '/fake.svg',
            status: 'success',
          },
          credits: 0,
        }
      },
    },
  ]
  const { turn } = await run('做视频', undefined, tools)
  assert.equal(turn.status, 'failed')
  assert.equal(
    turn.message.parts.some((p) => p.kind === 'artifact'),
    false,
  )
})
test('clarify 与积分复用 awaiting_user，澄清确认不跳过积分', async () => {
  const traces = new MemoryTraceWriter()
  const app = createApplication({
    ...flags,
    tools: fixtures,
    ports: { traces },
    intentMode: 'rules_plus_llm',
    intentLlm: {
      async classify() {
        return {
          goal: 'video',
          path: 'text_to_video',
          hasImage: false,
          explicitDirectI2v: false,
          source: 'llm',
          needsClarification: true,
          clarification: '是否生成视频？',
        }
      },
    },
    creditThreshold: 0,
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    reviewEnabled: false,
  })
  const turn = await app.service.prepare('让它动起来')
  await app.service.run(turn, async () => {})
  const snap = await app.ports.sessions.getInterruptSnapshot(turn.turnId)
  assert.equal(snap?.status, 'awaiting_user')
  assert.equal(snap?.interrupt_kind, 'clarify')
  const resumed = await app.service.prepareResume(turn.conversationId, snap!.confirmId, true)
  await app.service.run(resumed.turn, async () => {}, resumed.resume)
  const credit = await app.ports.sessions.getInterruptSnapshot(turn.turnId)
  assert.equal(credit?.interrupt_kind, 'credit')
  const accepted = await app.service.prepareResume(turn.conversationId, credit!.confirmId, true)
  await app.service.run(accepted.turn, async () => {}, accepted.resume)
  assert.equal(accepted.turn.status, 'completed')
})

test('多帧停止后续跑保留已完成帧，重新确认积分且不重复生图', async () => {
  let frames = 0
  const tools = fixtures.map((tool) =>
    tool.name !== 'image_generate'
      ? tool
      : {
          ...tool,
          async execute(input: Parameters<Tool['execute']>[0], signal: AbortSignal) {
            frames++
            return tool.execute(input, signal)
          },
        },
  )
  const app = createApplication({
    ...flags,
    tools,
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    creditThreshold: 100,
    reviewEnabled: false,
  })
  const turn = await app.service.prepare('生成视频', undefined, ['asset:a'])
  let stopped = false
  await app.service.run(turn, async (event) => {
    if (
      !stopped &&
      event.kind === 'message' &&
      event.message.parts.some((p) => p.kind === 'artifact')
    ) {
      stopped = true
      await app.service.stop(turn.conversationId, turn.turnId)
    }
  })
  assert.equal(turn.status, 'cancelled')
  assert.equal(frames, 1)
  const continued = await app.service.prepare('继续', turn.conversationId)
  await app.service.run(continued, async () => {})
  assert.equal(continued.status, 'completed')
  assert.equal(frames, 4)
  assert.ok(continued.message.parts.some((p) => p.kind === 'artifact' && p.type === 'video'))
})

test('Host 重新校验旧单图续跑计划，禁止成功写回', async () => {
  const app = createApplication({
    ...flags,
    tools: fixtures,
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
  })
  const turn = await app.service.prepare('做视频', undefined, ['asset:a'])
  const { initialExecution } = await import('../src/agent/host.js')
  turn.execution = {
    ...initialExecution(),
    phase: 'generating',
    calls: [{ id: 'old', name: 'image_generate', args: { prompt: '只要一张关键帧' } }],
  }
  await app.service.run(turn, async () => {})
  assert.equal(turn.status, 'failed')
  assert.equal(turn.execution.planValidation?.code, 'plan_invalid')
  assert.equal(turn.execution.toolCalls.length, 0)
})

test('v2 字段、默认四帧、忽略参考和多参考澄清', async () => {
  const gate = new IntentGate(flags)
  const intent = await gate.resolve({ prompt: '用参考图做视频', imageRefs: ['asset:a'] })
  assert.equal(intent.version, '1')
  assert.equal(intent.frameCount, 4)
  assert.deepEqual(intent.referenceAssetIds, ['asset:a'])
  const ignored = await gate.resolve({ prompt: '不要参考，纯文生视频', imageRefs: ['asset:a'] })
  assert.equal(ignored.path, 'text_to_video')
  assert.deepEqual(ignored.referenceAssetIds, [])
  const conflict = await gate.resolve({ prompt: '生成视频', imageRefs: ['asset:a', 'asset:b'] })
  assert.equal(conflict.path, 'clarify')
  assert.deepEqual(conflict.clarifySlots, ['reference_image'])
  assert.equal(
    (await gate.resolve({ prompt: '只要视频，用这张图', imageRefs: ['asset:a'] })).path,
    'image_to_multiframe',
  )
})
test('参考白名单和额度检查在 LLM/Planner 前阻止调用', async () => {
  let invoked = 0
  const gate = new IntentGate({
    intentMode: 'rules_plus_llm',
    intentLlm: {
      async classify() {
        invoked++
        return {}
      },
    },
    referenceUrlAllowedHosts: ['images.example.com'],
  })
  await assert.rejects(
    gate.resolve({ prompt: '动起来', imageRefs: ['https://evil.example/a.png'] }),
    /reference_unavailable/,
  )
  await assert.rejects(
    gate.resolve({
      prompt: '动起来',
      imageRefs: ['https://images.example.com@evil.example/a.png'],
    }),
    /reference_unavailable/,
  )
  await assert.rejects(
    gate.resolve({ prompt: '动起来', quota: { creditsLeft: 0 } }),
    /quota_exceeded/,
  )
  assert.equal(invoked, 0)
  const valid = await new IntentGate({ referenceUrlAllowedHosts: ['images.example.com'] }).resolve({
    prompt: '生成视频',
    imageRefs: ['https://images.example.com/a.png'],
  })
  assert.equal(valid.path, 'image_to_multiframe')
})
test('完整 JSON 契约的低置信度分类进入 clarify', async () => {
  const intent = await new IntentGate({
    intentMode: 'rules_plus_llm',
    intentLlm: {
      async classify() {
        return {
          version: '1',
          goal: 'video',
          path: 'text_to_video',
          needSubjectCutout: false,
          referenceAssetIds: [],
          confidence: 0.4,
          clarifyQuestion: '是否制作视频？',
        }
      },
    },
  }).resolve({ prompt: '动起来' })
  assert.equal(intent.path, 'clarify')
  assert.equal(intent.proposedPath, 'text_to_video')
})
test('多参考恢复可选择第二张，经过 Validator 并保持积分确认', async () => {
  const app = createApplication({
    ...flags,
    tools: fixtures,
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    creditThreshold: 0,
    reviewEnabled: false,
  })
  const turn = await app.service.prepare('生成视频', undefined, ['asset:a', 'asset:b'])
  await app.service.run(turn, async () => {})
  const snap = await app.ports.sessions.getInterruptSnapshot(turn.turnId)
  assert.equal(snap?.interrupt_kind, 'clarify')
  const resumed = await app.service.prepareResume(turn.conversationId, snap!.confirmId, true, {
    imageRefs: ['asset:b'],
  })
  await app.service.run(resumed.turn, async () => {}, resumed.resume)
  assert.equal(resumed.turn.execution?.intent?.path, 'image_to_multiframe')
  assert.deepEqual(resumed.turn.execution?.intent?.referenceAssetIds, ['asset:b'])
  assert.equal(
    (await app.ports.sessions.getInterruptSnapshot(turn.turnId))?.interrupt_kind,
    'credit',
  )
})
test('缺参考的澄清失败不认领确认，补充参考后可以恢复', async () => {
  const app = createApplication({
    ...flags,
    tools: fixtures,
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    creditThreshold: 100,
    reviewEnabled: false,
  })
  const turn = await app.service.prepare('生成视频', undefined, undefined, {
    canvas: { projectId: 'p', hasImage: true },
  })
  await app.service.run(turn, async () => {})
  const snap = await app.ports.sessions.getInterruptSnapshot(turn.turnId)
  assert.equal(snap?.interrupt_kind, 'clarify')
  await assert.rejects(
    app.service.prepareResume(turn.conversationId, snap!.confirmId, true),
    /参考/,
  )
  const resumed = await app.service.prepareResume(turn.conversationId, snap!.confirmId, true, {
    imageRefs: ['asset:a'],
  })
  await app.service.run(resumed.turn, async () => {}, resumed.resume)
  assert.equal(resumed.turn.status, 'completed')
})
test('开启 rules_plus_llm 默认使用结构化 Host port', async () => {
  const app = createApplication({
    intentMode: 'rules_plus_llm',
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
  })
  const turn = await app.service.prepare('好看一点')
  await app.service.run(turn, async () => {})
  assert.equal(turn.status, 'interrupted')
  assert.equal(turn.execution?.intent?.source, 'llm')
  assert.equal(turn.execution?.intent?.path, 'clarify')
})

test('多帧工具 need_user 更换调用 ID 后合成依赖同步，续跑不丢帧', async () => {
  let asked = false
  const tools: Tool[] = fixtures.map((tool) =>
    tool.name !== 'image_generate'
      ? tool
      : {
          ...tool,
          async execute(input, signal) {
            if (!asked) {
              asked = true
              return { need_user: true, estimateCredits: 4, summary: '确认帧生成' }
            }
            return tool.execute(input, signal)
          },
        },
  )
  const app = createApplication({
    ...flags,
    tools,
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    creditThreshold: 100,
    reviewEnabled: false,
  })
  const turn = await app.service.prepare('生成视频', undefined, ['asset:a'])
  await app.service.run(turn, async () => {})
  const snap = await app.ports.sessions.getInterruptSnapshot(turn.turnId)
  assert.equal(snap?.reason, 'approval')
  const resumed = await app.service.prepareResume(turn.conversationId, snap!.confirmId, true)
  await app.service.run(resumed.turn, async () => {}, resumed.resume)
  assert.equal(resumed.turn.status, 'completed')
  assert.ok(resumed.turn.message.parts.some((p) => p.kind === 'artifact' && p.type === 'video'))
})
