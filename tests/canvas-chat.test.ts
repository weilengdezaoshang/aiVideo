// 对话栏:消息持久化、组卡实时状态与渲染自愈(PRD §5.2/§7)。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { afterEach, describe } from 'node:test'
import { cleanup, screen } from '@testing-library/react'
import { createChat } from '../apps/web/canvas/flows/chat.js'
import { createDocStore, type CanvasDocData } from '../apps/web/canvas/state/doc-store.js'
import { cmdAddObjects } from '../apps/web/canvas/state/commands.js'

const flushRender = () => new Promise<void>((resolve) => queueMicrotask(() => resolve()))

function boot(doc: Partial<CanvasDocData> = {}) {
  const container = document.createElement('div')
  document.body.append(container)
  const docStore = createDocStore({
    id: 'doc-chat',
    name: '测试画布',
    objects: {},
    order: [],
    revision: 0,
    ...doc,
  })
  const chat = createChat({ docStore, container })
  return { container, docStore, chat }
}

afterEach(() => {
  cleanup()
  document.body.replaceChildren()
})

describe('对话栏', () => {
  test('空状态给出引导文案', () => {
    const { container } = boot()
    assert.ok(container.textContent.includes('从一个想法开始'))
  })

  test('文本消息追加进文档 chat 字段并渲染角色气泡', async () => {
    const { container, docStore, chat } = boot()
    chat.addUser('画一张海')
    chat.addSystem('计划已就绪')
    await flushRender()
    assert.equal(docStore.doc.chat?.length, 2)
    assert.ok(container.querySelector('.chat-msg.user .bubble')?.textContent === '画一张海')
    assert.ok(container.querySelector('.chat-msg.system .bubble'))
    assert.equal(docStore.doc.chat?.[0].role, 'user')
  })

  test('组卡从画布对象实时推导槽位状态', async () => {
    const { container, docStore, chat } = boot()
    docStore.apply(
      cmdAddObjects([
        {
          id: 's0',
          kind: 'placeholder',
          x: 0,
          y: 0,
          width: 100,
          height: 100,
          groupId: 'g1',
          slot: 0,
          gen: { jobId: 'j1', message: '准备中' },
        },
        {
          id: 's1',
          kind: 'image',
          x: 140,
          y: 0,
          width: 100,
          height: 100,
          groupId: 'g1',
          slot: 1,
          src: '/images/a.png',
        },
        {
          id: 's2',
          kind: 'error',
          x: 280,
          y: 0,
          width: 100,
          height: 100,
          groupId: 'g1',
          slot: 2,
          errorDetail: '上游超时',
        },
      ]),
    )
    chat.addGroupCard('g1', ['s0', 's1', 's2', 'gone'], '四季的海')
    await flushRender()
    const card = container.querySelector('.chat-card')!
    assert.ok(card.textContent.includes('四季的海'), '标题取组需求')
    // 3 个现存对象 + 1 个"已移除"占位格
    assert.equal(card.querySelectorAll('.group-cell').length, 4)
    assert.ok(card.textContent.includes('已移除'), '被删除的槽位显示已移除,不复活')
    assert.ok(card.textContent.includes('准备中'))
    assert.ok(card.textContent.includes('上游超时'))
  })

  test('pendingConfirm 槽位显示"结果待确认"并提供查询入口', async () => {
    const { container, docStore, chat } = boot()
    docStore.apply(
      cmdAddObjects([
        {
          id: 'p0',
          kind: 'placeholder',
          x: 0,
          y: 0,
          width: 100,
          height: 100,
          groupId: 'g2',
          slot: 0,
          pendingConfirm: true,
          gen: { jobId: '', message: '准备中' },
        },
      ]),
    )
    chat.addGroupCard('g2', ['p0'], '霓虹城市')
    await flushRender()
    const card = container.querySelector('.chat-card')!
    assert.ok(card.textContent.includes('结果待确认'))
    const check = card.querySelector('.chat-card-actions .chat-mini-btn')
    assert.ok(check?.textContent?.includes('查询结果'))
  })

  test('结果卡 pendingPlace 显示"放入画布",markPlaced 后回到定位入口', async () => {
    const { docStore, chat } = boot()
    chat.addResultCard({
      objId: 'cut-1',
      assetUrl: '/assets/a1/t256.webp',
      sourceLabel: '画布源图',
      sessionId: 'sess-1',
      pendingPlace: true,
      assetId: 'a1',
      ext: 'png',
      width: 512,
      height: 512,
      hasThumbs: true,
    })
    await flushRender()
    assert.ok(screen.getByText('结果已生成,尚未加入画布'))
    assert.ok(screen.getByText('放入画布'))
    chat.markPlaced('sess-1', 'cut-9')
    await flushRender()
    assert.ok(screen.getByText('抠图完成'))
    assert.ok(screen.getByText('定位到画布'))
    const entry = docStore.doc.chat?.[0]
    assert.equal(entry?.pendingPlace, false)
    assert.equal(entry?.objId, 'cut-9', '落位信息回写同一条结果卡')
  })

  test('updateSession 合并到同会话消息,不追加新消息', async () => {
    const { docStore, chat } = boot()
    chat.addPlanCard('sess-9', { steps: [{ label: '识别主体' }] }, '抠出人物')
    chat.updateSession('sess-9', {
      waitingMask: true,
      objId: 'img-1',
      maskUrl: '/assets/m/orig.png',
    })
    await flushRender()
    assert.equal(docStore.doc.chat?.length, 1, '会话更新不产生新消息')
    const entry = docStore.doc.chat?.[0]
    assert.equal(entry?.waitingMask, true)
    assert.ok(screen.getByText('检查主体选区'), '等待蒙版时提供定位入口')
  })
})
