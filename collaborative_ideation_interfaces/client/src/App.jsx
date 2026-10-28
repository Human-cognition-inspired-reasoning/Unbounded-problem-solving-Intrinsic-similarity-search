import { useEffect, useRef, useState } from 'react'
import './App.css'
import { chatApi, isSessionExpired } from './api/chatApi.js'
import { ChatComposer } from './chat/ChatComposer.jsx'
import { generationView } from './chat/generationView.js'
import { MessageList } from './chat/MessageList.jsx'
import { useChatGeneration } from './chat/useChatGeneration.js'
import { ModelBar } from './models/ModelBar.jsx'
import {
  conversationSettings,
  defaultSettings,
  findModel,
  settingsForModelChange,
  workspaceSettings,
} from './models/settings.js'
import { getInitialTheme, saveTheme } from './shared/theme.js'
import { LoginScreen } from './workspace/LoginScreen.jsx'
import { Sidebar } from './workspace/Sidebar.jsx'
import { useWorkspaceSync } from './workspace/useWorkspaceSync.js'
import { fetchWorkspace, fetchWorkspaceSnapshot } from './workspace/workspaceApi.js'
import { mergeSyncedConversations, messageCountOf } from './workspace/workspaceSync.js'

const EMPTY_USAGE = { limitUsd: 10, usedUsd: 0, remainingUsd: 10 }
const EMPTY_SETTINGS = { modelId: null, reasoningEffort: 'medium', thinkingEnabled: false }
const MODEL_CONTEXT_RESET_NOTICE = '모델을 변경하면 이전 답변은 유지되지만, 모델 내부의 추론 맥락은 초기화됩니다.'

