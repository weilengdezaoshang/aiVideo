// Wire contracts of the canvas Agent harness. Mirrors apps/api/backend/agent/contracts.
// Parsers are strict: unknown keys and out-of-range values are rejected, never coerced.

export type JsonRecord = Record<string, unknown>
export type FactValue = string | number | boolean | (string | number)[]

export type AgentSource = { documentId: string; revision: number; assetIds: string[] }
export type AgentOmitted = { objectIds: string[]; fields: string[] }
export type AgentPage = { offset: number; nextOffset: number | null; total: number }
export type AgentFact = {
  id: string
  statement: string
  path: string
  value: FactValue
  scope: 'complete' | 'partial' | 'item'
}
export type AgentCoverage = {
  complete: boolean
  reason: string | null
  missing: AgentOmitted | null
}
export type AgentNextAction = {
  action: 'call_tool' | 'complete_step' | 'ask_user' | 'report_blocker'
  tool: string | null
  arguments: JsonRecord | null
  reason: string
  priority: 'required' | 'suggested'
}
export type AgentFieldProblem = {
  path: string
  problem: 'type' | 'missing' | 'extra' | 'range' | 'value'
}
export type AgentFailureCategory =
  'invalid_arguments' | 'out_of_scope' | 'unavailable' | 'changed' | 'limit' | 'policy' | 'internal'
export type AgentToolFailure = {
  code: string
  category: AgentFailureCategory
  message: string
  retryable: boolean
  details: {
    fields: AgentFieldProblem[]
    unknownObjectIds: string[]
    allowedTools: string[]
    limit: Record<string, number> | null
  }
  next: AgentNextAction[]
}
export type AgentOkResult = {
  data: JsonRecord
  evidence: AgentFact[]
  coverage: AgentCoverage
  next: AgentNextAction[]
  truncated: boolean
  omitted: AgentOmitted
  page: AgentPage | null
}
type EnvelopeBase = {
  evidenceId: string
  kind: 'tool'
  tool: string
  toolVersion: number
  callId: string
  stepKey: string | null
  trust: 'untrusted_canvas_data'
  source: AgentSource
  producedAt: string
}
export type AgentOkEnvelope = EnvelopeBase & { status: 'ok'; result: AgentOkResult }
export type AgentErrorEnvelope = EnvelopeBase & { status: 'error'; error: AgentToolFailure }
export type AgentDuplicateEnvelope = EnvelopeBase & {
  status: 'duplicate'
  dedupeOf: string
  next: AgentNextAction[]
}
export type AgentEnvelope = AgentOkEnvelope | AgentErrorEnvelope | AgentDuplicateEnvelope
export type AgentControlReceipt = {
  kind: 'control'
  tool: string
  callId: string
  status: 'accepted' | 'rejected'
  effect: JsonRecord | null
  error: AgentToolFailure | null
}

export type AgentPlanStep = {
  key: string
  title: string
  intent: string
  tools: string[]
  dependsOn: string[]
  doneWhen: string
}
export type AgentPlanDraft = { goal: string; steps: AgentPlanStep[] }
export type AgentClaimScope = 'complete' | 'partial' | 'item' | 'unavailable'
export type AgentClaim = { text: string; evidenceRefs: string[]; scope: AgentClaimScope }
export type AgentFinish = { answer: string; claims: AgentClaim[] }
export type AgentIssueKind = 'ungrounded' | 'incomplete' | 'truncated' | 'off_target' | 'stale'
export type AgentReflectionIssue = {
  kind: AgentIssueKind
  problem: string
  stepKey: string | null
  evidenceRefs: string[]
}
export type AgentReflectionVerdict =
  'accept' | 'revise_answer' | 'retry_step' | 'replan' | 'ask_user'
export type AgentReflection = {
  verdict: AgentReflectionVerdict
  issues: AgentReflectionIssue[]
  question: string | null
}
export type AgentPersonaExpressionKey = 'refuse' | 'uncertain' | 'ask' | 'partial'
export type AgentPersona = {
  id: string
  version: number
  displayName: string
  identity: string
  voice: string[]
  principles: string[]
  expression: Partial<Record<AgentPersonaExpressionKey, string>>
}

