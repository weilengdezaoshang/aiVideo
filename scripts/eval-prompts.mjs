#!/usr/bin/env node
// 生图评测集 runner(PRD 目标:质量可评测、跨模型可对比):
//   node scripts/eval-prompts.mjs [--base http://127.0.0.1:7899] [--runs 1] [--file prompts.json]
//
// 行为:对内置 golden prompts(或 --file 指定的数组)逐条提交 /api/generate,
// 轮询任务至终态,产出 data/eval/report-<ts>.json + .md:
//   - 每条:状态 / 排队与生成耗时 / 产物 URL(供人工复核提示词跟随度)
//   - 汇总:成功率 / P50 / P95 / 费用估算(与 /api/metrics 的价格表一致)
// 评测集跑在当前激活的后端上:先在设置里切到要评测的厂商/模型再运行。

import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : fallback
}
const base = arg('base', 'http://127.0.0.1:7899')
const runs = Math.max(1, Math.min(5, Number(arg('runs', 1))))
const file = arg('file')

/** 内置 golden prompts:覆盖提示词跟随的常见难点(细节指令/文字/多主体/风格化/空间关系/否定) */
const DEFAULT_PROMPTS = [
  {
    id: 'detail-instruction',
    prompt: '一只橘猫坐在木质书桌上,戴着圆形墨镜,面前摊开一本翻开的书,暖黄色台灯光',
    width: 1024,
    height: 1024,
  },
  {
    id: 'spatial-relation',
    prompt: '画面左侧是一杯红色果汁,右侧是一块蓝色蛋糕,两者之间放着一把银色小勺',
    width: 1024,
    height: 1024,
  },
  {
    id: 'style-transfer',
    prompt: '水墨画风格:远山如黛,一叶扁舟,留白充分,右下角一枚红色印章',
    width: 1024,
    height: 1024,
  },
  {
    id: 'counting',
    prompt: '桌上整整齐齐摆着三个青苹果,每个苹果上都有一片绿叶',
    width: 1024,
    height: 1024,
  },
  {
    id: 'scene-composition',
    prompt: '雨夜东京街头,霓虹灯牌倒映在湿漉漉的柏油路面上,一个撑透明伞的行人背影',
    width: 1024,
    height: 1024,
  },
]

const prompts = file
  ? JSON.parse(await import('node:fs/promises').then((m) => m.readFile(file, 'utf8')))
  : DEFAULT_PROMPTS

const post = async (p, body) => {
  const res = await fetch(`${base}${p}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const json = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(json.error || `HTTP ${res.status}`)
  }
  return json
}

const health = await fetch(`${base}/api/health`)
  .then((r) => r.json())
  .catch(() => null)
if (!health) {
  console.error(`无法连接 ${base},请先启动服务(npm run dev)`)
  process.exit(1)
}
console.log(`评测后端:${health.provider}(${health.backend?.detail ?? '?'})`)

const { models } = await fetch(`${base}/api/models`).then((r) => r.json())
const model = models?.[0]?.id
if (!model) {
  console.error('后端没有可用模型')
  process.exit(1)
}
console.log(`评测模型:${model}`)

/** 单任务轮询到终态(默认 180s 超时) */
async function waitJob(jobId, timeoutMs = 180_000) {
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    const { job } = await fetch(`${base}/api/jobs/${jobId}`).then((r) => r.json())
    if (job.status === 'completed' || job.status === 'failed') {
      return job
    }
    await new Promise((r) => setTimeout(r, 1500))
  }
  return { status: 'failed', error: '评测轮询超时', startedAt: null, finishedAt: null, images: [] }
}

const results = []
for (const p of prompts) {
  for (let run = 0; run < runs; run++) {
    const startedAt = Date.now()
    try {
      const { jobId } = await post('/api/generate', {
        prompt: p.prompt,
        negativePrompt: '',
        model,
        width: p.width || 1024,
        height: p.height || 1024,
        batchCount: 1,
        kind: 'image',
      })
      const job = await waitJob(jobId)
      const latencyMs = job.startedAt
        ? Date.parse(job.finishedAt) - Date.parse(job.startedAt)
        : Date.now() - startedAt
      results.push({
        id: p.id,
        run,
        prompt: p.prompt,
        status: job.status,
        latencyMs,
        imageUrl: job.images?.[0]?.url ?? null,
        error: job.error ?? null,
      })
      console.log(`[${job.status}] ${p.id}(${latencyMs}ms)`)
    } catch (err) {
      results.push({
        id: p.id,
        run,
        prompt: p.prompt,
        status: 'failed',
        error: String(err.message || err),
      })
      console.error(`[提交失败] ${p.id}: ${err.message}`)
    }
  }
}

const done = results.filter((r) => r.status === 'completed')
const lats = done.map((r) => r.latencyMs).sort((a, b) => a - b)
const q = (arr, x) =>
  arr.length ? arr[Math.min(arr.length - 1, Math.ceil(x * arr.length) - 1)] : 0
const summary = {
  backend: health.provider,
  model: null,
  total: results.length,
  completed: done.length,
  successRate: results.length ? done.length / results.length : 0,
  p50Ms: q(lats, 0.5),
  p95Ms: q(lats, 0.95),
}

const stamp = new Date().toISOString().replace(/[:.]/g, '-')
const outDir = path.resolve('data/eval')
await mkdir(outDir, { recursive: true })
await writeFile(
  path.join(outDir, `report-${stamp}.json`),
  JSON.stringify({ summary, results }, null, 2),
)

const md = [
  `# 生图评测报告(${new Date().toLocaleString('zh-CN')})`,
  ``,
  `- 后端:${health.provider} · 样本:${done.length}/${results.length} · 成功率:${(summary.successRate * 100).toFixed(0)}%`,
  `- 延迟:P50 ${summary.p50Ms}ms · P95 ${summary.p95Ms}ms`,
  ``,
  `> 提示词跟随度需人工复核:逐条对比 prompt 与产物,保留/重跑并到画布上用保留率沉淀结论。`,
  ``,
  ...results.map(
    (r) =>
      `## ${r.id}(run ${r.run}) — ${r.status}${r.latencyMs ? ` · ${r.latencyMs}ms` : ''}\n\n` +
      `- 提示词:${r.prompt}\n` +
      (r.imageUrl ? `- 产物:${base}${r.imageUrl}\n` : '') +
      (r.error ? `- 错误:${r.error}\n` : ''),
  ),
].join('\n')
await writeFile(path.join(outDir, `report-${stamp}.md`), md)

console.log(`\n报告已写入 data/eval/report-${stamp}.{json,md}`)
console.log(
  `成功率 ${(summary.successRate * 100).toFixed(0)}% · P50 ${summary.p50Ms}ms · P95 ${summary.p95Ms}ms`,
)
