/**
 * 四方向文本规划器(PRD §5.1):一次用户需求 → 四条结构化创意方向。
 *
 * 结构契约:四方向共享"共同主体 + 硬约束",仅在"变化维度"上互相区别
 * (构图 / 光线 / 场景 / 表达)。用户指定的风格词归入硬约束,不被方向替换;
 * 方向规划完全确定(同输入同输出),便于版本化评估集验收(A04)。
 *
 * 这是规则版规划器:不调用模型、不产生费用;接入 LLM 规划时必须保持
 * DirectionPlan 结构与"共同约束不被替换"不变量。
 */

export interface GenerationDirection {
  /** 槽位 0..3,画布横排顺序固定 */
  slot: number
  /** 方向标题(组卡片与占位标签展示) */
  title: string
  /** 变化维度:构图 / 光线 / 场景 / 表达 */
  dimension: string
  /** 发给图像模型的完整提示词 = 主体 + 硬约束 + 方向描述 */
  prompt: string
}

export interface DirectionPlan {
  /** 共同主体(全部方向必须保留) */
  subject: string
  /** 硬约束(风格词 / 用户显式限定,全部方向必须遵守) */
  constraints: string[]
  directions: GenerationDirection[]
}

/** 风格词表:命中即视为用户显式指定的风格,进入硬约束 */
const STYLE_WORDS = [
  '水墨',
  '水彩',
  '油画',
  '素描',
  '线稿',
  '像素',
  '赛博朋克',
  '蒸汽波',
  '国风',
  '浮世绘',
  '极简',
  '写实',
  '卡通',
  '动漫',
  '插画',
  '3d',
  '厚涂',
  '胶片',
  '黑白',
  '低多边形',
  '扁平',
] as const

/** 四个变化维度与各自的画面描述(彼此语义不同,共同约束之外不重叠) */
const DIMENSIONS: Array<{ title: string; dimension: string; descriptor: string }> = [
  { title: '远景氛围', dimension: '构图', descriptor: '远景构图,主体完整入画,大面积留白交代环境氛围' },
  { title: '光影刻画', dimension: '光线', descriptor: '近景构图,用强烈的方向光与阴影刻画主体的立体感' },
  { title: '场景叙事', dimension: '场景', descriptor: '中景构图,把主体放进一个能讲故事的场景,加入环境细节' },
  { title: '特写情绪', dimension: '表达', descriptor: '特写镜头,聚焦主体最具辨识度的细节,强化情绪张力' },
]

/** 分句:中英文逗号 / 顿号 / 分号 / 句号 / 换行 */
const CLAUSE_RE = /[,，、;；。\n]+/

/**
 * 从用户需求规划四条结构化方向。纯函数,确定性输出。
 */
export function planDirections(rawPrompt: string): DirectionPlan {
  const prompt = rawPrompt.trim()
  const clauses = prompt
    .split(CLAUSE_RE)
    .map((c) => c.trim())
    .filter(Boolean)

  // 硬约束:风格词命中的分句 + 显式限定词分句(不要/避免/必须/不能/无 X)
  const CONSTRAINT_RE = /(不要|不能|避免|禁止|必须|一定要|无水|无人)/
  const constraints: string[] = []
  const plainClauses: string[] = []
  for (const clause of clauses) {
    const lower = clause.toLowerCase()
    const hasStyle = STYLE_WORDS.some((w) => lower.includes(w))
    if (hasStyle || CONSTRAINT_RE.test(clause)) {
      if (!constraints.includes(clause)) {
        constraints.push(clause)
      }
    } else {
      plainClauses.push(clause)
    }
  }
  // 共同主体 = 首个非约束分句;整句都进约束时退化为全句(不丢信息)
  const subject = plainClauses[0] ?? prompt
  // 主体之外的非约束分句作为补充描述,跟随所有方向
  const extras = plainClauses.slice(1)
  const constraintText = constraints.join(',')

  const directions: GenerationDirection[] = DIMENSIONS.map((dim, index) => {
    const parts = [subject, ...extras]
    if (constraintText) {
      parts.push(constraintText)
    }
    parts.push(dim.descriptor)
    return {
      slot: index,
      title: dim.title,
      dimension: dim.dimension,
      prompt: parts.join(','),
    }
  })

  return { subject, constraints, directions }
}
