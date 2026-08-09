import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Hero } from '../components/Hero'
import { ChatWindow } from '../components/ChatWindow'
import { ChatInput } from '../components/ChatInput'
import { Footer } from '../components/Footer'
import { useAgentChat } from '../hooks/useAgentChat'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

function GearIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}

export function ChatPage() {
  const { messages, isSending, sendQuestion, resetConversation } = useAgentChat()

  useEffect(() => {
    document.title = '客服智能问答 Demo'
  }, [])

  return (
    <div className="flex min-h-dvh flex-col bg-paper">
      <div className="border-b-2 border-ink bg-ink px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-accent-yellow">
        检索增强生成 + 知识图谱驱动的客服问答演示
      </div>
      <nav className="flex items-center justify-between border-b-2 border-ink bg-accent-yellow px-6 py-4">
        <span className="font-bold text-ink">客服智能问答 Demo</span>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={resetConversation}
            disabled={messages.length === 0}
            className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
          >
            重新开始对话
          </button>
          <Link
            to="/admin"
            className={`flex min-h-[44px] cursor-pointer items-center gap-1.5 border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
          >
            <GearIcon />
            管理后台
          </Link>
        </div>
      </nav>
      <Hero />
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col">
        <ChatWindow messages={messages} />
        <ChatInput disabled={isSending} onSend={sendQuestion} />
      </main>
      <Footer />
    </div>
  )
}
