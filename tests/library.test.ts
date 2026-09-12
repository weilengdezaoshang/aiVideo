import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  assetName,
  filterAssets,
  projectGroups,
  uploadFormat,
} from '../apps/web/workspace/library-model.js'
import type { AssetEntry } from '../apps/web/workspace/api.js'

test('canvas search preserves source order and groups by local calendar day', () => {
  const now = new Date(2026, 8, 10, 12)
  const projects = [
    { id: '1', name: '海岸 A', updatedAt: new Date(2026, 8, 9, 23).toISOString(), objectCount: 1 },
    { id: '2', name: '海岸 B', updatedAt: new Date(2026, 8, 10, 1).toISOString(), objectCount: 2 },
    { id: '3', name: '花', updatedAt: now.toISOString(), objectCount: 0 },
  ]
  assert.deepEqual(
    projectGroups(projects, ' 海岸 ', 'recent', now).map((group) =>
      group.items.map((item) => item.id),
    ),
    [['2'], ['1']],
  )
  assert.deepEqual(
    projects.map((item) => item.id),
    ['1', '2', '3'],
  )
  assert.equal(projectGroups(projects, '不存在', 'name', now)[0].items.length, 0)
})
test('asset filtering combines name, type and date without inventing legacy names', () => {
  const assets: AssetEntry[] = [
    {
      asset: {
        id: 'a12345678',
        kind: 'image',
        ext: 'png',
        width: 10,
        height: 10,
        createdAt: '2026-09-01',
      },
      urls: { original: '/a.png' },
    },
    {
      asset: {
        id: 'b',
        name: 'Coast.mp4',
        kind: 'video',
        ext: 'mp4',
        width: 0,
        height: 0,
        createdAt: '2026-09-02',
      },
      urls: { original: '/b.mp4' },
    },
  ]
  assert.equal(assetName(assets[0]), '图片-a1234567.png')
  assert.equal(filterAssets(assets, 'video', ' coast ', 'newest')[0].asset.id, 'b')
  assert.equal(filterAssets(assets, 'image', 'coast', 'newest').length, 0)
  assert.equal(filterAssets(assets, 'all', '', 'oldest')[0].asset.id, 'a12345678')
})
test('library upload rejects invalid formats, empty and oversized files before dispatch', () => {
  assert.deepEqual(uploadFormat({ type: 'video/mp4', size: 1024 }), { ext: 'mp4', kind: 'video' })
  for (const file of [
    { type: 'text/html', size: 10 },
    { type: 'image/png', size: 0 },
    { type: 'video/mp4', size: 40 * 1024 * 1024 + 1 },
  ]) {
    assert.throws(() => uploadFormat(file), /40MB/)
  }
})
