import type { ReactNode } from 'react'

import type { CanvasObj } from '../state/commands.js'

export function layeredLines(object: CanvasObj): NonNullable<CanvasObj['lineBoxes']> | null {
  if (object.layerRole === 'text' && object.lineBoxes && object.lineBoxes.length > 0) {
    return object.lineBoxes
  }
  return null
}

/** DOM readout of the same line boxes the canvas draws. Save/reload tests read this text. */
export function LayeredTextReadout({ object }: { object: CanvasObj }): ReactNode {
  const lines = layeredLines(object)
  if (!lines) {
    return <p data-testid="layered-text">{object.text ?? ''}</p>
  }
  return (
    <div data-testid="layered-text">
      {lines.map((line, index) => (
        <span
          key={`${index}-${line.text}`}
          data-align={object.textAlign}
          data-font={object.fontFamily}
        >
          {line.text}
        </span>
      ))}
    </div>
  )
}
