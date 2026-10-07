import test from 'node:test'
import assert from 'node:assert/strict'

import {
  addFavorite,
  isFavorite,
  loadFavorites,
  recentPrompts,
  removeFavorite,
} from '../apps/web/canvas/flows/prompt-library.js'

function memoryStorage(initial: Record<string, string> = {}): Storage {
  const map = new Map(Object.entries(initial))
  return {
    get length() {
      return map.size
    },
    clear: () => map.clear(),
    getItem: (key) => map.get(key) ?? null,
    key: (index) => [...map.keys()][index] ?? null,
    removeItem: (key) => void map.delete(key),
    setItem: (key, value) => void map.set(key, value),
  }
}

test('收藏提示词:同一文本去重并置顶,空文本忽略', () => {
  const storage = memoryStorage()
  addFavorite(storage, '  一只猫  ')
  addFavorite(storage, '夕阳下的海面')
  addFavorite(storage, '一只猫')
  const items = loadFavorites(storage)
  assert.deepEqual(
    items.map((item) => item.prompt),
    ['一只猫', '夕阳下的海面'],
  )
  assert.deepEqual(addFavorite(memoryStorage(), '   '), [])
})

test('取消收藏按 id 移除,收藏在会话内持久', () => {
  const storage = memoryStorage()
  addFavorite(storage, '甲')
  const [second] = addFavorite(storage, '乙')
  const rest = removeFavorite(storage, second.id)
  assert.deepEqual(
    rest.map((item) => item.prompt),
    ['甲'],
  )
  assert.equal(isFavorite(loadFavorites(storage), '甲'), true)
  assert.equal(isFavorite(rest, '乙'), false)
})

test('损坏或非法的收藏存储不会抛错,过滤非法条目', () => {
  assert.deepEqual(loadFavorites(memoryStorage({ 'gencanvas.promptFavorites.v1': 'not-json' })), [])
  assert.deepEqual(loadFavorites(memoryStorage({ 'gencanvas.promptFavorites.v1': '{"a":1}' })), [])
  const storage = memoryStorage({
    'gencanvas.promptFavorites.v1': JSON.stringify([
      { id: 'ok', prompt: '合法条目', createdAt: 1 },
      { id: 'bad', prompt: '', createdAt: 2 },
      '不是对象',
      { id: 3, prompt: '缺类型', createdAt: 4 },
    ]),
  })
  assert.deepEqual(
    loadFavorites(storage).map((item) => item.id),
    ['ok'],
  )
})

test('最近提示词按记录插入顺序倒序、去重并跳过空白与非字符串', () => {
  const generations = {
    a: { spec: { params: { prompt: '第一条' } } },
    b: { spec: { params: { prompt: ' 第二条 ' } } },
    c: { spec: { params: { prompt: '第一条' } } },
    d: { spec: { params: { prompt: '   ' } } },
    e: { spec: { params: { prompt: 7 } } },
    f: { spec: { params: { prompt: '第三条' } } },
  }
  // c 是"第一条"的较新记录,倒序去重保留最新一次出现,因此排在 b 之前
  assert.deepEqual(recentPrompts(generations), ['第三条', '第一条', '第二条'])
  assert.deepEqual(recentPrompts(undefined), [])
})

test('最近提示词最多保留 50 条', () => {
  const generations: Record<string, { spec: { params: { prompt: string } } }> = {}
  for (let index = 0; index < 80; index += 1) {
    generations[`g${index}`] = { spec: { params: { prompt: `提示词${index}` } } }
  }
  const recent = recentPrompts(generations)
  assert.equal(recent.length, 50)
  assert.equal(recent[0], '提示词79')
  assert.equal(recent[49], '提示词30')
})
