#!/usr/bin/env node
/**
 * E2E 冒烟测试(零依赖,Node 20+,自带 fetch)。
 * 运行:node scripts/e2e-smoke.mjs
 * 依次请求本地服务,逐项输出 ✓/✗,最后汇总;任一失败以非零码退出。
 */
const BASE = process.env.SMOKE_BASE_URL ?? 'http://127.0.0.1:7801'
const MODEL = 'mock-diffusion-xl'

let passed = 0
let total = 0
const failures = []

/** 断言:条件不满足时抛出可读错误 */
function assert(cond, msg) {
  if (!cond) {
    throw new Error(msg)
  }
}

/** 单项检查:异常记为失败并继续后续检查,不中断整体流程 */
async function check(name, fn) {
  total += 1
  try {
    await fn()
    passed += 1
    console.log(`✓ ${name}`)
  } catch (err) {
    failures.push(name)
    console.log(`✗ ${name}:${err?.message ?? String(err)}`)
  }
}

/** 发请求并解析 JSON;raw 用于发送非法 JSON 原文 */
async function req(method, path, { body, raw } = {}) {
  const hasPayload = body !== undefined || raw !== undefined
  const res = await fetch(BASE + path, {
    method,
    headers: hasPayload ? { 'content-type': 'application/json' } : undefined,
    body: raw ?? (body !== undefined ? JSON.stringify(body) : undefined),
  })
  let data = null
  try {
    data = await res.json()
  } catch {
    // 响应不是 JSON 时保持 null,由调用方断言
  }
  return { status: res.status, data }
}

/** 轮询任务直至满足 done 条件或超时,返回最终 job */
async function waitJob(jobId, done, label, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const { data } = await req('GET', `/api/jobs/${jobId}`)
    const job = data?.job
    assert(job, `任务不存在:${jobId}`)
    if (done(job)) {
      return job
    }
    assert(job.status !== 'failed', `任务失败:${job.error}`)
    if (Date.now() > deadline) {
      throw new Error(
        `等待${label}超时(${timeoutMs}ms),status=${job.status} progress=${job.progress}`,
      )
    }
    await new Promise((r) => setTimeout(r, 200))
  }
}

// 跨检查共享的状态:txt2img 任务产物 / img2img 产物
let txtJobId = null
let txtImages = []
let img2imgImage = null

