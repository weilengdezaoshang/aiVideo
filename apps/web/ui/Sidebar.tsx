import { Folder, House, PanelsTopLeft, PlusSquare, CircleHelp, Settings } from 'lucide-react'
import { Button } from './primitives.js'

export type WorkspaceTab = 'overview' | 'canvas' | 'assets'
export function Sidebar({
  tab,
  onNavigate,
  onCreate,
  onHelp,
  onSettings,
  busy,
}: {
  tab: WorkspaceTab
  onNavigate: (tab: WorkspaceTab) => void
  onCreate: () => void
  onHelp: () => void
  onSettings?: () => void
  busy: boolean
}) {
  const links = [
    { tab: 'overview', label: '创作首页', icon: House },
    { tab: 'canvas', label: '我的画布', icon: PanelsTopLeft },
    { tab: 'assets', label: '我的素材', icon: Folder },
  ] as const
  return (
    <aside className="workspace-sidebar" aria-label="工作台导航">
      <a className="identity-brand sidebar-brand" href="/" aria-label="帧屿集 FRAYUNE 品牌首页">
        <span className="identity-mark" />
        <span className="identity-wordmark">
          <span className="identity-en" />
          <span className="identity-cn" />
        </span>
      </a>
      <nav>
        {links.map(({ tab: key, label, icon: Icon }) => (
          <a
            key={key}
            href={`/workspace?tab=${key}`}
            aria-current={tab === key ? 'page' : undefined}
            title={label}
            onClick={(event) => {
              if (!event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
                event.preventDefault()
                onNavigate(key)
              }
            }}
          >
            <Icon size={22} strokeWidth={1.6} />
            <span>{label}</span>
          </a>
        ))}
      </nav>
      <div className="sidebar-create">
        <Button onClick={onCreate} disabled={busy} title="新建画布">
          <PlusSquare size={23} strokeWidth={1.6} />
          <span>{busy ? '创建中…' : '新建画布'}</span>
        </Button>
      </div>
      <div className="sidebar-bottom">
        <Button onClick={onHelp} title="帮助">
          <CircleHelp size={21} strokeWidth={1.6} />
          <span>帮助</span>
        </Button>
        {onSettings && (
          <Button onClick={onSettings} title="设置">
            <Settings size={21} strokeWidth={1.6} />
            <span>设置</span>
          </Button>
        )}
      </div>
    </aside>
  )
}
