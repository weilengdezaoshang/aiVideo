import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentPanel } from '../apps/web/canvas/agent-panel.js'
import { createAgentStore } from '../apps/web/canvas/agent/store.js'
import type { AgentApi } from '../apps/web/canvas/agent/api.js'
import type { AgentRequest, AgentRun } from '../apps/web/canvas/agent/types.js'
import type { ChatMessage } from '../apps/web/canvas/state/doc-store.js'

afterEach(() => {
  cleanup()
  localStorage.clear()
})
const history: ChatMessage[] = [
  { id: 'old', at: '', role: 'user', kind: 'text', text: '旧版对话记录' },
]
function setup(options: { initial?: AgentRun; saveFailure?: boolean; hostEnabled?: boolean } = {}) {
  let run = options.initial
  const requests: AgentRequest[] = []
  const stops: string[] = []
  const api: AgentApi = {
    models: async () => ({
      hostEnabled: options.hostEnabled,
      models: [
        { id: 'deepseek', label: 'DeepSeek', provider: 'deepseek', model: 'deepseek-chat' },
        { id: 'bailian', label: '百炼', provider: 'bailian', model: 'qwen-plus' },
      ],
      defaultModelId: 'deepseek',
    }),
    snapshot: async () => ({
      documentId: 'doc',
      sessionId: 's',
      cursor: 0,
      runs: run ? [run] : [],
    }),
    create: async (_doc, request) => {
      requests.push(request)
      run = {
        ...request,
        batch: undefined,
        id: 'run',
        status: 'running',
        reply: null,
        error: null,
        modelCalls: [],
        toolCalls: [],
        createdAt: '',
        updatedAt: '',
      }
      return { run }
    },
    get: async () => ({ run: run! }),
    stop: async (id) => {
      stops.push(id)
      run = { ...run!, status: 'stopped' }
      return { run }
    },
  }
  const store = createAgentStore({
    documentId: 'doc',
    api,
    ensureSaved: async () => {
      if (options.saveFailure) {
        throw new Error('画布版本冲突')
      }
      return 5
    },
    eventSourceFactory: () => ({ addEventListener() {}, close() {} }),
  })
  render(<AgentPanel store={store} history={history} getSelection={() => ['selected']} />)
  let close!: () => void
  act(() => {
    close = store.start()
  })
  return { store, close, requests, stops }
}

test('对话面板展示旧历史，选择模型发送独立Run并允许停止', async () => {
  const original = structuredClone(history)
  const { close, requests, stops } = setup()
  try {
    await screen.findByRole('option', { name: '百炼' })
    assert.ok(screen.getByText('旧版对话记录'))
    fireEvent.change(screen.getByRole('combobox', { name: '助手模型' }), {
      target: { value: 'bailian' },
    })
    fireEvent.change(screen.getByRole('textbox', { name: '向画布助手提问' }), {
      target: { value: '画布里有什么' },
    })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByRole('button', { name: '停止' })
    assert.equal(requests[0].modelId, 'bailian')
    assert.deepEqual(requests[0].selectedObjectIds, ['selected'])
    assert.equal(screen.queryByText('运行详情'), null)
    const info = screen.getByLabelText('查看执行信息')
    fireEvent.click(info)
    assert.ok(screen.getByText('画布版本 5'))
    fireEvent.click(screen.getByRole('button', { name: '停止' }))
    await screen.findByText('已停止')
    assert.deepEqual(stops, ['run'])
    assert.deepEqual(history, original)
  } finally {
    close()
  }
})

test('保存冲突保留输入且不发Agent请求', async () => {
  const { close, requests } = setup({ saveFailure: true })
  try {
    await screen.findByRole('option', { name: 'DeepSeek' })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '尚未提交的问题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByRole('alert')
    assert.match(screen.getByRole('alert').textContent ?? '', /版本冲突/)
    assert.equal((screen.getByRole('textbox') as HTMLTextAreaElement).value, '尚未提交的问题')
    assert.equal(requests.length, 0)
  } finally {
    close()
  }
})