async function main() {
  console.log(`目标服务:${BASE}\n`)

  await check('GET /api/health:ok=true 且 provider=mock', async () => {
    const { status, data } = await req('GET', '/api/health')
    assert(status === 200, `期望 200,实际 ${status}`)
    assert(data?.ok === true, `ok 应为 true,实际 ${JSON.stringify(data?.ok)}`)
    assert(data?.provider === 'mock', `provider 应为 mock,实际 ${data?.provider}`)
  })

  await check('GET /api/models:models ≥3 项', async () => {
    const { status, data } = await req('GET', '/api/models')
    assert(status === 200, `期望 200,实际 ${status}`)
    const n = Array.isArray(data?.models) ? data.models.length : -1
    assert(n >= 3, `models 应 ≥3 项,实际 ${n}`)
  })

  await check('GET /api/samplers:samplers 与 schedulers 非空', async () => {
    const { status, data } = await req('GET', '/api/samplers')
    assert(status === 200, `期望 200,实际 ${status}`)
    assert(Array.isArray(data?.samplers) && data.samplers.length > 0, 'samplers 为空')
    assert(Array.isArray(data?.schedulers) && data.schedulers.length > 0, 'schedulers 为空')
  })

  await check('POST /api/generate 空 prompt:400 且含 error', async () => {
    const { status, data } = await req('POST', '/api/generate', {
      body: { prompt: '', model: MODEL },
    })
    assert(status === 400, `期望 400,实际 ${status}`)
    assert(Boolean(data?.error), '响应应包含 error 字段')
  })

  await check('POST /api/generate 非法 JSON:400', async () => {
    const { status } = await req('POST', '/api/generate', { raw: '{bad' })
    assert(status === 400, `期望 400,实际 ${status}`)
  })

  await check('GET /api/nonexistent:404 JSON 含 error', async () => {
    const { status, data } = await req('GET', '/api/nonexistent')
    assert(status === 404, `期望 404,实际 ${status}`)
    assert(Boolean(data?.error), '404 响应应包含 error 字段')
  })

  await check('POST /api/generate 合法 txt2img:202 且返回 jobId', async () => {
    const { status, data } = await req('POST', '/api/generate', {
      body: {
        prompt: '冒烟测试:月光下的猫',
        model: MODEL,
        steps: 4,
        batchCount: 2,
        seed: 123,
      },
    })
    assert(status === 202, `期望 202,实际 ${status}:${data?.error ?? ''}`)
    txtJobId = data?.jobId
    assert(txtJobId, '未返回 jobId')
  })

  await check('轮询任务完成:2 张图、种子 123/124、progress=1', async () => {
    assert(txtJobId, '前置检查未拿到 jobId')
    const job = await waitJob(txtJobId, (j) => j.status === 'completed', '任务完成')
    txtImages = job.images ?? []
    assert(txtImages.length === 2, `应恰好 2 张,实际 ${txtImages.length}`)
    const seeds = txtImages.map((i) => i.params?.seed).sort((a, b) => a - b)
    assert(seeds[0] === 123 && seeds[1] === 124, `种子应为 123/124,实际 ${JSON.stringify(seeds)}`)
    assert(job.progress === 1, `progress 应为 1,实际 ${job.progress}`)
  })

  await check('GET /api/history:最新两条为刚生成产物', async () => {
    const { status, data } = await req('GET', '/api/history')
    assert(status === 200, `期望 200,实际 ${status}`)
    const front = (data?.images ?? [])
      .slice(0, 2)
      .map((i) => i.id)
      .sort()
      .join(',')
    const mine = txtImages
      .map((i) => i.id)
      .sort()
      .join(',')
    assert(front === mine, `列表最前应为刚生成的 2 张,实际首两 id:${front}`)
  })

  await check('SSE /api/events:3 秒内收到 snapshot 事件', async () => {
    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(new Error('3 秒内未收到 event: snapshot')), 3000)
    try {
      const res = await fetch(`${BASE}/api/events`, { signal: ac.signal })
      assert(res.ok, `HTTP ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) {
          throw new Error('连接提前关闭')
        }
        buf += decoder.decode(value, { stream: true })
        if (buf.split('\n').some((line) => line.trim() === 'event: snapshot')) {
          return
        }
      }
    } catch (err) {
      const msg = err?.message ?? String(err)
      throw new Error(/snapshot/.test(msg) ? msg : `SSE 读取失败:${msg}`)
    } finally {
      clearTimeout(timer)
      ac.abort() // 命中或失败后主动断开连接
    }
  })

  await check('图生图:任务完成且图片 url 可访问', async () => {
    const { status, data } = await req('POST', '/api/generate', {
      body: {
        prompt: '冒烟测试:图生图重绘',
        model: MODEL,
        steps: 4,
        batchCount: 1,
        denoise: 0.5,
        initImage: 'data:image/png;base64,iVBORw0KGgo=',
      },
    })
    assert(status === 202, `期望 202,实际 ${status}:${data?.error ?? ''}`)
    const jobId = data?.jobId
    assert(jobId, '未返回 jobId')
    const job = await waitJob(jobId, (j) => j.status === 'completed', '图生图完成')
    assert(job.hasInitImage === true, 'hasInitImage 应为 true')
    const image = job.images?.[0]
    assert(image, '任务没有产出图片')
    assert(
      /^\/images\/.+\.svg$/.test(image.url),
      `图片 url 应形如 /images/xxx.svg,实际 ${image.url}`,
    )
    const file = await fetch(BASE + image.url)
    assert(file.status === 200, `图片 GET 期望 200,实际 ${file.status}`)
    await file.arrayBuffer() // 读空响应体,释放连接
    img2imgImage = image
  })

  await check('DELETE /api/images/:id:history 消失且文件 404', async () => {
    assert(img2imgImage, '前置检查未产出图生图图片')
    const del = await req('DELETE', `/api/images/${img2imgImage.id}`)
    assert(del.status === 200, `期望 200,实际 ${del.status}:${del.data?.error ?? ''}`)
    const { data } = await req('GET', '/api/history?limit=500')
    const still = (data?.images ?? []).some((i) => i.id === img2imgImage.id)
    assert(!still, 'history 中仍存在该记录')
    const file = await fetch(BASE + img2imgImage.url)
    assert(file.status === 404, `删除后文件应 404,实际 ${file.status}`)
  })

  await check('取消任务:status=failed、error=已取消、不在活动列表', async () => {
    const { status, data } = await req('POST', '/api/generate', {
      body: { prompt: '冒烟测试:待取消任务', model: MODEL, steps: 150, batchCount: 4 },
    })
    assert(status === 202, `期望 202,实际 ${status}`)
    const jobId = data?.jobId
    assert(jobId, '未返回 jobId')
    const del = await req('DELETE', `/api/jobs/${jobId}`)
    assert(del.status === 200, `取消请求期望 200,实际 ${del.status}:${del.data?.error ?? ''}`)
    const job = await waitJob(jobId, (j) => j.status === 'failed', '任务取消', 15000)
    assert(job.error === '已取消', `error 应为 已取消,实际 "${job.error}"`)
    const act = await req('GET', '/api/jobs')
    const ids = (act.data?.jobs ?? []).map((j) => j.id)
    assert(!ids.includes(jobId), '活动任务列表中仍包含已取消任务')
  })

  console.log(`\n汇总:通过 ${passed} / ${total}`)
  if (failures.length > 0) {
    console.log(`失败项:${failures.join('、')}`)
    process.exitCode = 1
  }
}

main().catch((err) => {
  console.error(`脚本异常退出:${err?.stack ?? err}`)
  process.exitCode = 1
})
