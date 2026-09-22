import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type PropsWithChildren,
} from 'react'

import { createCanvasView, type CanvasView } from '../core/canvas-view.js'
import type { CanvasViewSnapshot, ViewCapabilities } from '../core/types.js'
import type { DocStore } from '../state/doc-store.js'

const CanvasViewContext = createContext<CanvasView | null>(null)

export type CanvasViewProviderProps = PropsWithChildren<{
  store: DocStore
  readonly?: boolean
  capabilities?: Partial<Omit<ViewCapabilities, 'canUndo' | 'canRedo'>>
  view?: CanvasView
}>

export function CanvasViewProvider({
  store,
  readonly = false,
  capabilities,
  view: providedView,
  children,
}: CanvasViewProviderProps) {
  const view = useMemo(
    () => providedView ?? createCanvasView({ store, readonly, capabilities }),
    [providedView, store, readonly, capabilities],
  )
  const lifecycleVersion = useRef(0)

  useEffect(() => {
    const version = ++lifecycleVersion.current
    return () => {
      queueMicrotask(() => {
        if (!providedView && lifecycleVersion.current === version) {
          view.destroy()
        }
      })
    }
  }, [providedView, view])

  return <CanvasViewContext.Provider value={view}>{children}</CanvasViewContext.Provider>
}

export function useCanvasView(): CanvasView {
  const view = useContext(CanvasViewContext)
  if (!view) {
    throw new Error('useCanvasView 必须在 CanvasViewProvider 内使用')
  }
  return view
}

export function useCanvasViewSnapshot(): CanvasViewSnapshot {
  const view = useCanvasView()
  return useSyncExternalStore(view.subscribe, view.getSnapshot, view.getSnapshot)
}
