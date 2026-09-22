// 首屏模式文案(与 landing.css 的展示样式配套);title 两行分别渲染。
export type LandingMode = 'generate' | 'edit' | 'video'

export type LandingModeContent = {
  title: [string, string]
  description: string
  label: string
  sample: string
}

export const MODES: Record<LandingMode, LandingModeContent> = {
  generate: {
    title: ['把想象，', '变成可见。'],
    description: '描述画面，选择模型，让第一张图从这里开始。',
    label: '提示词示例',
    sample: '暖色沙丘中的极简建筑，蓝天与柔和的自然光。',
  },
  edit: {
    title: ['让细节，', '更近一步。'],
    description: '局部重绘、智能抠图，在原图基础上继续创作。',
    label: '加工流程',
    sample: '选择图片 → 检查选区 → 确认加工',
  },
  video: {
    title: ['让画面，', '继续发生。'],
    description: '以图片作为首帧，描述动作，生成视频。',
    label: '动态描述示例',
    sample: '镜头缓慢推近，光影掠过建筑表面。',
  },
}

export const MODE_ORDER: LandingMode[] = ['generate', 'edit', 'video']

export const MODE_TAB_LABEL: Record<LandingMode, string> = {
  generate: 'AI 生图',
  edit: '图片加工',
  video: 'AI 视频',
}

/** 首屏视觉参考场景(本地素材,非生成结果) */
export type HeroScene = 'ocean' | 'architecture' | 'flower'

export const HERO_SCENE_LABEL: Record<HeroScene, string> = {
  ocean: '海面',
  architecture: '建筑',
  flower: '光影',
}

export const heroSceneUrl = (scene: HeroScene) => `/landing/assets/${scene}.jpg`
