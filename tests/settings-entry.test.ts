import './dom.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Sidebar } from '../apps/web/ui/Sidebar.js'

test('settings entry is absent by default and enabled only with its action', () => {
  const props = {
    tab: 'overview' as const,
    onNavigate: () => {},
    onCreate: () => {},
    onHelp: () => {},
    busy: false,
  }
  try {
    const { rerender } = render(createElement(Sidebar, props))
    assert.equal(screen.queryByRole('button', { name: '设置' }), null)
    let opened = false
    rerender(
      createElement(Sidebar, {
        ...props,
        onSettings: () => {
          opened = true
        },
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: '设置' }))
    assert.equal(opened, true)
    rerender(createElement(Sidebar, props))
    assert.equal(screen.queryByRole('button', { name: '设置' }), null)
  } finally {
    cleanup()
  }
})
