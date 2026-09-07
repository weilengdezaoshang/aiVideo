import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { logger } from './logger.js'

/**
 * 生成链路追踪(PRD 目标:质量可评测、行为可追踪):
 * 每次生成落一条 JSONL(追加写,零迁移成本);删除反馈作为独立事件,
 * 通过 imageId 与生成记录关联,聚合出"保留率"这一隐式质量信号。
 *
 * 隐私与体积:追踪里只存 promptHash(sha256 前 12 位)与长度,完整提示词
 * 已在 history.json,两侧用 imageId 关联。
 */

export type TraceStatus = 'completed' | 'failed' | 'cancelled'
export type ErrorCategory =
  | 'cancelled'
  | 'timeout'
  | 'auth'
  | 'rate_limit'
  | 'invalid_request'
  | 'server_error'
  | 'network'
  | 'unknown'

export interface GenTrace {
  ts: string
  kind: 'trace'
  jobId: string
  clientRef?: string
  provider: string
  /** 云厂商(provider === 'cloud' 时有值) */
  vendor?: string
  model: string
  /** 预留 agent 编排层:直通= user;agent 改写= agent(可分别评测) */
  origin: 'user' | 'agent'
  promptHash: string
  promptChars: number
  seed: number
  width: number
  height: number
  kind_of_media: 'image' | 'video'
  status: TraceStatus
  /** 提交 → 开始(排队时长) */
  queueMs: number
  /** 开始 → 结束(纯生成时长) */
  latencyMs: number
  imageId?: string
  /** 记录时可冗余;聚合以模型现算为准 */
  costCny?: number
  errorCategory?: ErrorCategory
  errorMessage?: string
}

export interface FeedbackTrace {
  ts: string
  kind: 'feedback'
  action: 'deleted'
  imageId: string
  model?: string
}

export type TraceRecord = GenTrace | FeedbackTrace

/** 估算单价(元/张):模糊匹配模型名;未收录的模型不计费(以厂商账单为准) */
const COST_PER_IMAGE_CNY: Array<[RegExp, number]> = [
  [/cogview-4/i, 0.06],
  [/FLUX\.1-schnell/i, 0.01],
  [/FLUX\.1-dev/i, 0.25],
  [/gpt-image/i, 0.25],
]

export function estimateCostCny(model: string): number | undefined {
  for (const [pattern, price] of COST_PER_IMAGE_CNY) {
    if (pattern.test(model)) {
      return price
    }
  }
  return undefined
}

/** 错误消息 → 分类(用于错误分布与厂商问题定位) */
export function classifyError(message: string): ErrorCategory {
  const m = message || ''
  if (/已取消/.test(m)) {
    return 'cancelled'
  }
  if (/超时|timeout/i.test(m)) {
    return 'timeout'
  }
  if (/401|403|未授权|API Key|Authorization/i.test(m)) {
    return 'auth'
  }
  if (/429|限流|rate.?limit/i.test(m)) {
    return 'rate_limit'
  }
  if (/400|参数|invalid|不存在/i.test(m)) {
    return 'invalid_request'
  }
  if (/HTTP 5|500|502|503|服务器/i.test(m)) {
    return 'server_error'
  }
  if (/fetch|network|ECONN|ENOTFOUND|下载失败/i.test(m)) {
    return 'network'
  }
  return 'unknown'
}

export function promptHash(prompt: string): string {
  return createHash('sha256').update(prompt).digest('hex').slice(0, 12)
}

const isGenTrace = (r: TraceRecord): r is GenTrace => r.kind === 'trace'

export interface ModelStats {
  model: string
  total: number
  completed: number
  failed: number
  cancelled: number
  successRate: number
  p50LatencyMs: number
  p95LatencyMs: number
  costCny: number
  deleted: number
  keptRate: number
}

export interface MetricsSummary {
  windowDays: number
  generations: {
    total: number
    completed: number
    failed: number
    cancelled: number
    successRate: number
  }
  latency: { p50Ms: number; p95Ms: number }
  queue: { p50Ms: number }
  cost: { totalCny: number; knownCount: number }
  byModel: ModelStats[]
  errors: Array<{ category: string; count: number }>
  feedback: { deleted: number }
}

const quantile = (sorted: number[], q: number): number => {
  if (sorted.length === 0) {
    return 0
  }
  // 下界取值:样本少时分位不放大(2 样本的 p50 = 较小值)
  const idx = Math.max(0, Math.ceil(q * sorted.length) - 1)
  return sorted[idx]
}

export class TraceStore {
  constructor(private filePath: string) {}

