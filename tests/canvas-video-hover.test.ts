import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clampSeek,
  createVideoHoverPreview,
  hoverFinePointer,
  hoverPlayProgress,
  schedulePosterRecache,
  shouldStartVideoHover,
  shouldStopHoverForSourceChange,
  type HoverVideoMedia,
} from '../apps/web/canvas/engine/video-hover.js'

function fakeMedia(overrides: Partial<HoverVideoMedia> = {}): HoverVideoMedia & {
  paused: boolean
} {
  const media: HoverVideoMedia & { paused: boolean } = {
    muted: false,
    loop: false,
    playsInline: false,
    currentTime: 1.5,
    duration: 8,
    readyState: 2,
    paused: true,
    play() {
      media.paused = false
      return Promise.resolve()
    },
    pause() {
      media.paused = true
    },
    ...overrides,
  }
  return media
}

function pendingPlay() {
  const resolvers: Array<() => void> = []
  const media = fakeMedia({
    play() {
      media.paused = false
      return new Promise<void>((resolve) => {
        resolvers.push(resolve)
      })
    },
  })
  return { media, resolvers }
}

function seekTarget() {
  const listeners = new Set<() => void>()
  return {
    addEventListener(_type: 'seeked', listener: () => void) {
      listeners.add(listener)
    },
    removeEventListener(_type: 'seeked', listener: () => void) {
      listeners.delete(listener)
    },
    emit() {
      for (const listener of [...listeners]) {
        listener()
      }
    },
    size() {
      return listeners.size
    },
  }
}

test('精细指针才启动悬停预览，触摸不抢选择与拖动', () => {
  assert.equal(shouldStartVideoHover({ pointerType: 'mouse', hoverFine: true }), true)
  assert.equal(shouldStartVideoHover({ hoverFine: true }), true)
  assert.equal(shouldStartVideoHover({ pointerType: 'touch', hoverFine: true }), false)
  assert.equal(shouldStartVideoHover({ pointerType: 'mouse', hoverFine: false }), false)
  assert.equal(
    hoverFinePointer((query) => ({
      matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    })),
    true,
  )
  assert.equal(
    hoverFinePointer(() => ({ matches: false })),
    false,
  )
})

test('悬停开始静音循环播放，离开后停在首帧并恢复海报回调', async () => {
  const started: string[] = []
  const stopped: string[] = []
  const hover = createVideoHoverPreview({
    onPlaying: (id) => started.push(id),
    onStopped: (id) => stopped.push(id),
  })
  const video = fakeMedia()
  hover.start('clip-a', video)
  assert.equal(video.muted, true)
  assert.equal(video.loop, true)
  assert.equal(video.playsInline, true)
  assert.equal(video.paused, false)
  assert.equal(hover.activeId(), 'clip-a')
  hover.stop('clip-a')
  await Promise.resolve()
  assert.equal(video.paused, true)
  assert.equal(video.currentTime, 0)
  assert.equal(hover.activeId(), null)
  assert.deepEqual(started, ['clip-a'])
  assert.deepEqual(stopped, ['clip-a'])
  hover.dispose()
})

test('切换对象会停掉上一个预览，mouseleave 旧对象不能打断新预览', () => {
  const stopped: string[] = []
  const hover = createVideoHoverPreview({
    onStopped: (id) => stopped.push(id),
  })
  const first = fakeMedia({ currentTime: 2 })
  const second = fakeMedia({ currentTime: 4 })
  hover.start('a', first)
  hover.start('b', second)
  assert.equal(first.paused, true)
  assert.equal(first.currentTime, 0)
  assert.equal(second.paused, false)
  assert.equal(hover.activeId(), 'b')
  hover.stop('a')
  assert.equal(second.paused, false)
  assert.equal(hover.activeId(), 'b')
  assert.deepEqual(stopped, ['a'])
  hover.dispose()
})

test('离开后迟到的 play 成功会立刻停回海报，拒绝不会抛出', async () => {
  const hover = createVideoHoverPreview()
  const { media: late, resolvers } = pendingPlay()
  hover.start('late', late)
  hover.stop('late')
  assert.equal(late.paused, true)
  assert.equal(late.currentTime, 0)
  resolvers[0]()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(late.paused, true)
  assert.equal(late.currentTime, 0)

  const rejected = fakeMedia({
    play() {
      return Promise.reject(new Error('play interrupted'))
    },
  })
  hover.start('reject', rejected)
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(hover.activeId(), null)
  assert.equal(rejected.paused, true)
  assert.equal(rejected.currentTime, 0)
  hover.dispose()
})

test('重入同一媒体时，旧 play 兑现不得 pause 新预览', async () => {
  const hover = createVideoHoverPreview()
  const { media, resolvers } = pendingPlay()
  hover.start('same', media)
  hover.stop('same')
  hover.start('same', media)
  assert.equal(media.paused, false)
  assert.equal(hover.activeId(), 'same')
  resolvers[0]()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(media.paused, false)
  assert.equal(hover.activeId(), 'same')
  resolvers[1]()
  await Promise.resolve()
  assert.equal(media.paused, false)
  hover.dispose()
})

