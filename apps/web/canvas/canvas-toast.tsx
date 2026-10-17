import { useCallback, useEffect, useRef, useState } from 'react'

const NOTICE_DURATION_MS = 3000
const NOTICE_EXIT_MS = 180

type CanvasNotice = { id: number; message: string; visible: boolean }

/** Each call owns its timers, including repeated messages and interrupted exits. */
export function useCanvasNotice() {
  const [notice, updateNotice] = useState<CanvasNotice | null>(null)
  const sequence = useRef(0)
  const setNotice = useCallback((message: string) => {
    updateNotice(message ? { id: ++sequence.current, message, visible: true } : null)
  }, [])
  const id = notice?.id
  useEffect(() => {
    if (id === undefined) {
      return
    }
    const hide = setTimeout(() => {
      updateNotice((current) => (current?.id === id ? { ...current, visible: false } : current))
    }, NOTICE_DURATION_MS)
    const remove = setTimeout(() => {
      updateNotice((current) => (current?.id === id ? null : current))
    }, NOTICE_DURATION_MS + NOTICE_EXIT_MS)
    return () => {
      clearTimeout(hide)
      clearTimeout(remove)
    }
  }, [id])
  return [notice, setNotice] as const
}

export function CanvasToast({ notice }: { notice: CanvasNotice | null }) {
  if (!notice) {
    return null
  }
  return (
    <div
      className="next-toast"
      role="status"
      aria-hidden={!notice.visible}
      data-visible={notice.visible}
    >
      {notice.message}
    </div>
  )
}
