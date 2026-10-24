import './dom.js'
import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  VersionHistory,
  type AssetVersion,
  type VersionHistoryApi,
} from '../apps/web/canvas/session/version-history.js'

afterEach(cleanup)
const versions: AssetVersion[] = [1, 2, 3].map((version) => ({
  id: String(version),
  version,
  kind: 'image',
  src: `/api/assets/${version}/content`,
  created_at: '',
}))

test('history compares two immutable versions and delegates switches to the server', async () => {
  const switched: string[] = []
  const api: VersionHistoryApi = {
    list: async () => versions,
    switch: async (asset, version) => {
      switched.push(`${asset}:${version}`)
    },
  }
  render(<VersionHistory assetId="asset" api={api} />)
  await screen.findByText('版本历史 · 3')
  const boxes = screen.getAllByRole('checkbox', { hidden: true })
  fireEvent.click(boxes[0])
  fireEvent.click(boxes[1])
  assert.equal((boxes[2] as HTMLInputElement).disabled, true)
  assert.equal(screen.getAllByRole('img', { hidden: true }).length, 2)
  fireEvent.click(screen.getAllByRole('button', { name: '切换到此版本', hidden: true })[0])
  await waitFor(() => assert.deepEqual(switched, ['asset:1']))
})

test('version switch reports CAS conflicts instead of applying a local canvas edit', async () => {
  const api: VersionHistoryApi = {
    list: async () => versions,
    switch: async () => {
      throw new Error('画布已更新，请重试')
    },
  }
  render(<VersionHistory assetId="asset" api={api} />)
  await screen.findByText('版本历史 · 3')
  fireEvent.click(screen.getAllByRole('button', { name: '切换到此版本', hidden: true })[0])
  await waitFor(() =>
    assert.match(screen.getByRole('alert', { hidden: true }).textContent ?? '', /画布已更新/),
  )
})
