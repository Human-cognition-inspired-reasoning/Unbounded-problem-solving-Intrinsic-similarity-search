import assert from 'node:assert/strict'
import test from 'node:test'
import {
  appendCitation,
  appendReasoningDelta,
  appendResponseReasoning,
  appendResponseSource,
  appendResponseText,
  applyStreamingMessageEvent,
  formatElapsedTime,
  groupResponseParts,
  isNearBottom,
  responseWritingEpoch,
} from './streamingState.js'
import {
  applyInlineCitations,
  citationsForTextPart,
  normalizeMathDelimiters,
  safeDecodeURIComponent,
} from './messageRendering.js'

test('citation title decoding falls back to the original malformed value', () => {
  assert.equal(safeDecodeURIComponent('정상%20제목'), '정상 제목')
  assert.equal(safeDecodeURIComponent('잘못된%제목'), '잘못된%제목')
})

test('reasoning summary blocks use provider IDs instead of colliding indexes', () => {
  let blocks = appendReasoningDelta([], 'item-a:0', 0, '첫 블록')
  blocks = appendReasoningDelta(blocks, 'item-b:0', 0, '둘째 블록')
  blocks = appendReasoningDelta(blocks, 'item-a:0', 0, ' 계속')

  assert.deepEqual(blocks.map(({ id, text }) => ({ id, text })), [
    { id: 'item-a:0', text: '첫 블록 계속' },
    { id: 'item-b:0', text: '둘째 블록' },
  ])
})

test('normalized streaming events update one assistant message reducer', () => {
  let message = {
    content: '',
    reasoningSummary: [],
    responseParts: [],
    citations: [],
    phase: 'thinking',
  }
  message = applyStreamingMessageEvent(message, 'reasoning_delta', {
    id: 'thought-1', index: 0, delta: '근거 검토',
  })
  message = applyStreamingMessageEvent(message, 'text_delta', { delta: '최종 답변' })
  message = applyStreamingMessageEvent(message, 'citation_added', {
    url: 'https://example.com', startIndex: 0, endIndex: 2,
  })

  assert.equal(message.reasoningSummary[0].text, '근거 검토')
  assert.equal(message.content, '최종 답변')
  assert.deepEqual(message.responseParts.map((part) => part.type), ['reasoning', 'text'])
  assert.equal(message.citations.length, 1)
  assert.equal(message.phase, 'writing')
})

test('resumed reasoning is appended after intervening search activity', () => {
  let items = appendResponseReasoning([], 'item-a:0', 0, '먼저 생각')
  items = appendResponseSource(items, 'search-1', { url: 'https://example.com' })
  items = appendResponseReasoning(items, 'item-a:0', 0, '검색 뒤 생각')

  assert.deepEqual(items.map((item) => item.type), ['reasoning', 'search', 'reasoning'])
  assert.equal(items.at(-1).text, '검색 뒤 생각')
  assert.notEqual(items[0].id, items[2].id)
})

test('response parts preserve interleaved text, reasoning, and search order', () => {
  let parts = appendResponseText([], '첫 답변')
  parts = appendResponseReasoning(parts, 'thought-2', 1, '다시 생각')
  parts = appendResponseSource(parts, 'search-1', {
    url: 'https://example.com/source',
    title: 'Source',
  })
  parts = appendResponseText(parts, '최종 답변')

  assert.deepEqual(parts.map((part) => part.type), ['text', 'reasoning', 'search', 'text'])
  assert.equal(parts[2].sources[0].title, 'Source')
})

test('live citations are deduplicated and receive stable source numbers', () => {
  let citations = appendCitation([], {
    url: 'https://example.com/a', startIndex: 0, endIndex: 2, placement: 'inline',
  })
  citations = appendCitation(citations, {
    url: 'https://example.com/a', startIndex: 4, endIndex: 6, placement: 'inline',
  })
  citations = appendCitation(citations, {
    url: 'https://example.com/a', startIndex: 4, endIndex: 6, placement: 'inline',
  })

  assert.equal(citations.length, 2)
  assert.deepEqual(citations.map((citation) => citation.sourceNumber), [1, 1])
})

