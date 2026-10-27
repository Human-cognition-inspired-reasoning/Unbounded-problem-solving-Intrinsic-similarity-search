const FAMILY_LABELS = {
  openai: 'GPT',
  anthropic: 'Claude',
  google: 'Gemini',
}

export function ModelBar({ models, selectedModelId, onSelectProvider, theme, onToggleTheme, disabled }) {
  const selectedModel = models.find((model) => model.id === selectedModelId)
  const providers = models.reduce((items, model) => {
    if (!items.some((item) => item.id === model.provider)) {
      items.push({ id: model.provider, label: FAMILY_LABELS[model.provider] || model.providerLabel })
    }
    return items
  }, [])

  return (
    <header className="model-bar">
      <div className="provider-tabs" role="group" aria-label="모델 계열 선택">
        {providers.map((provider) => (
          <button
            key={provider.id}
            type="button"
            aria-pressed={selectedModel?.provider === provider.id}
            title={`${provider.label} 계열 선택${selectedModel?.provider === provider.id ? '' : ' · 진행 중인 대화가 있으면 새 대화가 시작됩니다'}`}
            className={selectedModel?.provider === provider.id ? 'selected' : ''}
            onClick={() => onSelectProvider(provider.id)}
            disabled={disabled}
          >
            {provider.label}
          </button>
        ))}
      </div>
      <button className="icon-button" type="button" onClick={onToggleTheme} aria-label={`${theme === 'dark' ? '라이트' : '다크'} 모드로 변경`}>
        {theme === 'dark' ? '☀' : '☾'}
      </button>
    </header>
  )
}
