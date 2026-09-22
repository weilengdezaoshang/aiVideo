import { nodeRequest } from './node-request.js'
import type { createDocStore } from '../state/doc-store.js'
import type { CanvasObj } from '../state/commands.js'
import { cmdUpdateObject } from '../state/commands.js'
import { applyNodeJob, isBusy, isMediaNode } from '../state/node-model.js'
import type { NodeJob, NodeDraft } from '../state/node-model.js'

type Store = ReturnType<typeof createDocStore>
export function createNodeGeneration(store: Store, notify: (message: string) => void) {
  const inflight = new Set<string>()
  const cancelling = new Set<string>()
  const cancellationKey = `gencanvas.cancelledNodeRequests.${store.doc.id}`
  let cancellations = new Set<string>()
  try {
    cancellations = new Set(JSON.parse(localStorage.getItem(cancellationKey) || '[]') as string[])
  } catch {
    /* storage unavailable */
  }
  let previous = new Map(
    Object.values(store.doc.objects)
      .filter((obj) => obj.nodeRun)
      .map((obj) => [obj.id, obj.nodeRun!.requestId]),
  )
  const endpoint = (requestId: string) =>
    `/api/generation-requests/${requestId}?documentId=${encodeURIComponent(store.doc.id)}`
  const remember = () => {
    try {
      localStorage.setItem(cancellationKey, JSON.stringify([...cancellations]))
    } catch {
      /* best effort */
    }
  }

  async function cancelRequest(requestId: string) {
    if (cancelling.has(requestId)) {
      return
    }
    cancelling.add(requestId)
    cancellations.add(requestId)
    remember()
    try {
      const response = await fetch(endpoint(requestId), { method: 'DELETE' })
      if (!response.ok) {
        throw new Error('取消尚未确认，将在连接恢复后重试')
      }
      cancellations.delete(requestId)
      remember()
    } catch {
      notify('取消尚未确认，将在连接恢复后重试')
    } finally {
      cancelling.delete(requestId)
    }
  }
  function apply(job: NodeJob) {
    if (job.requestId && cancellations.has(job.requestId)) {
      return
    }
    const obj = job.clientRef ? store.doc.objects[job.clientRef] : undefined
    if (!obj) {
      return
    }
    // 分镜服务端执行的结果由执行控制器按镜头归属统一回填，这里只同步进度。
    if (job.status === 'completed' && obj.nodeRun?.runId) {
      return
    }
    store.mutateTransient(() => applyNodeJob(obj, job, store.doc.id))
  }
  // 分镜服务端执行曾归属的请求集合：节点删除/撤销后也不得在此自动取消。
  const runOwnedRequests = new Set<string>()
  const unsubscribe = store.subscribe(() => {
    const current = new Map<string, string>()
    for (const obj of Object.values(store.doc.objects)) {
      if (obj.nodeRun) {
        current.set(obj.id, obj.nodeRun.requestId)
        if (obj.nodeRun.runId) {
          runOwnedRequests.add(obj.nodeRun.requestId)
        }
      }
    }
    for (const [id, requestId] of previous) {
      if (current.get(id) !== requestId && !runOwnedRequests.has(requestId)) {
        const obj = store.doc.objects[id]
        // Successful result publication clears the run and sets an output in the same mutation.
        if (!obj || (!obj.src && !obj.assetId)) {
          void cancelRequest(requestId)
        }
      }
    }
    const restored = [...current].some(([id, rid]) => previous.get(id) !== rid)
    previous = current
    if (restored) {
      queueMicrotask(() => {
        void reconcile()
      })
    }
  })

  async function send(obj: CanvasObj, requestId: string, draft: NodeDraft) {
    if (inflight.has(requestId)) {
      return
    }
    inflight.add(requestId)
    try {
      const response = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(nodeRequest(store.doc.id, obj, requestId, draft)),
      })
      const result = (await response.json()) as { job?: NodeJob; error?: string }
      const current = store.doc.objects[obj.id]
      if (!current || current.nodeRun?.requestId !== requestId) {
        void cancelRequest(requestId)
        return
      }
      if (!response.ok || !result.job) {
        if (response.status >= 500) {
          throw new Error(result.error || '服务暂不可用')
        }
        store.mutateTransient(() => {
          current.nodeRun = {
            ...current.nodeRun!,
            status: 'failed',
            message: result.error || '提交失败',
          }
        })
      } else {
        apply(result.job)
      }
    } catch {
      const current = store.doc.objects[obj.id]
      if (current?.nodeRun?.requestId === requestId) {
        store.mutateTransient(() => {
          current.nodeRun = {
            ...current.nodeRun!,
            status: 'uncertain',
            message: '连接中断，点击确认结果，不会重复生成',
          }
        })
      }
    } finally {
      inflight.delete(requestId)
    }
  }
  async function submit(obj: CanvasObj, onSubmitted?: (requestId: string) => void) {
    if (!isMediaNode(obj) || !obj.nodeDraft.prompt.trim()) {
      return
    }
    if (obj.nodeRun?.status === 'uncertain') {
      await send(obj, obj.nodeRun.requestId, obj.nodeRun.snapshot)
      return
    }
    if (isBusy(obj)) {
      return
    }
    const snapshot = structuredClone(obj.nodeDraft)
    const requestId = crypto.randomUUID()
    store.apply(
      cmdUpdateObject(
        obj.id,
        { nodeRun: { requestId, status: 'submitting', snapshot, message: '提交中' } },
        { nodeRun: obj.nodeRun },
      ),
    )
    onSubmitted?.(requestId)
    await send(obj, requestId, snapshot)
  }
  async function reconcile() {
    await Promise.all([...cancellations].map(cancelRequest))
    await Promise.all(
      Object.values(store.doc.objects)
        .filter((obj) => obj.nodeRun)
        .map(async (obj) => {
          const run = obj.nodeRun!
          if (inflight.has(run.requestId)) {
            return
          }
          try {
            const response = await fetch(endpoint(run.requestId))
            const result = (await response.json()) as { job?: NodeJob; cancelled?: boolean }
            if (store.doc.objects[obj.id]?.nodeRun?.requestId !== run.requestId) {
              return
            }
            if (result.job) {
              apply(result.job)
            } else if (result.cancelled) {
              store.mutateTransient(() => {
                delete obj.nodeRun
              })
            } else if (response.status === 404) {
              if (run.runId) {
                // 分镜执行尚未提交该请求：真伪由执行控制器判断，不标记不确定
                return
              }
              store.mutateTransient(() => {
                obj.nodeRun = { ...run, status: 'uncertain', message: '尚未确认提交，点击确认结果' }
              })
            }
          } catch {
            /* EventSource retries; do not silently re-submit generation */
          }
        }),
    )
  }
  const events = new EventSource('/api/events')
  events.addEventListener('job', (event) => {
    try {
      apply(JSON.parse((event as MessageEvent<string>).data) as NodeJob)
    } catch {
      /* malformed event */
    }
  })
  events.addEventListener('error', () => {
    const pending = Object.values(store.doc.objects).filter(
      (obj) =>
        obj.nodeRun && !obj.nodeRun.runId && ['queued', 'running'].includes(obj.nodeRun.status),
    )
    if (pending.length) {
      store.mutateTransient(() => {
        for (const obj of pending) {
          obj.nodeRun = {
            ...obj.nodeRun!,
            status: 'uncertain',
            message: '连接中断，正在恢复结果；也可点击确认结果',
          }
        }
      })
    }
  })
  events.addEventListener('open', () => {
    void reconcile()
  })
  const online = () => {
    void reconcile()
  }
  window.addEventListener('online', online)
  return {
    submit,
    cancel(obj: CanvasObj) {
      const requestId = obj.nodeRun?.requestId
      if (!requestId) {
        return
      }
      // Manual cancellation is transient: redo must never resubmit a cancelled paid request.
      store.mutateTransient(() => {
        delete obj.nodeRun
      })
      if (!cancellations.has(requestId)) {
        void cancelRequest(requestId)
      }
    },
    dispose() {
      unsubscribe()
      events.close()
      window.removeEventListener('online', online)
    },
  }
}
