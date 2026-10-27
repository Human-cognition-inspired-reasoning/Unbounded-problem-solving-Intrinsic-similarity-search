import { formatKilobytes } from '../shared/format.js'

export function AttachmentPicker({ items, busy, onRemove }) {
  if (!items.length) return null
  return (
    <div className="draft-attachments" aria-label="전송할 첨부파일">
      {items.map((item) => (
        <div className="draft-attachment" key={item.key}>
          <div className="attachment-heading">
            {item.preview
              ? <img src={item.preview} alt="선택한 이미지 미리보기" />
              : <span aria-hidden="true">📎</span>}
            <div>
              <strong>{item.file.name}</strong>
              <small>{formatKilobytes(item.file.size)} · {item.progress || '전송 대기'}</small>
            </div>
            <button
              type="button"
              onClick={() => onRemove(item)}
              disabled={busy}
              aria-label={`${item.file.name} 제거`}
            >
              ×
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
