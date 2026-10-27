import { useEffect, useRef } from 'react'

const SURVEY_FORM_URL = 'https://docs.google.com/forms/d/e/1FAIpQLScZ9RiZZKgAzXnO28V7fe1vwI6I3eSE8a8pDQS0clLuKWn74A/viewform?usp=header'

// Update these values when the survey 안내 문구 needs to change.
const DIALOG_TITLE = '사용을 마치고 제안서 제출 및 설문 작성으로 이동할까요?'
const DIALOG_BODY = '연구 아이디어 탐색과 대화를 모두 마쳤는지 확인해 주세요. 설문은 새 탭에서 열립니다.'

export function SurveyDialog({ open, onClose }) {
  const dialogRef = useRef(null)

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return

    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  function handleBackdropClick(event) {
    if (event.target === event.currentTarget) onClose()
  }

  function handleCancel(event) {
    event.preventDefault()
    onClose()
  }

  return (
    <dialog
      ref={dialogRef}
      className="survey-dialog"
      aria-labelledby="survey-dialog-title"
      aria-describedby="survey-dialog-body"
      onCancel={handleCancel}
      onMouseDown={handleBackdropClick}
    >
      <div className="survey-dialog-card">
        <div className="survey-dialog-icon" aria-hidden="true">✓</div>
        <h2 id="survey-dialog-title">{DIALOG_TITLE}</h2>
        <p id="survey-dialog-body">{DIALOG_BODY}</p>

        <div className="survey-dialog-actions">
          <button className="survey-dialog-cancel" type="button" onClick={onClose}>
            계속 사용하기
          </button>
          <a
            className="survey-dialog-open"
            href={SURVEY_FORM_URL}
            target="_blank"
            rel="noreferrer"
            onClick={onClose}
          >
            설문 작성하기 <span aria-hidden="true">↗</span>
          </a>
        </div>
      </div>
    </dialog>
  )
}