function App() {
  const [theme, setTheme] = useState(getInitialTheme)
  const [participant, setParticipant] = useState(null)
  const [models, setModels] = useState([])
  const [conversations, setConversations] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [settings, setSettings] = useState(EMPTY_SETTINGS)
  const [webSearchEnabled, setWebSearchEnabled] = useState(false)
  const [usage, setUsage] = useState(EMPTY_USAGE)
  // Until the workspace loads, the server's own concurrency check still applies.
  const [maxConcurrentGenerations, setMaxConcurrentGenerations] = useState(Infinity)
  const [isBooting, setIsBooting] = useState(true)
  const [isAuthSubmitting, setIsAuthSubmitting] = useState(false)
  const [isCreating, setIsCreating] = useState(false)
  const loadingMessagesRef = useRef(new Set())
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const activeConversation = conversations.find((row) => row.id === activeId) || null

  function handleSessionExpired() {
    setParticipant(null)
    setConversations([])
    setActiveId(null)
    resetGenerations()
  }

  function handleRequestError(requestError) {
    if (isSessionExpired(requestError)) handleSessionExpired()
    else setError(requestError.message)
  }

  // 열어본 대화만 본문을 가져온다. 로그인 시 전부 싣던 것이 페이지 로드마다
  // 1.13 MB 응답과 수십 ms의 GIL 점유를 만들던 자리다. 새로 만든 대화는
  // 생성 응답이 이미 빈 `messages`를 들고 오므로 다시 부르지 않는다.
  async function loadConversationMessages(conversationId) {
    if (!conversationId || loadingMessagesRef.current.has(conversationId)) return
    loadingMessagesRef.current.add(conversationId)
    try {
      const data = await chatApi.conversationMessages(conversationId)
      setConversations((current) => current.map((row) => (
        row.id === conversationId && !row.messages ? { ...row, messages: data.messages } : row
      )))
    } catch (requestError) {
      handleRequestError(requestError)
    } finally {
      loadingMessagesRef.current.delete(conversationId)
    }
  }

  function applyWorkspace(workspace) {
    setModels(workspace.models)
    setUsage(workspace.usage)
    setMaxConcurrentGenerations(workspace.maxConcurrentGenerations)
    setConversations(workspace.conversations)
    if (workspace.conversations[0]) setActiveId(workspace.conversations[0].id)
    const nextSettings = workspaceSettings(workspace)
    if (nextSettings) setSettings(nextSettings)
  }

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    saveTheme(theme)
  }, [theme])

  const publishWorkspaceChange = useWorkspaceSync(refreshWorkspaceFromServer)
  const {
    generationRequests, handleSend, conversationHasActiveRequest,
    resetGenerations, readGenerationSnapshot,
  } = useChatGeneration({
    activeConversation, isCreating, maxConcurrentGenerations,
    settings: { ...settings, webSearchEnabled },
    setIsCreating, setConversations, setActiveId, setUsage, setError, setNotice,
    onSessionExpired: handleSessionExpired, publishWorkspaceChange,
  })

  async function refreshWorkspaceFromServer() {
    if (!participant) return
    const currentActiveId = activeId
    try {
      const snapshot = await readGenerationSnapshot(() => fetchWorkspaceSnapshot(currentActiveId))
      setUsage(snapshot.usage)
      setConversations((current) => mergeSyncedConversations(
        current,
        snapshot.conversations,
        currentActiveId,
        snapshot.activeMessages,
      ))

      const nextActiveId = snapshot.conversations.some((row) => row.id === currentActiveId)
        ? currentActiveId
        : (snapshot.conversations[0]?.id || null)
      if (nextActiveId !== currentActiveId) setActiveId(nextActiveId)

      const active = snapshot.conversations.find((row) => row.id === nextActiveId)
      const model = findModel(models, active?.modelId)
      if (active && model) setSettings(conversationSettings(active, model))
    } catch (requestError) {
      handleRequestError(requestError)
    }
  }

  useEffect(() => {
    let cancelled = false
    async function bootstrap() {
      try {
        await chatApi.health()
        const me = await chatApi.me()
        if (!cancelled && me.participant) {
          setParticipant(me.participant)
          const workspace = await fetchWorkspace()
          if (!cancelled) applyWorkspace(workspace)
        }
      } catch (requestError) {
        if (!cancelled && !isSessionExpired(requestError)) setError(requestError.message)
      } finally {
        if (!cancelled) setIsBooting(false)
      }
    }
    bootstrap()
    return () => { cancelled = true }
  }, [])

  async function handleLogin(studyId, password) {
    setIsAuthSubmitting(true)
    setError('')
    try {
      const data = await chatApi.login(studyId, password)
      setParticipant(data.participant)
      applyWorkspace(await fetchWorkspace())
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setIsAuthSubmitting(false)
    }
  }

  async function handleLogout() {
    await chatApi.logout()
    handleSessionExpired()
    setError('')
    setNotice('')
    publishWorkspaceChange('session_changed')
  }

  function isActiveConversationBusy() {
    return conversationHasActiveRequest(activeConversation?.id || null)
  }

  function openNewConversation(conversation) {
    setConversations((current) => [conversation, ...current])
    setActiveId(conversation.id)
    publishWorkspaceChange('conversation_created', conversation.id)
  }

  // Saves a settings change on the open conversation; without one it stays a draft choice.
  async function saveConversationSettings(changes) {
    if (!activeConversation) return true
    const conversationId = activeConversation.id
    try {
      const data = await chatApi.updateConversation(conversationId, changes)
      setConversations((current) => current.map((row) => (
        row.id === conversationId ? data.conversation : row
      )))
      publishWorkspaceChange('conversation_updated', conversationId)
      return true
    } catch (requestError) {
      handleRequestError(requestError)
      return false
    }
  }

  async function handleCreateConversation() {
    setIsCreating(true)
    setError('')
    setNotice('')
    try {
      const data = await chatApi.createConversation(settings)
      openNewConversation(data.conversation)
      setWebSearchEnabled(false)
    } catch (requestError) {
      handleRequestError(requestError)
    } finally {
      setIsCreating(false)
    }
  }

  async function handleSelectProvider(provider) {
    const nextModel = models.find((model) => (
      model.provider === provider && model.selectable !== false
    ))
    const currentModel = findModel(models, settings.modelId)
    if (
      !nextModel
      || currentModel?.provider === provider
      || isCreating
      || isActiveConversationBusy()
    ) return

    const nextSettings = defaultSettings(nextModel)
    setError('')
    setNotice('')
    setWebSearchEnabled(false)

    if (!activeConversation) {
      setSettings(nextSettings)
      return
    }

    if (messageCountOf(activeConversation) === 0) {
      await handleSelectModel(nextModel.id)
      return
    }

    // A conversation keeps one provider, so switching provider starts a new one.
    setIsCreating(true)
    try {
      const data = await chatApi.createConversation(nextSettings)
      setSettings(nextSettings)
      openNewConversation(data.conversation)
    } catch (requestError) {
      handleRequestError(requestError)
    } finally {
      setIsCreating(false)
    }
  }

  function handleSelectConversation(conversationId) {
    const conversation = conversations.find((row) => row.id === conversationId)
    if (conversation && !conversation.messages) void loadConversationMessages(conversationId)
    const model = findModel(models, conversation?.modelId) || models[0]
    setActiveId(conversationId)
    setSettings(conversationSettings(conversation, model))
    setWebSearchEnabled(false)
    setError('')
    setNotice('')
  }

  async function handleSelectModel(modelId) {
    if (isActiveConversationBusy()) return
    const currentModel = findModel(models, settings.modelId)
    const model = findModel(models, modelId)
    const resetsReasoningContext = Boolean(
      activeConversation
      && messageCountOf(activeConversation) > 0
      && currentModel
      && model
      && currentModel.id !== model.id
      && currentModel.provider === model.provider
    )
    const nextSettings = {
      ...settingsForModelChange(currentModel, model, settings.thinkingEnabled),
      modelId,
    }
    setSettings(nextSettings)
    if (!model?.supportsForcedWebSearch) setWebSearchEnabled(false)
    setNotice(resetsReasoningContext ? MODEL_CONTEXT_RESET_NOTICE : '')
    if (!(await saveConversationSettings(nextSettings))) setNotice('')
  }

  async function handleChangeSettings(changes) {
    if (isActiveConversationBusy()) return
    setSettings((current) => ({ ...current, ...changes }))
    await saveConversationSettings(changes)
  }

  if (isBooting) {
    return <main className="boot-screen"><div className="spinner" /><p>AI디어 공모전 웹사이트를 준비하고 있습니다…</p></main>
  }

  if (!participant) {
    return <LoginScreen onLogin={handleLogin} error={error} isSubmitting={isAuthSubmitting} />
  }

  const { isGenerating, limitReached, visibleMessages } = generationView({
    generationRequests,
    conversations,
    activeConversation,
    limit: maxConcurrentGenerations,
  })

  return (
    <div className="app-shell">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        usage={usage}
        onCreate={handleCreateConversation}
        onSelect={handleSelectConversation}
        onLogout={handleLogout}
        isCreating={isCreating}
      />
      <main className="chat-panel">
        <ModelBar
          models={models}
          selectedModelId={settings.modelId}
          onSelectProvider={handleSelectProvider}
          theme={theme}
          onToggleTheme={() => setTheme((current) => current === 'dark' ? 'light' : 'dark')}
          disabled={isCreating || isGenerating}
        />
        {(error || notice) && (
          <div className="banner-stack">
            {error && <div className="error-banner" role="alert"><span>{error}</span><button type="button" onClick={() => setError('')} aria-label="오류 닫기">×</button></div>}
            {notice && <div className="notice-banner" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice('')} aria-label="안내 닫기">×</button></div>}
          </div>
        )}
        <section className="chat-content" aria-label="현재 대화">
          <MessageList messages={visibleMessages} isSending={isGenerating} />
        </section>
        <ChatComposer
          onSend={handleSend}
          disabled={isCreating || isGenerating || limitReached}
          inputDisabled={isCreating}
          models={models}
          selectedModelId={settings.modelId}
          onSelectModel={handleSelectModel}
          reasoningEffort={settings.reasoningEffort}
          onSelectReasoning={(reasoningEffort) => handleChangeSettings({ reasoningEffort })}
          thinkingEnabled={settings.thinkingEnabled}
          onToggleThinking={(thinkingEnabled) => handleChangeSettings({ thinkingEnabled })}
          webSearchEnabled={webSearchEnabled}
          onToggleSearch={setWebSearchEnabled}
        />
      </main>
    </div>
  )
}

export default App