test('展示真实工具输入输出与最终回复，输出文本不能成为HTML', async () => {
  const { close } = setup({
    initial: {
      id: 'run',
      clientRequestId: 'request',
      status: 'completed',
      message: '检查图片',
      modelId: 'deepseek',
      baseRevision: 8,
      reply: '找到一张图片',
      error: null,
      modelCalls: [],
      createdAt: '',
      updatedAt: '',
      toolCalls: [
        {
          id: 'call',
          name: 'inspect_image',
          status: 'completed',
          arguments: { assetId: 'a' },
          output: { width: 1024, comment: '<img src=x onerror=alert(1)>' },
        },
      ],
    },
  })
  try {
    await screen.findByText('找到一张图片')
    const process = screen.getByRole('region', { name: '处理过程' })
    assert.match(process.textContent ?? '', /inspect_image/)
    const tool = screen.getByRole('button', { name: /其他\s*a\s*已返回/ })
    assert.equal(tool.getAttribute('aria-expanded'), 'false')
    fireEvent.click(tool)
    fireEvent.click(screen.getByRole('button', { name: '展开返回数据' }))
    await waitFor(() => assert.match(screen.getByText(/"width": 1024/).textContent ?? '', /1024/))
    assert.equal(document.querySelector('img'), null)
    assert.equal(screen.queryByRole('button', { name: '停止' }), null)
  } finally {
    close()
  }
})

test('建议只填入草稿，Shift Enter 与中文输入不提交，Enter提交一次', async () => {
  const { close, requests } = setup()
  try {
    await screen.findByRole('option', { name: 'DeepSeek' })
    fireEvent.click(screen.getByRole('button', { name: '画布上有哪些素材？' }))
    const input = screen.getByRole('textbox', { name: '向画布助手提问' }) as HTMLTextAreaElement
    assert.equal(input.value, '画布上有哪些素材？')
    assert.equal(document.activeElement, input)
    assert.equal(requests.length, 0)
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    assert.equal(requests.length, 0)
    fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findByRole('button', { name: '停止' })
    assert.equal(requests.length, 1)
    fireEvent.keyDown(input, { key: 'Enter' })
    assert.equal(requests.length, 1)
  } finally {
    close()
  }
})

test('生产 Host 隐藏模型选择，过程默认可见，样片确认新正片请求', async () => {
  const run: AgentRun = {
    id: 'preview',
    clientRequestId: 'request',
    status: 'awaiting_user_final',
    message: '四方向创作',
    reply: '样片完成',
    error: null,
    modelId: 'host',
    baseRevision: 5,
    modelCalls: [],
    toolCalls: [],
    createdAt: '',
    updatedAt: '',
    media: {
      tier: 'preview',
      goalType: 'image',
      previewRunId: null,
      quoteCredits: 40,
      chargedCredits: 40,
      refundCredits: 0,
      completed: 4,
      total: 4,
      documentRevision: 5,
      cancelReason: null,
      children: [],
    },
  }
  let finalRequest: { id: string; revision: number } | undefined
  const api: AgentApi = {
    models: async () => ({
      hostEnabled: true,
      models: [{ id: 'host', label: '画布助手', provider: 'host', model: 'host' }],
      defaultModelId: 'host',
    }),
    snapshot: async () => ({ documentId: 'doc', sessionId: 's', cursor: 0, runs: [run] }),
    create: async () => ({ run }),
    get: async () => ({ run }),
    stop: async () => ({ run }),
    final: async (id, _request, revision) => {
      finalRequest = { id, revision }
      return {
        run: {
          ...run,
          id: 'final',
          status: 'queued',
          media: { ...run.media!, tier: 'final', previewRunId: 'preview' },
        },
      }
    },
  }
  const store = createAgentStore({
    documentId: 'doc',
    api,
    ensureSaved: async () => 6,
    eventSourceFactory: () => ({ addEventListener() {}, close() {} }),
  })
  render(<AgentPanel store={store} history={[]} getSelection={() => []} />)
  let close!: () => void
  act(() => {
    close = store.start()
  })
  try {
    await screen.findByRole('button', { name: '生成正片' })
    assert.equal(screen.queryByRole('combobox', { name: '助手模型' }), null)
    assert.ok(screen.getByRole('region', { name: '处理过程' }))
    fireEvent.click(screen.getByRole('button', { name: '生成正片' }))
    assert.equal(finalRequest, undefined)
    fireEvent.click(screen.getByRole('button', { name: '暂不生成' }))
    assert.equal(finalRequest, undefined)
    fireEvent.click(screen.getByRole('button', { name: '生成正片' }))
    fireEvent.click(screen.getByRole('button', { name: '确认生成正片' }))
    await waitFor(() => assert.deepEqual(finalRequest, { id: 'preview', revision: 6 }))
  } finally {
    close()
  }
})

