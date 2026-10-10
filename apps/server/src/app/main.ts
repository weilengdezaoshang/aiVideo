import { serve } from '@hono/node-server'
import { createApplication } from './create-app.js'
const port = Number(process.env.PORT ?? 8787)
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('PORT 必须为有效端口')
}
const creditThreshold = Number(process.env.INTERRUPT_CREDITS_THRESHOLD ?? 50)
const imageCredits = Number(process.env.MOCK_IMAGE_CREDITS ?? 40)
if (![creditThreshold, imageCredits].every((value) => Number.isFinite(value) && value >= 0)) {
  throw new Error('积分配置必须是非负数')
}
const queueDriver = process.env.QUEUE_DRIVER ?? 'memory'
const rateDriver = process.env.RATE_LIMIT_DRIVER ?? 'memory'
if (queueDriver !== 'memory' || rateDriver !== 'memory') {
  throw new Error('BullMQ/Redis 为显式未实现的扩展骨架；本地请使用 memory 异步队列，不能静默回退')
}
function positive(name: string, fallback: number) {
  const value = Number(process.env[name] ?? fallback)
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${name} 必须为正整数`)
  }
  return value
}
const { api } = createApplication({
  queueOptions: {
    async: true,
    timeoutMs: positive('JOB_TIMEOUT_MS', 30000),
    maxPending: positive('QUEUE_MAX_PENDING', 32),
    retentionMs: positive('QUEUE_RETENTION_MS', 86400000),
    maxRecords: positive('QUEUE_MAX_RECORDS', 10000),
  },
  rateOptions: {
    requests: positive('RATE_LIMIT_REQUESTS', 60),
    windowMs: positive('RATE_LIMIT_WINDOW_MS', 60000),
    concurrency: positive('MAX_RUNNING_TURNS', 2),
    maxKeys: positive('RATE_LIMIT_MAX_KEYS', 10000),
  },
  creditThreshold,
  imageCredits,
  publicBaseUrl: process.env.PUBLIC_BASE_URL ?? `http://localhost:${port}`,
  traceDirectory: process.env.TRACE_DIR,
  origins: process.env.CORS_ORIGINS?.split(',').map((origin) => origin.trim()),
})
const server = serve({ fetch: api.fetch, port, hostname: '127.0.0.1' }, (address) => {
  console.log(`创作 Host mock 服务：http://127.0.0.1:${address.port}`)
})
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.once(signal, () => {
    server.close(() => process.exit(0))
  })
}
