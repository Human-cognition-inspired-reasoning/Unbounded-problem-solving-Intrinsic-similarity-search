import { formatKilobytes } from '../shared/format.js'

export function AttachmentList({ attachments = [] }) {
  if (!attachments.length) return null
  return (
    <ul className="message-attachments" aria-label="첨부파일">
      {attachments.map((file) => (
        <li key={file.id} className="message-attachment">
          <span className="attachment-name">📎 {file.name}</span>
          <small>{formatKilobytes(file.size)}</small>
        </li>
      ))}
    </ul>
  )
}
