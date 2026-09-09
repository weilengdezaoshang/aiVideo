import test from 'node:test'
import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {
  buildUseCaseAPlan,
  planAgentAction,
  planWithModel,
  planWithRules,
  TOOL_KEYS,
} from '../src/services/agent/agent-planner.js'
import { AgentStore } from '../src/services/agent/agent-store.js'

test('规则规划:缺引用语义由路由保证,明确抠图请求产出白名单内计划', () => {
  const ok = planWithRules('把这张图的主体抠出来,放到旁边')
  assert.ok(ok.ok)
  if (ok.ok) {
    assert.deepEqual(
      ok.plan.steps.map((s) => s.key),
      [...TOOL_KEYS],
    )
    assert.equal(ok.plan.meta.needManualConfirm, true)
  }
})

test('规则规划:超范围与含糊请求一律澄清,不产出计划', () => {
  for (const text of ['帮我生成一段视频', '把图画清楚一点', '']) {
    const result = planWithRules(text)
    assert.ok(!result.ok)
    if (!result.ok) {
      assert.notEqual(result.reason, 'need_reference')
      assert.ok(result.message.length > 0)
    }
  }
})

test('模型规划:合法 JSON 产出白名单计划;越权步骤被拒绝', async () => {
  const reply = (content: string) => async () => ({
    ok: true,
    status: 200,
    json: async () => ({ choices: [{ message: { content } }] }),
  })
  const good = await planWithModel({
    apiKey: 'k',
    baseUrl: 'http://x',
    model: 'm',
    request: '把主体抠出来',
    signal: AbortSignal.timeout(1000),
    fetchImpl: reply(
      JSON.stringify({
        capable: true,
        plan: {
          summary: '抠主体放旁边',
          steps: [
            { key: 'wait_user_check_mask', label: '乱序步骤' },
            { key: 'detect_mask', label: '识别' },
            { key: 'apply_cutout_place_right', label: '应用' },
          ],
        },
      }),
    ) as unknown as typeof fetch,
  })
  assert.ok(good.ok)
  if (good.ok) {
    // 乱序输出被服务端按能力依赖重排
    assert.equal(good.plan.steps[0].key, 'detect_mask')
    assert.equal(good.plannedBy, 'model')
  }

  // 白名单校验失败 → 抛错 → 调用方降级;越权计划绝不能被放行
  await assert.rejects(() =>
    planWithModel({
      apiKey: 'k',
      baseUrl: 'http://x',
      model: 'm',
      request: 'x',
      signal: AbortSignal.timeout(1000),
      fetchImpl: reply(
        JSON.stringify({ capable: true, plan: { steps: [{ key: 'hack', label: '越权' }] } }),
      ) as unknown as typeof fetch,
    }),
  )
})

test('planAgentAction:模型失败自动降级规则规划', async () => {
  const result = await planAgentAction('把这张图的主体抠出来,放到旁边', {
    apiKey: 'k',
    baseUrl: 'http://127.0.0.1:1',
    model: 'm',
    signal: AbortSignal.timeout(1500),
  })
  assert.ok(result.ok)
  if (result.ok) {
    assert.equal(result.plannedBy, 'rules')
  }
})

test('planAgentAction:未配置模型时直接走规则;用例A计划固定', async () => {
  const result = await planAgentAction('')
  assert.ok(!result.ok)
  const plan = buildUseCaseAPlan()
  assert.equal(plan.steps.length, 3)
  assert.equal(plan.steps[1].key, 'wait_user_check_mask')
})

test('Agent 会话:prepare 一次性语义 + 完成回写 + 跨重启恢复', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-agent-'))
  const file = path.join(dir, 'sessions.json')
  const store = new AgentStore(file)
  await store.init()
  const session = store.create({
    docId: 'doc-1',
    assetId: 'asset-1',
    assetExt: 'png',
    request: '把主体抠出来放到旁边',
    plan: buildUseCaseAPlan(),
    plannedBy: 'rules',
  })
  assert.equal(session.status, 'planned')

  // preparing → waiting_mask
  store.update(session.id, { status: 'preparing' })
  const waiting = store.update(session.id, {
    status: 'waiting_mask',
    maskAssetId: 'mask-1',
  })
  assert.equal(waiting?.status, 'waiting_mask')

  // 完成回写
  const done = store.update(session.id, { status: 'completed', resultAssetId: 'result-1' })
  assert.equal(done?.resultAssetId, 'result-1')
  assert.equal(store.listByDoc('doc-1').length, 1)

  // 防抖落盘后新实例恢复(A11:刷新/重启可恢复)
  await store.flush()
  const reborn = new AgentStore(file)
  await reborn.init()
  const restored = reborn.get(session.id)
  assert.equal(restored?.status, 'completed')
  assert.equal(restored?.plan.steps.length, 3)
})

test('Agent 会话:启动时把卡在 preparing 的会话回退为 planned(识别中断可重试)', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-agent-'))
  const file = path.join(dir, 'sessions.json')
  const stuck = {
    id: 's-stuck',
    docId: 'doc-9',
    assetId: 'asset-9',
    assetExt: 'png',
    request: '抠出主体',
    status: 'preparing',
    plan: buildUseCaseAPlan(),
    plannedBy: 'rules',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  }
  await fs.writeFile(file, JSON.stringify({ sessions: [stuck] }))
  const reborn = new AgentStore(file)
  await reborn.init()
  const restored = reborn.get('s-stuck')
  assert.equal(restored?.status, 'planned')
  assert.match(restored?.error ?? '', /中断/)
})
