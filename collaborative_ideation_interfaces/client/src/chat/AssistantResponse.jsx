import { useEffect, useState } from 'react'
import { MarkdownContent } from './MarkdownContent.jsx'
import { citationsForTextPart } from './messageRendering.js'
import {
  formatElapsedTime,
  groupResponseParts,
  responseWritingEpoch,
} from './streamingState.js'

function sourceLabel(source) {
  if (source.title) return source.title
  try {
    return new URL(source.url).hostname
  } catch {
    return source.url
  }
}

function useElapsedSeconds(startedAt, durationMs, active) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    if (!active) return undefined
    const parsedStart = Date.parse(startedAt)
    const start = Number.isNaN(parsedStart) ? Date.now() : parsedStart
    const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - start) / 1000)))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [active, startedAt])

  if (!active && Number.isFinite(durationMs)) return Math.max(0, Math.round(durationMs / 1000))
  return elapsed
}

export function ResponseTiming({ startedAt, durationMs, active }) {
  const seconds = useElapsedSeconds(startedAt, durationMs, active)
  if (!active && !Number.isFinite(durationMs)) return null
  return (
    <div className="response-timing" role={active ? 'timer' : undefined}>
      {active ? '답변 생성 중' : '답변 생성'}
      <span>· {formatElapsedTime(seconds)}</span>
    </div>
  )
}

function SourceList({ sources }) {
  if (!sources?.length) return null
  return (
    <div className="activity-sources" aria-label={`찾은 소스 ${sources.length}개`}>
      {sources.map((source) => (
        <a key={source.url} href={source.url} target="_blank" rel="noreferrer noopener">
          {sourceLabel(source)}
        </a>
      ))}
    </div>
  )
}

function ActivityGroupContent({ parts }) {
  return (
    <div className="activity-group-content">
      {parts.map((part) => {
        if (part.type === 'reasoning' && part.text) {
          return (
            <MarkdownContent
              key={part.id}
              content={part.text}
              className="activity-reasoning-content"
            />
          )
        }
        if (part.type === 'search' && part.sources?.length) {
          return (
            <div className="activity-source-block" key={part.id}>
              <SourceList sources={part.sources} />
            </div>
          )
        }
        return null
      })}
    </div>
  )
}

export function ActivityGroup({ parts, active, phase }) {
  const [expanded, setExpanded] = useState(active)
  const reasoningParts = parts.filter((part) => part.type === 'reasoning' && part.text)
  const sourceCount = parts.reduce(
    (count, part) => count + (part.type === 'search' ? (part.sources?.length || 0) : 0),
    0,
  )
  const hasContent = reasoningParts.length > 0 || sourceCount > 0
  const completedLabel = reasoningParts.length > 0 ? '생각한 과정' : '찾아본 자료'
  const activeLabel = phase === 'searching' ? '자료를 찾는 중' : '생각 중'

  if (active) {
    return (
      <div className={`reasoning-panel active${expanded ? '' : ' collapsed'}`} role="status">
        <button
          className="reasoning-heading reasoning-toggle"
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          <span className="reasoning-orb" aria-hidden="true" />
          <span>{activeLabel}</span>
          <span className="reasoning-chevron" aria-hidden="true">›</span>
        </button>
        {expanded && hasContent && <ActivityGroupContent parts={parts} />}
      </div>
    )
  }

  if (!hasContent) return null
  return (
    <details className="reasoning-panel activity-group">
      <summary>
        <span>{completedLabel}</span>
        {sourceCount > 0 && <span className="search-source-count">· 자료 {sourceCount}개</span>}
      </summary>
      <ActivityGroupContent parts={parts} />
    </details>
  )
}

function CitationFooter({ citations }) {
  const footerCitations = (citations || []).filter((citation) => citation.placement === 'footer')
  if (footerCitations.length === 0) return null
  return (
    <section className="citation-footer" aria-label="인용한 출처">
      <h3>인용한 출처</h3>
      <ol>
        {footerCitations.map((citation) => (
          <li key={`${citation.sourceNumber}:${citation.url}`}>
            <a
              href={citation.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              {sourceLabel(citation)}
            </a>
          </li>
        ))}
      </ol>
    </section>
  )
}

export function AssistantResponse({ message }) {
  const parts = message.responseParts || []
  const citations = message.citations || []
  const inlineCitations = citations.filter((citation) => citation.placement === 'inline')
  const interruptedNotice = message.completionReason === 'credit_exhausted'
    ? '남은 크레딧이 소진되어 이 지점에서 응답이 중단되었습니다.'
    : message.completionReason === 'client_disconnected'
      ? '연결이 종료되어 이 지점까지만 응답이 저장되었습니다.'
      : null

  if (parts.length === 0 && !message.streaming) {
    return (
      <>
        <ResponseTiming
          startedAt={message.startedAt}
          durationMs={message.reasoningDurationMs}
          active={false}
        />
        {(message.streaming || message.reasoningSummary?.length > 0) && (
          <ActivityGroup
            parts={(message.reasoningSummary || []).map((block) => ({
              id: block.id || `reasoning:${block.index}`,
              type: 'reasoning',
              text: block.text,
            }))}
            active={false}
            phase="thinking"
          />
        )}
        {message.content && (
          <MarkdownContent
            content={message.content}
            citations={inlineCitations}
          />
        )}
        {interruptedNotice && <p className="response-interruption">{interruptedNotice}</p>}
        <CitationFooter citations={citations} />
      </>
    )
  }

  const groups = groupResponseParts(parts)
  const lastGroupIndex = groups.length - 1
  const writingEpoch = responseWritingEpoch(parts)
  const rendered = groups.map((group, index) => {
    if (group.type === 'text') {
      const { part } = group
      const content = part.text
      const textPrefix = parts
        .slice(0, parts.indexOf(part))
        .filter((previous) => previous.type === 'text')
        .map((previous) => previous.text)
        .join('')
      const citations = citationsForTextPart(inlineCitations, textPrefix, part.text)
      return (
        <MarkdownContent
          key={part.id}
          content={content}
          citations={citations}
        />
      )
    }
    if (group.type === 'activity') {
      const active = Boolean(
        message.streaming
        && index === lastGroupIndex
        && ['thinking', 'searching'].includes(message.phase),
      )
      return (
        <ActivityGroup
          key={active ? 'active-activity' : `${group.id}:complete:${writingEpoch}`}
          parts={group.parts}
          active={active}
          phase={message.phase}
        />
      )
    }
    return null
  })

  const needsActivityPlaceholder = message.streaming
    && ['thinking', 'searching'].includes(message.phase)
    && groups.at(-1)?.type !== 'activity'

  return (
    <div className="response-parts">
      <ResponseTiming
        startedAt={message.startedAt}
        durationMs={message.reasoningDurationMs}
        active={Boolean(message.streaming)}
      />
      {rendered}
      {needsActivityPlaceholder && (
        <ActivityGroup
          key="active-activity"
          parts={[]}
          active
          phase={message.phase}
        />
      )}
      {interruptedNotice && <p className="response-interruption">{interruptedNotice}</p>}
      <CitationFooter citations={citations} />
    </div>
  )
}
