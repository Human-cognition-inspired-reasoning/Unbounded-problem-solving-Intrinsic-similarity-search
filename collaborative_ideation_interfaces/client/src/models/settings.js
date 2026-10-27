export function findModel(models, modelId) {
  return models.find((model) => model.id === modelId)
}

// A conversation's saved settings win; a fresh choice falls back to the model defaults.
export function conversationSettings(conversation, model) {
  return {
    modelId: model?.id,
    reasoningEffort: conversation?.reasoningEffort || model?.defaultReasoning || 'medium',
    thinkingEnabled: conversation?.thinkingEnabled ?? Boolean(model?.defaultThinkingEnabled),
  }
}

export function defaultSettings(model) {
  return conversationSettings(null, model)
}

// Before the first conversation, start from the setting the server assigned at random.
export function workspaceSettings({ models, conversations, initialSettings }) {
  const first = conversations[0]
  if (first) return conversationSettings(first, findModel(models, first.modelId) || models[0])

  const assignedModel = models.find((model) => (
    model.id === initialSettings?.modelId && model.selectable !== false
  ))
  if (assignedModel) {
    return {
      modelId: assignedModel.id,
      reasoningEffort: initialSettings.reasoningEffort,
      thinkingEnabled: initialSettings.thinkingEnabled,
    }
  }
  const model = models.find((row) => row.selectable !== false)
  return model ? defaultSettings(model) : null
}

// Switching models resets effort; thinking carries over only within one provider.
export function settingsForModelChange(currentModel, nextModel, thinkingEnabled) {
  return {
    modelId: nextModel?.id,
    reasoningEffort: nextModel?.defaultReasoning || 'medium',
    thinkingEnabled: nextModel?.supportsThinkingToggle
      ? (currentModel?.provider === nextModel.provider
        ? thinkingEnabled
        : Boolean(nextModel.defaultThinkingEnabled))
      : false,
  }
}
