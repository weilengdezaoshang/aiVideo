import type { Project } from '../ui/media-cards.js'
import type { AssetEntry } from './api.js'

export const assetName = ({ asset }: AssetEntry) =>
  asset.name || `${asset.kind === 'video' ? '视频' : '图片'}-${asset.id.slice(0, 8)}.${asset.ext}`
export function projectGroups(projects: Project[], query: string, sort: string, now = new Date()) {
  const needle = query.trim().toLocaleLowerCase()
  const items = projects
    .filter((item) => item.name.toLocaleLowerCase().includes(needle))
    .sort((a, b) =>
      sort === 'name'
        ? a.name.localeCompare(b.name, 'zh-CN')
        : b.updatedAt.localeCompare(a.updatedAt),
    )
  if (sort === 'name') {
    return [{ title: '全部画布', items }]
  }
  const today = now.toDateString()
  return [
    {
      title: '今天',
      items: items.filter((item) => new Date(item.updatedAt).toDateString() === today),
    },
    {
      title: '更早',
      items: items.filter((item) => new Date(item.updatedAt).toDateString() !== today),
    },
  ]
}
export function filterAssets(assets: AssetEntry[], kind: string, query: string, sort: string) {
  const needle = query.trim().toLocaleLowerCase()
  return assets
    .filter(
      (item) =>
        (kind === 'all' || item.asset.kind === kind) &&
        `${assetName(item)} ${item.asset.id}`.toLocaleLowerCase().includes(needle),
    )
    .sort((a, b) =>
      sort === 'oldest'
        ? a.asset.createdAt.localeCompare(b.asset.createdAt)
        : b.asset.createdAt.localeCompare(a.asset.createdAt),
    )
}
export function projectDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? '时间未知'
    : date.toLocaleString('zh-CN', {
        month: 'numeric',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
}

export function uploadFormat(file: Pick<File, 'type' | 'size'>) {
  const types: Record<string, { ext: string; kind: 'image' | 'video' }> = {
    'image/jpeg': { ext: 'jpg', kind: 'image' },
    'image/png': { ext: 'png', kind: 'image' },
    'image/webp': { ext: 'webp', kind: 'image' },
    'video/mp4': { ext: 'mp4', kind: 'video' },
    'video/webm': { ext: 'webm', kind: 'video' },
  }
  if (!types[file.type] || !file.size || file.size > 40 * 1024 * 1024) {
    throw new Error('请选择 40MB 以内的 JPG、PNG、WebP、MP4 或 WebM 文件')
  }
  return types[file.type]
}
