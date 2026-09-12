// 单标签页约束(PRD §9):BroadcastChannel 握手语义。
import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { watchDuplicateTabs } from '../apps/web/canvas/state/duplicate-tabs.js'

/** 捕获消息回调的 BroadcastChannel 测试替身 */
class FakeBroadcastChannel {
  static instances: FakeBroadcastChannel[] = []
  onmessage: ((event: { data: unknown }) => void) | null = null
  constructor(public name: string) {
    FakeBroadcastChannel.instances.push(this)
  }
  postMessage(data: unknown) {
    for (const other of FakeBroadcastChannel.instances) {
      if (other !== this) {
        queueMicrotask(() => other.onmessage?.({ data }))
      }
    }
  }
  close() {}
}

test('环境不支持 BroadcastChannel 时静默降级,不误报', () => {
  const original = globalThis.BroadcastChannel
  // @ts-expect-error 测试删除全局
  delete globalThis.BroadcastChannel
  try {
    let warned = 0
    const handle = watchDuplicateTabs({ onDuplicate: () => warned++ })
    handle.close()
    assert.equal(warned, 0)
  } finally {
    globalThis.BroadcastChannel = original
  }
})

test('收到 hello / exists 握手时只警告一次,close 后不再接收', async () => {
  const original = globalThis.BroadcastChannel
  FakeBroadcastChannel.instances = []
  globalThis.BroadcastChannel = FakeBroadcastChannel as unknown as typeof BroadcastChannel
  try {
    let warned = 0
    const handle = watchDuplicateTabs({ onDuplicate: () => warned++ })
    await new Promise((resolve) => setTimeout(resolve, 0))
    // 对端 hello → 回应 exists 并警告;对端 exists → 再次到达也只警告一次
    const channel = FakeBroadcastChannel.instances[0]
    channel.onmessage?.({ data: 'hello' })
    assert.equal(warned, 1)
    channel.onmessage?.({ data: 'exists' })
    assert.equal(warned, 1, '同一标签页会话只警告一次')
    handle.close()
    channel.onmessage?.({ data: 'hello' })
    assert.equal(warned, 1, 'close 之后不再警告')
  } finally {
    globalThis.BroadcastChannel = original
  }
})
