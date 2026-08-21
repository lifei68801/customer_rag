import { useState } from 'react'
import { useConfirm } from '../admin/ConfirmContext'
import { Tooltip } from '../admin/Tooltip'
import { useToast } from '../admin/ToastContext'
import type { SessionSummary } from '../lib/sessionsApi'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface ChatSidebarProps {
  sessions: SessionSummary[]
  sessionsError: string | null
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onDeleteSession: (sessionId: string) => Promise<void>
}

function TrashIcon() {
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
      <path d="M3 6h18" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    </svg>
  )
}

export function ChatSidebar({
  sessions,
  sessionsError,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: ChatSidebarProps) {
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const confirm = useConfirm()
  const showToast = useToast()

  const handleDelete = async (session: SessionSummary) => {
    if (!(await confirm(`确定要删除会话「${session.title}」吗？此操作不可撤销。`))) return
    setDeletingId(session.session_id)
    setDeleteError(null)
    try {
      await onDeleteSession(session.session_id)
      showToast('已删除会话')
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : '删除会话失败')
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <aside className="flex max-h-64 flex-col border-b border-subtle bg-card md:h-auto md:max-h-none md:w-64 md:flex-shrink-0 md:border-b-0 md:border-r">
      <div className="border-b border-subtle p-3">
        <button
          type="button"
          onClick={onNewSession}
          className={`min-h-[44px] w-full cursor-pointer rounded-control border border-subtle bg-accent-pink px-3 py-2 text-sm font-bold text-on-accent shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          + 新建会话
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {sessionsError && (
          <p className="p-2 text-sm text-status-error">会话列表加载失败：{sessionsError}</p>
        )}
        {deleteError && <p className="p-2 text-sm text-status-error">{deleteError}</p>}
        {sessions.length === 0 && !sessionsError && (
          <p className="p-2 text-sm text-ink-soft">还没有历史会话，点击上方「+ 新建会话」开始</p>
        )}
        <ul className="flex flex-col gap-1.5">
          {sessions.map((session) => {
            const isActive = session.session_id === activeSessionId
            return (
              <li key={session.session_id} className="group flex items-stretch gap-1">
                <button
                  type="button"
                  onClick={() => onSelectSession(session.session_id)}
                  className={`min-h-[44px] flex-1 cursor-pointer truncate rounded-control border border-subtle px-3 py-2 text-left text-sm font-bold transition ${focusRing} ${
                    isActive
                      ? 'bg-accent-yellow text-on-accent shadow-soft-sm'
                      : 'bg-paper text-ink hover:bg-interactive-hover'
                  }`}
                  title={session.title}
                >
                  {session.title}
                </button>
                <Tooltip label="删除会话">
                  <button
                    type="button"
                    onClick={() => handleDelete(session)}
                    disabled={deletingId === session.session_id}
                    aria-label={`删除会话「${session.title}」`}
                    className={`flex min-h-[44px] w-10 flex-shrink-0 cursor-pointer items-center justify-center rounded-control border border-subtle bg-paper text-ink shadow-soft-sm transition hover:bg-status-error-hover active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    <TrashIcon />
                  </button>
                </Tooltip>
              </li>
            )
          })}
        </ul>
      </div>
    </aside>
  )
}