test('citations are scoped to the matching interleaved text part', () => {
  const citations = [{
    url: 'https://example.com/a', title: 'A', startIndex: 3, endIndex: 5,
    indexUnit: 'unicode_codepoint', placement: 'inline', sourceNumber: 1,
  }]
  assert.equal(citationsForTextPart(citations, '앞부분', '근거문장').length, 1)
  assert.match(
    applyInlineCitations('근거문장', citationsForTextPart(citations, '앞부분', '근거문장')),
    /citation:A"\)/,
  )
})

test('plain bracketed LaTeX is promoted to display math', () => {
  const input = '[ \\text{Utility}\n\\text{Correct Answers} -\\lambda\\cdot\\text{Unnecessary Abstentions} ]'
  const normalized = normalizeMathDelimiters(input)
  assert.match(normalized, /\$\$/)
  assert.match(normalized, /\\lambda\\cdot/)
})

test('near-bottom detection yields control after a user scrolls up', () => {
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 400 }), false)
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 560, clientHeight: 400 }), false)
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 592, clientHeight: 400 }), true)
})

test('each new answer segment advances the collapse epoch once', () => {
  let parts = appendResponseReasoning([], 'thought-1', 0, '첫 생각')
  assert.equal(responseWritingEpoch(parts), 0)

  parts = appendResponseText(parts, '첫 답변')
  assert.equal(responseWritingEpoch(parts), 1)
  parts = appendResponseText(parts, ' 계속')
  assert.equal(responseWritingEpoch(parts), 1)

  parts = appendResponseReasoning(parts, 'thought-2', 1, '다시 생각')
  parts = appendResponseText(parts, '최종 답변')
  assert.equal(responseWritingEpoch(parts), 2)
})

test('reasoning and search stay in one activity group until answer text breaks them', () => {
  const groups = groupResponseParts([
    { id: 'r1', type: 'reasoning', text: '첫 생각' },
    { id: 's1', type: 'search', sources: [{ url: 'https://example.com' }] },
    { id: 'r2', type: 'reasoning', text: '이어서 생각' },
    { id: 't1', type: 'text', text: '중간 답변' },
    { id: 's2', type: 'search', sources: [{ url: 'https://example.org' }] },
  ])

  assert.deepEqual(groups.map((group) => group.type), ['activity', 'text', 'activity'])
  assert.deepEqual(groups[0].parts.map((part) => part.type), ['reasoning', 'search', 'reasoning'])
  assert.deepEqual(groups[2].parts.map((part) => part.type), ['search'])
})

test('elapsed time switches to minutes and seconds after one minute', () => {
  assert.equal(formatElapsedTime(59), '59초')
  assert.equal(formatElapsedTime(60), '1분 0초')
  assert.equal(formatElapsedTime(125), '2분 5초')
})

test('OpenAI commentary and answer remain separate ordinary text groups, including late phase metadata', () => {
  let message = { content: '', responseParts: [] }
  for (const [messageId, delta] of [['m1', '확인할게요.'], ['m2', '결론'], ['m2', '입니다.']]) {
    message = applyStreamingMessageEvent(message, 'text_delta', { messageId, contentIndex: 0, delta })
  }
  for (const [messageId, phase] of [['m1', 'commentary'], ['m2', 'final_answer']]) {
    message = applyStreamingMessageEvent(message, 'text_metadata', { messageId, phase })
  }
  assert.deepEqual(message.responseParts.map(({ text, phase }) => ({ text, phase })), [
    { text: '확인할게요.', phase: 'commentary' }, { text: '결론입니다.', phase: 'final_answer' },
  ])
  const groups = groupResponseParts(message.responseParts)
  assert.deepEqual(groups.map(({ type }) => type), ['text', 'text'])
  assert.equal(message.content, '확인할게요.결론입니다.')
  const citations = citationsForTextPart([
    { url: 'https://example.com', placement: 'inline', startIndex: 6, endIndex: 12 },
  ], '확인할게요.', '결론입니다.')
  assert.equal(citations[0].endIndex, 6)
})

test('same-phase OpenAI messages keep separate boundaries while legacy text still coalesces', () => {
  let parts = appendResponseText([], 'first', { messageId: 'm1', contentIndex: 0, phase: 'commentary' })
  parts = appendResponseText(parts, 'second', { messageId: 'm2', contentIndex: 0, phase: 'commentary' })
  assert.equal(parts.length, 2)
  assert.equal(appendResponseText(appendResponseText([], 'a'), 'b')[0].text, 'ab')
})
