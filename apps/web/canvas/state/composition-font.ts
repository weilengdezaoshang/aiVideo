/** Pinned Noto Sans SC. The browser loads this exact file; it does not assume a local install. */

export const COMPOSITION_FONT = {
  id: 'noto-sans-sc',
  sha256: 'faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9',
} as const

export type FontLoad = { state: 'ready'; sha256: string } | { state: 'error'; message: string }

const cache = new Map<string, FontLoad>()

type FontList = { fonts: { id: string; sha256: string; version: string }[] }

export async function ensureCompositionFont(
  fetchImpl: typeof fetch = fetch,
  fonts: { add: (face: FontFace) => void } | null = browserFontRegistry(),
): Promise<FontLoad> {
  const cached = cache.get(COMPOSITION_FONT.sha256)
  if (cached?.state === 'ready') {
    return cached
  }
  try {
    const listed = (await (await fetchImpl('/api/fonts')).json()) as FontList
    const match = listed.fonts.find((font) => font.id === COMPOSITION_FONT.id)
    if (!match || match.sha256 !== COMPOSITION_FONT.sha256) {
      return fail('字体清单与固定哈希不一致')
    }
    const response = await fetchImpl(
      `/api/fonts/${COMPOSITION_FONT.id}?sha256=${COMPOSITION_FONT.sha256}`,
    )
    if (!response.ok) {
      return fail('字体文件加载失败')
    }
    const bytes = await response.arrayBuffer()
    const digest = await crypto.subtle.digest('SHA-256', bytes)
    const sha256 = [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('')
    if (sha256 !== COMPOSITION_FONT.sha256) {
      return fail('字体哈希与固定值不一致')
    }
    if (typeof FontFace === 'undefined' || !fonts) {
      return fail('当前环境不能注册字体')
    }
    const face = new FontFace(COMPOSITION_FONT.id, bytes)
    await face.load()
    fonts.add(face)
    const ready: FontLoad = { state: 'ready', sha256 }
    cache.set(sha256, ready)
    return ready
  } catch {
    return fail('字体未能加载，未改用其他字体')
  }
}

function fail(message: string): FontLoad {
  const error: FontLoad = { state: 'error', message }
  cache.set(COMPOSITION_FONT.sha256, error)
  return error
}

export function resetCompositionFontCache(): void {
  cache.clear()
}

function browserFontRegistry(): { add: (face: FontFace) => void } | null {
  if (typeof document === 'undefined') {
    return null
  }
  const registry = document.fonts
  // DOM.Iterable is absent from the standalone canvas build; probe the runtime boundary.
  if (!registry || !('add' in registry) || typeof registry.add !== 'function') {
    return null
  }
  const add = registry.add
  return {
    add: (face: FontFace) => {
      add.call(registry, face)
    },
  }
}
