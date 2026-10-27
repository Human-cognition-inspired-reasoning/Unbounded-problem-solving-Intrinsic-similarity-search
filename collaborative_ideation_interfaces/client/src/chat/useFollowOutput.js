import { useEffect, useRef } from 'react'
import { isNearBottom } from './streamingState.js'

// Keeps a streaming answer in view until the participant scrolls up to read.
export function useFollowOutput(rootRef, conversationKey, messages, isSending) {
  const followOutputRef = useRef(true)
  const lastScrollTopRef = useRef(0)

  useEffect(() => {
    const scroller = rootRef.current?.parentElement
    if (!scroller) return undefined
    const handleScroll = () => {
      const scrollTop = Math.max(0, scroller.scrollTop)
      if (scrollTop < lastScrollTopRef.current) {
        followOutputRef.current = false
      } else if (scrollTop > lastScrollTopRef.current) {
        followOutputRef.current = isNearBottom(scroller)
      }
      lastScrollTopRef.current = scrollTop
    }
    const handleWheel = (event) => {
      if (event.deltaY < 0) followOutputRef.current = false
    }
    let touchY = null
    const handleTouchStart = (event) => {
      touchY = event.touches[0]?.clientY ?? null
    }
    const handleTouchMove = (event) => {
      const nextY = event.touches[0]?.clientY ?? null
      if (touchY !== null && nextY !== null && nextY > touchY) {
        followOutputRef.current = false
      }
      touchY = nextY
    }
    scroller.addEventListener('scroll', handleScroll, { passive: true })
    scroller.addEventListener('wheel', handleWheel, { passive: true })
    scroller.addEventListener('touchstart', handleTouchStart, { passive: true })
    scroller.addEventListener('touchmove', handleTouchMove, { passive: true })
    return () => {
      scroller.removeEventListener('scroll', handleScroll)
      scroller.removeEventListener('wheel', handleWheel)
      scroller.removeEventListener('touchstart', handleTouchStart)
      scroller.removeEventListener('touchmove', handleTouchMove)
    }
  }, [rootRef, conversationKey])

  useEffect(() => {
    const scroller = rootRef.current?.parentElement
    if (!scroller) return
    followOutputRef.current = true
    scroller.scrollTo({ top: scroller.scrollHeight })
    lastScrollTopRef.current = scroller.scrollTop
  }, [rootRef, conversationKey])

  useEffect(() => {
    const scroller = rootRef.current?.parentElement
    if (!scroller || !followOutputRef.current) return
    scroller.scrollTo({ top: scroller.scrollHeight })
    lastScrollTopRef.current = scroller.scrollTop
  }, [rootRef, messages, isSending])
}
