import type { DocStore } from '../state/doc-store.js'
import { applyNodeJob, type NodeDraft, type NodeJob } from '../state/node-model.js'
import { nodeRequest } from './node-request.js'
import { appendRunStoryboard } from './storyboard-timeline.js'
import { videoDuration } from './media-duration.js'

type RunStepRequest = { requestId: string; clientRef: string; documentId: string } & Record<
  string,
  unknown
>
export type RunStep = {
  shotId: string
  draft: NodeDraft
  request: RunStepRequest
  jobId?: string | null
  attempts?: { requestId?: string; jobId?: string | null; status?: string | null }[]
}
export type RunJob = {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'unknown'
  error?: string
  message?: string
  images?: {
    id: string
    url: string
    file: string
    params: { kind: string; width: number; height: number }
  }[]
}
export type ServerRun = {
  id: string
  documentId: string
  status: 'running' | 'paused' | 'completed' | 'cancelled'
  index: number
  message: string
  steps: RunStep[]
  jobs?: RunJob[]
}
export type UnmatchedResult = { index: number; title: string; url: string; reason: string }
export type StoryboardRunState = {
  run: ServerRun | null
  busy: boolean
  uncertain: boolean
  message: string
  legacy: string
  unmatched: UnmatchedResult[]
  assembleWanted: boolean
  assemblePending: boolean
}

const POLL_MS = 2000

