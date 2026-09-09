import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Hero } from '../components/Hero'
import { ChatWindow } from '../components/ChatWindow'
import { ChatInput } from '../components/ChatInput'
import { ChatSidebar } from '../components/ChatSidebar'
import { PersonaRail } from '../components/PersonaRail'
import { Footer } from '../components/Footer'
import { useAgentChat } from '../hooks/useAgentChat'
import { AccountMenu } from '../admin/AccountMenu'
import { TenantProvider, useAdminTenant } from '../admin/TenantContext'
import { useAdminAuth } from '../admin/useAdminAuth'
import { GuidedQuestions } from '../components/GuidedQuestions'
import {
  fetchPersonaDetail,
  fetchPersonas,
  type Persona,
  type PersonaDetail,
} from '../lib/personasApi'

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

/**
 * 当前挂着的租户不在这个账号能访问的数字人列表里时的落点。
 *
 * 会发生在：账号被显式授权了别的租户，但会话当前的 current_tenant_id
 * （多半是 admin_users.tenant_id 那个回退值）不在授权范围内。不拦这一屏
 * 的话，问答请求会一路撞到后端 require_chat_session 的 403，正文区只剩
 * 一串看不懂的错误气泡——用户看得见坏了，却不知道去哪修。
 *
 * 不在这里替用户选一个数字人：PersonaRail 已经因为「当前租户不在列表里」
 * 强制渲染出来（见 PersonaRail.tsx 的判断），这一屏只负责说清楚原因、
 * 把选择权指过去。
 */
function TenantInaccessibleNotice() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
      <p role="status" className="font-mono text-lg font-semibold text-ink">
        当前知识库你没有权限访问
      </p>
      <p className="text-sm text-ink-soft">
        这个账号被改过授权，当前挂着的知识库已经不在你能访问的范围里了。
        在数字人列表中选一个你能访问的——找不到列表就说明一个都没有，
        请联系管理员重新授权。
      </p>
    </main>
  )
}

function ChatWorkspace({ onLogout }: { onLogout: () => void }) {
  // 数字人（= 租户）的取数与切换状态放在这里，不放进 useAgentChat——那个
  // hook 管的是一次会话内的消息，数字人是会话之外的作用域（换数字人不是
  // 换一条消息，是换整个知识库）。
  const { tenantId, setTenantId } = useAdminTenant()
  const [personas, setPersonas] = useState<Persona[]>([])
  const [personasLoading, setPersonasLoading] = useState(true)
  const [personasError, setPersonasError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchPersonas('')
      .then((data) => {
        if (cancelled) return
        setPersonas(data.personas)
        setPersonasError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        // 拉取失败要让 PersonaRail 说出来，不能吞掉之后渲染一个和「只有
        // 一个知识库」长得一模一样的空栏——那两种情况要用户做的事不同。
        setPersonasError(err instanceof Error ? err.message : '数字人列表加载失败')
      })
      .finally(() => {
        if (!cancelled) setPersonasLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 当前这一个数字人的详情（含引导问题）。只拉当前这个、切换时重拉——
  // 列表接口刻意不带 questions，那里每条引导问题都要探一次图，N 个数字人
  // 就是 N 份这样的开销。
  //
  // 这个 effect 不受 messages 约束：带 ?session= 接着聊的用户看不到引导区，
  // 却仍然会付一次这个请求的钱（后端对每个已确认关系组合各探一次图，串行）。
  // 没有按会话状态去掉它，是因为「现在有没有消息」在挂载那一刻还没定
  // （历史是异步加载的），按它开关会变成一个竞态；而用户随时可能点「新会话」
  // 回到需要引导问题的状态。代价记在
  // .superpowers/sdd/2026-09-08-guided-questions/progress.md。
  const [personaDetail, setPersonaDetail] = useState<PersonaDetail | null>(null)
  const [personaDetailError, setPersonaDetailError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPersonaDetail(null)
    setPersonaDetailError(null)
    fetchPersonaDetail('', tenantId)
      .then((detail) => {
        if (cancelled) return
        setPersonaDetail(detail)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        // 「这个数字人没配引导问题」和「引导问题没拉回来」在界面上长得
        // 一样，后者却是该报修的故障——必须说出来。
        setPersonaDetailError(err instanceof Error ? err.message : '数字人信息加载失败')
      })
    return () => {
      cancelled = true
    }
  }, [tenantId])

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
  } = useAgentChat(tenantId)

  // 数字人列表拉回来之后才能判断「当前租户是否在里面」——加载中/拉取失败
  // 时不下结论，避免在真相还没到手之前就误判成「你没权限」而短暂闪一下
  // 这条通知（拉取失败已经由 PersonaRail 自己说清楚了）。
  const currentPersonaAccessible =
    personasLoading || personasError !== null
      ? true
      : personas.some((persona) => persona.tenant_id === tenantId)

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
        {currentPersonaAccessible ? (
          <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col">
            {messages.length === 0 &&
              (personaDetailError !== null ? (
                <p role="status" className="mx-auto w-full max-w-2xl p-6 text-sm text-status-error-strong">
                  {personaDetailError}
                </p>
              ) : (
                personaDetail !== null && (
                  <GuidedQuestions
                    tagline={personaDetail.tagline}
                    questions={personaDetail.questions}
                    onAsk={sendQuestion}
                  />
                )
              ))}
            <ChatWindow messages={messages} />
            <ChatInput disabled={isSending} onSend={sendQuestion} />
          </main>
        ) : (
          <TenantInaccessibleNotice />
        )}
      </div>
      {/* 切换直接复用 TenantContext.setTenantId——它内部已经会 PUT
          /api/admin/auth/session/tenant。不另写一份切租户请求：两份实现
          会在「切了但没生效」这个 bug 上分叉。 */}
      <PersonaRail
        personas={personas}
        activeTenantId={tenantId}
        onSelect={setTenantId}
        loading={personasLoading}
        error={personasError}
      />
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