const MAX_NEXT = 5
const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const isInt = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value)
const isStr = (value: unknown): value is string => typeof value === 'string'
const strList = (value: unknown, max = Infinity): value is string[] =>
  Array.isArray(value) && value.length <= max && value.every(isStr)
const oneOf = <T extends string>(options: readonly T[], value: unknown): value is T =>
  isStr(value) && (options as readonly string[]).includes(value)
const exactKeys = (value: JsonRecord, keys: readonly string[]): boolean =>
  Object.keys(value).length === keys.length && keys.every((key) => key in value)
const list = <T>(
  value: unknown,
  parse: (item: unknown) => T | null,
  max = Infinity,
): T[] | null => {
  if (!Array.isArray(value) || value.length > max) {
    return null
  }
  const out: T[] = []
  for (const item of value) {
    const parsed = parse(item)
    if (parsed === null) {
      return null
    }
    out.push(parsed)
  }
  return out
}
const nullable = <T>(value: unknown, parse: (item: unknown) => T | null): T | null | undefined => {
  if (value === null) {
    return null
  }
  const parsed = parse(value)
  return parsed === null ? undefined : parsed
}

function parseSource(value: unknown): AgentSource | null {
  if (!isRecord(value) || !exactKeys(value, ['documentId', 'revision', 'assetIds'])) {
    return null
  }
  if (!isStr(value.documentId) || !isInt(value.revision) || !strList(value.assetIds)) {
    return null
  }
  return { documentId: value.documentId, revision: value.revision, assetIds: value.assetIds }
}
function parseOmitted(value: unknown): AgentOmitted | null {
  if (!isRecord(value) || !exactKeys(value, ['objectIds', 'fields'])) {
    return null
  }
  if (!strList(value.objectIds) || !strList(value.fields)) {
    return null
  }
  return { objectIds: value.objectIds, fields: value.fields }
}
function parsePage(value: unknown): AgentPage | null {
  if (!isRecord(value) || !exactKeys(value, ['offset', 'nextOffset', 'total'])) {
    return null
  }
  const { offset, nextOffset, total } = value
  if (!isInt(offset) || offset < 0 || !isInt(total) || total < 0) {
    return null
  }
  if (nextOffset !== null && (!isInt(nextOffset) || nextOffset < 0)) {
    return null
  }
  return { offset, nextOffset: nextOffset as number | null, total }
}
function parseFactValue(value: unknown): FactValue | null {
  if (isStr(value) || typeof value === 'boolean') {
    return value
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  if (
    Array.isArray(value) &&
    value.every((v) => isStr(v) || (typeof v === 'number' && Number.isFinite(v)))
  ) {
    return value as (string | number)[]
  }
  return null
}
function parseFact(value: unknown): AgentFact | null {
  if (!isRecord(value) || !exactKeys(value, ['id', 'statement', 'path', 'value', 'scope'])) {
    return null
  }
  const factValue = parseFactValue(value.value)
  if (!isStr(value.id) || !/^E\d+\.f\d+$/.test(value.id) || !isStr(value.statement)) {
    return null
  }
  if (!isStr(value.path) || factValue === null) {
    return null
  }
  if (!oneOf(['complete', 'partial', 'item'] as const, value.scope)) {
    return null
  }
  return {
    id: value.id,
    statement: value.statement,
    path: value.path,
    value: factValue,
    scope: value.scope,
  }
}
function parseCoverage(value: unknown): AgentCoverage | null {
  if (!isRecord(value) || !exactKeys(value, ['complete', 'reason', 'missing'])) {
    return null
  }
  if (typeof value.complete !== 'boolean') {
    return null
  }
  if (value.reason !== null && !isStr(value.reason)) {
    return null
  }
  const missing = nullable(value.missing, parseOmitted)
  if (missing === undefined) {
    return null
  }
  return { complete: value.complete, reason: value.reason, missing }
}
function parseNext(value: unknown): AgentNextAction | null {
  if (
    !isRecord(value) ||
    !exactKeys(value, ['action', 'tool', 'arguments', 'reason', 'priority'])
  ) {
    return null
  }
  const actions = ['call_tool', 'complete_step', 'ask_user', 'report_blocker'] as const
  if (!oneOf(actions, value.action) || !oneOf(['required', 'suggested'] as const, value.priority)) {
    return null
  }
  if (value.tool !== null && !isStr(value.tool)) {
    return null
  }
  if (value.arguments !== null && !isRecord(value.arguments)) {
    return null
  }
  if (!isStr(value.reason)) {
    return null
  }
  return {
    action: value.action,
    tool: value.tool,
    arguments: value.arguments,
    reason: value.reason,
    priority: value.priority,
  }
}
function parseFieldProblem(value: unknown): AgentFieldProblem | null {
  if (!isRecord(value) || !exactKeys(value, ['path', 'problem'])) {
    return null
  }
  if (
    !isStr(value.path) ||
    !oneOf(['type', 'missing', 'extra', 'range', 'value'] as const, value.problem)
  ) {
    return null
  }
  return { path: value.path, problem: value.problem }
}
function parseFailure(value: unknown): AgentToolFailure | null {
  if (
    !isRecord(value) ||
    !exactKeys(value, ['code', 'category', 'message', 'retryable', 'details', 'next'])
  ) {
    return null
  }
  const categories = [
    'invalid_arguments',
    'out_of_scope',
    'unavailable',
    'changed',
    'limit',
    'policy',
    'internal',
  ] as const
  if (!isStr(value.code) || !oneOf(categories, value.category) || !isStr(value.message)) {
    return null
  }
  if (typeof value.retryable !== 'boolean' || !isRecord(value.details)) {
    return null
  }
  const details = value.details
  if (!exactKeys(details, ['fields', 'unknownObjectIds', 'allowedTools', 'limit'])) {
    return null
  }
  const fields = list(details.fields, parseFieldProblem)
  if (!fields || !strList(details.unknownObjectIds) || !strList(details.allowedTools)) {
    return null
  }
  let limit: Record<string, number> | null = null
  if (details.limit !== null) {
    if (!isRecord(details.limit) || !Object.values(details.limit).every(isInt)) {
      return null
    }
    limit = details.limit as Record<string, number>
  }
  const next = list(value.next, parseNext, MAX_NEXT)
  if (!next) {
    return null
  }
  return {
    code: value.code,
    category: value.category,
    message: value.message,
    retryable: value.retryable,
    details: {
      fields,
      unknownObjectIds: details.unknownObjectIds,
      allowedTools: details.allowedTools,
      limit,
    },
    next,
  }
}
function parseOkResult(value: unknown): AgentOkResult | null {
  const keys = ['data', 'evidence', 'coverage', 'next', 'truncated', 'omitted', 'page']
  if (!isRecord(value) || !exactKeys(value, keys) || !isRecord(value.data)) {
    return null
  }
  const evidence = list(value.evidence, parseFact)
  const coverage = parseCoverage(value.coverage)
  const next = list(value.next, parseNext, MAX_NEXT)
  const omitted = parseOmitted(value.omitted)
  const page = nullable(value.page, parsePage)
  if (!evidence || !coverage || !next || !omitted || page === undefined) {
    return null
  }
  if (typeof value.truncated !== 'boolean') {
    return null
  }
  return { data: value.data, evidence, coverage, next, truncated: value.truncated, omitted, page }
}

const ENVELOPE_KEYS = [
  'evidenceId',
  'kind',
  'tool',
  'toolVersion',
  'callId',
  'stepKey',
  'trust',
  'source',
  'producedAt',
]
export function parseEnvelope(value: unknown): AgentEnvelope | null {
  if (!isRecord(value) || !isStr(value.status)) {
    return null
  }
  const tail =
    value.status === 'ok' ? ['result'] : value.status === 'error' ? ['error'] : ['dedupeOf', 'next']
  if (!['ok', 'error', 'duplicate'].includes(value.status)) {
    return null
  }
  if (!exactKeys(value, [...ENVELOPE_KEYS, 'status', ...tail])) {
    return null
  }
  const source = parseSource(value.source)
  if (
    !isStr(value.evidenceId) ||
    !/^E\d+$/.test(value.evidenceId) ||
    value.kind !== 'tool' ||
    !source
  ) {
    return null
  }
  if (
    !isStr(value.tool) ||
    !isInt(value.toolVersion) ||
    !isStr(value.callId) ||
    !isStr(value.producedAt)
  ) {
    return null
  }
  if (value.stepKey !== null && !isStr(value.stepKey)) {
    return null
  }
  if (value.trust !== 'untrusted_canvas_data') {
    return null
  }
  const base: EnvelopeBase = {
    evidenceId: value.evidenceId,
    kind: 'tool',
    tool: value.tool,
    toolVersion: value.toolVersion,
    callId: value.callId,
    stepKey: value.stepKey,
    trust: 'untrusted_canvas_data',
    source,
    producedAt: value.producedAt,
  }
  if (value.status === 'ok') {
    const result = parseOkResult(value.result)
    return result ? { ...base, status: 'ok', result } : null
  }
  if (value.status === 'error') {
    const error = parseFailure(value.error)
    return error ? { ...base, status: 'error', error } : null
  }
  const next = list(value.next, parseNext, MAX_NEXT)
  if (!next || !isStr(value.dedupeOf) || !/^E\d+$/.test(value.dedupeOf)) {
    return null
  }
  return { ...base, status: 'duplicate', dedupeOf: value.dedupeOf, next }
}

export function parseControlReceipt(value: unknown): AgentControlReceipt | null {
  if (
    !isRecord(value) ||
    !exactKeys(value, ['kind', 'tool', 'callId', 'status', 'effect', 'error'])
  ) {
    return null
  }
  if (value.kind !== 'control' || !isStr(value.tool) || !isStr(value.callId)) {
    return null
  }
  if (!oneOf(['accepted', 'rejected'] as const, value.status)) {
    return null
  }
  if (value.effect !== null && !isRecord(value.effect)) {
    return null
  }
  const error = nullable(value.error, parseFailure)
  if (error === undefined) {
    return null
  }
  return {
    kind: 'control',
    tool: value.tool,
    callId: value.callId,
    status: value.status,
    effect: value.effect,
    error,
  }
}

function parseStep(value: unknown): AgentPlanStep | null {
  const keys = ['key', 'title', 'intent', 'tools', 'dependsOn', 'doneWhen']
  if (!isRecord(value) || !exactKeys(value, keys)) {
    return null
  }
  if (!isStr(value.key) || !/^[a-z][a-z0-9_]{0,31}$/.test(value.key)) {
    return null
  }
  const { title, intent, doneWhen } = value
  if (!isStr(title) || title.length < 1 || title.length > 80) {
    return null
  }
  if (!isStr(intent) || intent.length < 1 || intent.length > 400) {
    return null
  }
  if (!isStr(doneWhen) || doneWhen.length < 1 || doneWhen.length > 300) {
    return null
  }
  if (!strList(value.tools, 8) || !strList(value.dependsOn, 6)) {
    return null
  }
  return { key: value.key, title, intent, tools: value.tools, dependsOn: value.dependsOn, doneWhen }
}
export function parsePlan(value: unknown): AgentPlanDraft | null {
  if (!isRecord(value) || !exactKeys(value, ['goal', 'steps'])) {
    return null
  }
  if (!isStr(value.goal) || value.goal.length < 1 || value.goal.length > 300) {
    return null
  }
  const steps = list(value.steps, parseStep, 6)
  return steps && steps.length >= 1 ? { goal: value.goal, steps } : null
}

function parseClaim(value: unknown): AgentClaim | null {
  if (!isRecord(value) || !exactKeys(value, ['text', 'evidenceRefs', 'scope'])) {
    return null
  }
  if (!isStr(value.text) || value.text.length < 1 || value.text.length > 400) {
    return null
  }
  if (!strList(value.evidenceRefs, 8)) {
    return null
  }
  if (!oneOf(['complete', 'partial', 'item', 'unavailable'] as const, value.scope)) {
    return null
  }
  return { text: value.text, evidenceRefs: value.evidenceRefs, scope: value.scope }
}
export function parseFinish(value: unknown): AgentFinish | null {
  if (!isRecord(value) || !exactKeys(value, ['answer', 'claims'])) {
    return null
  }
  if (!isStr(value.answer) || value.answer.length < 1 || value.answer.length > 4000) {
    return null
  }
  const claims = list(value.claims, parseClaim, 32)
  return claims ? { answer: value.answer, claims } : null
}

function parseIssue(value: unknown): AgentReflectionIssue | null {
  if (!isRecord(value) || !exactKeys(value, ['kind', 'problem', 'stepKey', 'evidenceRefs'])) {
    return null
  }
  const kinds = ['ungrounded', 'incomplete', 'truncated', 'off_target', 'stale'] as const
  if (
    !oneOf(kinds, value.kind) ||
    !isStr(value.problem) ||
    value.problem.length < 1 ||
    value.problem.length > 300
  ) {
    return null
  }
  if (value.stepKey !== null && !isStr(value.stepKey)) {
    return null
  }
  if (!strList(value.evidenceRefs, 8)) {
    return null
  }
  return {
    kind: value.kind,
    problem: value.problem,
    stepKey: value.stepKey,
    evidenceRefs: value.evidenceRefs,
  }
}
export function parseReflection(value: unknown): AgentReflection | null {
  if (!isRecord(value) || !exactKeys(value, ['verdict', 'issues', 'question'])) {
    return null
  }
  const verdicts = ['accept', 'revise_answer', 'retry_step', 'replan', 'ask_user'] as const
  if (!oneOf(verdicts, value.verdict)) {
    return null
  }
  const issues = list(value.issues, parseIssue, 8)
  if (!issues) {
    return null
  }
  if (value.question !== null && (!isStr(value.question) || value.question.length > 300)) {
    return null
  }
  return { verdict: value.verdict, issues, question: value.question }
}

const CONTROL_MARKERS =
  /\[E\d|\bE\d+\.f\d+\b|submit_plan|complete_step|report_blocker|ask_user|submit_reflection|read_evidence|\bfinish\b|tool_call|function_call|read_canvas|list_canvas_elements|get_canvas_elements|inspect_image/
const cleanLine = (value: unknown, max: number): value is string =>
  isStr(value) && value.length >= 1 && value.length <= max && !CONTROL_MARKERS.test(value)
export function parsePersona(value: unknown): AgentPersona | null {
  const keys = ['id', 'version', 'displayName', 'identity', 'voice', 'principles', 'expression']
  if (!isRecord(value) || !exactKeys(value, keys)) {
    return null
  }
  if (!isStr(value.id) || !/^[a-zA-Z0-9_-]{1,64}$/.test(value.id)) {
    return null
  }
  if (!isInt(value.version) || value.version < 1) {
    return null
  }
  if (!cleanLine(value.displayName, 40) || !cleanLine(value.identity, 300)) {
    return null
  }
  const lines = (input: unknown, min: number, max: number): string[] | null =>
    Array.isArray(input) &&
    input.length >= min &&
    input.length <= max &&
    input.every((x) => cleanLine(x, 120))
      ? (input as string[])
      : null
  const voice = lines(value.voice, 0, 6)
  const principles = lines(value.principles, 1, 8)
  if (!voice || !principles || !isRecord(value.expression)) {
    return null
  }
  const expression: Partial<Record<AgentPersonaExpressionKey, string>> = {}
  for (const [key, text] of Object.entries(value.expression)) {
    if (!oneOf(['refuse', 'uncertain', 'ask', 'partial'] as const, key) || !cleanLine(text, 120)) {
      return null
    }
    expression[key] = text
  }
  return {
    id: value.id,
    version: value.version,
    displayName: value.displayName,
    identity: value.identity,
    voice,
    principles,
    expression,
  }
}

export const CONTRACT_PARSERS = {
  Envelope: parseEnvelope,
  ControlReceipt: parseControlReceipt,
  Plan: parsePlan,
  Finish: parseFinish,
  Reflection: parseReflection,
  PersonaProfile: parsePersona,
} as const
