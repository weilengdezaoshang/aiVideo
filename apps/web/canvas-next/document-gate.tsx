import React, { useEffect, useState, type ReactNode } from 'react'
import type { CanvasDocData } from '../canvas/state/doc-store.js'
import {
  clearLocalDraftIfMatches,
  loadLocalDraft,
  openDocument,
  resolveDraftOnOpen,
} from '../canvas/state/document-api.js'

/** Keep the editor (and its autosaver) unmounted until a recovery choice is made. */
export function CanvasDocumentGate({
  children,
}: {
  children: (document: CanvasDocData, restored: boolean) => ReactNode
}) {
  const [ready, setReady] = useState<{ document: CanvasDocData; restored: boolean } | null>(null)
  const [choice, setChoice] = useState<{ document: CanvasDocData; draft: CanvasDocData } | null>(
    null,
  )
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true
    void openDocument()
      .then((document) => {
        if (!active) {
          return
        }
        const draft = loadLocalDraft(document.id)
        const resolution = resolveDraftOnOpen(document, draft)
        if (resolution.action === 'needs-choice') {
          setChoice({ document, draft: resolution.draft })
        } else {
          if (resolution.clearDraft && draft) {
            clearLocalDraftIfMatches(document.id, draft)
          }
          setReady({ document, restored: false })
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(cause instanceof Error ? cause.message : '画布文档读取失败')
        }
      })
    return () => {
      active = false
    }
  }, [attempt])

  if (choice) {
    return (
      <main className="next-state">
        <section role="dialog" aria-modal="true" aria-labelledby="draft-title">
          <h2 id="draft-title">发现未保存的本机草稿</h2>
          <p>本机草稿与服务器内容不同。请选择要继续编辑的版本。</p>
          <button
            onClick={() => {
              setReady({
                document: {
                  ...choice.draft,
                  id: choice.document.id,
                  revision: choice.document.revision,
                },
                restored: true,
              })
              setChoice(null)
            }}
          >
            恢复本机草稿
          </button>
          <button
            onClick={() => {
              clearLocalDraftIfMatches(choice.document.id, choice.draft)
              setReady({ document: choice.document, restored: false })
              setChoice(null)
            }}
          >
            放弃草稿，打开服务器版本
          </button>
        </section>
      </main>
    )
  }
  if (error) {
    return (
      <main className="next-state next-error">
        <section role="alert">
          <p>{error}</p>
          <p>本机草稿已保留，连接恢复后可重新读取。</p>
          <button
            onClick={() => {
              setError('')
              setAttempt((value) => value + 1)
            }}
          >
            重试读取
          </button>
        </section>
      </main>
    )
  }
  return ready ? (
    children(ready.document, ready.restored)
  ) : (
    <main className="next-state">正在打开画布…</main>
  )
}
