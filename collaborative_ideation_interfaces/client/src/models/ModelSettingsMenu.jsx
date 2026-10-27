import { useRef, useState } from 'react'
import { useDismiss } from '../shared/useDismiss.js'

const EFFORT_LABELS = {
  none: 'Instant',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra High',
  max: 'Max',
}

function effortLabel(effort) {
  return EFFORT_LABELS[effort] || effort
}

function shortModelLabel(model) {
  if (!model) return 'Model'
  if (model.provider === 'openai') return model.label.replace(/^GPT-/, '')
  if (model.provider === 'anthropic') return model.label.replace(/^Claude\s+/, '')
  if (model.provider === 'google') return model.label.replace(/^Gemini\s+/, '')
  return model.label
}

export function ModelSettingsMenu({
  models,
  selectedModelId,
  reasoningEffort,
  onSelectModel,
  onSelectReasoning,
  thinkingEnabled,
  onToggleThinking,
  disabled,
}) {
  const [isOpen, setIsOpen] = useState(false)
  const [isSubmenuOpen, setIsSubmenuOpen] = useState(false)
  const rootRef = useRef(null)
  const selectedModel = models.find((model) => model.id === selectedModelId)
  const availableModels = models.filter((model) => (
    model.provider === selectedModel?.provider
    && (model.selectable !== false || model.id === selectedModelId)
  ))
  const provider = selectedModel?.provider || 'openai'
  const showsReasoningInTrigger = provider !== 'google'
    && selectedModel?.reasoningParameter !== 'none'
  const triggerLabel = !showsReasoningInTrigger
    ? shortModelLabel(selectedModel)
    : `${shortModelLabel(selectedModel)} ${effortLabel(reasoningEffort)}`

  function close() {
    setIsOpen(false)
    setIsSubmenuOpen(false)
  }

  useDismiss(rootRef, isOpen, close)

  function chooseReasoning(effort) {
    onSelectReasoning(effort)
    close()
  }

  function chooseModel(modelId) {
    onSelectModel(modelId)
    close()
  }

  function renderModelChoices() {
    return (
      <div className="model-choice-list">
        {availableModels.map((model) => (
          <button
            key={model.id}
            type="button"
            className={model.id === selectedModelId ? 'selected' : ''}
            role="menuitemradio"
            aria-checked={model.id === selectedModelId}
            onClick={() => chooseModel(model.id)}
          >
            <span className="menu-check" aria-hidden="true">{model.id === selectedModelId ? '✓' : ''}</span>
            <span>
              <strong>{shortModelLabel(model)}</strong>
              <small>{model.description || model.providerLabel}</small>
            </span>
          </button>
        ))}
      </div>
    )
  }

  function renderReasoningChoices(compact = false) {
    return (
      <div className={compact ? 'reasoning-chip-list' : 'reasoning-choice-list'}>
        {(selectedModel?.reasoningOptions || []).map((effort) => (
          <button
            key={effort}
            type="button"
            className={effort === reasoningEffort ? 'selected' : ''}
            role="menuitemradio"
            aria-checked={effort === reasoningEffort}
            onClick={() => chooseReasoning(effort)}
          >
            <span>{effortLabel(effort)}</span>
            {!compact && <span className="menu-check" aria-hidden="true">{effort === reasoningEffort ? '✓' : ''}</span>}
          </button>
        ))}
      </div>
    )
  }

  function renderOpenAISettings() {
    return (
      <>
        <p className="menu-section-title">Intelligence</p>
        {renderReasoningChoices()}
        <div className="menu-divider" />
        <button
          type="button"
          className={`model-menu-opener ${isSubmenuOpen ? 'active' : ''}`}
          role="menuitem"
          aria-haspopup="menu"
          aria-expanded={isSubmenuOpen}
          onClick={() => setIsSubmenuOpen((open) => !open)}
        >
          <span>{selectedModel?.label || 'Model'}</span>
          <span aria-hidden="true">›</span>
        </button>
        {isSubmenuOpen && (
          <div className="model-submenu" role="menu" aria-label="모델 선택">
            <p className="menu-section-title">Model</p>
            {renderModelChoices()}
          </div>
        )}
      </>
    )
  }

  function renderClaudeSettings() {
    // Effort와 Thinking은 겹치는 다이얼이라 한 모델에 둘 다 두지 않는다.
    // effort가 있으면 그것으로 깊이를 조절하고 thinking은 항상 켜져 있으며,
    // effort가 없는 모델(Haiku)만 토글을 노출한다.
    const supportsEffort = selectedModel?.reasoningParameter !== 'none'
    const supportsThinking = Boolean(selectedModel?.supportsThinkingToggle)

    return (
      <>
        <p className="menu-section-title">Model</p>
        {renderModelChoices()}
        {(supportsEffort || supportsThinking) && <div className="menu-divider" />}
        {supportsEffort && (
          <button
            type="button"
            className={`model-menu-opener ${isSubmenuOpen ? 'active' : ''}`}
            role="menuitem"
            aria-haspopup="menu"
            aria-expanded={isSubmenuOpen}
            onClick={() => setIsSubmenuOpen((open) => !open)}
          >
            <span><strong>Effort</strong><small>{effortLabel(reasoningEffort)}</small></span>
            <span aria-hidden="true">›</span>
          </button>
        )}
        {supportsThinking && (
          <button
            type="button"
            className="thinking-toggle-row"
            role="menuitemcheckbox"
            aria-checked={thinkingEnabled}
            onClick={() => onToggleThinking(!thinkingEnabled)}
          >
            <span><strong>Thinking</strong><small>Show extended thinking</small></span>
            <span className={`switch-control ${thinkingEnabled ? 'enabled' : ''}`} aria-hidden="true"><span /></span>
          </button>
        )}
        {supportsEffort && isSubmenuOpen && (
          <div className="model-submenu" role="menu" aria-label="Claude Effort 설정">
            <p className="menu-section-title">Effort</p>
            {renderReasoningChoices()}
          </div>
        )}
      </>
    )
  }

  function renderGeminiSettings() {
    return (
      <>
        <p className="menu-section-title">Model</p>
        {renderModelChoices()}
        <div className="menu-divider" />
        <p className="menu-section-title">Thinking level</p>
        {renderReasoningChoices(true)}
      </>
    )
  }

  return (
    <div className="model-settings" ref={rootRef}>
      <button
        type="button"
        className="model-settings-trigger"
        onClick={() => {
          setIsOpen((open) => !open)
          setIsSubmenuOpen(false)
        }}
        aria-expanded={isOpen}
        aria-haspopup="menu"
        aria-label={`모델 및 추론 설정, 현재 ${triggerLabel}`}
        disabled={disabled}
      >
        <span>{triggerLabel}</span>
        <span className="menu-chevron" aria-hidden="true">⌄</span>
      </button>

      {isOpen && (
        <div className={`model-settings-menu model-settings-${provider}`} role="menu" aria-label="모델 및 추론 설정">
          {provider === 'openai' && renderOpenAISettings()}
          {provider === 'anthropic' && renderClaudeSettings()}
          {provider === 'google' && renderGeminiSettings()}
        </div>
      )}
    </div>
  )
}
