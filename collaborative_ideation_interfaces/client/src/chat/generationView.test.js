import assert from 'node:assert/strict'
import test from 'node:test'
import { generationView } from './generationView.js'
import { previewTitle } from './titles.js'

const saved = { id: 'm1', role: 'user', content: 'saved' }
const pending = { id: 'pending-1', role: 'user', pending: true }
const streaming = { id: 'a1', role: 'assistant', streaming: true }

test('the open conversation shows its own pending and streaming messages', () => {
  const view = generationView({
    generationRequests: {
      r1: { conversationId: 'c1', pendingMessage: pending, streamingMessage: null },
      r2: { conversationId: 'c2', pendingMessage: null, streamingMessage: streaming },
    },
    conversations: [{ id: 'c1' }, { id: 'c2' }],
    activeConversation: { id: 'c1', messages: [saved] },
    limit: 3,
  })
  assert.deepEqual(view.visibleMessages, [saved, pending])
  assert.equal(view.isGenerating, true)
  assert.equal(view.limitReached, false)
})

test('a new chat matches a request that has no conversation yet', () => {
  const view = generationView({
    generationRequests: { r1: { conversationId: null, pendingMessage: pending } },
    conversations: [],
    activeConversation: null,
    limit: 3,
  })
  assert.deepEqual(view.visibleMessages, [pending])
  assert.equal(view.isGenerating, true)
})

test('other tabs count toward the limit once per conversation', () => {
  const view = generationView({
    generationRequests: { r1: { conversationId: 'c1' } },
    conversations: [
      { id: 'c1', isGenerating: true },
      { id: 'c2', isGenerating: true },
      { id: 'c3', isGenerating: false, messages: [saved] },
    ],
    activeConversation: { id: 'c3', messages: [saved] },
    limit: 2,
  })
  assert.equal(view.limitReached, true)
  assert.equal(view.isGenerating, false)
  assert.deepEqual(view.visibleMessages, [saved])
})

test('conversation titles are cut at twenty characters', () => {
  assert.equal(previewTitle('짧은 제목'), '짧은 제목')
  assert.equal(previewTitle('a'.repeat(20)), 'a'.repeat(20))
  assert.equal(previewTitle('a'.repeat(21)), `${'a'.repeat(20)}…`)
})