test('两对象共享同一视频元素时，旧对象的 play 兑现不得打断新对象', async () => {
  const hover = createVideoHoverPreview()
  const { media, resolvers } = pendingPlay()
  hover.start('one', media)
  hover.start('two', media)
  assert.equal(hover.activeId(), 'two')
  assert.equal(media.paused, false)
  resolvers[0]()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(media.paused, false)
  assert.equal(hover.activeId(), 'two')
  hover.stop('one')
  assert.equal(hover.activeId(), 'two')
  assert.equal(media.paused, false)
  hover.dispose()
})

test('dispose 停止预览并忽略之后的 start 与迟到 play', async () => {
  const hover = createVideoHoverPreview()
  const { media, resolvers } = pendingPlay()
  hover.start('keep', media)
  hover.dispose()
  assert.equal(media.paused, true)
  assert.equal(hover.activeId(), null)
  resolvers[0]()
  await Promise.resolve()
  hover.start('keep', media)
  assert.equal(hover.activeId(), null)
  assert.equal(media.paused, true)
})

test('异步 seek 完成后再烘焙海报，取消或过期时不烘焙且不泄漏监听', () => {
  const media = seekTarget()
  let recache = 0
  let stale = false
  const cancel = schedulePosterRecache(
    media,
    () => recache++,
    () => stale,
    true,
  )
  assert.equal(recache, 0)
  assert.equal(media.size(), 1)
  media.emit()
  assert.equal(recache, 1)
  assert.equal(media.size(), 0)

  recache = 0
  const pending = schedulePosterRecache(
    media,
    () => recache++,
    () => stale,
    true,
  )
  stale = true
  media.emit()
  assert.equal(recache, 0)

  recache = 0
  stale = false
  const leaked = schedulePosterRecache(
    media,
    () => recache++,
    () => stale,
    true,
  )
  leaked()
  assert.equal(media.size(), 0)
  media.emit()
  assert.equal(recache, 0)
  cancel()
  pending()
})

test('相对请求 URL 与规范化 src 不同时不误停；换源且尚未 loadeddata 也要停', () => {
  const relative = '/assets/clip/original.mp4'
  assert.equal(
    shouldStopHoverForSourceChange({
      hovering: true,
      activeHoverUrl: relative,
      requestedUrl: relative,
    }),
    false,
    '同一相对路径不得因为 media.src 是绝对地址而停预览',
  )
  assert.equal(
    shouldStopHoverForSourceChange({
      hovering: true,
      activeHoverUrl: relative,
      requestedUrl: relative,
    }),
    false,
  )
  assert.equal(
    shouldStopHoverForSourceChange({
      hovering: true,
      activeHoverUrl: relative,
      requestedUrl: '/assets/clip/other.mp4',
    }),
    true,
    '请求 URL 已变且 image 尚未挂上(无 current.src)也必须停掉旧预览',
  )
  assert.equal(
    shouldStopHoverForSourceChange({
      hovering: true,
      activeHoverUrl: relative,
      requestedUrl: null,
    }),
    true,
  )
  assert.equal(
    shouldStopHoverForSourceChange({
      hovering: false,
      activeHoverUrl: relative,
      requestedUrl: '/other.mp4',
    }),
    false,
  )
})

test('海报烘焙用已播放源身份，对象已换成新 URL 时旧 seeked 过期', () => {
  const media = seekTarget()
  let recache = 0
  let objectUrl = '/old.mp4'
  const playedUrl = '/old.mp4'
  schedulePosterRecache(
    media,
    () => recache++,
    () => objectUrl !== playedUrl,
    true,
  )
  objectUrl = '/new.mp4'
  media.emit()
  assert.equal(recache, 0)
})

test('无需 seek 时立即烘焙；重悬停/换组/换 URL 视为过期', () => {
  const media = seekTarget()
  let recache = 0
  schedulePosterRecache(
    media,
    () => recache++,
    () => false,
    false,
  )
  assert.equal(recache, 1)
  assert.equal(media.size(), 0)

  recache = 0
  let rehovered = false
  const cancel = schedulePosterRecache(
    media,
    () => recache++,
    () => rehovered,
    true,
  )
  rehovered = true
  media.emit()
  assert.equal(recache, 0)
  cancel()
})

test('clampSeek 钳制到 [0, duration]，时长未就绪只能停在 0', () => {
  assert.equal(clampSeek(2.5, 8), 2.5)
  assert.equal(clampSeek(-1, 8), 0)
  assert.equal(clampSeek(12, 8), 8)
  assert.equal(clampSeek(5, 0), 0)
  assert.equal(clampSeek(5, Number.NaN), 0)
  assert.equal(clampSeek(Number.NaN, 8), 0)
  assert.equal(clampSeek(Number.POSITIVE_INFINITY, 8), 0, '非有限输入防御性回退到 0')
})

