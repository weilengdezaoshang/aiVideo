import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as sleep } from 'node:timers/promises'
import { createApplication } from '../src/app/create-app.js'
import { MemoryTraceWriter } from '../src/adapters/memory/index.js'
import { CapabilityCatalog, type Capability } from '../src/agent/capabilities.js'
import { executionDag, validateDag, BindingSlots } from '../src/agent/scheduler.js'
import { videoTemplate } from '../src/agent/plan.js'
import { IntentGate } from '../src/agent/intent.js'
import type { Tool } from '../src/ports/index.js'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((done) => {
    resolve = done
  })
  return { promise, resolve }
}
function image(execute?: Tool['execute']): Tool {
  return {
    name: 'image_generate',
    parallelSafe: true,
    estimateCredits: 1,
    execute:
      execute ??
      (async () => ({
        credits: 1,
        artifact: {
          kind: 'artifact',
          id: crypto.randomUUID(),
          type: 'image',
          url: '/fixture.png',
          status: 'success',
        },
      })),
  }
}
function setup(tools: Tool[], extra: Parameters<typeof createApplication>[0] = {}) {
  return createApplication({
    tools,
    ports: { traces: new MemoryTraceWriter() },
    llmDelayMs: 0,
    streamCharDelayMs: 0,
    reviewEnabled: false,
    creditThreshold: 100,
    ...extra,
  })
}
const flags = { videoToolEnabled: true, multiframeEnabled: true }

test('DAG 拒绝环、缺依赖、重复 ID、并行抢节点；有依赖的同节点写合法', () => {
  const calls = ['a', 'b'].map((id) => ({
    id,
    name: 'image_generate',
    args: { prompt: id },
    dag: {
      title: id,
      capabilityId: 'image.text_to_image',
      dependsOn: [] as string[],
      targetNodes: ['node'],
      tier: 'preview' as const,
    },
  }))
  assert.match(validateDag(executionDag(calls)).join(), /同一节点/)
  calls[1]!.dag.dependsOn = ['a']
  assert.deepEqual(validateDag(executionDag(calls)), [])
  calls[0]!.dag.dependsOn = ['b']
  assert.match(validateDag(executionDag(calls)).join(), /环/)
  calls[0]!.dag.dependsOn = ['missing']
  assert.match(validateDag(executionDag(calls)).join(), /无效/)
  calls[0]!.dag.dependsOn = []
  calls[1]!.id = 'a'
  assert.match(validateDag(executionDag(calls)).join(), /唯一/)
})

test('四方向实际同时执行四路，全部产物与 DAG 计入进程和 Trace', async () => {
  const gate = deferred()
  const started = deferred()
  let active = 0
  let peak = 0
  const prompts: string[] = []
  const tool = image(async (input) => {
    prompts.push(input.prompt)
    peak = Math.max(peak, ++active)
    if (active === 4) {
      started.resolve()
    }
    await gate.promise
    active--
    return image().execute(input, new AbortController().signal)
  })
  const app = setup([tool])
  const turn = await app.service.prepare('生成前后左右四方向图片')
  const running = app.service.run(turn, async () => {})
  await started.promise
  assert.equal(peak, 4)
  assert.equal(
    turn.execution?.plan?.steps.filter((step) => step.status === 'in_progress').length,
    4,
  )
  gate.resolve()
  await running
  assert.equal(turn.status, 'completed')
  assert.equal(turn.execution?.credits, 4)
  assert.equal(new Set(prompts).size, 4)
  assert.equal(turn.message.parts.filter((part) => part.kind === 'artifact').length, 4)
  assert.ok(
    turn.execution?.toolCalls.every(
      (call) => call.bindingVersion && call.capabilityId && call.parallelGroup,
    ),
  )
})

test('maxParallelSteps 限制为两路，并行前检查整批额度与确认', async () => {
  let active = 0
  let peak = 0
  let calls = 0
  const tool = image(async (input, signal) => {
    calls++
    peak = Math.max(peak, ++active)
    await sleep(5)
    active--
    return image().execute(input, signal)
  })
  const app = setup([tool], { maxParallelSteps: 2 })
  const turn = await app.service.prepare('生成四方向')
  await app.service.run(turn, async () => {})
  assert.equal(peak, 2)
  assert.equal(calls, 4)
  const insufficient = await app.service.prepare('生成四方向', undefined, undefined, {
    quota: { creditsLeft: 3 },
  })
  await app.service.run(insufficient, async () => {})
  assert.equal(insufficient.status, 'failed')
  assert.equal(calls, 4)
  const confirmation = setup([tool], { creditThreshold: 2 })
  const pending = await confirmation.service.prepare('生成四方向')
  await confirmation.service.run(pending, async () => {})
  assert.equal(pending.status, 'interrupted')
  assert.equal(calls, 4)
})

