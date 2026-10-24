import { useEffect, useState } from 'react'

export type AssetVersion = {
  id: string
  version: number
  kind: 'image' | 'video' | 'audio' | 'mask' | 'text'
  src: string | null
  created_at: string
}
export type VersionHistoryApi = {
  list(assetId: string): Promise<AssetVersion[]>
  switch(assetId: string, versionId: string): Promise<void>
}

/** Switching existing media is a server command; this component never rewrites canvas nodes. */
export function VersionHistory({ assetId, api }: { assetId: string; api: VersionHistoryApi }) {
  const [versions, setVersions] = useState<AssetVersion[]>([])
  const [compare, setCompare] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    setVersions([])
    setCompare([])
    setError('')
    void api
      .list(assetId)
      .then((items) => {
        if (active) {
          setVersions(items)
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(String(reason))
        }
      })
    return () => {
      active = false
    }
  }, [assetId, api])
  const switchVersion = async (version: AssetVersion) => {
    if (busy) {
      return
    }
    setBusy(true)
    setError('')
    try {
      await api.switch(assetId, version.id)
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(false)
    }
  }
  return (
    <details className="next-agent-history">
      <summary>版本历史 · {versions.length}</summary>
      {error && <p role="alert">{error}</p>}
      {versions.map((version) => (
        <div key={version.id}>
          <label>
            <input
              type="checkbox"
              checked={compare.includes(version.id)}
              disabled={!compare.includes(version.id) && compare.length >= 2}
              onChange={(event) =>
                setCompare((old) =>
                  event.target.checked
                    ? [...old, version.id]
                    : old.filter((id) => id !== version.id),
                )
              }
            />
            版本 {version.version}
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              void switchVersion(version)
            }}
          >
            切换到此版本
          </button>
        </div>
      ))}
      {compare.length === 2 && (
        <div aria-label="版本对比">
          {compare.map((id) => {
            const item = versions.find((version) => version.id === id)
            if (!item) {
              return null
            }
            return (
              <figure key={id}>
                <figcaption>版本 {item.version}</figcaption>
                {item.src && item.kind === 'image' && (
                  <img src={item.src} alt={`版本 ${item.version}`} />
                )}
                {item.src && item.kind === 'video' && <video src={item.src} controls />}
                {!item.src && <p>此版本暂不可预览</p>}
              </figure>
            )
          })}
        </div>
      )}
    </details>
  )
}
