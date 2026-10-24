import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createSessionApi } from './api.js'
import {
  createSessionStore,
  type SessionApi,
  type SessionArtifact,
  type SessionSource,
} from './store.js'
import { SessionPanel } from './panel.js'
import { VersionHistory, type VersionHistoryApi } from './version-history.js'

export function ConnectedSessionPanel(props: {
  documentId: string
  selectedAssets: () => readonly string[]
  selectedNodes?: () => readonly string[]
  ensureSaved: () => Promise<unknown>
  syncDocument: () => Promise<unknown>
  heartbeatIntervalMs?: number
  api?: SessionApi
  source?: (url: string) => SessionSource
  versionAnchor?: (nodeId: string) => { documentRevision: number; anchorVersion: number } | null
}) {
  const current = useRef(props)
  current.current = props
  const connection = useMemo(() => {
    const clientId = crypto.randomUUID()
    const api =
      props.api ??
      createSessionApi({ language: document.documentElement.lang || navigator.language })
    const store = createSessionStore({
      documentId: props.documentId,
      clientId,
      storage: localStorage,
      source: props.source ?? ((url) => new EventSource(url)),
      api: {
        ...api,
        open: async (id) => {
          await current.current.ensureSaved()
          return api.open(id)
        },
        send: async (id, body) => {
          await current.current.ensureSaved()
          return api.send(id, body)
        },
      },
    })
    return { store, api, clientId }
  }, [props.documentId, props.api, props.source])
  const state = useSyncExternalStore(connection.store.subscribe, connection.store.getSnapshot)
  const [artifacts, setArtifacts] = useState<SessionArtifact[]>([])
  const [chosenId, setChosenId] = useState('')
  const selected = props.selectedAssets()
  const chosen =
    artifacts.find((item) => item.id === chosenId) ??
    artifacts.find((item) => selected.includes(item.asset_id)) ??
    artifacts[0]
  const versionApi = useMemo<VersionHistoryApi>(
    () => ({
      list: (id) => connection.api.versions?.(id) ?? Promise.resolve([]),
      switch: async (assetId, versionId) => {
        if (!chosen?.canvas_node_id || !connection.api.switchVersion) {
          throw new Error('该产物当前不在画布上')
        }
        await current.current.ensureSaved()
        const anchor = current.current.versionAnchor?.(chosen.canvas_node_id)
        if (!anchor) {
          throw new Error('画布节点已变更，请同步后重试')
        }
        await connection.api.switchVersion(assetId, versionId, {
          document_id: current.current.documentId,
          node_id: chosen.canvas_node_id,
          expected_document_revision: anchor.documentRevision,
          expected_anchor_version: anchor.anchorVersion,
          idempotency_key: crypto.randomUUID(),
        })
        await current.current.syncDocument()
      },
    }),
    [chosen?.id, chosen?.canvas_node_id, connection],
  )
  useEffect(() => connection.store.start(), [connection])
  const cursor = state.snapshot?.cursor
  useEffect(() => {
    let active = true
    const id = state.snapshot?.id
    if (id && connection.api.artifacts) {
      void connection.api
        .artifacts(id)
        .then((value) => {
          if (active) {
            setArtifacts(value.artifacts)
          }
        })
        .catch(() => {
          /* Later journal updates retry this read. */
        })
    } else {
      setArtifacts([])
    }
    return () => {
      active = false
    }
  }, [state.snapshot?.id, cursor, connection])
  useEffect(() => {
    if (cursor === undefined) {
      return
    }
    void current.current.syncDocument().catch(() => {
      /* Autosave owns conflict presentation. */
    })
  }, [cursor, connection])
  useEffect(() => {
    const id = state.snapshot?.id
    if (!id) {
      return
    }
    const heartbeat = (online: boolean) => {
      void connection.api
        .heartbeat?.(
          id,
          connection.clientId,
          connection.store.getSnapshot().snapshot?.cursor ?? 0,
          online,
        )
        .catch(() => {
          /* Lease expires independently. */
        })
    }
    heartbeat(true)
    const timer = setInterval(() => heartbeat(true), props.heartbeatIntervalMs ?? 30000)
    return () => {
      clearInterval(timer)
      heartbeat(false)
    }
  }, [state.snapshot?.id, connection, props.heartbeatIntervalMs])
  return (
    <>
      <SessionPanel
        store={connection.store}
        selectedAssets={() => current.current.selectedAssets()}
        selectedNodes={() => current.current.selectedNodes?.() ?? []}
      />
      {chosen && connection.api.versions && (
        <div className="next-panel-body">
          <label>
            查看产物历史
            <select value={chosenId} onChange={(event) => setChosenId(event.target.value)}>
              <option value="">随当前选择</option>
              {artifacts.map((item) => (
                <option key={item.id} value={item.id}>
                  产物 #{item.ordinal}
                </option>
              ))}
            </select>
          </label>
          <VersionHistory assetId={chosen.core_asset_id} api={versionApi} />
        </div>
      )}
    </>
  )
}
