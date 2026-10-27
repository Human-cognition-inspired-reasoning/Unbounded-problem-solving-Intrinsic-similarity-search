function looksLikeDisplayMath(value) {
  return /\\(?:text|frac|d?frac|sum|prod|int|lambda|mu|sigma|alpha|beta|gamma|cdot|times|begin|left|right|mathbb|mathrm|operatorname)\b/.test(value)
}

export function safeDecodeURIComponent(value) {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

export function normalizeMathDelimiters(content) {
  if (!content) return ''
  let normalized = content
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, expression) => `\n$$\n${expression.trim()}\n$$\n`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, expression) => `$${expression.trim()}$`)

  normalized = normalized.replace(
    /(^|\n)\s*\[\s*([\s\S]*?)\s*\]\s*(?=\n|$)/g,
    (match, prefix, expression) => (
      looksLikeDisplayMath(expression)
        ? `${prefix}$$\n${expression.trim()}\n$$`
        : match
    ),
  )
  return normalized
}

function indexLength(value, unit) {
  if (unit === 'utf8_byte') return new TextEncoder().encode(value).length
  if (unit === 'unicode_codepoint') return Array.from(value).length
  return value.length
}

function toCodeUnitIndex(value, index, unit) {
  const safeIndex = Math.max(0, Number(index) || 0)
  if (unit === 'utf8_byte') {
    return new TextDecoder().decode(new TextEncoder().encode(value).slice(0, safeIndex)).length
  }
  if (unit === 'unicode_codepoint') return Array.from(value).slice(0, safeIndex).join('').length
  return Math.min(safeIndex, value.length)
}

export function citationsForTextPart(citations, prefixText, partText) {
  return (citations || []).flatMap((citation) => {
    if (citation.placement !== 'inline' || !citation.url) return []
    const unit = citation.indexUnit || 'unicode_codepoint'
    const prefixLength = indexLength(prefixText, unit)
    const partLength = indexLength(partText, unit)
    const end = Number(citation.endIndex)
    if (!Number.isFinite(end) || end <= prefixLength || end > prefixLength + partLength) return []
    return [{
      ...citation,
      startIndex: Math.max(0, Number(citation.startIndex || end) - prefixLength),
      endIndex: end - prefixLength,
    }]
  })
}

export function applyInlineCitations(content, citations) {
  const positioned = (citations || []).map((citation, index) => ({
    ...citation,
    sourceNumber: citation.sourceNumber || index + 1,
    codeUnitIndex: toCodeUnitIndex(content, citation.endIndex, citation.indexUnit),
  })).sort((left, right) => (
    right.codeUnitIndex - left.codeUnitIndex || right.sourceNumber - left.sourceNumber
  ))

  let result = content
  for (const citation of positioned) {
    const url = encodeURI(citation.url).replaceAll('(', '%28').replaceAll(')', '%29')
    const title = encodeURIComponent(citation.title || citation.url)
    const marker = `[${citation.sourceNumber}](${url} "citation:${title}")`
    result = `${result.slice(0, citation.codeUnitIndex)}${marker}${result.slice(citation.codeUnitIndex)}`
  }
  return result
}
