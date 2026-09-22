import { useState } from 'react'
import { Plus } from 'lucide-react'
import { Button, EmptyState, SearchInput, Select } from '../ui/primitives.js'
import { CardMenu, LibraryHeader, LibraryToolbar, MediaPreview, ViewToggle } from '../ui/library.js'
import type { Project } from '../ui/media-cards.js'
import { projectDate, projectGroups } from './library-model.js'

export function CanvasLibrary({
  projects,
  loading,
  error,
  busy,
  onCreate,
  onReload,
  onRename,
  onDelete,
}: {
  projects: Project[]
  loading: boolean
  error: string
  busy: boolean
  onCreate: () => void
  onReload: () => void
  onRename: (project: Project) => void
  onDelete: (project: Project) => void
}) {
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('recent')
  const [view, setView] = useState<'grid' | 'list'>('grid')
  const groups = projectGroups(projects, query, sort).filter((group) => group.items.length)
  return (
    <section className="library-page canvas-library">
      <LibraryHeader
        title="我的画布"
        description="让每一个灵感，都有继续的地方。"
        count={loading ? undefined : `${projects.length} 个画布`}
        action={
          <Button variant="primary" disabled={busy} onClick={onCreate}>
            <Plus size={22} />
            新建画布
          </Button>
        }
      />
      <LibraryToolbar
        actions={
          <>
            <Select
              label="画布排序"
              value={sort}
              onChange={setSort}
              options={[
                { value: 'recent', label: '最近编辑' },
                { value: 'name', label: '名称排序' },
              ]}
            />
            <ViewToggle value={view} onChange={setView} />
          </>
        }
      >
        <SearchInput label="搜索画布" value={query} onChange={setQuery} />
      </LibraryToolbar>
      {loading ? (
        <p role="status">正在读取画布…</p>
      ) : error ? (
        <EmptyState action={<Button onClick={onReload}>重试</Button>}>{error}</EmptyState>
      ) : !groups.length ? (
        <EmptyState
          action={
            query ? (
              <Button onClick={() => setQuery('')}>清除搜索</Button>
            ) : (
              <Button onClick={onCreate} disabled={busy}>
                新建画布
              </Button>
            )
          }
        >
          {query ? '没有找到匹配的画布' : '还没有画布，开始你的第一幅作品。'}
        </EmptyState>
      ) : (
        groups.map((group) => (
          <section className="library-group" key={group.title}>
            <h2>{group.title}</h2>
            <div className={`canvas-collection canvas-collection--${view}`}>
              {group.items.map((project) => (
                <article className="canvas-tile" key={project.id}>
                  <a
                    href={`/canvas?doc=${encodeURIComponent(project.id)}`}
                    className="canvas-tile-link"
                  >
                    <MediaPreview src={project.thumbnail} label={project.name} decorative />
                    <div className="library-card-copy">
                      <h3 title={project.name}>{project.name}</h3>
                      <p>
                        {projectDate(project.updatedAt)} · {project.objectCount} 个对象
                      </p>
                    </div>
                  </a>
                  <CardMenu
                    label={`${project.name}的操作`}
                    actions={[
                      { label: '重命名', onClick: () => onRename(project) },
                      { label: '删除画布', danger: true, onClick: () => onDelete(project) },
                    ]}
                  />
                </article>
              ))}
            </div>
          </section>
        ))
      )}
    </section>
  )
}
