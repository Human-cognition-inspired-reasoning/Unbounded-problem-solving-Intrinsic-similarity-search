import { useCallback, useEffect, useRef } from 'react'
import { createRefreshScheduler, createWorkspaceChannel } from './workspaceSync.js'

export function useWorkspaceSync(refreshWorkspace) {
  const refreshRef = useRef(refreshWorkspace)
  const channelRef = useRef(null)

  useEffect(() => {
    refreshRef.current = refreshWorkspace
  }, [refreshWorkspace])

  useEffect(() => {
    const scheduler = createRefreshScheduler(() => refreshRef.current())
    const channel = createWorkspaceChannel(() => scheduler.schedule())
    channelRef.current = channel

    function handleFocus() {
      scheduler.schedule({ immediate: true })
    }

    function handleVisibilityChange() {
      if (document.visibilityState === 'visible') scheduler.schedule({ immediate: true })
    }

    window.addEventListener('focus', handleFocus)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => {
      scheduler.close()
      channel?.close()
      channelRef.current = null
      window.removeEventListener('focus', handleFocus)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [])

  return useCallback((reason, conversationId = null) => {
    channelRef.current?.publish(reason, conversationId)
  }, [])
}
