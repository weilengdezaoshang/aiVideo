import { ArrowUpRight, Ellipsis, Image as ImageIcon, Pencil, Trash2 } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { Button } from './primitives.js'

export type Project = {
  id: string
  name: string
  updatedAt: string
  objectCount: number
  thumbnail?: string
}
export type Inspiration = {
  id: string
  title: string
  image: string
  category: string
  prompt: string
  ratio: string
  aspect: string
}
export function ProjectCard({
  project,
  onRename,
  onDelete,
}: {
  project: Project
  onRename: () => void
  onDelete: () => void
}) {
  const ref = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !ref.current?.contains(event.target)) {
        ref.current?.removeAttribute('open')
      }
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [])
  return (
    <article className="project-card">
      <a href={`/canvas?doc=${encodeURIComponent(project.id)}`} className="project-link">
        <div className="project-thumbnail">
          {project.thumbnail ? (
            <img src={project.thumbnail} alt="" loading="lazy" />
          ) : (
            <ImageIcon size={26} strokeWidth={1.3} />
          )}
        </div>
        <div>
          <h3 title={project.name}>{project.name}</h3>
          <p>画布 · {project.objectCount} 个对象</p>
        </div>
      </a>
      <details
        ref={ref}
        className="project-menu"
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            ref.current?.removeAttribute('open')
            ref.current?.querySelector('summary')?.focus()
          }
        }}
      >
        <summary aria-label={`${project.name}的操作`}>
          <Ellipsis size={20} />
        </summary>
        <div>
          <Button
            onClick={() => {
              ref.current?.removeAttribute('open')
              onRename()
            }}
          >
            <Pencil size={15} />
            重命名
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              ref.current?.removeAttribute('open')
              onDelete()
            }}
          >
            <Trash2 size={15} />
            删除画布
          </Button>
        </div>
      </details>
    </article>
  )
}
export function InspirationCard({
  item,
  onSelect,
  onUse,
}: {
  item: Inspiration
  onSelect: () => void
  onUse: () => void
}) {
  return (
    <article className="inspiration-card">
      <div className="inspiration-image" style={{ aspectRatio: item.aspect }}>
        <button
          className="image-open"
          type="button"
          onClick={onSelect}
          aria-label={`查看${item.title}`}
        >
          <img src={item.image} alt={item.title} loading="lazy" />
        </button>
        <Button className="use-prompt" onClick={onUse}>
          使用提示词
          <ArrowUpRight size={15} />
        </Button>
      </div>
      <button className="inspiration-title" onClick={onSelect}>
        {item.title}
      </button>
    </article>
  )
}
