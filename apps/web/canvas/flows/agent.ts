// 创作助手流程(PRD §7 首条 Agent 用例 A):
//   显式引用一张图 → 提出需求 → 计划卡 → 服务端自动准备蒙版(最多一次)→
//   等待用户在画布检查蒙版并点击蒙版栏"抠图" → 结果落位 + 结果卡。
// 边界:聊天侧任何入口都不绕过"抠图"按钮;"检查主体选区"只定位源图与已有蒙版;
// 刷新后从服务端会话恢复状态,不自动重提、不二次识别。

import { ensureAsset, objectThumbUrl } from './asset-util.js'
import type { DocStore } from '../state/doc-store.js'
import type { Chat } from './chat.js'

export type AgentPlan = {
  steps?: { label: string }[]
  [key: string]: unknown
}

export type AgentSession = {
  id: string
  status: 'planned' | 'waiting_mask' | 'completed' | 'failed'
  plannedBy?: 'model' | 'rules'
  plan?: AgentPlan
  request?: string
  error?: string
  resultAssetId?: string
  maskAssetId?: string
}

export type AgentFlow = {
  send(input: {
    request: string
    refObjId?: string | null
  }): Promise<{ ok: boolean; busy?: boolean }>
  recover(): Promise<void>
  markCompleted(sessionId: string, assetUrl: string, objId?: string): void
}

export function createAgentFlow({
  docStore,
  chat,
  fetchImpl = (...args) => fetch(...args),
}: {
  docStore: DocStore
  chat: Chat
  fetchImpl?: typeof fetch
}): AgentFlow {
  // 执行中暂停第二个执行请求(PRD §7):避免上下文与计划交错;
  // 停止/等待期间不删除任何已完成产物。
  let running = false

  async function api(path: string, init?: RequestInit) {
    const res = await fetchImpl(path, init)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 服务端错误体结构不固定,仅透传展示
    const body = (await res.json().catch(() => ({}))) as Record<string, any>
    return { ok: res.ok, status: res.status, body } as {
      ok: boolean
      status: number
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 服务端错误体结构不固定,仅透传展示
      body: Record<string, any>
    }
  }

  /** 助手消息入口(底部输入区"创作助手"模式)。 */
  async function send({ request, refObjId }: { request: string; refObjId?: string | null }) {
    const text = request.trim()
    if (!text) {
      return { ok: false }
    }
    if (running) {
      chat.addSystem('助手正在执行上一个任务;完成后再发送下一条,避免计划交错。')
      return { ok: false, busy: true }
    }
    running = true
    try {
      return await runSend(text, refObjId)
    } finally {
      running = false
    }
  }

  async function runSend(text: string, refObjId: string | null | undefined) {
    chat.addUser(text)
    const refObj = refObjId ? docStore.doc.objects[refObjId] : null
    let assetId: string | undefined
    if (refObj && refObj.kind === 'image') {
      try {
        chat.addSystem('正在引用所选图片…')
        const asset = await ensureAsset(refObj)
        assetId = asset.id
      } catch (err) {
        chat.addError(`引用图片失败:${err instanceof Error ? err.message : String(err)}`)
        return { ok: false }
      }
    }
    const created = await api('/api/agent/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ docId: docStore.doc.id, assetId, request: text }),
    })
    if (created.status === 422) {
      // 零工具调用的澄清路径:缺引用 / 超范围 / 含糊
      chat.addError(created.body.error || '这个请求需要先澄清,助手没有执行任何操作。')
      return { ok: false }
    }
    if (!created.ok) {
      chat.addError(created.body.error || `助手服务不可用(HTTP ${created.status})`)
      return { ok: false }
    }
    const session = created.body.session as AgentSession
    chat.addPlanCard(session.id, session.plan, text, refObj ? objectThumbUrl(refObj) : '')
    chat.addSystem(
      `计划已就绪(${session.plannedBy === 'model' ? '模型规划' : '规则规划'}),准备蒙版中…`,
      // 挂到同会话,后续状态更新合并展示
    )
    chat.updateSession(session.id, { planObjId: refObjId ?? undefined })
    return prepareMask(session.id, refObjId)
  }

  /** 准备蒙版(幂等:服务端只在 planned 状态识别一次)。 */
  async function prepareMask(sessionId: string, refObjId: string | null | undefined) {
    const prepared = await api(`/api/agent/sessions/${sessionId}/prepare`, { method: 'POST' })
    const session = prepared.body?.session as AgentSession | undefined
    if (!prepared.ok) {
      chat.addError(prepared.body?.error || '主体识别失败,可稍后重试')
      return { ok: false }
    }
    if (session?.status === 'waiting_mask') {
      chat.updateSession(sessionId, {
        waitingMask: true,
        objId: refObjId ?? undefined,
        maskUrl: prepared.body?.urls?.original,
      })
      chat.addSystem('蒙版已就绪。请在画布检查主体选区,点击蒙版栏"抠图"继续。')
      return { ok: true, waiting: true }
    }
    if (session?.status === 'completed') {
      // 已完成(恢复场景):补结果卡
      markCompleted(sessionId, session.resultAssetId ?? '')
      return { ok: true, completed: true }
    }
    if (session?.status === 'failed') {
      chat.addError(session.error || '主体识别失败')
      return { ok: false }
    }
    return { ok: true }
  }

  /** 用户点击蒙版栏"抠图"后,由 cutout 流程回调:回写结果。 */
  function markCompleted(sessionId: string, assetUrl: string, objId?: string) {
    chat.updateSession(sessionId, { waitingMask: false, completed: true })
    chat.addResultCard({
      objId,
      assetUrl: assetUrl || '',
      sourceLabel: '对话引用图片',
      sessionId,
    })
  }

  /** 刷新/重启恢复(PRD A11):以服务端会话校准对话卡,不重提、不二次识别。 */
  async function recover() {
    try {
      const res = await fetchImpl(
        `/api/agent/sessions?docId=${encodeURIComponent(docStore.doc.id)}`,
      )
      if (!res.ok) {
        return
      }
      const { sessions } = (await res.json()) as { sessions?: AgentSession[] }
      for (const session of sessions || []) {
        const history = docStore.doc.chat || []
        const known = history.find((m) => m.sessionId === session.id)
        const hasResult = history.some(
          (m) => m.sessionId === session.id && (m.kind === 'result' || m.completed),
        )
        if (!known) {
          // 有会话历史但消息缺失(如保存失败):重建计划卡
          chat.addPlanCard(session.id, session.plan, session.request || '')
        }
        const objId = known?.objId || known?.planObjId
        if (session.status === 'waiting_mask') {
          chat.updateSession(session.id, {
            waitingMask: true,
            objId,
            maskUrl: session.maskAssetId
              ? `/assets/${session.maskAssetId}/original.png`
              : undefined,
          })
        } else if (session.status === 'completed' && !hasResult) {
          // 只补缺失的结果卡;已呈现过的会话不重复追加(每次刷新各来一张)
          markCompleted(
            session.id,
            session.resultAssetId ? `/assets/${session.resultAssetId}/t256.webp` : '',
            objId,
          )
        }
      }
    } catch {
      // 恢复失败不阻塞编辑
    }
  }

  return { send, recover, markCompleted }
}