export function createStoryboardRun(options: {
  documentId: string
  store: DocStore
  ensureSaved: () => Promise<void>
  prepareNode: (id: string, signal?: AbortSignal) => Promise<NodeDraft>
  changed: () => void
  /** 测试注入更快的轮询间隔；生产默认 2 秒 */
  pollMs?: number
}) {
  const { documentId, store, changed } = options
  const pollMs = options.pollMs ?? POLL_MS
  const operation = new AbortController()
  let disposed = false
  let busy = false
  let polling = false
  let uncertain = false
  let message = ''
  let legacy = ''
  let unmatched: UnmatchedResult[] = []
  let assemblePending = false
  let assembleNext = false
  let run: ServerRun | null = null
  const applied = new Set<string>()
  let timer: ReturnType<typeof setTimeout> | undefined

  const attemptKey = `gencanvas.storyboardRunStart.${documentId}`
  const assembleKey = (runId: string) => `gencanvas.storyboardAssemble.${runId}`
  const assembledKey = (runId: string) => `gencanvas.storyboardAssembled.${runId}`
  const baselineKey = (runId: string) => `gencanvas.storyboardRunTimeline.${runId}`
  const timelineSignature = () => JSON.stringify(store.doc.timeline ?? null)

  function publish() {
    if (!disposed) {
      changed()
    }
  }
  function store_json(key: string, value: unknown) {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch {
      /* 存储不可用时仅当前会话生效 */
    }
  }
  function read_json<T>(key: string): T | null {
    try {
      return JSON.parse(localStorage.getItem(key) || 'null') as T | null
    } catch {
      return null
    }
  }
  function rememberAttempt(attempt: { id: string; documentId: string; steps: unknown }) {
    store_json(attemptKey, attempt)
  }
  function readAttempt() {
    const saved = read_json<{ id: string; documentId: string; steps: unknown }>(attemptKey)
    return saved && saved.documentId === documentId && typeof saved.id === 'string' ? saved : null
  }
  function clearAttempt() {
    try {
      localStorage.removeItem(attemptKey)
    } catch {
      /* 存储不可用 */
    }
  }
  function assembleWanted(runId: string): boolean {
    try {
      const saved = localStorage.getItem(assembleKey(runId))
      return saved === null ? true : saved === '1'
    } catch {
      return true
    }
  }
  function readTombstone(runId: string): boolean {
    try {
      const saved = localStorage.getItem(assembledKey(runId))
      return saved === '1' || (saved != null && JSON.parse(saved).done === true)
    } catch {
      return false
    }
  }
  function markAssembled(runId: string) {
    try {
      localStorage.setItem(assembledKey(runId), '1')
    } catch {
      /* 存储不可用时无法记录“已装配”，恢复后至多提供手动装配入口，不静默重复追加 */
    }
  }
  function readBaseline(runId: string): string | null {
    try {
      return localStorage.getItem(baselineKey(runId))
    } catch {
      return null
    }
  }
  function writeBaseline(runId: string, value: string) {
    try {
      localStorage.setItem(baselineKey(runId), value)
    } catch {
      /* 存储不可用时按“无法确认”处理，装配交给用户选择 */
    }
  }
  function hasAssemblyEvidence(runId: string): boolean {
    return !!store.doc.timeline?.clips.some((clip) => clip.source.runId === runId)
  }
  function activeStep() {
    if (!run || run.index >= run.steps.length) {
      return null
    }
    return run.steps[run.index]
  }
  function jobById(jobId: string | null | undefined): RunJob | undefined {
    if (!jobId || !run?.jobs) {
      return undefined
    }
    return run.jobs.find((job) => job.id === jobId)
  }

  /** 恢复/刷新后为执行中的镜头重建归属标记：阻止单节点入口重复提交。 */
  function restoreMarkers() {
    if (!run || !['running', 'paused'].includes(run.status)) {
      return
    }
    const board = store.doc.storyboard
    store.mutateTransient(() => {
      run!.steps.forEach((step, index) => {
        if (index < run!.index) {
          return
        }
        const nodeId = step.request.clientRef
        const node = store.doc.objects[nodeId]
        const shot = board?.shots.find((item) => item.id === step.shotId)
        if (!node || !shot || shot.nodeId !== nodeId || node.src || node.assetId) {
          return
        }
        if (node.nodeRun?.runId === run!.id) {
          if (node.nodeRun.requestId !== step.request.requestId) {
            // 重试创建了新的尝试标识：替换归属，旧结果保留在执行记录中
            node.nodeRun = {
              requestId: step.request.requestId,
              status: 'queued',
              snapshot: structuredClone(step.draft),
              message: '分镜顺序执行中',
              runId: run!.id,
            }
          }
          return
        }
        if (node.nodeRun) {
          // 用户另有任务：执行器推进时会被服务端校验拦下
          return
        }
        node.nodeRun = {
          requestId: step.request.requestId,
          status: 'queued',
          snapshot: structuredClone(step.draft),
          message: '分镜顺序执行中',
          runId: run!.id,
        }
      })
    })
  }

  /** 终态执行清除镜头归属标记：用户可重新手动生成或开始新一轮。 */
  function clearMarkersIfTerminal() {
    if (!run || !['completed', 'cancelled'].includes(run.status)) {
      return
    }
    store.mutateTransient(() => {
      for (const step of run!.steps) {
        const node = store.doc.objects[step.request.clientRef]
        if (node?.nodeRun?.runId === run!.id && !node.src && !node.assetId) {
          delete node.nodeRun
        }
      }
    })
  }

  /** 结果回填：核对项目、镜头关联、节点、请求标识与参数版本后写入选用结果。 */
  function processJobs() {
    if (!run) {
      return
    }
    unmatched = []
    const activeRun = run
    activeRun.steps.forEach((step, index) => {
      const job = jobById(step.jobId)
      if (!job || job.status !== 'completed' || applied.has(step.request.requestId)) {
        return
      }
      const shot = store.doc.storyboard?.shots.find((item) => item.id === step.shotId)
      const node = store.doc.objects[step.request.clientRef]
      const title = shot?.title || `镜头 ${index + 1}`
      const url = job.images?.at(-1)?.url || ''
      if (!shot || !node) {
        unmatched.push({ index, title, url, reason: '镜头或节点已删除，结果仅保留在历史产物中' })
        return
      }
      if (shot.nodeId !== node.id) {
        unmatched.push({ index, title, url, reason: '镜头已改用其他版本，结果未回填' })
        return
      }
      if (node.src || node.assetId) {
        applied.add(step.request.requestId)
        return
      }
      if (
        node.nodeRun &&
        (node.nodeRun.runId !== activeRun.id || node.nodeRun.requestId !== step.request.requestId)
      ) {
        unmatched.push({ index, title, url, reason: '节点已被修改或重新提交，结果未回填' })
        return
      }
      if (JSON.stringify(node.nodeDraft) !== JSON.stringify(step.draft)) {
        unmatched.push({ index, title, url, reason: '节点参数已修改，结果未覆盖新内容' })
        return
      }
      store.mutateTransient(() => {
        applyNodeJob(node, job as unknown as NodeJob, store.doc.id)
      })
      applied.add(step.request.requestId)
    })
  }

  async function maybeAssemble() {
    if (!run || run.status !== 'completed' || disposed) {
      return
    }
    if (!assembleWanted(run.id) || hasAssemblyEvidence(run.id) || readTombstone(run.id)) {
      return
    }
    const baseline = readBaseline(run.id)
    if (!baseline || baseline !== timelineSignature()) {
      // 执行期间时间线被手动修改（或无法确认）：展示待装配，由用户选择追加
      assemblePending = true
      publish()
      return
    }
    await assembleNow(true)
  }

  async function assembleNow(auto: boolean) {
    if (!run || run.status !== 'completed') {
      return
    }
    busy = true
    message = '正在检查素材时长并装配时间线…'
    publish()
    try {
      await appendRunStoryboard(
        store,
        run.id,
        run.steps.map((step) => step.shotId),
        videoDuration,
        operation.signal,
      )
      markAssembled(run.id)
      assemblePending = false
      message = auto ? '已按本轮镜头顺序自动装配时间线，可撤销或调整' : '已按本轮镜头顺序追加时间线'
    } catch (error) {
      assemblePending = true
      message = error instanceof Error ? error.message : '装配失败'
    } finally {
      busy = false
      publish()
    }
  }

  function schedule() {
    clearTimeout(timer)
    if (disposed || !run || !['running', 'paused'].includes(run.status)) {
      return
    }
    timer = setTimeout(() => void poll(), pollMs)
  }

  async function poll() {
    if (!run || disposed || polling) {
      return
    }
    polling = true
    clearTimeout(timer)
    const ident = run.id
    try {
      const response = await fetch(`/api/storyboards/runs/${encodeURIComponent(ident)}`, {
        signal: operation.signal,
      })
      const payload = (await response.json()) as { run?: ServerRun }
      if (disposed) {
        return
      }
      if (!response.ok || !payload.run || payload.run.id !== ident) {
        throw new Error('执行状态暂不可用')
      }
      run = payload.run
      afterUpdate()
    } catch {
      if (!disposed) {
        message = '执行状态暂不可用，正在重试查询…'
        publish()
      }
    } finally {
      polling = false
      schedule()
    }
  }

  function afterUpdate() {
    restoreMarkers()
    processJobs()
    if (run && ['completed', 'cancelled'].includes(run.status)) {
      clearMarkersIfTerminal()
      void maybeAssemble()
    }
    publish()
  }

  function adoptRun(next: ServerRun | undefined | null) {
    if (!next || next.documentId !== documentId || typeof next.id !== 'string') {
      return false
    }
    run = next
    applied.clear()
    uncertain = false
    assemblePending = false
    assembleNext = assembleWanted(run.id)
    const attempt = readAttempt()
    if (attempt?.id === run.id) {
      clearAttempt()
    }
    if (['running', 'paused'].includes(run.status)) {
      restoreMarkers()
      processJobs()
      schedule()
    } else {
      // 已结束的执行也要回填：浏览器关闭期间完成的结果恢复后仍然落位
      processJobs()
      clearMarkersIfTerminal()
    }
    void maybeAssemble()
    publish()
    return true
  }

  /** 按项目发现执行记录：恢复不依赖 localStorage，只查询不重复提交。 */
  async function adopt(): Promise<boolean> {
    try {
      const list = await fetch(
        `/api/storyboards/runs?documentId=${encodeURIComponent(documentId)}`,
        { signal: operation.signal },
      )
      const payload = (await list.json()) as {
        runs?: { id: string; status?: string; documentId?: string }[]
      }
      if (!list.ok || !Array.isArray(payload.runs)) {
        return false
      }
      const own = payload.runs.filter((item) => item.documentId === documentId)
      const candidate =
        own.find((item) => item.status === 'running' || item.status === 'paused') || own[0]
      if (!candidate) {
        return false
      }
      const detail = await fetch(`/api/storyboards/runs/${encodeURIComponent(candidate.id)}`, {
        signal: operation.signal,
      })
      const full = (await detail.json()) as { run?: ServerRun }
      return detail.ok && adoptRun(full.run)
    } catch {
      return false
    }
  }

  async function start() {
    if (busy) {
      throw new Error('当前操作尚未结束，请稍后再试')
    }
    if (!run || ['completed', 'cancelled'].includes(run.status)) {
      // 先按项目发现执行记录：其他标签页或关闭前开启的执行不得重复启动
      await adopt()
    }
    if (run && ['running', 'paused'].includes(run.status)) {
      throw new Error('当前项目已有执行记录，请继续或取消该记录')
    }
    const shots = store.doc.storyboard?.shots || []
    const plan: { shotId: string; nodeId: string }[] = []
    for (const shot of shots) {
      const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
      if (node?.src || node?.assetId) {
        continue
      }
      if (shot.locked || !node) {
        throw new Error('请先为每个未完成镜头创建节点并解锁')
      }
      if (
        node.nodeRun &&
        (!node.nodeRun.runId ||
          ['submitting', 'queued', 'running', 'uncertain'].includes(node.nodeRun.status))
      ) {
        throw new Error(`镜头「${shot.title}」已有生成任务，请先处理该节点任务`)
      }
      plan.push({ shotId: shot.id, nodeId: node.id })
    }
    if (!plan.length) {
      throw new Error('没有待生成镜头')
    }
    busy = true
    uncertain = false
    message = '正在准备生成参数…'
    publish()
    const preparation = new AbortController()
    const boardSignature = () =>
      JSON.stringify(shots.map((shot) => [shot.id, shot.nodeId, shot.locked]))
    const before = boardSignature()
    try {
      const drafts: NodeDraft[] = []
      for (const item of plan) {
        drafts.push(await options.prepareNode(item.nodeId, preparation.signal))
      }
      await options.ensureSaved()
      if (boardSignature() !== before) {
        throw new Error('分镜在准备期间已变化，请重新开始')
      }
      const steps = plan.map((item, index) => {
        const node = store.doc.objects[item.nodeId]
        if (!node || JSON.stringify(node.nodeDraft) !== JSON.stringify(drafts[index])) {
          throw new Error('节点在准备期间已变化，请重新开始')
        }
        return {
          shotId: item.shotId,
          draft: drafts[index],
          request: nodeRequest(documentId, node, crypto.randomUUID(), drafts[index]),
        }
      })
      const attempt = { id: crypto.randomUUID(), documentId, steps }
      rememberAttempt(attempt)
      writeBaseline(attempt.id, timelineSignature())
      let result: { status: number; payload: { run?: ServerRun; error?: string; detail?: string } }
      try {
        const response = await fetch('/api/storyboards/runs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(attempt),
          signal: preparation.signal,
        })
        const payload = (await response.json()) as {
          run?: ServerRun
          error?: string
          detail?: string
        }
        result = { status: response.status, payload }
      } catch {
        if (preparation.signal.aborted) {
          return
        }
        // 响应丢失：保留原标识，确认时幂等复用，不重复提交
        uncertain = true
        message = '提交结果未确认；可确认原执行请求，将复用同一标识避免重复提交'
        return
      }
      if (result.status === 409) {
        clearAttempt()
        message = result.payload.error || result.payload.detail || '已存在执行记录'
        await adopt()
        return
      }
      if (!result.status.toString().startsWith('2') || !result.payload.run) {
        throw new Error(result.payload.error || result.payload.detail || '执行提交失败')
      }
      clearAttempt()
      adoptRun(result.payload.run)
      message = '服务端顺序执行已开始，关闭页面后仍会继续'
    } catch (error) {
      message = error instanceof Error ? error.message : '无法开始执行'
    } finally {
      busy = false
      publish()
    }
  }

  async function confirmStart() {
    const attempt = readAttempt()
    if (busy) {
      return
    }
    if (!attempt) {
      uncertain = false
      message = ''
      publish()
      return
    }
    busy = true
    message = '正在确认原执行请求…'
    publish()
    try {
      const response = await fetch('/api/storyboards/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(attempt),
        signal: operation.signal,
      })
      const payload = (await response.json()) as {
        run?: ServerRun
        error?: string
        detail?: string
      }
      if (payload.run && adoptRun(payload.run)) {
        clearAttempt()
        message = '已确认原执行请求，继续查询结果'
      } else {
        clearAttempt()
        uncertain = false
        message = payload.error || payload.detail || '原执行请求未能确认，可重新开始'
        if (response.status === 409) {
          await adopt()
        }
      }
    } catch {
      message = '仍无法确认提交，请稍后重试'
    } finally {
      busy = false
      publish()
    }
  }

  async function control(action: 'pause' | 'resume' | 'cancel' | 'retry') {
    if (!run || disposed || busy) {
      return
    }
    busy = true
    publish()
    try {
      const response = await fetch(
        `/api/storyboards/runs/${encodeURIComponent(run.id)}/${action}`,
        { method: 'POST', signal: operation.signal },
      )
      const payload = (await response.json()) as {
        run?: ServerRun
        error?: string
        detail?: string
      }
      if (!response.ok || !payload.run) {
        message = payload.error || payload.detail || '操作暂未生效，请稍后重试'
      } else {
        run = payload.run
        message = ''
        if (['running', 'paused'].includes(run.status)) {
          restoreMarkers()
        } else {
          clearMarkersIfTerminal()
          void maybeAssemble()
        }
        schedule()
      }
    } catch {
      message = '操作暂未送达，请检查连接后重试'
    } finally {
      busy = false
      publish()
    }
  }

  function setAssemble(wanted: boolean) {
    if (!run) {
      return
    }
    assembleNext = wanted
    try {
      localStorage.setItem(assembleKey(run.id), wanted ? '1' : '0')
    } catch {
      /* 存储不可用时仅当前会话生效 */
    }
    if (wanted) {
      void maybeAssemble()
    }
    publish()
  }

  function checkLegacy() {
    const key = `gencanvas.storyboardRun.${documentId}`
    const saved = read_json<{ status?: string; legacyHint?: boolean }>(key)
    if (
      saved &&
      !saved.legacyHint &&
      saved.status &&
      !['completed', 'cancelled'].includes(saved.status)
    ) {
      legacy =
        '发现旧版浏览器执行记录：已停止旧的自动调度，其中已提交的任务仍会在服务端完成。请检查各镜头结果后使用服务端顺序执行。'
      store_json(key, { ...saved, status: 'paused', legacyHint: true })
      publish()
    }
  }

  checkLegacy()
  void adopt()

  return {
    get state(): StoryboardRunState {
      return {
        run,
        busy,
        uncertain,
        message,
        legacy,
        unmatched: [...unmatched],
        assembleWanted: run ? assembleNext : false,
        assemblePending,
      }
    },
    /** 当前暂停镜头是否可重试：failed/unknown/未提交的镜头可以重试或对账 */
    canRetryCurrent(): boolean {
      const step = activeStep()
      if (!run || run.status !== 'paused' || !step) {
        return false
      }
      const job = jobById(step.jobId)
      return !job || job.status === 'failed' || job.status === 'unknown'
    },
    start,
    confirmStart,
    control,
    assembleNow: () => assembleNow(false),
    setAssemble,
    dispose() {
      disposed = true
      clearTimeout(timer)
      operation.abort()
    },
  }
}
