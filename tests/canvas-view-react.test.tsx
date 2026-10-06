import './dom.js'
import assert from 'node:assert/strict'
import test, { afterEach } from 'node:test'
import { StrictMode } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import {
  CanvasViewProvider,
  useCanvasView,
  useCanvasViewSnapshot,
} from '../apps/web/canvas/react/canvas-view-context.js'
import { createDocStore } from '../apps/web/canvas/state/doc-store.js'

afterEach(() => cleanup())

function ToolProbe() {
  const view = useCanvasView()
  const snapshot = useCanvasViewSnapshot()
  return (
    <button type="button" onClick={() => view.activateTool('text')}>
      {snapshot.effectiveLayer}
    </button>
  )
}

test('CanvasViewProvider 在 StrictMode 重挂载后仍可切换 Layer', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  render(
    <StrictMode>
      <CanvasViewProvider store={store}>
        <ToolProbe />
      </CanvasViewProvider>
    </StrictMode>,
  )
  const button = screen.getByRole('button', { name: 'select' })
  fireEvent.click(button)
  assert.equal(button.textContent, 'text')
})

test('Provider 卸载后延迟销毁内部 CanvasView', async () => {
  const store = createDocStore({ id: 'doc', name: 'doc', objects: {}, order: [] })
  const rendered = render(
    <CanvasViewProvider store={store}>
      <ToolProbe />
    </CanvasViewProvider>,
  )
  rendered.unmount()
  await Promise.resolve()
  assert.equal(store.canUndo(), false)
})
