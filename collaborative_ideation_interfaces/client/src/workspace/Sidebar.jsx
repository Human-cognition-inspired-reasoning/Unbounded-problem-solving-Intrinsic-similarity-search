import { useState } from 'react'
import { previewTitle } from '../chat/titles.js'
import { ContactMenu } from './ContactMenu.jsx'
import { SurveyDialog } from './SurveyDialog.jsx'

export function Sidebar({
  conversations,
  activeId,
  usage,
  onCreate,
  onSelect,
  onLogout,
  isCreating,
}) {
  const [surveyDialogOpen, setSurveyDialogOpen] = useState(false)
  const usedPercent = usage.limitUsd > 0 ? Math.min(100, (usage.usedUsd / usage.limitUsd) * 100) : 0

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span>🔬 ResearChat</span>
      </div>

      <section className="usage-card" aria-label="AI 사용 한도">
        <div className="usage-row">
          <span>AI 사용 한도</span>
          <strong>${usage.remainingUsd.toFixed(2)} 남음</strong>
        </div>
        <div className="progress-track" aria-hidden="true">
          <span style={{ width: `${usedPercent}%` }} />
        </div>
        <p>${usage.usedUsd.toFixed(4)} 사용 / 총 ${usage.limitUsd.toFixed(2)}</p>
      </section>

      <button className="new-chat-button" type="button" onClick={onCreate} disabled={isCreating}>
        <span aria-hidden="true">＋</span> {isCreating ? '만드는 중…' : '새 대화'}
      </button>

      <nav className="conversation-nav" aria-label="대화 기록">
        <p className="nav-label">대화 기록</p>
        {conversations.length === 0 ? (
          <p className="empty-list">아직 대화가 없습니다.</p>
        ) : (
          <ul>
            {conversations.map((conversation) => (
              <li key={conversation.id} className={conversation.id === activeId ? 'active' : ''}>
                <button className="conversation-select" type="button" onClick={() => onSelect(conversation.id)}>
                  <span title={conversation.title}>{previewTitle(conversation.title)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </nav>

      <section className="survey-card" aria-label="사용 후 설문">
        <div className="survey-card-copy">
          <strong>작업을 마쳤나요?</strong>
          <p>마지막으로 제안서 제출 및 설문 작성을 해 주세요.</p>
        </div>
        <button className="survey-open-button" type="button" onClick={() => setSurveyDialogOpen(true)}>
          <span className="survey-open-label">연구 아이디어 제안서 제출</span>
          <span aria-hidden="true">↗</span>
        </button>
      </section>

      <div className="sidebar-footer">
        <ContactMenu />
        <button className="logout-button sidebar-footer-button" type="button" onClick={onLogout}>로그아웃</button>
      </div>

      <SurveyDialog open={surveyDialogOpen} onClose={() => setSurveyDialogOpen(false)} />
    </aside>
  )
}
