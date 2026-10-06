import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentPanel } from '../apps/web/canvas-next/agent-panel.js'
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
function setup(options: { initial?: AgentRun; saveFailure?: boolean } = {}) {
  let run = options.initial
  const requests: AgentRequest[] = []
  const stops: string[] = []
  const api: AgentApi = {
    models: async () => ({
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
    assert.ok(screen.getByText('inspect_image · 已返回'))
    await waitFor(() => assert.match(screen.getByText(/"width": 1024/).textContent ?? '', /1024/))
    assert.equal(document.querySelector('img'), null)
    assert.equal(screen.queryByRole('button', { name: '停止' }), null)
  } finally {
    close()
  }
})
