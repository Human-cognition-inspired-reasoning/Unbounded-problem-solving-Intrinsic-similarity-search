import { useRef } from 'react'
import { AttachmentList } from '../files/AttachmentList.jsx'
import { ActivityGroup, AssistantResponse, ResponseTiming } from './AssistantResponse.jsx'
import { useFollowOutput } from './useFollowOutput.js'

export function MessageList({ messages, isSending }) {
  const rootRef = useRef(null)
  const conversationKey = messages[0]?.conversationId || messages[0]?.id || 'empty'
  const hasStreamingAssistant = messages.some((message) => (
    message.role === 'assistant' && message.streaming
  ))

  useFollowOutput(rootRef, conversationKey, messages, isSending)

  if (messages.length === 0 && !isSending) {
    return (
      <div className="empty-chat" ref={rootRef}>
        <h2>어떤 연구 아이디어를 탐색할까요?</h2>
        <p>연구 문제, 관련 연구, 방법론 또는 검증 계획에 대해 자유롭게 물어보세요.</p>
      </div>
    )
  }

  return (
    <div className="message-list" ref={rootRef} aria-live="polite" aria-busy={isSending}>
      {messages.map((message) => (
        <article
          key={message.id}
          className={`message ${message.role}${message.pending ? ' pending' : ''}${message.streaming ? ' streaming' : ''}`}
        >
          <div className="message-body">
            {message.role === 'assistant'
              ? <AssistantResponse message={message} />
              : <><AttachmentList attachments={message.attachments} />{message.content && <p>{message.content}</p>}</>}
          </div>
        </article>
      ))}
      {isSending && !hasStreamingAssistant && (
        <article className="message assistant pending">
          <div className="message-body">
            <ResponseTiming active />
            <ActivityGroup parts={[]} active phase="thinking" />
          </div>
        </article>
      )}
    </div>
  )
}
