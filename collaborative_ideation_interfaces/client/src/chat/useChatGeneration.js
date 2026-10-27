import { useRef, useState } from 'react'
import { chatApi } from '../api/chatApi.js'
import { applyStreamingMessageEvent } from './streamingState.js'
import { DEFAULT_CONVERSATION_TITLE, previewTitle } from './titles.js'
import { messageCountOf, readStableSnapshot } from '../workspace/workspaceSync.js'

export function useChatGeneration({
  activeConversation, isCreating, settings, maxConcurrentGenerations,
  setIsCreating, setConversations, setActiveId, setUsage, setError, setNotice,
  onSessionExpired, publishWorkspaceChange,
}) {
  const [generationRequests, setGenerationRequests] = useState({})
  const generationRequestsRef = useRef({})
  const generationRevisionRef = useRef(0)

  function replaceGenerationRequests(next) {
    generationRequestsRef.current = next
    setGenerationRequests(next)
  }

  function updateGenerationRequest(requestId, updater) {
    const currentRequest = generationRequestsRef.current[requestId]
    if (!currentRequest) return
    replaceGenerationRequests({
      ...generationRequestsRef.current,
      [requestId]: updater(currentRequest),
    })
  }

  function removeGenerationRequest(requestId) {
    if (!generationRequestsRef.current[requestId]) return
    const next = { ...generationRequestsRef.current }
    delete next[requestId]
    replaceGenerationRequests(next)
  }

  function conversationHasActiveRequest(conversationId) {
    return Object.values(generationRequestsRef.current).some((request) => (
      request.conversationId === conversationId
    ))
  }

  function handleSend(content, attachments = []) {
    const initialConversation = activeConversation
    const initialConversationId = initialConversation?.id || null
    const activeRequests = Object.values(generationRequestsRef.current)
    if (isCreating || activeRequests.length >= maxConcurrentGenerations) {
      setError(`동시에 생성할 수 있는 응답은 최대 ${maxConcurrentGenerations}개입니다.`)
      return false
    }
    if (conversationHasActiveRequest(initialConversationId)) {
      setError('이 대화에서는 이미 응답을 생성하고 있습니다. 다른 대화에서 새 메시지를 보낼 수 있습니다.')
      return false
    }

    setError('')
    setNotice('')
    let targetConversation = initialConversation
    const firstMessageTitle = previewTitle(content || attachments[0]?.name || '첨부파일')
    const clientRequestId = crypto.randomUUID()
    // Later changes in the composer must not alter a request already sent.
    const requestSettings = { ...settings }
    const pendingMessage = {
      id: `pending-${crypto.randomUUID()}`,
      role: 'user',
      content,
      createdAt: new Date().toISOString(),
      pending: true,
      attachments,
    }

    replaceGenerationRequests({
      ...generationRequestsRef.current,
      [clientRequestId]: {
        conversationId: initialConversationId,
        pendingMessage,
        streamingMessage: null,
      },
    })

    if (
      targetConversation?.title === DEFAULT_CONVERSATION_TITLE
      && messageCountOf(targetConversation) === 0
    ) {
      targetConversation = { ...targetConversation, title: firstMessageTitle }
      setConversations((current) => current.map((row) => (
        row.id === targetConversation.id ? targetConversation : row
      )))
    }

    function updateStreamingMessage(updater) {
      updateGenerationRequest(clientRequestId, (request) => {
        if (!request.streamingMessage) return request
        return { ...request, streamingMessage: updater(request.streamingMessage) }
      })
    }

    let acknowledge
    const accepted = new Promise((resolve) => { acknowledge = resolve })

    async function runGeneration() {
      try {
        if (!targetConversation) {
          setIsCreating(true)
          try {
            const created = await chatApi.createConversation(requestSettings)
            targetConversation = { ...created.conversation, title: firstMessageTitle }
            setConversations((current) => [targetConversation, ...current])
            setActiveId(created.conversation.id)
            publishWorkspaceChange('conversation_created', created.conversation.id)
            updateGenerationRequest(clientRequestId, (current) => ({
              ...current,
              conversationId: created.conversation.id,
            }))
          } finally {
            setIsCreating(false)
          }
        }

        function handleStreamEvent(eventType, payload) {
          if (eventType === 'message_start') {
            acknowledge(true)
            publishWorkspaceChange('generation_started', targetConversation.id)
            setConversations((current) => current.map((row) => {
              if (row.id !== targetConversation.id) return row
              const loaded = row.messages || []
              const hasUserMessage = loaded.some((message) => (
                message.id === payload.userMessage.id
              ))
              return {
                ...row,
                messages: hasUserMessage ? loaded : [...loaded, payload.userMessage],
              }
            }))
            updateGenerationRequest(clientRequestId, (current) => ({
              ...current,
              pendingMessage: null,
              streamingMessage: payload.assistantMessage,
            }))
            return
          }
          updateStreamingMessage((message) => (
            applyStreamingMessageEvent(message, eventType, payload)
          ))
        }

        const data = await chatApi.streamMessage({
          conversationId: targetConversation.id,
          content,
          attachmentIds: attachments.map((file) => file.id),
          settings: requestSettings,
          clientRequestId,
        }, handleStreamEvent)
        acknowledge(true)
        setUsage((current) => data.usage.usedUsd >= current.usedUsd ? data.usage : current)
        setConversations((current) => current.map((row) => {
          if (row.id !== targetConversation.id) return row
          return {
            ...row,
            title: row.title === DEFAULT_CONVERSATION_TITLE ? firstMessageTitle : row.title,
            isGenerating: false,
            modelId: requestSettings.modelId,
            reasoningEffort: requestSettings.reasoningEffort,
            thinkingEnabled: requestSettings.thinkingEnabled,
            messages: [
              ...(row.messages || []).filter((message) => (
                message.id !== data.userMessage.id && message.id !== data.assistantMessage.id
              )),
              data.userMessage,
              data.assistantMessage,
            ],
          }
        }))
        publishWorkspaceChange('generation_completed', targetConversation.id)
      } catch (requestError) {
        setError(requestError.message)
        if (requestError.status === 401) {
          onSessionExpired()
        } else {
          try {
            const [usageData, conversationData] = await Promise.all([
              chatApi.usage(),
              chatApi.conversations(),
            ])
            setUsage(usageData)
            if (targetConversation) {
              const refreshed = conversationData.conversations.find((row) => (
                row.id === targetConversation.id
              ))
              if (refreshed) {
                setConversations((current) => current.map((row) => (
                  row.id === refreshed.id ? refreshed : row
                )))
              }
            }
          } catch {
            // Keep the original provider or storage error visible.
          }
        }
        if (targetConversation) {
          publishWorkspaceChange('generation_completed', targetConversation.id)
        }
      } finally {
        acknowledge(false)
        generationRevisionRef.current += 1
        removeGenerationRequest(clientRequestId)
      }
    }

    void runGeneration()
    return accepted
  }

  return {
    generationRequests,
    handleSend,
    conversationHasActiveRequest,
    resetGenerations: () => replaceGenerationRequests({}),
    readGenerationSnapshot: (fetchSnapshot) => readStableSnapshot(
      fetchSnapshot, () => generationRevisionRef.current,
    ),
  }
}
