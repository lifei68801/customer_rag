import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Hero } from '../components/Hero'
import { ChatWindow } from '../components/ChatWindow'
import { ChatInput } from '../components/ChatInput'
import { ChatSidebar } from '../components/ChatSidebar'
import { Footer } from '../components/Footer'
import { useAgentChat } from '../hooks/useAgentChat'
import { AccountMenu } from '../admin/AccountMenu'
import { TenantProvider } from '../admin/TenantContext'
import { useAdminAuth } from '../admin/useAdminAuth'

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

/**
 * 还没选定租户时的落点。
 *
 * admin 的 tenant_id 恒为 None，当前租户要显式切过一次才有值；在那之前
 * 前台每个请求都会撞上后端的 400「请先选择一个租户」。这一屏把那句话摆
 * 出来，并且把账号块（含租户切换器）留在原位——猜错了要让用户看得见、
 * 也够得着纠正的地方，不能只给一片空白。
 */
function NoTenantNotice() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
      <p role="status" className="font-mono text-lg font-semibold text-ink">
        请先选择一个租户
      </p>
      <p className="text-sm text-ink-soft">
        租户就是知识库。没选之前问答不知道该去哪一份里找答案——在左下角的账号块里选一个。
      </p>
    </main>
  )
}

function ChatWorkspace({ onLogout }: { onLogout: () => void }) {
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

  return (
    <>
      {/* 侧边栏在窄屏（<768px）下改成顶部横条（ChatSidebar 内部处理），
          和 AdminLayout 的响应式方案同一个思路。 */}
      <ChatSidebar
        sessions={sessions}
        sessionsError={sessionsError}
        activeSessionId={activeSessionId}
        onSelectSession={selectSession}
        onNewSession={resetConversation}
        onDeleteSession={deleteSession}
        footer={<AccountMenu onLogout={onLogout} showManagementLinks={false} />}
      />
      <div className="flex flex-1 flex-col">
        <Hero />
        <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col">
          <ChatWindow messages={messages} />
          <ChatInput disabled={isSending} onSend={sendQuestion} />
        </main>
      </div>
    </>
  )
}

/**
 * 前台问答页。只在已登录时渲染（登录门在 App.tsx），所以这里可以直接
 * 认 useAdminAuth 给的身份。
 *
 * TenantProvider 包在这里而不是只包后台：账号块里的租户切换器要用它，
 * 而前台的租户就是问答落在哪个知识库上——admin 需要它来验证刚配好的
 * 本体问答到底通不通。
 */
export function ChatPage() {
  const { currentTenantId, logout } = useAdminAuth()

  useEffect(() => {
    document.title = '企业数字员工'
  }, [])

  return (
    <TenantProvider>
      <div className="flex min-h-dvh flex-col bg-paper">
        <div className="border-b border-subtle bg-card px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-ink-soft">
          知识驱动的企业数字员工
        </div>
        <nav
          data-testid="site-topbar"
          className="flex items-center justify-between border-b border-subtle bg-card px-6 py-4"
        >
          <span className="font-mono font-semibold text-ink">企业数字员工</span>
          <Link
            to="/admin"
            className={`flex min-h-[44px] cursor-pointer items-center gap-1.5 rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
          >
            <GearIcon />
            管理后台
          </Link>
        </nav>
        <div className="flex flex-1 flex-col md:flex-row">
          {currentTenantId === null ? (
            <>
              <aside className="flex flex-col border-b border-subtle bg-card p-3 md:w-64 md:flex-shrink-0 md:border-b-0 md:border-r">
                <AccountMenu onLogout={logout} showManagementLinks={false} />
              </aside>
              <NoTenantNotice />
            </>
          ) : (
            <ChatWorkspace onLogout={logout} />
          )}
        </div>
        <Footer />
      </div>
    </TenantProvider>
  )
}
