import test from 'node:test'
import assert from 'node:assert/strict'
import { latestJob } from '../apps/web/canvas/state/job-version.js'

test('late snapshots and duplicate progress cannot regress a completed job', () => {
  const cache = new Map<
    string,
    { id: string; stateVersion: number; progressSeq?: number; status: string }
  >()
  const completed = { id: 'job', stateVersion: 3, status: 'completed' }
  assert.equal(latestJob(cache, completed), completed)
  assert.equal(latestJob(cache, { id: 'job', stateVersion: 2, status: 'running' }), completed)
  const progressed = { id: 'other', stateVersion: 2, progressSeq: 5, status: 'running' }
  latestJob(cache, progressed)
  assert.equal(latestJob(cache, { ...progressed, progressSeq: 4 }), progressed)
})
