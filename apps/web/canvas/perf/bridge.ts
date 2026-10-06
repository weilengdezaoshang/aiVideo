import type { CanvasView } from '../core/canvas-view.js'
import type { DocStore } from '../state/doc-store.js'
import type { BenchmarkBridge } from './protocol.js'

/** Opt-in benchmark seam; never enabled on an ordinary canvas URL. */
export function exposeBenchmark(view: CanvasView, store: DocStore): () => void {
  const params = new URLSearchParams(location.search)
  if (params.get('benchmark') !== '1') {
    return () => {}
  }
  const diagnostic = params.get('mode') === 'diagnostic'
  const counts = { documentNotifications: 0, viewNotifications: 0 }
  const offDoc = diagnostic ? store.subscribe(() => counts.documentNotifications++) : () => {}
  const offView = diagnostic ? view.subscribe(() => counts.viewNotifications++) : () => {}
  const bridge: BenchmarkBridge = {
    state() {
      const snapshot = view.getSnapshot()
      return {
        documentId: store.doc.id,
        objectCount: store.doc.order.length,
        selectionCount: snapshot.selection.length,
        camera: { ...snapshot.camera },
        viewport: { ...snapshot.viewport },
      }
    },
    counters: () => (diagnostic ? { ...counts } : null),
  }
  window.__canvasBenchmark = bridge
  return () => {
    offDoc()
    offView()
    if (window.__canvasBenchmark === bridge) {
      delete window.__canvasBenchmark
    }
  }
}
