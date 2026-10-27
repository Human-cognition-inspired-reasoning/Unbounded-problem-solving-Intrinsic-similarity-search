// Combine saved messages with this tab's in-flight requests for the open conversation.
export function generationView({ generationRequests, conversations, activeConversation, limit }) {
  const requests = Object.values(generationRequests)
  const activeId = activeConversation?.id || null
  const activeGeneration = requests.find((request) => request.conversationId === activeId) || null

  // Other tabs report their work through isGenerating; this tab's requests count too.
  const generatingIds = new Set(
    conversations.filter((row) => row.isGenerating).map((row) => row.id),
  )
  for (const request of requests) {
    if (request.conversationId) generatingIds.add(request.conversationId)
  }

  const visibleMessages = [...(activeConversation?.messages || [])]
  if (activeGeneration?.pendingMessage) visibleMessages.push(activeGeneration.pendingMessage)
  if (activeGeneration?.streamingMessage) visibleMessages.push(activeGeneration.streamingMessage)

  return {
    isGenerating: Boolean(activeGeneration || activeConversation?.isGenerating),
    limitReached: generatingIds.size >= limit,
    visibleMessages,
  }
}
