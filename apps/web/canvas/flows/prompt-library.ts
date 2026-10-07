// T18 提示词收藏复用:收藏存全局 localStorage,历史来自文档生成实体(复用现有能力)。

export type PromptFavorite = { id: string; prompt: string; createdAt: number }

const KEY = 'gencanvas.promptFavorites.v1'
const MAX_FAVORITES = 200
const MAX_RECENT = 50

export function loadFavorites(storage: Storage): PromptFavorite[] {
  try {
    const raw: unknown = JSON.parse(storage.getItem(KEY) || '[]')
    if (!Array.isArray(raw)) {
      return []
    }
    const items: PromptFavorite[] = []
    for (const entry of raw) {
      if (!entry || typeof entry !== 'object') {
        continue
      }
      const item = entry as Record<string, unknown>
      if (
        typeof item.id === 'string' &&
        typeof item.prompt === 'string' &&
        typeof item.createdAt === 'number' &&
        item.prompt.trim()
      ) {
        items.push({ id: item.id, prompt: item.prompt, createdAt: item.createdAt })
      }
    }
    return items.slice(0, MAX_FAVORITES)
  } catch {
    return []
  }
}

function persist(storage: Storage, items: PromptFavorite[]): PromptFavorite[] {
  try {
    storage.setItem(KEY, JSON.stringify(items.slice(0, MAX_FAVORITES)))
  } catch {
    /* storage unavailable: the in-memory list still works for this session */
  }
  return items
}

/** 收藏一条提示词;同一文本去重并置顶。 */
export function addFavorite(storage: Storage, prompt: string): PromptFavorite[] {
  const text = prompt.trim()
  if (!text) {
    return loadFavorites(storage)
  }
  const rest = loadFavorites(storage).filter((item) => item.prompt !== text)
  return persist(storage, [
    { id: crypto.randomUUID(), prompt: text, createdAt: Date.now() },
    ...rest,
  ])
}

export function removeFavorite(storage: Storage, id: string): PromptFavorite[] {
  return persist(
    storage,
    loadFavorites(storage).filter((item) => item.id !== id),
  )
}

export function isFavorite(favorites: readonly PromptFavorite[], prompt: string): boolean {
  const text = prompt.trim()
  return text.length > 0 && favorites.some((item) => item.prompt === text)
}

/** 文档内最近的提示词(新→旧,去重);生成实体没有时间戳,按记录插入顺序(即创建顺序)取。 */
export function recentPrompts(
  generations: Record<string, { spec: { params: Record<string, unknown> } }> | undefined,
): string[] {
  const seen = new Set<string>()
  const entries = Object.values(generations ?? {})
  const prompts: string[] = []
  for (let index = entries.length - 1; index >= 0 && prompts.length < MAX_RECENT; index -= 1) {
    const value = entries[index].spec.params.prompt
    if (typeof value !== 'string') {
      continue
    }
    const text = value.trim()
    if (text && !seen.has(text)) {
      seen.add(text)
      prompts.push(text)
    }
  }
  return prompts
}