test('hoverPlayProgress 计算比例，时长或位置无效时为 0', () => {
  assert.equal(hoverPlayProgress(2, 8), 0.25)
  assert.equal(hoverPlayProgress(8, 8), 1)
  assert.equal(hoverPlayProgress(9, 8), 1)
  assert.equal(hoverPlayProgress(0, 8), 0)
  assert.equal(hoverPlayProgress(3, 0), 0)
  assert.equal(hoverPlayProgress(3, Number.NaN), 0)
  assert.equal(hoverPlayProgress(Number.NaN, 8), 0)
})

test('progress 无活跃预览时为 null，播放中返回位置与比例', () => {
  const hover = createVideoHoverPreview()
  assert.equal(hover.progress(), null)
  const media = fakeMedia({ currentTime: 2, duration: 8 })
  hover.start('p', media)
  const progress = hover.progress()
  assert.ok(progress)
  assert.equal(progress.current, 2)
  assert.equal(progress.duration, 8)
  assert.equal(progress.ratio, 0.25)
  hover.dispose()
})

test('拖动进度条：暂停不回封面，scrubTo 钳制，松手恢复播放', () => {
  const events: string[] = []
  const hover = createVideoHoverPreview({
    onPlaying: () => events.push('playing'),
    onPaused: (id) => events.push(`paused:${id}`),
  })
  const media = fakeMedia({ currentTime: 3 })
  hover.start('scrub', media)
  events.length = 0

  assert.equal(hover.isScrubbing(), false)
  hover.beginScrub()
  assert.equal(hover.isScrubbing(), true)
  assert.equal(media.paused, true)
  assert.equal(media.currentTime, 3, '暂停不得重置播放位置')
  assert.deepEqual(events, ['paused:scrub'])

  hover.scrubTo(2)
  assert.equal(media.currentTime, 2)
  hover.scrubTo(99)
  assert.equal(media.currentTime, 8, '越界拖动钳制到时长')
  hover.scrubTo(-3)
  assert.equal(media.currentTime, 0)

  events.length = 0
  hover.endScrub()
  assert.equal(hover.isScrubbing(), false)
  assert.equal(media.paused, false, '松手后恢复播放')
  assert.equal(media.currentTime, 0, '恢复播放保留拖动位置')
  assert.deepEqual(events, ['playing'])
  hover.dispose()
})

test('scrubTo 在未 beginScrub 时不生效；无活跃预览时 beginScrub/endScrub 无副作用', () => {
  const hover = createVideoHoverPreview()
  const media = fakeMedia({ currentTime: 4 })
  hover.scrubTo(1)
  assert.equal(media.currentTime, 4)

  hover.beginScrub()
  assert.equal(hover.isScrubbing(), false)
  assert.equal(media.paused, true, '无活跃预览时不得触碰媒体')
  assert.equal(media.currentTime, 4)

  hover.endScrub()
  assert.equal(hover.isScrubbing(), false)
  assert.equal(media.paused, true, '无活跃预览时 endScrub 不得触发播放')
  assert.equal(hover.activeId(), null)
  hover.dispose()
})

test('拖动中 stop 仍回首帧并复位拖动态', () => {
  const hover = createVideoHoverPreview()
  const media = fakeMedia({ currentTime: 6 })
  hover.start('s', media)
  hover.beginScrub()
  hover.scrubTo(7)
  hover.stop('s')
  assert.equal(hover.isScrubbing(), false)
  assert.equal(media.paused, true)
  assert.equal(media.currentTime, 0)
  assert.equal(hover.activeId(), null)
  hover.dispose()
})

test('拖动中的 pause 打断 play 拒绝不得杀掉预览，endScrub 可继续播放', async () => {
  const hover = createVideoHoverPreview()
  let playCount = 0
  const outcomes: Array<Promise<void>> = [
    Promise.reject(new Error('play interrupted by pause')),
    Promise.resolve(),
  ]
  const media = fakeMedia({
    currentTime: 1,
    play() {
      media.paused = false
      return outcomes[playCount++] ?? Promise.resolve()
    },
  })
  hover.start('drag', media)
  hover.beginScrub()
  assert.equal(media.paused, true)
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(hover.activeId(), 'drag', '拖动中的 play 拒绝不得停掉预览')

  hover.endScrub()
  assert.equal(media.paused, false)
  assert.equal(hover.activeId(), 'drag')
  hover.dispose()
})

test('endScrub 后恢复播放被拒时回封面，不留僵尸态', async () => {
  const stopped: string[] = []
  let playCount = 0
  const outcomes: Array<Promise<void>> = [
    Promise.resolve(),
    Promise.reject(new Error('autoplay blocked')),
  ]
  const media = fakeMedia({
    currentTime: 2,
    play() {
      media.paused = false
      return outcomes[playCount++] ?? Promise.resolve()
    },
  })
  const hover = createVideoHoverPreview({ onStopped: (id) => stopped.push(id) })
  hover.start('z', media)
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(hover.activeId(), 'z')

  stopped.length = 0
  hover.beginScrub()
  hover.endScrub()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(hover.activeId(), null)
  assert.equal(media.paused, true)
  assert.equal(media.currentTime, 0)
  assert.deepEqual(stopped, ['z'])
  hover.dispose()
})
