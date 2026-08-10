import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'

interface MarkdownContentProps {
  text: string
}

// LLM 回复习惯性会带标准 markdown 语法（列表/加粗/表格/代码块），聊天气泡
// 之前是纯文本渲染（<p>{text}</p>），用户看到的是没转换过的原始符号。
// components 覆盖每个元素的样式而不是引入 @tailwindcss/typography，是为了
// 跟这套 neo-brutalist 设计 token（border-ink/accent-*/shadow-brutal）保持
// 一致，而不是套一个通用 prose 主题。
const components: Components = {
  p: ({ children }) => <p className="whitespace-pre-wrap leading-relaxed">{children}</p>,
  strong: ({ children }) => <strong className="font-bold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="underline decoration-2 underline-offset-2 hover:text-accent-pink"
    >
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  h1: ({ children }) => <h1 className="text-lg font-bold">{children}</h1>,
  h2: ({ children }) => <h2 className="text-base font-bold">{children}</h2>,
  h3: ({ children }) => <h3 className="text-base font-bold">{children}</h3>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-ink-soft pl-3 text-ink-soft">{children}</blockquote>
  ),
  code: ({ className, children }) => {
    const isBlock = /language-/.test(className ?? '')
    if (isBlock) {
      return (
        <code className={`block overflow-x-auto whitespace-pre font-mono text-sm ${className ?? ''}`}>
          {children}
        </code>
      )
    }
    return (
      <code className="rounded bg-paper px-1 py-0.5 font-mono text-sm text-ink">{children}</code>
    )
  },
  pre: ({ children }) => (
    <pre className="overflow-x-auto rounded border-2 border-ink bg-paper p-3">{children}</pre>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-2 border-ink bg-paper px-2 py-1 text-left font-bold">{children}</th>
  ),
  td: ({ children }) => <td className="border-2 border-ink px-2 py-1">{children}</td>,
  hr: () => <hr className="border-t-2 border-ink" />,
}

export function MarkdownContent({ text }: MarkdownContentProps) {
  return (
    <div className="space-y-2">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
