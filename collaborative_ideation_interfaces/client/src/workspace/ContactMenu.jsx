import { useRef, useState } from 'react'
import { CONTACT_EMAIL } from '../shared/contact.js'
import { useDismiss } from '../shared/useDismiss.js'

export function ContactMenu() {
  const [isOpen, setIsOpen] = useState(false)
  const [copyState, setCopyState] = useState('idle')
  const rootRef = useRef(null)

  useDismiss(rootRef, isOpen, () => setIsOpen(false))

  // 메일 앱이 연결되지 않은 PC가 많아서 mailto 링크만으로는 부족하다.
  async function copyEmail() {
    try {
      await navigator.clipboard.writeText(CONTACT_EMAIL)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  function toggle() {
    setCopyState('idle')
    setIsOpen((open) => !open)
  }

  return (
    <div className="contact-menu" ref={rootRef}>
      <button
        className="sidebar-footer-button"
        type="button"
        onClick={toggle}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
      >
        <span aria-hidden="true">✉</span> 문의
      </button>
      {isOpen && (
        <div className="contact-popover" role="dialog" aria-label="문의 연락처">
          <p>문제가 있으면 아래 주소로 연락해 주세요.</p>
          <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>
          <button type="button" onClick={copyEmail}>
            {copyState === 'copied' ? '복사됨' : copyState === 'failed' ? '복사하지 못했어요' : '주소 복사'}
          </button>
        </div>
      )}
    </div>
  )
}
