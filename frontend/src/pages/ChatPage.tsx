import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Hero } from '../components/Hero'
import { ChatWindow } from '../components/ChatWindow'
import { ChatInput } from '../components/ChatInput'
import { ChatSidebar } from '../components/ChatSidebar'
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
  const {
    messages,
    isSending,
    sendQuestion,
    resetConversation,
    sessions,
    sessionsError,
    activeSessionId,
    selectSession,
    deleteSession,
  } = useAgentChat()

  useEffect(() => {
    document.title = '企业数字员工'
  }, [])

  return (
    <div className="flex min-h-dvh flex-col bg-paper">
      <div className="border-b border-subtle bg-card px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-ink-soft">
        知识驱动的企业数字员工
      </div>
      <nav className="flex items-center justify-between border-b border-subtle bg-card px-6 py-4">
        <span className="font-mono font-semibold text-ink">企业数字员工</span>
        <Link
          to="/admin"
          className={`flex min-h-[44px] cursor-pointer items-center gap-1.5 rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          <GearIcon />
          管理后台
        </Link>
      </nav>
      {/* 侧边栏在窄屏（<768px）下改成顶部横条（ChatSidebar 内部处理），
          和 AdminLayout 的响应式方案同一个思路。 */}
      <div className="flex flex-1 flex-col md:flex-row">
        <ChatSidebar
          sessions={sessions}
          sessionsError={sessionsError}
          activeSessionId={activeSessionId}
          onSelectSession={selectSession}
          onNewSession={resetConversation}
          onDeleteSession={deleteSession}
        />
        <div className="flex flex-1 flex-col">
          <Hero />
          <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col">
            <ChatWindow messages={messages} />
            <ChatInput disabled={isSending} onSend={sendQuestion} />
          </main>
        </div>
      </div>
      <Footer />
    </div>
  )
}
