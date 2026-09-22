import { useCallback, useEffect, useRef, useState } from 'react'
// 落地页(PRD §2 首屏):模式 Tab、视频镜头运动示意、首屏视觉参考切换。
// 与原 landing.js 行为一致:示例素材均为本地视觉参考,页面明确标注"非生成结果"。
// 动效沿用 WAAPI + prefers-reduced-motion 门控;页面隐藏/离开时停止播放。

import {
  HERO_SCENE_LABEL,
  MODE_ORDER,
  MODE_TAB_LABEL,
  MODES,
  heroSceneUrl,
  type HeroScene,
  type LandingMode,
} from './modes.js'

const ARROW_SVG = (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M5 12h14m-6-6 6 6-6 6" />
  </svg>
)

const VIDEO_DURATION_SEC = 4

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** Tab 键盘导航(PRD 无障碍约定):左右循环,Home/End 定位。 */
function nextTabIndex(key: string, index: number, length: number): number | undefined {
  if (key === 'ArrowRight') {
    return (index + 1) % length
  }
  if (key === 'ArrowLeft') {
    return (index + length - 1) % length
  }
  if (key === 'Home') {
    return 0
  }
  if (key === 'End') {
    return length - 1
  }
  return undefined
}

export function LandingPage() {
  const [mode, setMode] = useState<LandingMode>('generate')
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [scene, setScene] = useState<HeroScene>('ocean')
  const content = MODES[mode]
  const isVideoMode = mode === 'video'

  const copyRef = useRef<HTMLDivElement>(null)
  const heroImageRef = useRef<HTMLImageElement>(null)
  const playbackStartRef = useRef<number | null>(null)
  const frameRef = useRef(0)
  const progressRef = useRef(0)
  const sceneSequence = useRef(0)

  progressRef.current = progress

  const renderProgress = useCallback((value: number) => {
    setProgress(value)
  }, [])

  const stopPlayback = useCallback(() => {
    if (playbackStartRef.current !== null) {
      cancelAnimationFrame(frameRef.current)
      playbackStartRef.current = null
      setPlaying(false)
    }
  }, [])

  const startPlayback = useCallback(() => {
    if (playbackStartRef.current !== null) {
      stopPlayback()
      return
    }
    const from = progressRef.current >= 100 ? 0 : progressRef.current
    if (progressRef.current >= 100) {
      renderProgress(0)
    }
    playbackStartRef.current = performance.now() - from * 40
    setPlaying(true)
    const tick = (time: number) => {
      if (playbackStartRef.current === null) {
        return
      }
      const value = Math.min(100, (time - playbackStartRef.current) / 40)
      renderProgress(value)
      if (value >= 100) {
        stopPlayback()
        return
      }
      frameRef.current = requestAnimationFrame(tick)
    }
    frameRef.current = requestAnimationFrame(tick)
  }, [renderProgress, stopPlayback])

  /** 模式切换:内容重渲染 + 面板入场动画(尊重 reduced-motion)。 */
  const selectMode = useCallback(
    (next: LandingMode, focusTab?: HTMLElement | null) => {
      setMode((prev) => {
        if (prev !== next) {
          stopPlayback()
          renderProgress(0)
        }
        return next
      })
      if (focusTab) {
        focusTab.focus({ preventScroll: true })
      }
    },
    [renderProgress, stopPlayback],
  )

  // 面板入场动画(240ms,与原 WAAPI 时序一致)
  useEffect(() => {
    const element = copyRef.current
    if (!element || prefersReducedMotion()) {
      return
    }
    const animation = element.animate(
      [
        { opacity: 0.45, transform: 'translateY(8px)' },
        { opacity: 1, transform: 'translateY(0)' },
      ],
      { duration: 240, easing: 'cubic-bezier(.23,1,.32,1)' },
    )
    return () => animation.cancel()
  }, [mode])

  // 首屏视觉切换:预加载解码成功后才换图,快速连点只保留最后一次
  const switchScene = useCallback(async (next: HeroScene) => {
    const sequence = ++sceneSequence.current
    const preload = new Image()
    preload.src = heroSceneUrl(next)
    try {
      await preload.decode()
    } catch {
      return
    }
    if (sequence !== sceneSequence.current) {
      return
    }
    const image = heroImageRef.current
    setScene(next)
    if (image && !prefersReducedMotion()) {
      image.animate(
        [
          { opacity: 0.4, transform: 'scale(1.025)' },
          { opacity: 1, transform: 'scale(1)' },
        ],
        { duration: 420, easing: 'cubic-bezier(.23,1,.32,1)' },
      )
    }
  }, [])

  // 系统级动效偏好变化:停止播放、取消入场动画残留
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => {
      stopPlayback()
    }
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [stopPlayback])

  // 页面隐藏/离开:停止播放(不残留 rAF)
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) {
        stopPlayback()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', stopPlayback)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', stopPlayback)
    }
  }, [stopPlayback])

  const progressSeconds = ((progress / 100) * VIDEO_DURATION_SEC).toFixed(1)

  return (
    <>
      <a className="skip-link" href="#main">
        跳至主要内容
      </a>
      <header className="nav shell">
        <a className="identity-brand" href="/" aria-label="帧屿集 FRAYUNE 首页">
          <span className="identity-mark" aria-hidden="true" />
          <span className="identity-wordmark" aria-hidden="true">
            <span className="identity-en" />
            <span className="identity-cn" />
          </span>
        </a>
        <a className="text-link" href="/workspace">
          进入工作台 {ARROW_SVG}
        </a>
      </header>
      <main id="main">
        <section className="intro" aria-labelledby="hero-title">
          <div className="hero-backdrop" aria-hidden="true">
            <img
              id="hero-visual"
              ref={heroImageRef}
              src={heroSceneUrl(scene)}
              alt=""
              width={1600}
              height={1000}
              fetchPriority="high"
            />
          </div>
          <div className="hero-heading">
            <h1 id="hero-title">
              让想象，
              <br />
              不止于一帧。
            </h1>
            <p>AI 图像与视频创作平台</p>
          </div>
          <div className="hero-launch">
            <a className="text-link" href="/workspace">
              开始创作 {ARROW_SVG}
            </a>
          </div>
          <div className="hero-scenes" role="group" aria-label="切换首屏视觉参考">
            {(Object.keys(HERO_SCENE_LABEL) as HeroScene[]).map((key) => (
              <button
                key={key}
                type="button"
                data-scene={key}
                aria-label={`切换到${HERO_SCENE_LABEL[key]}`}
                title={HERO_SCENE_LABEL[key]}
                aria-pressed={scene === key}
                onClick={() => void switchScene(key)}
              >
                <span aria-hidden="true" />
              </button>
            ))}
          </div>
          <p className="hero-credit">视觉参考 · 非生成结果</p>
        </section>
        <section className="showcase shell" id="creation" aria-label="创作能力展示">
          <div className="section-intro">
            <h2>从静态画面，到动态表达。</h2>
            <p>生成图像、制作视频，在画布继续创作。</p>
            <button
              className="text-link video-entry"
              id="explore-video"
              type="button"
              onClick={(event) => {
                const tab = document.getElementById('mode-video')
                selectMode('video', tab)
                document.getElementById('creation')?.scrollIntoView({
                  behavior: prefersReducedMotion() ? 'instant' : 'smooth',
                  block: 'start',
                })
                event.currentTarget.blur()
              }}
            >
              探索视频创作 <span aria-hidden="true">↓</span>
            </button>
          </div>
          <div className="showcase-heading">
            <div className="mode-tabs" role="tablist" aria-label="创作方式">
              {MODE_ORDER.map((item, index) => (
                <button
                  key={item}
                  id={`mode-${item}`}
                  role="tab"
                  aria-selected={mode === item}
                  aria-controls="mode-panel"
                  data-mode={item}
                  tabIndex={mode === item ? 0 : -1}
                  onClick={(event) => selectMode(item, event.currentTarget)}
                  onKeyDown={(event) => {
                    const next = nextTabIndex(event.key, index, MODE_ORDER.length)
                    if (next === undefined) {
                      return
                    }
                    event.preventDefault()
                    selectMode(
                      MODE_ORDER[next],
                      event.currentTarget.parentElement?.children[next] as HTMLElement,
                    )
                  }}
                >
                  {MODE_TAB_LABEL[item]}
                </button>
              ))}
            </div>
            <span className="demo-label">功能示意</span>
          </div>
          <div
            className="studio"
            id="mode-panel"
            role="tabpanel"
            aria-labelledby={`mode-${mode}`}
            tabIndex={0}
            data-mode={mode}
          >
            <div className="studio-copy" ref={copyRef}>
              <span className="studio-mark identity-mark" aria-hidden="true" />
              <h2 id="mode-title">
                {content.title[0]}
                <br />
                {content.title[1]}
              </h2>
              <p id="mode-description">{content.description}</p>
              <div className="sample">
                <span id="sample-label">{content.label}</span>
                <p id="sample-text">{content.sample}</p>
              </div>
              <a className="text-link mode-action" href="/workspace" id="mode-action">
                进入创作工作台 <span aria-hidden="true">↗</span>
              </a>
            </div>
            <figure className="studio-art">
              <div className="image-frame">
                <img
                  src="/landing/assets/architecture.jpg"
                  alt="沙丘中建筑的彩色视觉参考"
                  width={1200}
                  height={900}
                  loading="lazy"
                  style={
                    isVideoMode && !prefersReducedMotion()
                      ? { transform: `scale(${1 + progress * 0.0012})` }
                      : undefined
                  }
                />
                <span className="crop-corner corner-tl" aria-hidden="true" />
                <span className="crop-corner corner-tr" aria-hidden="true" />
                <span className="crop-corner corner-bl" aria-hidden="true" />
                <span className="crop-corner corner-br" aria-hidden="true" />
                <span className="frame-label" id="frame-label" hidden={!isVideoMode}>
                  首帧参考
                </span>
              </div>
              <div className="motion-controls" id="motion-controls" hidden={!isVideoMode}>
                <button
                  id="motion-toggle"
                  type="button"
                  aria-pressed={playing}
                  onClick={startPlayback}
                >
                  {playing ? '暂停示意' : '播放示意'}
                </button>
                <input
                  id="motion-progress"
                  type="range"
                  min={0}
                  max={100}
                  value={progress}
                  aria-label="镜头运动进度"
                  onChange={(event) => {
                    stopPlayback()
                    renderProgress(Number(event.target.value))
                  }}
                />
                <output id="motion-time" htmlFor="motion-progress">
                  {progressSeconds} / {VIDEO_DURATION_SEC.toFixed(1)} s
                </output>
              </div>
              <figcaption id="art-caption">
                {isVideoMode
                  ? '静态参考图的镜头运动示意 · 非 AI 生成视频'
                  : '视觉参考素材 · 非实时生成'}
              </figcaption>
            </figure>
          </div>
        </section>
        <section className="workflow shell" aria-label="平台相关功能">
          <details>
            <summary>
              图像生成与加工 <span aria-hidden="true">+</span>
            </summary>
            <p>文生图、参考图生图与局部重绘；智能抠图支持检查蒙版后确认处理。</p>
          </details>
          <details>
            <summary>
              视频生成与管理 <span aria-hidden="true">+</span>
            </summary>
            <p>参考图作为视频首帧，描述镜头与动作；查看生成进度，预览并下载完成的视频。</p>
          </details>
          <details>
            <summary>
              画布与素材管理 <span aria-hidden="true">+</span>
            </summary>
            <p>自由编排素材、保存创作文档；收藏与筛选作品，失败任务可重新尝试。</p>
          </details>
        </section>
      </main>
      <footer className="footer shell">
        <p>帧屿集 · AI 视觉创作平台</p>
        <a className="text-link" href="/workspace">
          打开你的画布 {ARROW_SVG}
        </a>
      </footer>
    </>
  )
}
