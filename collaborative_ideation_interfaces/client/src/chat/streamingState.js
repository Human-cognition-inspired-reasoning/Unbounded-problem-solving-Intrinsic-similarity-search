export function appendReasoningDelta(blocks, id, index, delta) {
  const next = [...(blocks || [])]
  const blockId = id || `reasoning:${index}`
  const position = next.findIndex((block) => (block.id || `reasoning:${block.index}`) === blockId)
  if (position === -1) {
    next.push({ id: blockId, index, text: delta })
  } else {
    next[position] = { ...next[position], text: next[position].text + delta }
  }
  return next
}

export function appendResponseText(parts, delta, metadata = {}) {
  if (!delta) return [...(parts || [])]
  const next = [...(parts || [])]
  const last = next.at(-1)
  const fields = Object.fromEntries(
    ['messageId', 'contentIndex', 'phase'].filter((key) => metadata[key] !== undefined)
      .map((key) => [key, metadata[key]]),
  )
  if (last?.type === 'text'
    && ['messageId', 'contentIndex', 'phase'].every((key) => last[key] === fields[key])) {
    next[next.length - 1] = { ...last, text: last.text + delta }
  } else {
    next.push({ id: `text:${next.length}`, type: 'text', text: delta, ...fields })
  }
  return next
}

export function appendResponseReasoning(parts, id, index, delta) {
  if (!delta) return [...(parts || [])]
  const next = [...(parts || [])]
  const streamId = id || `reasoning:${index}`
  const last = next.at(-1)
  if (last?.type === 'reasoning' && last.streamId === streamId) {
    next[next.length - 1] = { ...last, text: last.text + delta }
  } else {
    next.push({
      id: `${streamId}:segment:${next.length}`,
      streamId,
      type: 'reasoning',
      index,
      text: delta,
    })
  }
  return next
}

export function upsertResponseSearch(parts, id, changes = {}) {
  const next = [...(parts || [])]
  const position = next.findIndex((part) => part.type === 'search' && part.id === id)
  if (position === -1) {
    next.push({ id, type: 'search', sources: [], ...changes })
  } else {
    next[position] = { ...next[position], ...changes }
  }
  return next
}

export function appendResponseSource(parts, id, source) {
  const existing = (parts || []).find((part) => part.type === 'search' && part.id === id)
  const currentSources = existing?.sources || []
  const sources = currentSources.some((item) => item.url === source.url)
    ? currentSources.map((item) => (
      item.url === source.url && !item.title && source.title ? { ...item, title: source.title } : item
    ))
    : [...currentSources, source]
  return upsertResponseSearch(parts, id, { sources })
}

export function appendCitation(citations, citation) {
  const next = [...(citations || [])]
  const duplicate = next.some((item) => (
    item.url === citation.url
    && item.startIndex === citation.startIndex
    && item.endIndex === citation.endIndex
    && item.placement === citation.placement
  ))
  if (!duplicate) {
    const existingSource = next.find((item) => item.url === citation.url)
    const sourceNumbers = next.map((item) => item.sourceNumber).filter(Number.isFinite)
    next.push({
      ...citation,
      sourceNumber: citation.sourceNumber
        || existingSource?.sourceNumber
        || Math.max(0, ...sourceNumbers) + 1,
    })
  }
  return next
}

export function applyStreamingMessageEvent(message, eventType, payload) {
  if (eventType === 'text_delta') {
    return {
      ...message,
      content: message.content + payload.delta,
      responseParts: appendResponseText(message.responseParts, payload.delta, payload),
      phase: 'writing',
    }
  }
  if (eventType === 'text_metadata') {
    return {
      ...message,
      responseParts: (message.responseParts || []).map((part) => (
        part.type === 'text' && part.messageId === payload.messageId
          ? { ...part, phase: payload.phase } : part
      )),
    }
  }
  if (eventType === 'reasoning_delta') {
    return {
      ...message,
      reasoningSummary: appendReasoningDelta(
        message.reasoningSummary,
        payload.id,
        payload.index,
        payload.delta,
      ),
      responseParts: appendResponseReasoning(
        message.responseParts,
        payload.id,
        payload.index,
        payload.delta,
      ),
    }
  }
  if (eventType === 'status') return { ...message, phase: payload.phase }
  if (eventType === 'search_started') {
    return {
      ...message,
      responseParts: upsertResponseSearch(message.responseParts, payload.id),
    }
  }
  if (eventType === 'source_added') {
    return {
      ...message,
      responseParts: appendResponseSource(
        message.responseParts,
        payload.searchId,
        payload.source,
      ),
    }
  }
  if (eventType === 'citation_added') {
    return { ...message, citations: appendCitation(message.citations, payload) }
  }
  return message
}

export function isNearBottom(element, threshold = 8) {
  if (!element) return true
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold
}

export function responseWritingEpoch(parts) {
  return (parts || []).reduce(
    (count, part) => count + (part.type === 'text' ? 1 : 0),
    0,
  )
}

export function groupResponseParts(parts) {
  const groups = []
  for (const part of parts || []) {
    if (part.type === 'text') {
      groups.push({ id: part.id, type: 'text', part })
      continue
    }
    if (part.type !== 'reasoning' && part.type !== 'search') continue
    const last = groups.at(-1)
    if (last?.type === 'activity') {
      last.parts.push(part)
    } else {
      groups.push({ id: `activity:${part.id}`, type: 'activity', parts: [part] })
    }
  }
  return groups
}

export function formatElapsedTime(seconds) {
  const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0))
  if (totalSeconds < 60) return `${totalSeconds}초`
  const minutes = Math.floor(totalSeconds / 60)
  return `${minutes}분 ${totalSeconds % 60}초`
}
