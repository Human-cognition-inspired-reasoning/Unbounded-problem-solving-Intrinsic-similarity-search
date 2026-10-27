import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import {
  applyInlineCitations,
  normalizeMathDelimiters,
  safeDecodeURIComponent,
} from './messageRendering.js'

const REMARK_PLUGINS = [remarkGfm, remarkMath]
const REHYPE_PLUGINS = [rehypeKatex]

function ExternalLink({ node, title, children, ...props }) {
  void node
  if (title?.startsWith('citation:')) {
    const citationTitle = safeDecodeURIComponent(title.slice('citation:'.length))
    return (
      <sup className="inline-citation">
        <a
          {...props}
          title={citationTitle}
          aria-label={`출처 ${children}: ${citationTitle}`}
          target="_blank"
          rel="noreferrer noopener"
        >
          {children}
        </a>
      </sup>
    )
  }
  return <a {...props} title={title} target="_blank" rel="noreferrer noopener">{children}</a>
}

const MARKDOWN_COMPONENTS = { a: ExternalLink }

export function MarkdownContent({ content, citations = [], className = 'markdown-body' }) {
  const withCitations = applyInlineCitations(content, citations)
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        skipHtml
        components={MARKDOWN_COMPONENTS}
      >
        {normalizeMathDelimiters(withCitations)}
      </ReactMarkdown>
    </div>
  )
}
