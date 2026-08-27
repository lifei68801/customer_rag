import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import type { Components } from 'react-markdown'

interface MarkdownContentProps {
  text: string
}

// 两个独立问题都在真实回复里复现过，这里一并处理：
// 1) 模型有时不按提示词要求的 $ / $$ 写公式，而是抄检索资料原文常见的
//    学术论文定界符 \( \) / \[ \]——remark-math 完全不认这两种，公式会
//    原样显示成带反斜杠的文字，一个字符都渲染不出来。先统一转换成
//    $ / $$。
// 2) 模型有时把连续多个独立公式紧挨着写，中间不留任何字符（如
//    "...h_t$$$$\mathbf{q}..."）。remark-math 按定界符扫描 $$，四个连续
//    的 $ 会导致配对错位，把好几个公式和字面的 $$ 一起错误合并成一个畸形
//    节点，KaTeX 解析时因为内容里夹着 "$" 直接报错，页面上显示成红色报错
//    文本。强制给每个 $$...$$ 前后补空行，既拆开了紧挨着的公式，也让它们
//    被正确识别为独立成行的 display math（而不是被当成行内公式）。
function normalizeMath(text: string): string {
  const withDollarDelimiters = text
    .replace(/\\\[([\s\S]+?)\\\]/g, (_match, inner: string) => `$$${inner}$$`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_match, inner: string) => `$${inner}$`)

  return withDollarDelimiters.replace(
    /\$\$([\s\S]+?)\$\$/g,
    (_match, inner: string) => `\n\n$$\n${inner}\n$$\n\n`,
  )
}

// LLM 回复习惯性会带标准 markdown 语法（列表/加粗/表格/代码块），聊天气泡
// 之前是纯文本渲染（<p>{text}</p>），用户看到的是没转换过的原始符号。
// components 覆盖每个元素的样式而不是引入 @tailwindcss/typography，是为了
// 跟这套圆角 + 边框的设计 token（rounded-*/border-subtle/accent-*）
// 保持一致，而不是套一个通用 prose 主题。
const components: Components = {
  p: ({ children }) => <p className="whitespace-pre-wrap leading-relaxed">{children}</p>,
  strong: ({ children }) => <strong className="font-bold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="underline decoration-2 underline-offset-2 hover:text-ink-soft"
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
    <pre className="overflow-x-auto rounded border border-subtle bg-paper p-3">{children}</pre>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-subtle bg-paper px-2 py-1 text-left font-bold">{children}</th>
  ),
  td: ({ children }) => <td className="border border-subtle px-2 py-1">{children}</td>,
  hr: () => <hr className="border-t border-subtle" />,
}

export function MarkdownContent({ text }: MarkdownContentProps) {
  return (
    <div className="space-y-2 [&_.katex-display]:overflow-x-auto [&_.katex-display]:overflow-y-hidden [&_.katex-display]:py-1">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {normalizeMath(text)}
      </ReactMarkdown>
    </div>
  )
}
