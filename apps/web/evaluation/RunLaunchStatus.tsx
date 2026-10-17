import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from './api.js'
import { ACTIVE_POLL_MS, useEvalQuery } from './queries.js'

const activeStatuses = new Set(['queued', 'running', 'stopping'])
const statusNames: Record<string, string> = {
  queued: '已启动，等待执行',
  running: '评测进行中',
  stopping: '正在取消',
  completed: '评测已完成',
  failed: '评测执行失败',
  cancelled: '评测已取消',
  interrupted: '评测已中断',
  budget_exhausted: '预算已用尽',
}
export function useRunLaunch() {
  const [runId, setRunId] = useState<string | null>(null)
  const client = useQueryClient()
  const query = useEvalQuery(
    ['eval-launch', runId],
    () => api.run(runId!),
    (current) =>
      !current.state.data || activeStatuses.has(current.state.data.status) ? ACTIVE_POLL_MS : false,
    runId !== null,
  )
  function recordRun(id: string) {
    setRunId(id)
    void client.invalidateQueries({ queryKey: ['eval-runs'] })
  }
  return {
    runId,
    recordRun,
    query,
    active: runId !== null && (!query.data || activeStatuses.has(query.data.status)),
  }
}
export function RunLaunchStatus({
  launch,
  onOpenRun,
  onReviews,
}: {
  launch: ReturnType<typeof useRunLaunch>
  onOpenRun?: (id: string) => void
  onReviews?: (runId: string) => void
}) {
  if (!launch.runId) {
    return null
  }
  const detail = launch.query.data
  const done = detail
    ? detail.progress.completed +
      detail.progress.failed +
      detail.progress.cancelled +
      detail.progress.timedOut
    : 0
  const total = detail?.progress.total ?? 0
  const ratio = total ? Math.min(1, done / total) : 0
  const heading = launch.query.error
    ? '已创建评测，暂时无法读取进度'
    : detail
      ? (statusNames[detail.status] ?? `未知状态：${detail.status}`)
      : '已启动，正在读取进度'
  return (
    <section className="eval-launch-status" aria-label="本次评测" key={launch.runId}>
      <div className="eval-launch-heading">
        <h3 aria-live="polite">{heading}</h3>
        {onOpenRun ? (
          <button type="button" onClick={() => onOpenRun(launch.runId!)}>
            {detail?.status === 'completed' ? '查看结果' : '查看详情'}
          </button>
        ) : null}
      </div>
      <p>
        运行编号：<code>{launch.runId}</code>
      </p>
      {detail && (
        <>
          <div
            className="eval-launch-progress"
            role="progressbar"
            aria-label="评测完成进度"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={done}
          >
            <span style={{ width: `${ratio * 100}%` }} />
          </div>
          {activeStatuses.has(detail.status) && (
            <p>
              已结束 {done} / {total} 条 · 待跑 {detail.progress.pending} 条 · 执行中{' '}
              {detail.progress.running} 条
            </p>
          )}
        </>
      )}
      {detail?.extra?.reviewSummary && (
        <p role="status">
          {detail.extra.reviewSummary.status === 'failed'
            ? `评测已结束，但自动送审失败：${detail.extra.reviewSummary.error ?? '请检查审核服务'}`
            : !detail.extra.reviewSummary.enabled
              ? '本次运行已关闭自动人工审核。'
              : detail.extra.reviewSummary.count
                ? `已自动创建 ${detail.extra.reviewSummary.count} 项人工审核任务（必审 ${detail.extra.reviewSummary.required} 项）。`
                : '本 run 未命中送审策略，无需人工审核。'}
          {detail.extra.reviewSummary.count > 0 && onReviews && (
            <button type="button" onClick={() => onReviews(launch.runId!)}>
              处理自动审核任务
            </button>
          )}
        </p>
      )}
      {detail && !activeStatuses.has(detail.status) && detail.denominators && (
        <p className="eval-launch-verdict">
          {detail.progress.total} 道题 · 通过 {detail.denominators.pass} 条
          {detail.denominators.determined > detail.denominators.pass &&
            ` · 判分未通过 ${detail.denominators.determined - detail.denominators.pass} 条`}
          {detail.progress.failed + detail.progress.timedOut > 0 &&
            ` · 执行失败或超时 ${detail.progress.failed + detail.progress.timedOut} 条`}
          {detail.progress.cancelled > 0 && ` · 已取消 ${detail.progress.cancelled} 条`}
          {detail.progress.total -
            detail.denominators.determined -
            detail.progress.failed -
            detail.progress.timedOut -
            detail.progress.cancelled >
            0 &&
            ` · 未判定 ${detail.progress.total - detail.denominators.determined - detail.progress.failed - detail.progress.timedOut - detail.progress.cancelled} 条`}
        </p>
      )}
      {launch.query.error && (
        <p role="alert">
          {launch.query.error.message}{' '}
          <button type="button" onClick={() => void launch.query.refetch()}>
            重试读取进度
          </button>
        </p>
      )}
    </section>
  )
}
