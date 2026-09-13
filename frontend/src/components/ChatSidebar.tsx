import { Trash2 } from 'lucide-react'
import { useState, type ReactNode } from 'react'
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
  /** 钉在侧边栏底部的东西（前台放账号块）。侧边栏自己不关心它是什么。 */
  footer?: ReactNode
}

export function ChatSidebar({
  sessions,
  sessionsError,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  footer,
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
          className={`min-h-[44px] w-full cursor-pointer rounded-control border border-subtle bg-accent-primary px-3 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 ${focusRing}`}
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
              <li key={session.session_id} className="flex items-stretch gap-1">
                <button
                  type="button"
                  onClick={() => onSelectSession(session.session_id)}
                  // 只有当前会话是实心的，其余安静。此前每一条都是
                  // `border + bg-paper + font-bold`——二十条会话就是二十个
                  // 描边加粗块，全都在喊，而"我现在在哪一条"反而看不出来。
                  className={`min-h-[44px] flex-1 cursor-pointer truncate rounded-control px-3 py-2 text-left text-sm transition ${focusRing} ${
                    isActive
                      ? 'bg-accent-primary font-medium text-on-accent'
                      : 'font-normal text-ink-soft hover:bg-interactive-hover hover:text-ink'
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
                    // 常驻可见而不是悬停才出现：悬停才出现的话，用触屏的人
                    // 根本够不着它（§hover-vs-tap）。但它不该跟会话本身抢
                    // 注意力——平时是弱色，悬停/聚焦时才转成危险色。
                    className={`flex min-h-[44px] w-10 flex-shrink-0 cursor-pointer items-center justify-center rounded-control text-ink-soft transition hover:bg-status-error-hover hover:text-status-error active:scale-95 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    <Trash2 aria-hidden="true" className="h-4 w-4" />
                  </button>
                </Tooltip>
              </li>
            )
          })}
        </ul>
      </div>
      {footer && <div className="border-t border-subtle p-3">{footer}</div>}
    </aside>
  )
}
