import type { TaskQueue, RateLimiter, TaskHandler } from '../../ports/index.js'
/** 扩展骨架：真正接入时必须在 app 创建连接；未实现的驱动禁止静默退回 memory。 */
export abstract class BullMqAdapterSkeleton implements TaskQueue {
  abstract register(name: string, handler: TaskHandler): void
  abstract enqueue: TaskQueue['enqueue']
  abstract dequeue: TaskQueue['dequeue']
  abstract complete: TaskQueue['complete']
  abstract fail: TaskQueue['fail']
  abstract get: TaskQueue['get']
  abstract getByIdempotencyKey: TaskQueue['getByIdempotencyKey']
}
export abstract class RedisLimiterSkeleton implements RateLimiter {
  abstract allow: RateLimiter['allow']
  abstract acquire(key: string): Promise<{ ok: boolean; retryAfterMs?: number }>
  abstract release(key: string): void
}
