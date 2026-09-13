// 评测工作台的查询约定:统一 TanStack Query 默认值,组件不再各自手写轮询/错误态。
// - 不自动重试(证据读取失败要立刻显式呈现,不静默吞);
// - 窗口聚焦不触发刷新(轮询节奏由各查询的 refetchInterval 控制);
// - 轮询间隔由调用方声明,终态查询不传即不轮询。
import { useQuery, type UseQueryResult } from '@tanstack/react-query'

export function useEvalQuery<T>(
  key: readonly unknown[],
  fetcher: () => Promise<T>,
  refetchInterval:
    number | false | ((query: { state: { data: T | undefined } }) => number | false) = false,
  enabled = true,
): UseQueryResult<T, Error> {
  return useQuery({
    queryKey: key,
    queryFn: fetcher,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval,
    enabled,
  })
}

/** 活跃 run 的详情轮询:queued/running/stopping 每 2s,终态停止。 */
export const ACTIVE_POLL_MS = 2000
export const LIST_POLL_MS = 4000
export const OPS_POLL_MS = 5000
