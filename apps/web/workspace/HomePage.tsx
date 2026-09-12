import { useState } from 'react'
import { ArrowRight, Plus } from 'lucide-react'
import { Button, Dialog, EmptyState, SearchInput, SectionHeader, Tabs } from '../ui/primitives.js'
import { InspirationCard, ProjectCard, type Project, type Inspiration } from '../ui/media-cards.js'
import { Composer, type PromptPreset } from './Composer.js'
import type { Capabilities, MediaKind, NodeDraft } from '../canvas/state/node-model.js'
import { filterInspiration, inspiration } from './inspiration.js'

type Props = {
  caps: Capabilities | null
  capsError: string
  onReloadCaps: () => void
  projects: Project[]
  loading: boolean
  projectsError: string
  onReloadProjects: () => void
  onCreate: () => void
  onAll: () => void
  onRename: (project: Project) => void
  onDelete: (project: Project) => void
  onGenerate: (kind: MediaKind, draft: NodeDraft) => void
  pending: boolean
  busy: boolean
  onTool: (tool: string) => void
}
export function HomePage(props: Props) {
  const [category, setCategory] = useState('精选')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Inspiration | null>(null)
  const [preset, setPreset] = useState<PromptPreset | null>(null)
  const filtered = filterInspiration(inspiration, category, query)
  function usePrompt(item: Inspiration) {
    if (props.pending || props.busy) {
      return
    }
    setPreset({ prompt: item.prompt, ratio: item.ratio, version: Date.now() })
    setSelected(null)
    window.scrollTo({
      top: 0,
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    })
  }
  return (
    <>
      <Composer
        caps={props.caps}
        capsError={props.capsError}
        onReload={props.onReloadCaps}
        preset={preset}
        onGenerate={props.onGenerate}
        busy={props.busy}
        pending={props.pending}
        onTool={props.onTool}
      />
      <section className="recent-section" aria-label="最近项目">
        <SectionHeader
          title="最近项目"
          action={
            <Button onClick={props.onAll}>
              查看全部
              <ArrowRight size={17} />
            </Button>
          }
        />
        {props.loading ? (
          <div className="project-grid" aria-label="正在加载项目">
            {[0, 1, 2].map((i) => (
              <div className="project-skeleton" key={i} />
            ))}
          </div>
        ) : props.projectsError ? (
          <EmptyState action={<Button onClick={props.onReloadProjects}>重新加载</Button>}>
            {props.projectsError}
          </EmptyState>
        ) : props.projects.length ? (
          <div className="project-grid">
            {props.projects.slice(0, 3).map((project) => (
              <ProjectCard
                key={project.id}
                project={project}
                onRename={() => props.onRename(project)}
                onDelete={() => props.onDelete(project)}
              />
            ))}
          </div>
        ) : (
          <div className="recent-empty">
            <div>
              <h3>你的下一幅作品，从这里开始</h3>
              <p>创建画布后，可以在这里继续上次的创作。</p>
            </div>
            <Button onClick={props.onCreate} disabled={props.busy}>
              <Plus size={18} />
              新建画布
            </Button>
          </div>
        )}
      </section>
      <section className="inspiration-section" aria-label="灵感探索">
        <SectionHeader
          title="灵感探索"
          badge="示例作品"
          action={<SearchInput value={query} onChange={setQuery} />}
        />
        <Tabs
          label="灵感分类"
          value={category}
          onChange={setCategory}
          options={['精选', '电影感', '自然', '产品', '插画'].map((value) => ({
            value,
            label: value,
          }))}
        />
        {filtered.length ? (
          <div className="inspiration-grid">
            {filtered.map((item) => (
              <InspirationCard
                key={item.id}
                item={item}
                onSelect={() => setSelected(item)}
                onUse={() => {
                  if (props.pending) {
                    setSelected(item)
                  } else {
                    usePrompt(item)
                  }
                }}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            action={
              <Button
                onClick={() => {
                  setQuery('')
                  setCategory('精选')
                }}
              >
                清除筛选
              </Button>
            }
          >
            没有找到相关灵感，试试其他关键词。
          </EmptyState>
        )}
      </section>
      {selected && (
        <Dialog title={selected.title} onClose={() => setSelected(null)}>
          <img className="inspiration-detail" src={selected.image} alt={selected.title} />
          <p className="detail-prompt">{selected.prompt}</p>
          <div className="dialog-actions">
            <span className="muted">示例图片 · {selected.ratio}</span>
            <Button
              variant="primary"
              disabled={props.pending || props.busy}
              onClick={() => usePrompt(selected)}
            >
              使用提示词
              <ArrowRight size={17} />
            </Button>
          </div>
          {props.pending && <p className="muted">请先确认上一次生成请求，再开始新创作。</p>}
        </Dialog>
      )}
    </>
  )
}
