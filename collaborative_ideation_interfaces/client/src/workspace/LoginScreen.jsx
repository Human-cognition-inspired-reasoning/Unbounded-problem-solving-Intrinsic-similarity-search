import { useState } from 'react'
import { CONTACT_EMAIL } from '../shared/contact.js'

export function LoginScreen({ onLogin, error, isSubmitting }) {
  const [studyId, setStudyId] = useState('')
  const [password, setPassword] = useState('')

  function handleSubmit(event) {
    event.preventDefault()
    onLogin(studyId.trim(), password)
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <p className="eyebrow">AI × Research Idea Challenge</p>
        <h1 id="login-title">연세대학교 AI디어 공모전</h1>
        <p className="login-description">AI와 함께 새로운 연구 아이디어를 탐색하고 발전시켜보세요.</p>

        <form onSubmit={handleSubmit} className="login-form">
          <label>
            ID
            <input
              autoComplete="username"
              value={studyId}
              onChange={(event) => setStudyId(event.target.value)}
              required
            />
          </label>
          <label>
            비밀번호
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button login-button" type="submit" disabled={isSubmitting}>
            {isSubmitting ? '로그인 중…' : '로그인'}
          </button>
        </form>

        <p className="privacy-note">개인정보, 미공개 연구자료 또는 기관의 기밀정보를 입력하지 마세요.</p>
        <p className="contact-note">
          문제가 있으면 <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>로 연락해 주세요.
        </p>
      </section>
    </main>
  )
}
