import { createStoryboardPlanner } from './storyboard-planner.js'
import { objectOriginalUrl } from './asset-util.js'
import { createStoryboardRun } from './storyboard-run.js'
import { runStoryboardChecks } from './storyboard-checks.js'
import { applySubjectToShots } from './shot-subject.js'
import { appendStoryboard } from './storyboard-timeline.js'
import { createMissingShotNodes, createShotNode, createShotVideoVersions } from './shot-node.js'
import type { NodeDraft } from '../state/node-model.js'
import type { DocStore } from '../state/doc-store.js'
import {
  emptyStoryboard,
  isStoryboard,
  newShot,
  type Storyboard,
  type Shot,
} from '../state/storyboard.js'

/** 主体候选：已生成或素材库中的图片节点 */
function isSubjectCandidate(obj: unknown): boolean {
  return (
    !!obj &&
    typeof obj === 'object' &&
    (obj as { kind?: unknown }).kind === 'image' &&
    !!((obj as { src?: unknown }).src || (obj as { assetId?: unknown }).assetId)
  )
}

export function createStoryboardPanel(
  store: DocStore,
  selection: () => string[],
  openNode: (id: string) => void = () => {},
  execution?: {
    submit(
      id: string,
      onSubmitted?: (requestId: string) => void,
      signal?: AbortSignal,
    ): Promise<void>
    cancel(id: string): void
  },
  runDeps?: {
    ensureSaved: () => Promise<void>
    prepareNode: (id: string, signal?: AbortSignal) => Promise<NodeDraft>
  },
) {
  const style = document.createElement('link')
  style.rel = 'stylesheet'
  style.href = '/canvas/storyboard.css'
  document.head.append(style)
  const toggle = document.createElement('button')
  toggle.textContent = '分镜'
  toggle.className = 'timeline-toggle'
  toggle.type = 'button'
  toggle.setAttribute('aria-expanded', 'false')
  const panel = document.createElement('section')
  panel.className = 'storyboard-panel'
  panel.setAttribute('aria-label', '分镜编辑器')
  panel.hidden = true
  document.querySelector('.topbar-tools')!.prepend(toggle)
  document.body.append(panel)
  panel.addEventListener('keydown', (event) => event.stopPropagation())
  const board = () => store.doc.storyboard || emptyStoryboard()
  const save = (next: Storyboard) =>
    store.apply({ type: 'setStoryboard', before: store.doc.storyboard, after: next })
  function update(id: string, patch: Partial<Shot>) {
    const next = structuredClone(board())
    const shot = next.shots.find((item) => item.id === id)
    if (!shot || (shot.locked && Object.keys(patch).some((key) => key !== 'locked'))) {
      return
    }
    if (patch.nodeId) {
      const versions = [
        ...new Set([...(shot.versions || []), ...(shot.nodeId ? [shot.nodeId] : []), patch.nodeId]),
      ]
      if (versions.length > 100) {
        return
      }
      shot.versions = versions
    }
    Object.assign(shot, patch)
    save(next)
  }
  function button(parent: HTMLElement, name: string, run: () => void, disabled = false) {
    const item = document.createElement('button')
    item.type = 'button'
    item.textContent = name
    item.disabled = disabled
    item.addEventListener('click', run)
    parent.append(item)
  }
  const operation = new AbortController()
  let planning = false
  let historyLoading = false
  let history: { id: string; prompt: string; status: string; createdAt: string }[] | null = null
  let prompt = ''
  const workflows = [
    ['general', '自由创作'],
    ['product', '产品广告'],
    ['story', '故事短片'],
    ['explainer', '知识讲解'],
  ] as const
  let workflow = 'general'
  let candidate: Storyboard | null = null
  const recoveryKey = `gencanvas.storyboardPlan.${store.doc.id}`
  try {
    const recovered = JSON.parse(localStorage.getItem(recoveryKey) || '{}') as {
      workflow?: unknown
      prompt?: unknown
      candidate?: unknown
    }
    if (typeof recovered.prompt === 'string' && recovered.prompt.length <= 12000) {
      prompt = recovered.prompt
    }
    if (workflows.some(([id]) => id === recovered.workflow)) {
      workflow = recovered.workflow as string
    }
    if (isStoryboard(recovered.candidate)) {
      candidate = recovered.candidate
    }
  } catch {
    /* 本地缓存损坏不阻止编辑项目 */
  }
  function rememberPlan() {
    try {
      localStorage.setItem(recoveryKey, JSON.stringify({ prompt, candidate, workflow }))
    } catch {
      /* 存储不可用时保留内存结果 */
    }
  }
  let preparingVideos = false
  let assembling = false
  let applyingSubject = false
  let assemblyMessage = ''
  let checksOpen = false
  let signature = ''
  let pendingRefresh = false
  let disposed = false
  const contentSignature = () =>
    JSON.stringify([
      board(),
      board().shots.map((shot) =>
        [shot.nodeId, ...(shot.versions || [])].map((id) => {
          const node = id ? store.doc.objects[id] : undefined
          return node ? [node.name, node.src, node.assetId, node.nodeRun] : null
        }),
      ),
    ])
  const runner = runDeps
    ? createStoryboardRun({
        documentId: store.doc.id,
        store,
        ensureSaved: runDeps.ensureSaved,
        prepareNode: runDeps.prepareNode,
        changed: () => {
          pendingRefresh = true
          refreshFromStore()
        },
      })
    : null
  const planner = createStoryboardPlanner(store.doc.id, (state) => {
    planning = state.busy
    assemblyMessage = state.message
    if (state.storyboard) {
      candidate = state.storyboard
      rememberPlan()
    }
    pendingRefresh = true
    refreshFromStore()
  })
  function pauseComparisons() {
    for (const video of Array.from(panel.querySelectorAll('video'))) {
      video.pause()
    }
  }
  function render() {
    pauseComparisons()
    pendingRefresh = false
    signature = contentSignature()
    panel.replaceChildren()
    const header = document.createElement('header')
    const title = document.createElement('strong')
    title.textContent = `分镜 · ${board().shots.length} 镜头 · ${(board().shots.reduce((sum, shot) => sum + shot.durationFrames, 0) / 30).toFixed(1)} 秒`
    header.append(title)
    button(
      header,
      '添加镜头',
      () => save({ ...board(), shots: [...board().shots, newShot()] }),
      board().shots.length >= 200,
    )
    for (const [label, kind] of [
      ['补齐图片节点', 'image'],
      ['补齐视频节点', 'video'],
    ] as const) {
      button(
        header,
        label,
        () => {
          try {
            const ids = createMissingShotNodes(store, kind)
            assemblyMessage = `已创建 ${ids.length} 个节点，可检查参数后顺序生成`
          } catch (error) {
            assemblyMessage = error instanceof Error ? error.message : '创建失败'
          }
          render()
        },
        !board().shots.some(
          (shot) => !shot.locked && (!shot.nodeId || !store.doc.objects[shot.nodeId]),
        ),
      )
    }
    button(
      header,
      preparingVideos ? '正在准备视频节点…' : '分镜图片转视频',
      () => {
        preparingVideos = true
        assemblyMessage = '正在准备图片参考…'
        render()
        void createShotVideoVersions(store, operation.signal)
          .then((ids) => {
            assemblyMessage = `已创建 ${ids.length} 个视频草稿，原图片保留在版本中`
          })
          .catch((error: unknown) => {
            assemblyMessage = error instanceof Error ? error.message : '准备失败'
          })
          .finally(() => {
            preparingVideos = false
            pendingRefresh = true
            refreshFromStore()
          })
      },
      preparingVideos ||
        !board().shots.some((shot) => {
          const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
          return (
            !shot.locked && node?.kind === 'image' && !node.nodeRun && !!(node.assetId || node.src)
          )
        }),
    )
    button(
      header,
      assembling ? '正在装配…' : '按分镜追加到时间线',
      () => {
        assembling = true
        assemblyMessage = '正在检查素材时长…'
        render()
        void appendStoryboard(store, undefined, operation.signal)
          .then(() => {
            assemblyMessage = '已按镜头顺序追加，可在时间线调整或撤销'
          })
          .catch((error: unknown) => {
            assemblyMessage = error instanceof Error ? error.message : '装配失败'
          })
          .finally(() => {
            assembling = false
            if (!operation.signal.aborted) {
              render()
            }
          })
      },
      assembling || !board().shots.length,
    )
    const status = document.createElement('span')
    status.setAttribute('role', 'status')
    status.textContent = assemblyMessage
    header.append(status)
    button(
      header,
      applyingSubject ? '正在准备主体…' : '主体引用到全部镜头',
      () => {
        applyingSubject = true
        assemblyMessage = '正在准备主体素材…'
        render()
        const subject = store.doc.objects[selection()[0]]
        void applySubjectToShots(store, subject, operation.signal)
          .then((result) => {
            const notes = result.skipped.length ? `；跳过：${result.skipped.join('、')}` : ''
            assemblyMessage = `已将主体引用到 ${result.applied} 个镜头${notes}`
          })
          .catch((error: unknown) => {
            if (!operation.signal.aborted) {
              assemblyMessage = error instanceof Error ? error.message : '主体引用失败'
            }
          })
          .finally(() => {
            applyingSubject = false
            pendingRefresh = true
            refreshFromStore()
          })
      },
      applyingSubject ||
        !isSubjectCandidate(store.doc.objects[selection()[0]]) ||
        !(store.doc.storyboard?.shots || []).some((shot) => {
          const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
          return (
            !shot.locked &&
            !!node &&
            !node.src &&
            !node.assetId &&
            !node.nodeRun &&
            !node.nodeDraft?.references.length
          )
        }),
    )
    button(header, '撤销', () => store.undo())
    button(header, '重做', () => store.redo())
    button(header, checksOpen ? '收起检查' : '质量检查', () => {
      checksOpen = !checksOpen
      render()
    })
    button(header, '收起分镜', () => {
      panel.hidden = true
      pauseComparisons()
      toggle.setAttribute('aria-expanded', 'false')
    })
    if (runner) {
      const runState = runner.state
      const run = runState.run
      const runStatus = document.createElement('span')
      runStatus.setAttribute('role', 'status')
      runStatus.textContent = runState.uncertain
        ? runState.message
        : runState.message || run?.message || '按顺序执行待生成镜头（服务端执行，关闭页面后仍继续）'
      header.append(runStatus)
      if (runState.legacy) {
        const notice = document.createElement('p')
        notice.textContent = runState.legacy
        panel.append(notice)
      }
      button(
        header,
        runState.uncertain ? '确认原执行请求' : '顺序生成镜头',
        () => {
          const action = runState.uncertain ? runner.confirmStart() : runner.start()
          void action.catch((error: unknown) => {
            assemblyMessage = error instanceof Error ? error.message : '无法开始'
            render()
          })
        },
        runState.uncertain ? runState.busy : !!run && ['running', 'paused'].includes(run.status),
      )
      if (runState.uncertain) {
        button(header, '放弃原执行请求', () => {
          try {
            localStorage.removeItem(`gencanvas.storyboardRunStart.${store.doc.id}`)
          } catch {
            /* 存储不可用 */
          }
          render()
        })
      }
      button(header, '暂停执行', () => void runner.control('pause'), run?.status !== 'running')
      button(header, '继续执行', () => void runner.control('resume'), run?.status !== 'paused')
      button(
        header,
        '重试当前镜头',
        () => void runner.control('retry'),
        !runner.canRetryCurrent() || runState.busy,
      )
      button(
        header,
        '取消执行',
        () => void runner.control('cancel'),
        !run || ['completed', 'cancelled'].includes(run.status),
      )
      if (run) {
        const assembled = !!store.doc.timeline?.clips.some((clip) => clip.source.runId === run.id)
        if (!assembled) {
          const assembleLabel = document.createElement('label')
          assembleLabel.className = 'run-assemble'
          const assembleCheck = document.createElement('input')
          assembleCheck.type = 'checkbox'
          assembleCheck.checked = runState.assembleWanted
          assembleCheck.setAttribute('aria-label', '完成后装配时间线')
          assembleCheck.addEventListener('change', () => runner.setAssemble(assembleCheck.checked))
          assembleLabel.append(assembleCheck, document.createTextNode('完成后装配时间线'))
          header.append(assembleLabel)
          if (run.status === 'completed') {
            button(
              header,
              runState.assemblePending
                ? '追加装配时间线（执行期间时间线已修改）'
                : '立即装配时间线',
              () => void runner.assembleNow(),
              runState.busy,
            )
          }
        }
        const failed = runState.unmatched
        if (failed.length) {
          const list = document.createElement('div')
          list.setAttribute('aria-label', '未回填结果')
          for (const item of failed) {
            const line = document.createElement('p')
            line.textContent = `第 ${item.index + 1} 镜「${item.title}」${item.reason}`
            if (item.url) {
              const link = document.createElement('a')
              link.href = item.url
              link.target = '_blank'
              link.rel = 'noreferrer'
              link.textContent = '查看该结果'
              line.append(link)
            }
            list.append(line)
          }
          panel.append(list)
        }
      }
    }
    panel.append(header)
    if (checksOpen) {
      // 打开期间每次渲染都重算：镜头、任务或时间线变化后结果保持最新
      const issues = runStoryboardChecks(board(), store.doc.objects, store.doc.timeline)
      const list = document.createElement('div')
      list.setAttribute('aria-label', '质量检查结果')
      if (!issues.length) {
        list.textContent = '未发现问题'
      }
      for (const issue of issues) {
        const line = document.createElement('p')
        line.textContent = `${issue.severity === 'error' ? '问题' : '提示'}：${issue.message}`
        list.append(line)
      }
      panel.append(list)
    }
    const workflowLabel = document.createElement('label')
    workflowLabel.textContent = '创作工作流 '
    const workflowSelect = document.createElement('select')
    workflowSelect.setAttribute('aria-label', '创作工作流')
    workflowSelect.disabled = planning
    for (const [id, label] of workflows) {
      workflowSelect.append(new window.Option(label, id))
    }
    workflowSelect.value = workflow
    workflowSelect.addEventListener('change', () => {
      workflow = workflowSelect.value
      rememberPlan()
    })
    workflowLabel.append(workflowSelect)
    panel.append(workflowLabel)
    const request = document.createElement('textarea')
    request.setAttribute('aria-label', 'Agent 创作需求')
    request.placeholder = '描述目标、风格和总时长，或粘贴剧本'
    request.maxLength = 12000
    request.value = prompt
    request.addEventListener('input', () => {
      prompt = request.value
      rememberPlan()
    })
    panel.append(request)
    button(
      panel,
      planning ? '正在规划…' : planner.state.uncertain ? '确认原规划请求' : 'Agent 规划分镜',
      () => {
        void planner.start(prompt, workflow)
      },
      planning,
    )
    if (planning || planner.state.uncertain) {
      button(panel, '取消规划', () => {
        void planner.cancel()
      })
    }
    button(
      panel,
      historyLoading ? '正在读取历史…' : '查看最近 20 次规划',
      () => {
        historyLoading = true
        render()
        void fetch(`/api/storyboards/jobs?documentId=${encodeURIComponent(store.doc.id)}`, {
          signal: operation.signal,
        })
          .then(async (response) => {
            const result = (await response.json()) as { jobs?: typeof history }
            if (!response.ok || !Array.isArray(result.jobs)) {
              throw new Error('读取失败')
            }
            history = result.jobs
          })
          .catch(() => {
            assemblyMessage = '无法读取规划历史，请重试'
          })
          .finally(() => {
            historyLoading = false
            pendingRefresh = true
            refreshFromStore()
          })
      },
      historyLoading,
    )
    if (history) {
      const list = document.createElement('div')
      list.setAttribute('aria-label', '规划历史')
      const labels: Record<string, string> = {
        completed: '已完成',
        running: '规划中',
        queued: '排队中',
        failed: '失败',
        cancelled: '已取消',
      }
      for (const item of history) {
        button(
          list,
          `${labels[item.status] || '未知状态'} · ${item.prompt.slice(0, 60)}`,
          () => {
            void planner.restore(item.id)
          },
          planning || planner.state.uncertain,
        )
      }
      if (!history.length) {
        list.textContent = '当前项目暂无规划历史'
      }
      button(list, '收起规划历史', () => {
        history = null
        render()
      })
      panel.append(list)
    }
    if (candidate) {
      const preview = document.createElement('pre')
      preview.style.whiteSpace = 'pre-wrap'
      preview.textContent =
        candidate.outline +
        '\n\n' +
        candidate.shots
          .map(
            (shot, index) =>
              `${index + 1}. ${shot.title} · ${shot.durationFrames / 30} 秒\n${shot.visual}\n台词：${shot.dialogue}\n运镜：${shot.camera}`,
          )
          .join('\n\n')
      panel.append(preview)
      button(panel, '采用并追加分镜', () => {
        if (!candidate) {
          return
        }
        const next = {
          ...board(),
          outline: board().outline || candidate.outline,
          shots: [
            ...board().shots,
            ...candidate.shots.map((shot) => ({
              ...shot,
              id: board().shots.some((existing) => existing.id === shot.id)
                ? crypto.randomUUID()
                : shot.id,
            })),
          ],
        }
        if (
          next.shots.length > 200 ||
          next.shots.reduce((sum, shot) => sum + shot.durationFrames, 0) > 108000
        ) {
          assemblyMessage = '追加后超过镜头数量或一小时上限'
          render()
          return
        }
        candidate = null
        rememberPlan()
        save(next)
      })
      button(panel, '放弃本次规划', () => {
        candidate = null
        rememberPlan()
        render()
      })
    }
    const outline = document.createElement('textarea')
    outline.setAttribute('aria-label', '故事大纲')
    outline.placeholder = '故事大纲'
    outline.maxLength = 20000
    outline.value = board().outline
    const commitOutline = () => {
      if (outline.value !== board().outline) {
        save({ ...board(), outline: outline.value })
      }
    }
    outline.addEventListener('change', commitOutline)
    outline.addEventListener('focusout', commitOutline)
    panel.append(outline)
    for (const [index, shot] of board().shots.entries()) {
      const card = document.createElement('article')
      card.setAttribute('aria-label', `镜头 ${index + 1}`)
      for (const [key, label, max] of [
        ['title', '标题', 500],
        ['visual', '画面描述', 4000],
        ['dialogue', '台词', 4000],
        ['camera', '运镜', 1000],
      ] as const) {
        const field = document.createElement('textarea')
        field.setAttribute('aria-label', `镜头 ${index + 1} ${label}`)
        field.placeholder = label
        field.value = shot[key]
        field.maxLength = max
        field.disabled = shot.locked
        const commitField = () => {
          if (field.value !== shot[key]) {
            update(shot.id, { [key]: field.value })
          }
        }
        field.addEventListener('change', commitField)
        // 部分环境失焦不派发 change（如无痕焦点迁移），离开控件时兜底提交
        field.addEventListener('focusout', commitField)
        card.append(field)
      }
      const duration = document.createElement('input')
      duration.type = 'number'
      duration.min = '0.0333333333'
      duration.max = '3600'
      duration.step = 'any'
      duration.value = String(shot.durationFrames / 30)
      duration.setAttribute('aria-label', `镜头 ${index + 1} 时长（秒）`)
      duration.disabled = shot.locked
      const commitDuration = () => {
        const frames = Math.round(Number(duration.value) * 30)
        const total = board().shots.reduce(
          (sum, item) => sum + (item.id === shot.id ? frames : item.durationFrames),
          0,
        )
        if (!Number.isFinite(frames) || frames < 1 || total > 108000) {
          duration.value = String(shot.durationFrames / 30)
          return
        }
        if (frames !== shot.durationFrames) {
          update(shot.id, { durationFrames: frames })
        }
      }
      duration.addEventListener('change', commitDuration)
      duration.addEventListener('focusout', commitDuration)
      card.append(duration)
      const media = document.createElement('span')
      const node = shot.nodeId ? store.doc.objects[shot.nodeId] : undefined
      media.textContent = node
        ? `选用：${node.name || node.id}`
        : shot.nodeId
          ? '关联节点已删除'
          : '尚未关联素材'
      card.append(media)
      const feedback = document.createElement('span')
      feedback.setAttribute('role', 'status')
      for (const [label, kind] of [
        ['创建图片节点', 'image'],
        ['创建视频节点', 'video'],
      ] as const) {
        button(
          card,
          label,
          () => {
            try {
              const id = createShotNode(store, shot.id, kind)
              panel.hidden = true
              pauseComparisons()
              toggle.setAttribute('aria-expanded', 'false')
              openNode(id)
            } catch (error) {
              feedback.textContent = error instanceof Error ? error.message : '创建失败'
            }
          },
          shot.locked,
        )
      }
      button(
        card,
        '打开关联节点',
        () => {
          if (shot.nodeId) {
            panel.hidden = true
            pauseComparisons()
            toggle.setAttribute('aria-expanded', 'false')
            openNode(shot.nodeId)
          }
        },
        !node,
      )
      if (execution && node) {
        const busy =
          !!node.nodeRun && ['submitting', 'queued', 'running'].includes(node.nodeRun.status)
        const state = document.createElement('span')
        state.textContent =
          node.src || node.assetId ? '生成完成' : node.nodeRun?.message || '尚未生成'
        card.append(state)
        button(
          card,
          node.nodeRun?.status === 'uncertain'
            ? '确认镜头结果'
            : node.nodeRun?.status === 'failed'
              ? '重试镜头生成'
              : '生成此镜头',
          () => {
            void execution.submit(node.id).catch((error: unknown) => {
              feedback.textContent = error instanceof Error ? error.message : '提交失败'
            })
          },
          shot.locked || busy || !!node.src || !!node.assetId,
        )
        button(card, '取消镜头生成', () => execution.cancel(node.id), !node.nodeRun)
      }
      card.append(feedback)
      button(
        card,
        '关联选中素材',
        () => {
          const obj = store.doc.objects[selection()[0]]
          if (obj && ['image', 'video'].includes(obj.kind)) {
            update(shot.id, { nodeId: obj.id })
          }
        },
        shot.locked,
      )
      const versions = [
        ...new Set([...(shot.versions || []), ...(shot.nodeId ? [shot.nodeId] : [])]),
      ]
      if (versions.length) {
        const picker = document.createElement('select')
        picker.setAttribute('aria-label', `镜头 ${index + 1} 选用版本`)
        picker.disabled = shot.locked
        picker.add(new window.Option('未选用', ''))
        versions.forEach((id, version) => {
          const item = store.doc.objects[id]
          const option = new window.Option(
            `版本 ${version + 1} · ${item ? (item.src || item.assetId ? '已生成' : '草稿/任务') : '节点已删除'} · ${item?.name || id}`,
            id,
          )
          option.disabled = !item
          picker.add(option)
        })
        picker.value = shot.nodeId || ''
        picker.addEventListener('change', () => update(shot.id, { nodeId: picker.value || null }))
        card.append(picker)
        const comparison = document.createElement('details')
        comparison.className = 'shot-comparison'
        const summary = document.createElement('summary')
        summary.textContent = `比较 ${versions.length} 个版本`
        comparison.append(summary)
        const grid = document.createElement('div')
        comparison.append(grid)
        comparison.addEventListener('toggle', () => {
          if (!comparison.open) {
            for (const video of Array.from(grid.querySelectorAll('video'))) {
              video.pause()
            }
            return
          }
          if (grid.childElementCount) {
            return
          }
          versions.forEach((id, version) => {
            const item = store.doc.objects[id]
            const figure = document.createElement('figure')
            const caption = document.createElement('figcaption')
            caption.textContent = `版本 ${version + 1}${id === shot.nodeId ? ' · 已选用' : ''}`
            figure.append(caption)
            const url = item ? objectOriginalUrl(item) : ''
            if (item && url && ['image', 'video'].includes(item.kind)) {
              if (item.kind === 'video') {
                const video = document.createElement('video')
                video.controls = true
                video.preload = 'none'
                video.playsInline = true
                video.src = url
                video.setAttribute('aria-label', `版本 ${version + 1} 视频`)
                figure.append(video)
              } else {
                const image = document.createElement('img')
                image.loading = 'lazy'
                image.src = url
                image.alt = `版本 ${version + 1} ${item.name || '图片'}`
                figure.append(image)
              }
            } else {
              const hint = document.createElement('p')
              hint.textContent = item ? '尚未生成结果' : '节点已删除'
              figure.append(hint)
            }
            button(
              figure,
              `选用版本 ${version + 1}`,
              () => update(shot.id, { nodeId: id }),
              shot.locked || !item,
            )
            grid.append(figure)
          })
        })
        card.append(comparison)
      }
      button(card, '解除关联', () => update(shot.id, { nodeId: null }), shot.locked || !shot.nodeId)
      button(card, shot.locked ? '解锁镜头' : '锁定镜头', () =>
        update(shot.id, { locked: !shot.locked }),
      )
      for (const [label, delta] of [
        ['前移镜头', -1],
        ['后移镜头', 1],
      ] as const) {
        const other = board().shots[index + delta]
        button(
          card,
          label,
          () => {
            const next = structuredClone(board())
            ;[next.shots[index], next.shots[index + delta]] = [
              next.shots[index + delta],
              next.shots[index],
            ]
            save(next)
          },
          shot.locked || !other || other.locked,
        )
      }
      button(
        card,
        '删除镜头',
        () => save({ ...board(), shots: board().shots.filter((item) => item.id !== shot.id) }),
        shot.locked,
      )
      panel.append(card)
    }
  }
  toggle.addEventListener('click', () => {
    pauseComparisons()
    panel.hidden = !panel.hidden
    toggle.setAttribute('aria-expanded', String(!panel.hidden))
    if (!panel.hidden) {
      render()
    }
  })
  function refreshFromStore() {
    const focused = document.activeElement
    const editing =
      panel.contains(focused) &&
      (focused instanceof HTMLTextAreaElement || focused instanceof HTMLInputElement)
    if (
      !disposed &&
      !panel.hidden &&
      !editing &&
      (pendingRefresh || signature !== contentSignature())
    ) {
      render()
    }
  }
  // 输入在 change/失焦时提交；生成进度通知不能重建正在输入的控件。
  panel.addEventListener('focusout', () => queueMicrotask(refreshFromStore))
  const unsubscribe = store.subscribe(refreshFromStore)
  return {
    dispose() {
      pauseComparisons()
      disposed = true
      runner?.dispose()
      planner.dispose()
      operation.abort()
      unsubscribe()
      panel.remove()
      toggle.remove()
      style.remove()
    },
  }
}
