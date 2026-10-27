export class ApiError extends Error {
  constructor(message, code, status) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

export function isSessionExpired(error) {
  return error instanceof ApiError && error.status === 401
}

function apiFetch(path, { headers, ...options } = {}, accept) {
  return fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(accept ? { Accept: accept } : {}),
      'Content-Type': 'application/json',
      ...headers,
    },
  })
}

function errorFromPayload(payload, status, fallback = '요청을 처리하지 못했습니다.', code = 'REQUEST_FAILED') {
  return new ApiError(payload.error?.message || fallback, payload.error?.code || code, status)
}

export async function request(path, options) {
  const response = await apiFetch(path, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw errorFromPayload(payload, response.status)
  return payload
}

function parseSseFrame(frame) {
  let eventType = 'message'
  const dataLines = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) eventType = line.slice(6).trim()
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (dataLines.length === 0) return null
  return { eventType, payload: JSON.parse(dataLines.join('\n')) }
}

export async function streamRequest(path, options, onEvent) {
  const response = await apiFetch(path, options, 'text/event-stream')
  if (!response.ok) {
    throw errorFromPayload(await response.json().catch(() => ({})), response.status)
  }
  if (!response.body) {
    throw new ApiError('스트리밍 응답을 읽을 수 없습니다.', 'STREAM_UNAVAILABLE', 500)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completion = null

  function handleFrame(frame) {
    const event = parseSseFrame(frame)
    if (!event) return
    if (event.eventType === 'error') {
      throw errorFromPayload(
        event.payload,
        event.payload.status || 500,
        '응답 생성 중 오류가 발생했습니다.',
        'STREAM_FAILED',
      )
    }
    onEvent?.(event.eventType, event.payload)
    if (event.eventType === 'message_done') completion = event.payload
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        if (frame.trim()) handleFrame(frame)
        // The server has already saved the answer before sending message_done.
        // Transport EOF must not keep the composer locked after that event.
        if (completion) return completion
        boundary = buffer.indexOf('\n\n')
      }
      if (done) break
    }
    if (buffer.trim()) handleFrame(buffer)
    if (!completion) {
      throw new ApiError('스트리밍 응답이 완료되기 전에 연결이 종료되었습니다.', 'STREAM_INCOMPLETE', 502)
    }
    return completion
  } finally {
    // Cleanup can also wait on the transport, so do not await cancellation.
    void reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
