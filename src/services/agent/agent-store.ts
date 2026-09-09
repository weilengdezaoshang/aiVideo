import { promises as fs } from 'node:fs'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { logger } from '../../logger.js'
import type { AgentPlan } from './agent-planner.js'

/**
 * Agent 会话存储(PRD §7 用例 A):计划、人工暂停点与结果跨刷新可恢复。
 * 全量 JSON + 防抖落盘(模式同任务快照);蒙版/结果本体存资产库,会话只存引用。
 *
 * 状态机:planned → preparing → waiting_mask →(用户点"抠图")→ completed
 *                                   ↘ failed
 * 不变量(验收 A10/A11):
 *   - prepare 只在 planned 状态执行一次,waiting_mask 后重复 prepare 直接返回现态
 *     (恢复后不自动识别第二次);
 *   - 完成(apply)只接受 waiting_mask 会话,且 assetId 必须与计划引用一致。
 */

export type AgentSessionStatus =
  | 'planned'
  | 'preparing'
  | 'waiting_mask'
  | 'completed'
  | 'failed'

export interface AgentSession {
  id: string
  docId: string
  /** 显式引用的源图资产(计划的事实来源;缺引用的请求不会建会话) */
  assetId: string
  assetExt: string
  request: string
  status: AgentSessionStatus
  plan: AgentPlan
  plannedBy: 'model' | 'rules'
  maskAssetId?: string
  resultAssetId?: string
  resultExt?: string
  error?: string
  createdAt: string
  updatedAt: string
}

const MAX_SESSIONS = 300
const FLUSH_DEBOUNCE_MS = 500

export class AgentStore {
  private sessions = new Map<string, AgentSession>()
  private loaded = false
  private timer: NodeJS.Timeout | null = null

  constructor(private file: string) {}

  async init(): Promise<void> {
    try {
      const raw = JSON.parse(await fs.readFile(this.file, 'utf8')) as { sessions?: unknown }
      if (Array.isArray(raw.sessions)) {
        for (const session of raw.sessions as AgentSession[]) {
          if (session && typeof session.id === 'string') {
            this.sessions.set(session.id, session)
          }
        }
      }
    } catch {
      // 首次启动 / 文件损坏:从空开始
    }
    this.loaded = true
    logger.debug('Agent 会话已加载', { count: this.sessions.size })
  }

  private assertLoaded(): void {
    if (!this.loaded) {
      throw new Error('AgentStore 未初始化')
    }
  }

  private markDirty(): void {
    if (this.timer) {
      return
    }
    this.timer = setTimeout(() => {
      this.timer = null
      void this.flush()
    }, FLUSH_DEBOUNCE_MS)
  }

  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
    const payload = JSON.stringify({ sessions: [...this.sessions.values()] })
    const tmp = `${this.file}.tmp`
    try {
      await fs.mkdir(path.dirname(this.file), { recursive: true })
      await fs.writeFile(tmp, payload)
      await fs.rename(tmp, this.file)
    } catch (err) {
      logger.error('Agent 会话写入失败', { file: this.file, err })
    }
  }

  create(input: {
    docId: string
    assetId: string
    assetExt: string
    request: string
    plan: AgentPlan
    plannedBy: 'model' | 'rules'
  }): AgentSession {
    this.assertLoaded()
    const now = new Date().toISOString()
    const session: AgentSession = {
      id: randomUUID(),
      docId: input.docId,
      assetId: input.assetId,
      assetExt: input.assetExt,
      request: input.request,
      status: 'planned',
      plan: input.plan,
      plannedBy: input.plannedBy,
      createdAt: now,
      updatedAt: now,
    }
    this.sessions.set(session.id, session)
    // 只保留最近 MAX_SESSIONS 条(新的在前裁剪)
    while (this.sessions.size > MAX_SESSIONS) {
      const oldest = [...this.sessions.values()].sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1))[0]
      this.sessions.delete(oldest.id)
    }
    this.markDirty()
    return session
  }

  get(id: string): AgentSession | undefined {
    this.assertLoaded()
    return this.sessions.get(id)
  }

  /** 某文档的会话,新的在前(刷新恢复用)。 */
  listByDoc(docId: string): AgentSession[] {
    this.assertLoaded()
    return [...this.sessions.values()]
      .filter((s) => s.docId === docId)
      .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
  }

  update(id: string, patch: Partial<Omit<AgentSession, 'id' | 'createdAt'>>): AgentSession | undefined {
    this.assertLoaded()
    const session = this.sessions.get(id)
    if (!session) {
      return undefined
    }
    Object.assign(session, patch, { updatedAt: new Date().toISOString() })
    this.markDirty()
    return session
  }
}
