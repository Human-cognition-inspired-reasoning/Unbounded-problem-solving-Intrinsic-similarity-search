// Same rule as the server (api/routes.py, chat/generation.py).
export const DEFAULT_CONVERSATION_TITLE = '새 대화'
const TITLE_PREVIEW_LENGTH = 20

export function previewTitle(text) {
  return text.length > TITLE_PREVIEW_LENGTH ? `${text.slice(0, TITLE_PREVIEW_LENGTH)}…` : text
}
