import assert from 'node:assert/strict'
import test from 'node:test'
import {
  conversationSettings,
  defaultSettings,
  settingsForModelChange,
  workspaceSettings,
} from './settings.js'

const SOL = {
  id: 'gpt-sol', provider: 'openai', defaultReasoning: 'medium', defaultThinkingEnabled: false,
}
const OPUS = {
  id: 'opus', provider: 'anthropic', defaultReasoning: 'high', defaultThinkingEnabled: true,
}
const HAIKU = {
  id: 'haiku',
  provider: 'anthropic',
  defaultReasoning: 'none',
  defaultThinkingEnabled: true,
  supportsThinkingToggle: true,
}
const RETIRED = { ...SOL, id: 'retired', selectable: false }

test('saved conversation settings win over model defaults', () => {
  assert.deepEqual(
    conversationSettings({ reasoningEffort: 'low', thinkingEnabled: false }, OPUS),
    { modelId: 'opus', reasoningEffort: 'low', thinkingEnabled: false },
  )
  assert.deepEqual(
    conversationSettings({}, OPUS),
    { modelId: 'opus', reasoningEffort: 'high', thinkingEnabled: true },
  )
  assert.deepEqual(defaultSettings(SOL), conversationSettings(null, SOL))
})

test('the workspace opens on the first conversation, falling back to the first model', () => {
  const models = [SOL, OPUS]
  assert.deepEqual(
    workspaceSettings({ models, conversations: [{ modelId: 'opus', reasoningEffort: 'max' }] }),
    { modelId: 'opus', reasoningEffort: 'max', thinkingEnabled: true },
  )
  assert.deepEqual(
    workspaceSettings({ models, conversations: [{ modelId: 'unknown' }] }),
    { modelId: 'gpt-sol', reasoningEffort: 'medium', thinkingEnabled: false },
  )
})

test('without conversations the server assignment applies only to a selectable model', () => {
  const assigned = { modelId: 'opus', reasoningEffort: 'low', thinkingEnabled: false }
  assert.deepEqual(
    workspaceSettings({ models: [SOL, OPUS], conversations: [], initialSettings: assigned }),
    assigned,
  )
  assert.deepEqual(
    workspaceSettings({
      models: [RETIRED, OPUS],
      conversations: [],
      initialSettings: { ...assigned, modelId: 'retired' },
    }),
    { modelId: 'opus', reasoningEffort: 'high', thinkingEnabled: true },
  )
  assert.equal(workspaceSettings({ models: [RETIRED], conversations: [], initialSettings: null }), null)
})

test('a model switch keeps the thinking toggle only inside one provider', () => {
  assert.deepEqual(
    settingsForModelChange(OPUS, HAIKU, false),
    { modelId: 'haiku', reasoningEffort: 'none', thinkingEnabled: false },
  )
  assert.deepEqual(
    settingsForModelChange(SOL, HAIKU, false),
    { modelId: 'haiku', reasoningEffort: 'none', thinkingEnabled: true },
  )
  assert.deepEqual(
    settingsForModelChange(HAIKU, OPUS, true),
    { modelId: 'opus', reasoningEffort: 'high', thinkingEnabled: false },
  )
})
