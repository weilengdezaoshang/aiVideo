import type { ReactNode } from 'react'

/** Keep both input sessions mounted while making only the active surface interactive. */
export function ComposerPresence({
  sidebarOpen,
  children,
}: {
  sidebarOpen: boolean
  children: ReactNode
}) {
  return (
    <div className="composer-presence" aria-hidden={sidebarOpen} inert={sidebarOpen}>
      {children}
    </div>
  )
}

export function SidebarPresence({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <aside className="chat-panel" aria-label="画布侧边栏" aria-hidden={!open} inert={!open}>
      {children}
    </aside>
  )
}
