import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError, chatApi } from './chatApi.js'
import { request } from './http.js'

const encoder = new TextEncoder()
const frame = (event, payload) => `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`
const SETTINGS = {
  modelId: 'model-1', reasoningEffort: 'medium', thinkingEnabled: false, webSearchEnabled: false,
}
const send = (onEvent) => chatApi.streamMessage({
  conversationId: 'conversation-1', content: '질문', settings: SETTINGS, clientRequestId: 'request-1',
}, onEvent)

test('message_done finishes without EOF or transport cancellation completing', async (t) => {
  const completion = { assistantMessage: { id: 'answer-1', content: '완료된 답변' } }
  let cancelled = false
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frame('message_done', completion)))
      // The connection remains open even though the application is finished.
    },
    cancel() {
      cancelled = true
      return new Promise(() => {})
    },
  })
  t.mock.method(globalThis, 'fetch', async () => new Response(body))
  const events = []
  let timer
  try {
    const result = await Promise.race([
      send((type) => events.push(type)),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Still waiting after message_done')), 1000)
      }),
    ])
    assert.deepEqual(result, completion)
    assert.deepEqual(events, ['message_done'])
    assert.equal(cancelled, true)
    assert.equal(body.locked, false)
  } finally {
    clearTimeout(timer)
  }
})

test('chunked Korean text and a final event are delivered in order', async (t) => {
  const completion = { assistantMessage: { id: 'answer-1', content: '한글 답변' } }
  const bytes = encoder.encode(
    frame('text_delta', { delta: '한글 답변' }) + frame('message_done', completion),
  )
  const body = new ReadableStream({
    start(controller) {
      for (let offset = 0; offset < bytes.length; offset += 7) {
        controller.enqueue(bytes.slice(offset, offset + 7))
      }
      controller.close()
    },
  })
  t.mock.method(globalThis, 'fetch', async () => new Response(body))
  const events = []
  assert.deepEqual(await send((type, payload) => events.push([type, payload])), completion)
  assert.deepEqual(events, [
    ['text_delta', { delta: '한글 답변' }],
    ['message_done', completion],
  ])
  assert.equal(body.locked, false)
})

test('EOF without message_done remains an incomplete-stream error', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(frame('text_delta', { delta: '일부' })))
  await assert.rejects(send(), (error) => (
    error instanceof ApiError && error.code === 'STREAM_INCOMPLETE'
  ))
})

test('provider errors reject and cancel an otherwise open stream', async (t) => {
  let cancelled = false
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frame('error', {
        error: { message: 'Provider failed', code: 'STREAM_FAILED' }, status: 502,
      })))
    },
    cancel() { cancelled = true },
  })
  t.mock.method(globalThis, 'fetch', async () => new Response(body))
  await assert.rejects(send(), (error) => (
    error instanceof ApiError && error.code === 'STREAM_FAILED' && error.status === 502
  ))
  assert.equal(cancelled, true)
  assert.equal(body.locked, false)
})

test('message requests keep the server body contract', async (t) => {
  let url
  let request
  t.mock.method(globalThis, 'fetch', async (requestUrl, options) => {
    url = requestUrl
    request = { ...JSON.parse(options.body), headers: options.headers }
    return new Response(frame('message_done', { assistantMessage: { content: 'ok' } }))
  })
  await chatApi.streamMessage({
    conversationId: 'c1',
    content: 'unchanged question',
    attachmentIds: ['attachment-1'],
    settings: { modelId: 'm1', reasoningEffort: 'low', thinkingEnabled: true, webSearchEnabled: true },
    clientRequestId: 'r1',
  })
  assert.equal(url, '/api/conversations/c1/messages')
  const { clientInstanceId, ...body } = request
  assert.match(clientInstanceId, /^[0-9a-f-]{36}$/)
  assert.deepEqual(body, {
    content: 'unchanged question',
    attachmentIds: ['attachment-1'],
    modelId: 'm1',
    reasoningEffort: 'low',
    thinkingEnabled: true,
    webSearchEnabled: true,
    webSearchMode: 'required',
    clientRequestId: 'r1',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
  })
})

test('extra request headers keep the JSON content type', async (t) => {
  let headers
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    headers = options.headers
    return new Response('{}')
  })
  await request('/api/health', { headers: { 'X-Test': '1' } })
  assert.deepEqual(headers, { 'Content-Type': 'application/json', 'X-Test': '1' })
})

test('local uploads use binary payload and authenticated same-origin request', async (t) => {
  let request
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    request = options
    return new Response('{}')
  })
  const file = new File(['binary'], 'a.pdf')
  await chatApi.uploadAttachment({ method: 'PUT', url: '/api/attachments/a/upload', headers: { 'Content-Type': 'application/octet-stream' } }, file)
  assert.equal(request.body, file)
  assert.equal(request.credentials, 'same-origin')
  assert.equal(request.method, 'PUT')
})
