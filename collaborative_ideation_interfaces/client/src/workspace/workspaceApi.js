import { chatApi } from '../api/chatApi.js'

export async function fetchWorkspace() {
  const [modelData, usageData, conversationData] = await Promise.all([
    chatApi.models(),
    chatApi.usage(),
    chatApi.conversations(),
  ])
  // 목록은 메시지 본문 없이 온다. 바로 열리는 첫 대화만 본문을 함께 받아서
  // 빈 화면이 잠깐 스치는 걸 막고, 나머지는 열어볼 때 가져온다.
  const conversations = [...conversationData.conversations]
  if (conversations[0]) {
    const { messages } = await chatApi.conversationMessages(conversations[0].id)
    conversations[0] = { ...conversations[0], messages }
  }
  return {
    models: modelData.models,
    initialSettings: modelData.initialSettings,
    maxConcurrentGenerations: modelData.maxConcurrentGenerations,
    usage: usageData,
    conversations,
  }
}

export async function fetchWorkspaceSnapshot(activeConversationId) {
  const [usageData, conversationData] = await Promise.all([
    chatApi.usage(),
    chatApi.conversations(),
  ])
  const activeExists = conversationData.conversations.some((row) => (
    row.id === activeConversationId
  ))
  const activeMessages = activeExists
    ? (await chatApi.conversationMessages(activeConversationId)).messages
    : null
  return { usage: usageData, conversations: conversationData.conversations, activeMessages }
}
