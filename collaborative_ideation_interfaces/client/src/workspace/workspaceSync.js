export const WORKSPACE_SYNC_CHANNEL = 'co-scientist:workspace:v1'

const WORKSPACE_INVALIDATED = 'workspace_invalidated'

export function createWorkspaceChannel(onInvalidate, BroadcastChannelClass = globalThis.BroadcastChannel) {
  if (typeof BroadcastChannelClass !== 'function') return null

  const channel = new BroadcastChannelClass(WORKSPACE_SYNC_CHANNEL)
  const handleMessage = (event) => {
    const message = event?.data
    if (!message || message.type !== WORKSPACE_INVALIDATED) return
    onInvalidate(message)
  }
  channel.addEventListener('message', handleMessage)

  return {
    publish(reason, conversationId = null) {
      channel.postMessage({
        type: WORKSPACE_INVALIDATED,
        reason,
        conversationId,
        sentAt: new Date().toISOString(),
      })
    },
    close() {
      channel.removeEventListener('message', handleMessage)
      channel.close()
    },
  }
}

export function mergeSyncedConversations(current, incoming, activeId, activeMessages) {
  const currentById = new Map(current.map((conversation) => [conversation.id, conversation]))

  return incoming.map((conversation) => {
    const previous = currentById.get(conversation.id)
    if (conversation.id === activeId && activeMessages) {
      return { ...conversation, messages: activeMessages }
    }
    if (
      previous?.messages
      && previous.messageCount === conversation.messageCount
      && previous.updatedAt === conversation.updatedAt
    ) {
      return { ...conversation, messages: previous.messages }
    }
    return conversation
  })
}

export function createRefreshScheduler(refresh, delayMs = 120) {
  let timer = null
  let running = false
  let queued = false
  let closed = false

  async function run() {
    timer = null
    if (closed) return
    if (running) {
      queued = true
      return
    }

    running = true
    try {
      await refresh()
    } finally {
      running = false
      if (queued && !closed) {
        queued = false
        timer = setTimeout(run, delayMs)
      }
    }
  }

  return {
    schedule({ immediate = false } = {}) {
      if (closed) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(run, immediate ? 0 : delayMs)
    },
    close() {
      closed = true
      if (timer) clearTimeout(timer)
      timer = null
    },
  }
}

// A missing body is lazy-loaded; an empty array is an already loaded conversation.
export function messageCountOf(conversation) {
  if (!conversation) return 0
  return conversation.messages ? conversation.messages.length : (conversation.messageCount || 0)
}

export async function readStableSnapshot(fetchSnapshot, getGenerationRevision) {
  let snapshot
  let revision
  do {
    revision = getGenerationRevision()
    snapshot = await fetchSnapshot()
    // Completion invalidates an in-flight focus refresh's old busy flag and body.
  } while (revision !== getGenerationRevision())
  return snapshot
}
