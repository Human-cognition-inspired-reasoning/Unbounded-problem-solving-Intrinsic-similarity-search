import { ApiError, request, streamRequest } from './http.js'

export { ApiError, isSessionExpired } from './http.js'

// Identifies this browser tab on each model call.
const CLIENT_INSTANCE_ID = crypto.randomUUID()

export const chatApi = {
  health: () => request('/api/health'),
  me: () => request('/api/auth/me'),
  login: (studyId, password) =>
    request('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ studyId, password }),
    }),
  logout: () => request('/api/auth/logout', { method: 'POST' }),
  models: () => request('/api/models'),
  usage: () => request('/api/usage'),
  discardAttachment: (id) => request(`/api/attachments/${id}`, { method: 'DELETE' }),
  attachmentConfig: () => request('/api/attachments/config'),
  initAttachment: (file) => request('/api/attachments', {
    method: 'POST', body: JSON.stringify({ name: file.name, size: file.size }),
  }),
  uploadAttachment: async (ticket, file) => {
    const response = await fetch(ticket.url, {
      method: ticket.method, body: file, headers: ticket.headers || {},
      credentials: 'same-origin',
    })
    if (!response.ok) throw new ApiError('파일 업로드에 실패했습니다. 다시 시도해 주세요.', 'UPLOAD_FAILED', response.status)
  },
  renewAttachmentUpload: (id) => request(`/api/attachments/${id}/upload-ticket`, { method: 'POST' }),
  completeAttachment: (id) => request(`/api/attachments/${id}/complete`, {
    method: 'POST', body: JSON.stringify({}),
  }),
  conversations: () => request('/api/conversations'),
  conversationMessages: (conversationId) =>
    request(`/api/conversations/${conversationId}/messages`),
  createConversation: ({ modelId, reasoningEffort, thinkingEnabled }) =>
    request('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({ modelId, reasoningEffort, thinkingEnabled }),
    }),
  updateConversation: (conversationId, changes) =>
    request(`/api/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }),
  streamMessage: ({
    conversationId,
    content,
    attachmentIds = [],
    settings,
    clientRequestId,
  }, onEvent) => streamRequest(`/api/conversations/${conversationId}/messages`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      attachmentIds,
      modelId: settings.modelId,
      reasoningEffort: settings.reasoningEffort,
      thinkingEnabled: settings.thinkingEnabled,
      webSearchEnabled: settings.webSearchEnabled,
      webSearchMode: settings.webSearchEnabled ? 'required' : 'auto',
      clientRequestId,
      clientInstanceId: CLIENT_INSTANCE_ID,
    }),
  }, onEvent),
}