test('运行时停止位于输入区，输入新需求后保留发送与停止两个入口', async () => {
  const { store, close } = setup({ hostEnabled: true })
  try {
    await screen.findByRole('button', { name: '发送' })
    const input = screen.getByRole('textbox', { name: '向画布助手提问' })
    fireEvent.change(input, { target: { value: '生成一张图片' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    const stop = await screen.findByRole('button', { name: '停止' })
    assert.ok(stop.closest('.next-agent-composer-toolbar'))
    assert.equal(document.querySelector('.next-agent-run button.next-agent-stop'), null)
    fireEvent.change(input, { target: { value: '换成暖色' } })
    assert.ok(screen.getByRole('button', { name: '修改需求' }))
    let finish!: () => void
    let count = 0
    store.stop = async () => {
      count++
      await new Promise<void>((resolve) => {
        finish = resolve
      })
    }
    fireEvent.click(screen.getByRole('button', { name: '停止' }))
    const waiting = screen.getByRole('button', { name: '正在停止' }) as HTMLButtonElement
    assert.equal(waiting.disabled, true)
    fireEvent.click(waiting)
    assert.equal(count, 1)
    finish()
    await waitFor(() => assert.ok(screen.getByRole('button', { name: '修改需求' })))
  } finally {
    close()
  }
})

test('抠图交接显示画布确认入口，不显示未支持审批或付费生成入口', async () => {
  const initial: AgentRun = {
    id: 'cutout-run',
    clientRequestId: 'request',
    status: 'waiting_approval',
    message: '帮这张图抠图',
    reply: '请在画布检查主体选区，点击抠图确认',
    error: null,
    modelId: 'host',
    baseRevision: 5,
    modelCalls: [],
    toolCalls: [],
    createdAt: '2026-10-16T00:00:00Z',
    updatedAt: '2026-10-16T00:00:00Z',
    canvasAction: {
      type: 'cutout',
      documentId: 'doc',
      sourceObjectId: 'image',
      sourceAssetId: 'asset',
    },
  }
  const { close } = setup({ initial, hostEnabled: true })
  try {
    assert.ok(await screen.findByRole('button', { name: '进入抠图模式' }))
    assert.ok(screen.getByRole('button', { name: '取消抠图' }))
    assert.equal(screen.queryByRole('button', { name: '确认生成' }), null)
    assert.equal(screen.queryByText('暂不支持审批，操作未执行。'), null)
  } finally {
    close()
  }
})

test('意图澄清按钮发送绑定的run与选项ID', async () => {
  const initial: AgentRun = {
    id: 'clarify-run',
    clientRequestId: 'original',
    status: 'waiting_input',
    message: '活一点',
    reply: null,
    error: null,
    modelId: 'host',
    baseRevision: 5,
    modelCalls: [],
    toolCalls: [],
    createdAt: '',
    updatedAt: '',
    waiting: {
      stage: 'intent',
      question: '改图还是视频？',
      options: [{ id: 'image_to_multiframe', label: '做成视频' }],
    },
  }
  const { requests, close } = setup({ initial, hostEnabled: true })
  try {
    const button = await screen.findByRole('button', { name: '做成视频' })
    await waitFor(() => assert.equal(button.hasAttribute('disabled'), false))
    fireEvent.click(button)
    await waitFor(() => assert.equal(requests.length, 1))
    assert.equal(requests[0].clarifyRunId, 'clarify-run')
    assert.equal(requests[0].clarifyOptionId, 'image_to_multiframe')
  } finally {
    close()
  }
})

test('等待澄清时可把当前草稿作为新需求发送', async () => {
  const initial: AgentRun = {
    id: 'expired-clarify',
    clientRequestId: 'original',
    status: 'waiting_input',
    message: '活一点',
    reply: null,
    error: null,
    modelId: 'host',
    baseRevision: 5,
    modelCalls: [],
    toolCalls: [],
    createdAt: '',
    updatedAt: '',
    waiting: { stage: 'intent', question: '改图还是视频？' },
  }
  const { requests, close, store } = setup({ initial, hostEnabled: true })
  try {
    const button = await screen.findByRole('button', { name: '作为新需求发送' })
    assert.equal(button.hasAttribute('disabled'), true)
    act(() => store.setDraft('雪山海报'))
    await waitFor(() => assert.equal(button.hasAttribute('disabled'), false))
    fireEvent.click(button)
    await waitFor(() => assert.equal(requests.length, 1))
    assert.equal(requests[0].message, '雪山海报')
    assert.equal(requests[0].clarifyRunId, undefined)
  } finally {
    close()
  }
})
