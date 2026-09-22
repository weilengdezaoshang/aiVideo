import type { DocStore } from '../state/doc-store.js'
import { cmdUpdateObject } from '../state/commands.js'
import type { CanvasConnection } from '../state/connections.js'

export function createConnectionInspector(store: DocStore) {
  const inspector = document.createElement('dialog')
  inspector.setAttribute('aria-label', '连线详情')
  document.body.append(inspector)
  inspector.addEventListener('keydown', (event) => event.stopPropagation())
  const close = () => inspector.close()
  inspector.addEventListener('click', (event) => {
    if (event.target === inspector) {
      close()
    }
  })
  function inspect(edge: CanvasConnection) {
    const source = store.doc.objects[edge.sourceId]
    const target = store.doc.objects[edge.targetId]
    if (!source || !target) {
      return
    }
    inspector.replaceChildren()
    const title = document.createElement('h3')
    title.textContent = `${source.name || '素材'} → ${target.name || '节点'}`
    const description = document.createElement('p')
    description.textContent =
      edge.kind === 'lineage'
        ? '生成来源：这份结果生成时使用了该素材。历史来源不可断开。'
        : target.nodeRun
          ? '任务引用：已提交任务使用的素材快照。请在任务结束后调整参考。'
          : '参考素材：下次生成此节点时使用。断开不会删除素材。'
    const done = document.createElement('button')
    done.type = 'button'
    done.textContent = '关闭'
    done.addEventListener('click', close)
    inspector.append(title, description)
    if (edge.kind === 'reference' && target.nodeDraft && !target.nodeRun) {
      const remove = document.createElement('button')
      remove.type = 'button'
      remove.textContent = '断开引用'
      remove.addEventListener('click', () => {
        const current = store.doc.objects[edge.targetId]
        if (current?.nodeDraft && !current.nodeRun && !current.src && !current.assetId) {
          store.apply(
            cmdUpdateObject(
              current.id,
              {
                nodeDraft: {
                  ...current.nodeDraft,
                  references: current.nodeDraft.references.filter(
                    (ref) => ref.sourceNodeId !== edge.sourceId,
                  ),
                },
              },
              { nodeDraft: current.nodeDraft },
            ),
          )
        }
        close()
      })
      inspector.append(remove)
    }
    inspector.append(done)
    if (!inspector.open) {
      inspector.showModal()
    }
  }
  return { inspect, dispose: () => inspector.remove() }
}
