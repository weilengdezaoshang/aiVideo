/** 本地只允许 Memory 驱动。BullMQ/Redis 仍是未实现骨架，禁止静默降级。 */
export function assertMemoryDrivers(queueDriver: string, rateDriver: string): void {
  if (queueDriver !== 'memory' || rateDriver !== 'memory') {
    throw new Error('BullMQ/Redis 为显式未实现的扩展骨架；本地请使用 memory 异步队列，不能静默回退')
  }
}
