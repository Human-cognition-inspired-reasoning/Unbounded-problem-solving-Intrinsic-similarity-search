import { useRef, useState } from 'react'
import { useAttachmentDraft } from '../files/useAttachmentDraft.js'
import { AttachmentPicker } from '../files/AttachmentPicker.jsx'
import { ModelSettingsMenu } from '../models/ModelSettingsMenu.jsx'
import { formatMegabytes } from '../shared/format.js'

export function ChatComposer({
  onSend,
  disabled,
  inputDisabled = disabled,
  models,
  selectedModelId,
  onSelectModel,
  reasoningEffort,
  onSelectReasoning,
  thinkingEnabled,
  onToggleThinking,
  webSearchEnabled,
  onToggleSearch,
}) {
  const [text, setText] = useState('')
  const {
    items, fileConfig, fileError, setFileError,
    chooseFiles, removeItem, prepareAttachments, clearAttachments,
  } = useAttachmentDraft()
  const [busy, setBusy] = useState(false)
  const fileInputRef = useRef(null)

  const [isToolMenuOpen, setIsToolMenuOpen] = useState(false)
  const textareaRef = useRef(null)
  const isSubmitPendingRef = useRef(false)
  const selectedModel = models.find((model) => model.id === selectedModelId)
  const supportsForcedWebSearch = Boolean(selectedModel?.supportsForcedWebSearch)
  const forcedWebSearchEnabled = webSearchEnabled && supportsForcedWebSearch

  async function submit() {
    const trimmed = text.trim()
    if ((!trimmed && !items.length) || disabled || inputDisabled || isSubmitPendingRef.current) return
    isSubmitPendingRef.current = true
    setBusy(true)
    setFileError('')
    try {
      const attachments = await prepareAttachments()
      const accepted = await onSend(trimmed, attachments)
      if (accepted) {
        setText('')
        clearAttachments()
        if (textareaRef.current) {
          textareaRef.current.style.height = 'auto'
          textareaRef.current.style.overflowY = 'hidden'
        }
      }
    } catch (error) {
      setFileError(error.message || '파일을 준비하지 못했습니다. 다시 전송해 주세요.')
    } finally {
      setBusy(false)
      isSubmitPendingRef.current = false
    }
  }

  function handleTextChange(event) {
    const textarea = event.currentTarget
    setText(textarea.value)
    textarea.style.height = 'auto'
    const maxHeight = 180
    textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden'
  }

  function handleKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer" aria-busy={busy}>
        <input ref={fileInputRef} type="file" hidden multiple accept={fileConfig?.extensions.join(',')} onChange={(event) => {
          chooseFiles(Array.from(event.target.files || []))
          event.target.value = ''
          setIsToolMenuOpen(false)
        }} disabled={busy || disabled} />
        <AttachmentPicker items={items} busy={busy} onRemove={removeItem} />
        {fileError && <p className="attachment-error" role="alert">{fileError}</p>}
        {busy && <p className="attachment-progress" role="status">{items.length ? '파일 준비 및 전송 중…' : '전송 중…'}</p>}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
          placeholder={disabled && !inputDisabled
            ? '응답이 끝난 후 보낼 메시지를 입력하세요…'
            : '메시지를 입력하세요…'}
          disabled={inputDisabled || busy}
          rows={1}
        />
        <div className="composer-footer">
          <div className="tool-control">
            <button
              className={`add-tool-button ${forcedWebSearchEnabled ? 'active' : ''}`}
              type="button"
              onClick={() => setIsToolMenuOpen((open) => !open)}
              aria-expanded={isToolMenuOpen}
              aria-haspopup="menu"
              aria-label={forcedWebSearchEnabled ? '도구 추가, 이번 요청은 웹 검색 필수' : '도구 추가, 웹 검색 자동'}
              disabled={disabled || busy}
            >
              +
            </button>
            {isToolMenuOpen && (
              <div className="tool-menu" role="menu">
                {fileConfig?.enabled && <button type="button" role="menuitem" onClick={() => fileInputRef.current?.click()}>
                  <span className="tool-menu-icon" aria-hidden="true">📎</span>
                  <span><strong>파일 첨부</strong><small>PDF, 문서, 이미지, 표 · 최대 {formatMegabytes(fileConfig.maxFileBytes)}</small></span>
                </button>}
                <button
                  type="button"
                  role="menuitemcheckbox"
                  aria-checked={forcedWebSearchEnabled}
                  disabled={!supportsForcedWebSearch}
                  onClick={() => {
                    onToggleSearch(!forcedWebSearchEnabled)
                    setIsToolMenuOpen(false)
                  }}
                >
                  <span className="tool-menu-icon" aria-hidden="true">⌕</span>
                  <span>
                    <strong>{supportsForcedWebSearch ? '웹 검색 필수' : '웹 검색 자동'}</strong>
                    <small>
                      {!supportsForcedWebSearch
                        ? '이 모델은 필수 검색 모드를 지원하지 않습니다'
                        : '이번 요청에서는 반드시 검색'}
                    </small>
                  </span>
                  <span aria-hidden="true">{forcedWebSearchEnabled ? '✓' : ''}</span>
                </button>
              </div>
            )}
          </div>

          <div className="composer-right-controls">
            <ModelSettingsMenu
              models={models}
              selectedModelId={selectedModelId}
              reasoningEffort={reasoningEffort}
              onSelectModel={onSelectModel}
              onSelectReasoning={onSelectReasoning}
              thinkingEnabled={thinkingEnabled}
              onToggleThinking={onToggleThinking}
              disabled={disabled || busy}
            />
            <button className="send-button" type="button" onClick={submit} disabled={disabled || inputDisabled || busy || (!text.trim() && !items.length)} aria-label="메시지 전송">
              ↑
            </button>
          </div>
        </div>
      </div>
      <p>대화 기록은 연구에 쓰여요. 실제 연구 아이디어를 고민하듯 성실히 참여해 주세요.</p>
    </div>
  )
}
