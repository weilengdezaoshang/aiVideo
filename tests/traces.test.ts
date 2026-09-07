import test from 'node:test'
import assert from 'node:assert/strict'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { classifyError, estimateCostCny, promptHash, TraceStore } from '../src/traces.js'
import type { GenTrace } from '../src/traces.js'

const trace = (overrides: Partial<GenTrace>): GenTrace => ({
  ts: new Date().toISOString(),
  kind: 'trace',
  jobId: 'job-1',
  provider: 'cloud',
  vendor: 'siliconflow',
  model: 'black-forest-labs/FLUX.1-schnell',
  origin: 'user',
  promptHash: promptHash('猫'),
  promptChars: 1,
  seed: 1,
  width: 1024,
  height: 1024,
  kind_of_media: 'image',
  status: 'completed',
  queueMs: 100,
  latencyMs: 2000,
  ...overrides,
})

test('promptHash:同提示词稳定,不同提示词不同,不泄露原文', () => {
  assert.equal(promptHash('一只猫'), promptHash('一只猫'))
  assert.notEqual(promptHash('一只猫'), promptHash('一只狗'))
  assert.equal(promptHash('一只猫').length, 12)
})

test('estimateCostCny:已知模型有价,未知模型不计费', () => {
  assert.equal(estimateCostCny('black-forest-labs/FLUX.1-schnell'), 0.01)
  assert.equal(estimateCostCny('cogview-4-250304'), 0.06)
  assert.equal(estimateCostCny('totally-unknown-model'), undefined)
})

test('classifyError:取消/超时/鉴权/限流/参数/服务端/网络', () => {
  assert.equal(classifyError('已取消'), 'cancelled')
  assert.equal(classifyError('连接超时(5 秒)'), 'timeout')
  assert.equal(classifyError('HTTP 401'), 'auth')
  assert.equal(classifyError('HTTP 429 too many requests'), 'rate_limit')
  assert.equal(classifyError('参数不合法:宽度'), 'invalid_request')
  assert.equal(classifyError('云端生成失败(HTTP 502)'), 'server_error')
  assert.equal(classifyError('fetch failed'), 'network')
  assert.equal(classifyError('莫名其妙'), 'unknown')
})

test('append/readAll:JSONL 落盘,半截行跳过', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-traces-'))
  const store = new TraceStore(path.join(dir, 'traces.jsonl'))
  await store.append(trace({ jobId: 'a' }))
  await store.append(trace({ jobId: 'b', status: 'failed', errorCategory: 'timeout' }))
  // 模拟崩溃残留的半截行
  await fs.appendFile(path.join(dir, 'traces.jsonl'), '{"ts":"2026', 'utf8')
  const records = await store.readAll()
  assert.equal(records.length, 2)
  assert.equal(records[0].kind, 'trace')
})

test('aggregate:成功率/分位延迟/费用/保留率联动删除反馈', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-traces-'))
  const store = new TraceStore(path.join(dir, 'traces.jsonl'))
  const imgA = 'img-aaa'
  const imgB = 'img-bbb'
  await store.append(trace({ jobId: '1', latencyMs: 1000, imageId: imgA }))
  await store.append(
    trace({ jobId: '2', latencyMs: 3000, imageId: imgB, model: 'cogview-4-250304' }),
  )
  await store.append(trace({ jobId: '3', status: 'failed', errorCategory: 'auth' }))
  await store.append({
    ts: new Date().toISOString(),
    kind: 'feedback',
    action: 'deleted',
    imageId: imgA,
  })

  const m = await store.aggregate(7)
  assert.equal(m.generations.total, 3)
  assert.equal(m.generations.completed, 2)
  assert.equal(m.generations.failed, 1)
  assert.ok(Math.abs(m.generations.successRate - 2 / 3) < 1e-9)
  assert.equal(m.latency.p50Ms, 1000, '两条完成:分位取下界')
  assert.equal(m.latency.p95Ms, 3000)
  // FLUX 0.01 元 + CogView 0.06 元
  assert.ok(Math.abs(m.cost.totalCny - 0.07) < 1e-9)
  // imgA 被删 → FLUX 保留率 0;imgB 未删 → CogView 保留率 1
  const flux = m.byModel.find((x) => /schnell/.test(x.model))
  const cog = m.byModel.find((x) => /cogview/.test(x.model))
  assert.equal(flux?.deleted, 1)
  assert.equal(flux?.keptRate, 0)
  assert.equal(cog?.deleted, 0)
  assert.equal(cog?.keptRate, 1)
  assert.deepEqual(m.errors, [{ category: 'auth', count: 1 }])
})

test('aggregate:窗口外的记录不参与', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'gencanvas-traces-'))
  const store = new TraceStore(path.join(dir, 'traces.jsonl'))
  const old = trace({ jobId: 'old' })
  old.ts = new Date(Date.now() - 30 * 86_400_000).toISOString()
  await store.append(old)
  const m = await store.aggregate(7)
  assert.equal(m.generations.total, 0)
})
