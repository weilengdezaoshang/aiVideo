export type BenchmarkMode = 'clean' | 'diagnostic'
export type BenchmarkState = {
  documentId: string
  objectCount: number
  selectionCount: number
  camera: { x: number; y: number; scale: number }
  viewport: { width: number; height: number }
}
export type BenchmarkBridge = {
  state(): BenchmarkState
  counters(): Record<string, number> | null
}
export type InputSample = {
  type: string
  trusted: boolean
  atMs: number
  x?: number
  y?: number
}
export type EventSample = {
  name: string
  interactionId: number
  startTime: number
  duration: number
  inputDelay: number
  processing: number
}
export type FunctionSample = { calls: number; totalMs: number; maxMs: number }
export type BrowserSample = {
  startMs: number
  elapsedMs: number
  frameIntervals: number[]
  inputs: InputSample[]
  events: EventSample[]
  longTasks: { startTime: number; duration: number }[]
  visibilityChanges: number
  hiddenAtStart: boolean
  supportedEntries: string[]
  initial: BenchmarkState
  final: BenchmarkState
  counters: Record<string, number> | null
  functions: Record<string, FunctionSample> | null
  heapBefore: number | null
  heapAfter: number | null
}
declare global {
  interface Window {
    __canvasBenchmark?: BenchmarkBridge
    __canvasBenchmarkFunctions?: {
      reset(): void
      snapshot(): Record<string, FunctionSample>
    }
    __canvasCollector?: {
      ready(): boolean
      start(): void
      stop(): BrowserSample
      receivedMoves(): number
    }
  }
}