test('多帧并行后才合成，失败保留成功兄弟且不调合成', async () => {
  let frames = 0
  let composed = 0
  let fail = false
  const tool = image(async (input, signal) => {
    await sleep(2)
    frames++
    if (fail && input.prompt.includes('关键帧 1')) {
      throw new Error('provider_failed')
    }
    return image().execute(input, signal)
  })
  const compose: Tool = {
    name: 'frames_to_video',
    estimateCredits: 1,
    async execute(input) {
      composed++
      assert.equal(frames, 4)
      assert.equal(input.frameUrls?.length, 4)
      return {
        credits: 1,
        artifact: {
          kind: 'artifact',
          id: 'video',
          type: 'video',
          url: '/fixture.mp4',
          status: 'success',
        },
      }
    },
  }
  // Prevent the fixture Planner from replacing the deterministic frame prompts.
  const llm = {
    async act() {
      return {}
    },
  }
  const app = setup([tool, compose], { ...flags, ports: { traces: new MemoryTraceWriter(), llm } })
  const turn = await app.service.prepare('生成视频', undefined, ['asset:ref'])
  await app.service.run(turn, async () => {})
  assert.equal(turn.status, 'completed')
  assert.equal(composed, 1)
  frames = 0
  fail = true
  const failed = await app.service.prepare('生成视频', undefined, ['asset:ref'])
  await app.service.run(failed, async () => {})
  assert.equal(failed.status, 'failed')
  assert.equal(frames, 4)
  assert.equal(composed, 1)
  assert.equal(failed.message.parts.filter((part) => part.kind === 'artifact').length, 3)
})

test('模板合成 DAG 依赖全部关键帧，旧计划和非法字段不能绕过门禁', async () => {
  const intent = await new IntentGate(flags).resolve({ prompt: '生成视频', imageRefs: ['asset:a'] })
  const calls = videoTemplate(intent, '视频', ['asset:a'])
  assert.deepEqual(
    calls.at(-1)?.dag?.dependsOn,
    calls.slice(0, -1).map((call) => call.id),
  )
  const app = setup([image()])
  const turn = await app.service.prepare('生成四方向')
  const { initialExecution } = await import('../src/agent/host.js')
  turn.execution = {
    ...initialExecution(),
    phase: 'generating',
    calls: [
      {
        id: 'invalid',
        name: 'image_generate',
        args: { prompt: 'a' },
        dag: {
          title: 'a',
          capabilityId: 'image.text_to_image',
          dependsOn: ['missing'],
          targetNodes: ['n'],
          tier: 'preview',
        },
      },
    ],
  }
  await app.service.run(turn, async () => {})
  assert.equal(turn.status, 'failed')
  assert.equal(turn.execution.toolCalls.length, 0)
})

test('目录未开通 fail-closed，公开响应不含绑定/模型/凭据，prod mock 被拒', async () => {
  const capability: Capability = {
    id: 'image.text_to_image',
    enabled: false,
    outputType: 'image',
    tiers: ['preview'],
    maxConcurrency: 2,
    maxRefImages: 1,
    binding: {
      version: 'v1',
      toolName: 'image_generate',
      adapter: 'mock',
      modelId: 'private-model',
      credentialRef: 'secret-reference',
    },
  }
  let calls = 0
  const app = setup(
    [
      image(async (input, signal) => {
        calls++
        return image().execute(input, signal)
      }),
    ],
    { capabilities: [capability] },
  )
  const turn = await app.service.prepare('生成四方向')
  await app.service.run(turn, async () => {})
  assert.equal(turn.status, 'failed')
  assert.equal(calls, 0)
  assert.ok(
    turn.message.parts.some(
      (part) => part.kind === 'error' && part.code === 'capability_unavailable',
    ),
  )
  const publicJson = await (await app.api.request('/v1/capabilities')).text()
  assert.doesNotMatch(publicJson, /private-model|secret-reference|binding/)
  assert.throws(() => new CapabilityCatalog([{ ...capability, enabled: true }], true), /Mock/)
})

test('BindingSlots 跨 run 限制并发，等待取消不泄漏槽位', async () => {
  const slots = new BindingSlots()
  const release = deferred()
  let starts = 0
  const first = slots.execute('binding', 1, new AbortController().signal, async () => {
    starts++
    await release.promise
  })
  const controller = new AbortController()
  const cancelled = slots.execute('binding', 1, controller.signal, async () => {
    starts++
  })
  controller.abort()
  await assert.rejects(cancelled)
  assert.equal(starts, 1)
  release.resolve()
  await first
  await slots.execute('binding', 1, new AbortController().signal, async () => {
    starts++
  })
  assert.equal(starts, 2)
})

test('取消不释放原生任务槽位，迟到收费保存收据且不落产物', async () => {
  const gate = deferred()
  const started = deferred()
  const tool = image(async (input) => {
    started.resolve()
    await gate.promise
    return image().execute(input, new AbortController().signal)
  })
  const app = setup([tool])
  const turn = await app.service.prepare('生成四方向')
  const running = app.service.run(turn, async () => {})
  await started.promise
  await app.service.stop(turn.conversationId, turn.turnId)
  let settled = false
  void running.then(() => {
    settled = true
  })
  await sleep(2)
  assert.equal(settled, false)
  gate.resolve()
  await running
  assert.equal(turn.status, 'cancelled')
  assert.equal(turn.message.parts.filter((part) => part.kind === 'artifact').length, 0)
  assert.equal(turn.execution?.credits, 4)
})

test('production composition root 拒绝内存和默认 Mock，不允许绕开 main', () => {
  assert.throws(() => createApplication({ environment: 'production' }), /生产模式/)
})
