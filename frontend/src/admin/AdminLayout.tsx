import { NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { TenantSwitcher } from './TenantSwitcher'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `border-2 border-ink px-3 py-2.5 text-sm font-bold transition ${
    isActive ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink hover:bg-card'
  }`

export function AdminLayout() {
  const { sessionToken, logout } = useAdminAuth()

  if (!sessionToken) {
    return <Navigate to="/admin/login" replace />
  }

  return (
    <div className="flex min-h-dvh bg-paper">
      <aside className="flex w-56 flex-shrink-0 flex-col justify-between border-r-2 border-ink bg-card p-4">
        <nav className="flex flex-col gap-2">
          <NavLink to="/admin/documents" className={navLinkClass}>
            文档管理
          </NavLink>
          <NavLink to="/admin/graph-reviews" className={navLinkClass}>
            知识图谱审核
          </NavLink>
        </nav>
        <div className="flex flex-col gap-3">
          <TenantSwitcher />
          <a
            href="/"
            className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-center text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
          >
            返回前台
          </a>
          <button
            type="button"
            onClick={logout}
            className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none"
          >
            登出
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
