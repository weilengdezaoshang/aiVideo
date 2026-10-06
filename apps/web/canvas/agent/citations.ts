export type AnswerPart = { kind: 'text'; text: string } | { kind: 'ref'; ref: string }

const REF = /\[(E\d+(?:\.f\d+)?)\]/g

/** Splits an answer into plain text and `[E3]` / `[E3.f2]` citations, preserving order. */
export function splitCitations(answer: string): AnswerPart[] {
  const parts: AnswerPart[] = []
  let last = 0
  for (const match of answer.matchAll(REF)) {
    const at = match.index ?? 0
    if (at > last) {
      parts.push({ kind: 'text', text: answer.slice(last, at) })
    }
    parts.push({ kind: 'ref', ref: match[1] })
    last = at + match[0].length
  }
  if (last < answer.length) {
    parts.push({ kind: 'text', text: answer.slice(last) })
  }
  return parts
}

export const evidenceOf = (ref: string): string => ref.split('.')[0]
