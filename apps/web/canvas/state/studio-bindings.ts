import { cmdUpdateObject, type CanvasObj } from './commands.js'
import type { DocStore } from './doc-store.js'
import { emptyTimeline } from './timeline.js'
import type { NodeDraft } from './node-model.js'

export function saveNodeDraft(store: DocStore, id: string, draft: NodeDraft) {
  const object = store.doc.objects[id]
  if (!object || (object.kind === 'placeholder' && object.generationId) || object.nodeRun) {
    return
  }
  if (JSON.stringify(object.nodeDraft) === JSON.stringify(draft)) {
    return
  }
  store.apply(
    cmdUpdateObject(id, { nodeDraft: structuredClone(draft) }, { nodeDraft: object.nodeDraft }),
  )
}

/** Scope legacy timeline adapters to one canvas node while keeping document history/autosave. */
export function timelineNodeStore(store: DocStore, id?: string): DocStore {
  if (!id) {
    return store
  }
  return {
    ...store,
    get doc() {
      return { ...store.doc, timeline: store.doc.objects[id]?.timeline ?? emptyTimeline() }
    },
    apply(command, options) {
      if (command.type !== 'setTimeline') {
        store.apply(command, options)
        return
      }
      const object = store.doc.objects[id]
      if (!object) {
        throw new Error('时间线节点已删除')
      }
      store.apply(
        cmdUpdateObject(id, { timeline: command.after }, { timeline: object.timeline }),
        options,
      )
    },
  }
}

export function detachReference(store: DocStore, targetId: string, sourceId: string) {
  const object = store.doc.objects[targetId]
  if (!object?.nodeDraft || object.nodeRun || object.src || object.assetId || object.generationId) {
    throw new Error('只能编辑尚未提交的节点引用')
  }
  store.apply(
    cmdUpdateObject(
      targetId,
      {
        nodeDraft: {
          ...object.nodeDraft,
          references: object.nodeDraft.references.filter((ref) => ref.sourceNodeId !== sourceId),
        },
      },
      { nodeDraft: object.nodeDraft },
    ),
  )
}

export function mediaSource(object: CanvasObj): string | undefined {
  return object.assetId ? `/api/assets/${encodeURIComponent(object.assetId)}/content` : object.src
}
