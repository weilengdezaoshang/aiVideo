import { useEffect, useState } from 'react'

import type { CanvasObj } from '../state/commands.js'
import { ensureCompositionFont, type FontLoad } from '../state/composition-font.js'
import {
  readTextStyle,
  textStyleUpdate,
  TEXT_ALIGNS,
  TEXT_REGIONS,
  type TextStyle,
} from '../state/text-style.js'

const FAMILIES = ['noto-sans-sc']

export function TextPropertyBar({
  object,
  onPatch,
}: {
  object: CanvasObj
  onPatch: (patch: Record<string, unknown>, prev: Record<string, unknown>) => void
}) {
  const style = readTextStyle(object)
  const [fontLoad, setFontLoad] = useState<FontLoad | null>(null)
  useEffect(() => {
    let active = true
    void ensureCompositionFont().then((result) => {
      if (active) {
        setFontLoad(result)
      }
    })
    return () => {
      active = false
    }
  }, [])
  const families = FAMILIES.includes(style.fontFamily) ? FAMILIES : [style.fontFamily, ...FAMILIES]
  function commit(patch: Partial<TextStyle>): void {
    const update = textStyleUpdate(object, patch)
    if (update) {
      onPatch(update.patch, update.prev)
    }
  }
  return (
    <form
      aria-label="文字属性"
      style={{
        display: 'flex',
        gap: 8,
        padding: 8,
        background: '#fff',
        borderBottom: '1px solid #e0e0e0',
      }}
      onSubmit={(event) => event.preventDefault()}
    >
      {fontLoad?.state === 'error' && <p role="alert">{fontLoad.message}</p>}
      <label>
        文案
        <input
          aria-label="文案"
          value={style.text}
          onChange={(event) => commit({ text: event.target.value })}
        />
      </label>
      <label>
        字体
        <select
          aria-label="字体"
          value={style.fontFamily}
          onChange={(event) => commit({ fontFamily: event.target.value })}
        >
          {families.map((family) => (
            <option key={family} value={family}>
              {family}
            </option>
          ))}
        </select>
      </label>
      <label>
        字号
        <input
          aria-label="字号"
          type="number"
          min={1}
          max={1024}
          value={style.fontSize}
          onChange={(event) => commit({ fontSize: Number(event.target.value) })}
        />
      </label>
      <label>
        颜色
        <input
          aria-label="颜色"
          value={style.fill}
          onChange={(event) => commit({ fill: event.target.value })}
        />
      </label>
      <label>
        对齐
        <select
          aria-label="对齐"
          value={style.textAlign}
          onChange={(event) => commit({ textAlign: event.target.value as TextStyle['textAlign'] })}
        >
          {TEXT_ALIGNS.map((align) => (
            <option key={align} value={align}>
              {align}
            </option>
          ))}
        </select>
      </label>
      <label>
        版区
        <select
          aria-label="版区"
          value={style.region ?? ''}
          onChange={(event) =>
            commit({ region: (event.target.value || undefined) as TextStyle['region'] })
          }
        >
          <option value="">未指定</option>
          {TEXT_REGIONS.map((region) => (
            <option key={region} value={region}>
              {region}
            </option>
          ))}
        </select>
      </label>
    </form>
  )
}