  /** 追加一条记录(单行 JSON);写失败只告警,不阻塞生成主流程 */
  async append(record: TraceRecord): Promise<void> {
    try {
      await fs.mkdir(path.dirname(this.filePath), { recursive: true })
      await fs.appendFile(this.filePath, `${JSON.stringify(record)}\n`, 'utf8')
      logger.debug('追踪已记录', { kind: record.kind })
    } catch (err) {
      logger.warn('追踪写入失败(不影响生成)', { err })
    }
  }

  async readAll(): Promise<TraceRecord[]> {
    let raw: string
    try {
      raw = await fs.readFile(this.filePath, 'utf8')
    } catch {
      return []
    }
    const records: TraceRecord[] = []
    for (const line of raw.split('\n')) {
      if (!line.trim()) {
        continue
      }
      try {
        records.push(JSON.parse(line) as TraceRecord)
      } catch {
        // 半截行(崩溃残留):跳过
      }
    }
    return records
  }

  /** 聚合最近 windowDays 天的生成与反馈 */
  async aggregate(windowDays = 7): Promise<MetricsSummary> {
    const since = Date.now() - windowDays * 86_400_000
    const records = (await this.readAll()).filter((r) => {
      const t = Date.parse(r.ts)
      return Number.isFinite(t) && t >= since
    })
    const traces = records.filter(isGenTrace)
    const deletedByImage = new Map<string, number>()
    for (const r of records) {
      if (r.kind === 'feedback' && r.action === 'deleted') {
        deletedByImage.set(r.imageId, (deletedByImage.get(r.imageId) || 0) + 1)
      }
    }

    const completed = traces.filter((t) => t.status === 'completed')
    const failed = traces.filter((t) => t.status === 'failed')
    const cancelled = traces.filter((t) => t.status === 'cancelled')
    const latencies = completed.map((t) => t.latencyMs).sort((a, b) => a - b)
    const queues = completed.map((t) => t.queueMs).sort((a, b) => a - b)
    // 费用聚合时按模型现算:价格表更新可对历史追踪追溯生效
    const costOf = (t: GenTrace): number | undefined => estimateCostCny(t.model)
    const costTotal = completed.reduce((sum, t) => sum + (costOf(t) ?? 0), 0)
    const knownCount = completed.filter((t) => costOf(t) != null).length

    // 按模型聚合
    const byModelMap = new Map<string, GenTrace[]>()
    for (const t of traces) {
      const list = byModelMap.get(t.model) || []
      list.push(t)
      byModelMap.set(t.model, list)
    }
    const byModel: ModelStats[] = [...byModelMap.entries()]
      .map(([model, list]) => {
        const done = list.filter((t) => t.status === 'completed')
        const lats = done.map((t) => t.latencyMs).sort((a, b) => a - b)
        const deleted = done.reduce(
          (sum, t) => sum + (t.imageId ? deletedByImage.get(t.imageId) || 0 : 0),
          0,
        )
        const keptRate = done.length > 0 ? 1 - Math.min(1, deleted / done.length) : 1
        return {
          model,
          total: list.length,
          completed: done.length,
          failed: list.filter((t) => t.status === 'failed').length,
          cancelled: list.filter((t) => t.status === 'cancelled').length,
          successRate: list.length > 0 ? done.length / list.length : 0,
          p50LatencyMs: quantile(lats, 0.5),
          p95LatencyMs: quantile(lats, 0.95),
          costCny: done.reduce((sum, t) => sum + (costOf(t) ?? 0), 0),
          deleted,
          keptRate,
        }
      })
      .sort((a, b) => b.total - a.total)

    const errorMap = new Map<string, number>()
    for (const t of failed) {
      const cat = t.errorCategory || 'unknown'
      errorMap.set(cat, (errorMap.get(cat) || 0) + 1)
    }

    return {
      windowDays,
      generations: {
        total: traces.length,
        completed: completed.length,
        failed: failed.length,
        cancelled: cancelled.length,
        successRate: traces.length > 0 ? completed.length / traces.length : 0,
      },
      latency: { p50Ms: quantile(latencies, 0.5), p95Ms: quantile(latencies, 0.95) },
      queue: { p50Ms: quantile(queues, 0.5) },
      cost: { totalCny: costTotal, knownCount },
      byModel,
      errors: [...errorMap.entries()]
        .map(([category, count]) => ({ category, count }))
        .sort((a, b) => b.count - a.count),
      feedback: { deleted: records.filter((r) => r.kind === 'feedback').length },
    }
  }
}
