import { useEffect, useRef } from 'react'

// Closes a popover on an outside pointer press or Escape while it is open.
export function useDismiss(rootRef, isOpen, onDismiss) {
  const dismissRef = useRef(onDismiss)

  useEffect(() => {
    dismissRef.current = onDismiss
  }, [onDismiss])

  useEffect(() => {
    if (!isOpen) return undefined

    function handlePointerDown(event) {
      if (!rootRef.current?.contains(event.target)) dismissRef.current()
    }

    function handleKeyDown(event) {
      if (event.key === 'Escape') dismissRef.current()
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [rootRef, isOpen])
}
