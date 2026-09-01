import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronUp, LogOut, Settings, SquareArrowOutUpRight } from 'lucide-react'
import { ADMIN_ROUTES } from '../adminRoutes'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const itemClass =
  `flex min-h-[40px] cursor-pointer items-center gap-2 rounded-control px-3 text-sm text-ink transition hover:bg-interactive-hover ${focusRing}`

/**
 * 侧边栏底部的账号菜单。
 *
 * 收走的是账号级的动作，让侧边栏只剩工作流程。登出尤其需要挪进来：它
 * 此前和「返回前台」并排、同样的样式，只有文字不同——一个有代价的误触
 * 和一个无害的跳转长得一模一样。菜单里它被分隔线隔开，并用危险色。
 */
export function AccountMenu({ onLogout }: { onLogout: () => void }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    // 点菜单外面关掉。菜单浮在内容上方，不给一条退路就得回来点按钮。
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('pointerdown', onPointerDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative flex flex-col gap-1">
      {open && (
        <div
          role="menu"
          aria-label="账号"
          className="flex flex-col gap-1 rounded-card border border-subtle bg-card p-1 shadow-lg"
        >
          <Link to={ADMIN_ROUTES.settings} role="menuitem" className={itemClass} onClick={() => setOpen(false)}>
            <Settings aria-hidden="true" className="h-4 w-4" />
            设置
          </Link>
          <Link to="/" role="menuitem" className={itemClass} onClick={() => setOpen(false)}>
            <SquareArrowOutUpRight aria-hidden="true" className="h-4 w-4" />
            返回前台
          </Link>
          {/* 登出是有代价的误触：分隔线 + 危险色，跟上面两项拉开距离。 */}
          <div role="separator" className="my-0.5 border-t border-subtle" />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              onLogout()
            }}
            className={`${itemClass} text-status-error`}
          >
            <LogOut aria-hidden="true" className="h-4 w-4" />
            登出
          </button>
        </div>
      )}
      <button
        type="button"
        aria-label="账号"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((v) => !v)}
        className={`flex min-h-[44px] cursor-pointer items-center justify-between rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition ${focusRing}`}
      >
        账号
        <ChevronUp
          aria-hidden="true"
          className={`h-4 w-4 transition-transform ${open ? '' : 'rotate-180'}`}
        />
      </button>
    </div>
  )
}
