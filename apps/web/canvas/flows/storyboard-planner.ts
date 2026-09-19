import { isStoryboard, type Storyboard } from '../state/storyboard.js'

type PlanningRequest = { id: string; documentId: string; prompt: string; workflow: string }
type PlanningState = { busy: boolean; uncertain: boolean; message: string; storyboard?: Storyboard }

/** 页面关闭只停止查询，服务端规划继续；不确定提交复用相同请求标识。 */
export function createStoryboardPlanner(
  documentId: string,
  changed: (state: PlanningState) => void,
) {
  const key = `gencanvas.storyboardJob.${documentId}`
  let request: PlanningRequest | null = null
  let state: PlanningState = { busy: false, uncertain: false, message: '' }
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const operation = new AbortController()
  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null') as PlanningRequest | null
    if (
      saved &&
      saved.documentId === documentId &&
      typeof saved.id === 'string' &&
      typeof saved.prompt === 'string' &&
      typeof saved.workflow === 'string'
    ) {
      request = saved
      state = { busy: true, uncertain: false, message: '正在恢复规划任务…' }
    }
  } catch {
    /* 损坏的本地记录不自动提交 */
  }
  function publish(next: PlanningState) {
    state = next
    if (!disposed) {
      changed(state)
    }
  }
  function remember() {
    try {
      if (request) {
        localStorage.setItem(key, JSON.stringify(request))
      } else {
        localStorage.removeItem(key)
      }
    } catch {
      /* 存储不可用时继续当前查询 */
    }
  }
  function later() {
    if (!disposed) {
      timer = setTimeout(() => void poll(), 1500)
    }
  }
  async function show(response: Response) {
    const payload = (await response.json()) as {
      job?: { id: string; documentId: string; status: string; storyboard?: unknown; error?: string }
    }
    const job = payload.job
    if (!response.ok || !job || job.id !== request?.id || job.documentId !== documentId) {
      throw new Error('规划状态暂不可用')
    }
    if (disposed) {
      return
    }
    if (job.status === 'completed' && isStoryboard(job.storyboard)) {
      publish({
        busy: false,
        uncertain: false,
        message: '规划完成，请检查后追加分镜',
        storyboard: job.storyboard,
      })
      request = null
      remember()
    } else if (job.status === 'failed' || job.status === 'cancelled') {
      publish({ busy: false, uncertain: false, message: job.error || '规划未完成' })
      request = null
      remember()
    } else if (job.status === 'queued' || job.status === 'running') {
      publish({
        busy: true,
        uncertain: false,
        message:
          job.status === 'queued' ? '规划排队中…' : '文本模型正在规划，关闭页面后仍可恢复结果',
      })
      later()
    } else {
      throw new Error('规划结果格式无效')
    }
  }
  async function poll() {
    if (!request || disposed) {
      return
    }
    clearTimeout(timer)
    try {
      const response = await fetch(`/api/storyboards/jobs/${encodeURIComponent(request.id)}`, {
        signal: operation.signal,
      })
      if (response.status === 404) {
        publish({
          busy: false,
          uncertain: true,
          message: '未查到规划任务，可确认原请求；将复用同一标识避免重复规划',
        })
        return
      }
      await show(response)
    } catch {
      if (!disposed) {
        publish({ busy: true, uncertain: true, message: '规划状态暂不可用，正在查询原任务…' })
        later()
      }
    }
  }
  if (request) {
    queueMicrotask(() => {
      if (!disposed) {
        changed(state)
        void poll()
      }
    })
  }
  return {
    get state() {
      return state
    },
    async restore(ident: string) {
      if (disposed || state.busy || request) {
        return
      }
      publish({ busy: true, uncertain: false, message: '正在读取历史规划…' })
      try {
        const response = await fetch(`/api/storyboards/jobs/${encodeURIComponent(ident)}`, {
          signal: operation.signal,
        })
        const payload = (await response.clone().json()) as { job?: PlanningRequest }
        const job = payload.job
        if (
          !response.ok ||
          !job ||
          job.id !== ident ||
          job.documentId !== documentId ||
          typeof job.prompt !== 'string' ||
          typeof job.workflow !== 'string'
        ) {
          throw new Error('历史规划不存在或不属于当前项目')
        }
        if (disposed) {
          return
        }
        request = { id: job.id, documentId, prompt: job.prompt, workflow: job.workflow }
        remember()
        await show(response)
      } catch {
        if (!disposed) {
          if (request) {
            void poll()
          } else {
            publish({ busy: false, uncertain: false, message: '无法读取历史规划，请重试' })
          }
        }
      }
    },
    async start(prompt: string, workflow: string) {
      if (state.busy || disposed) {
        return
      }
      request ||= { id: crypto.randomUUID(), documentId, prompt, workflow }
      remember()
      publish({ busy: true, uncertain: false, message: '正在提交规划…' })
      try {
        const response = await fetch('/api/storyboards/jobs', {
          method: 'POST',
          signal: operation.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(request),
        })
        if ([400, 409, 429].includes(response.status)) {
          const error = (await response.json()) as { error?: string; detail?: string }
          request = null
          remember()
          publish({
            busy: false,
            uncertain: false,
            message: error.error || error.detail || '规划提交失败',
          })
          return
        }
        await show(response)
      } catch {
        if (!disposed) {
          void poll()
        }
      }
    },
    async cancel() {
      if (!request || disposed) {
        return
      }
      clearTimeout(timer)
      try {
        await show(
          await fetch(`/api/storyboards/jobs/${encodeURIComponent(request.id)}`, {
            method: 'DELETE',
            signal: operation.signal,
          }),
        )
      } catch {
        if (!disposed) {
          void poll()
        }
      }
    },
    dispose() {
      disposed = true
      clearTimeout(timer)
      operation.abort()
    },
  }
}
