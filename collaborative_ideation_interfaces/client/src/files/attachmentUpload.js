import { chatApi } from '../api/chatApi.js'

// Persist each completed stage in the draft so retries keep the same original.
export async function prepareAttachment(item, patch, api = chatApi) {
  let ticket
  let attachment = item.attachment
  if (!attachment) {
    patch({ progress: '업로드 중…' })
    const result = await api.initAttachment(item.file)
    attachment = result.attachment
    patch({ attachment })
    ticket = result.upload
  }
  if (!item.uploaded && attachment.status === 'pending') {
    ticket ||= (await api.renewAttachmentUpload(attachment.id)).upload
    await api.uploadAttachment(ticket, item.file)
    patch({ uploaded: true })
  }
  if (attachment.status !== 'ready') {
    patch({ progress: '파일 준비 중…' })
    const result = await api.completeAttachment(attachment.id)
    attachment = result.attachment
    patch({ attachment, progress: '준비 완료' })
  }
  return attachment
}
