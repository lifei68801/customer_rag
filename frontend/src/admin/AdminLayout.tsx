import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `border-2 border-ink px-3 py-2.5 text-sm font-bold transition ${focusRing} ${
    isActive ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink hover:bg-card'
  }`

export function AdminLayout() {
  const { sessionToken, logout } = useAdminAuth()

  if (!sessionToken) {
    return <Navigate to="/admin/login" replace />
  }

  // TenantProvider 必须包住侧边栏（租户下拉框）和 <Outlet />（各页面）两者，
  // 它们才共用同一份租户状态；只包其中一边等于没修。
  //
  // 侧边栏在窄屏（<768px）下改成顶部横条：flex-col 让 aside 和 main 上下堆叠、
  // aside 内部改 flex-row 排布，避免固定 w-56 的侧边栏在手机宽度下把主内容区
  // 挤到不到 150px 宽、必须横向滚动才能看全的问题。
  return (
    <TenantProvider>
      <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
        <aside className="flex flex-col gap-3 border-b-2 border-ink bg-card p-4 md:w-56 md:flex-shrink-0 md:flex-col md:justify-between md:border-b-0 md:border-r-2">
          <nav className="flex flex-row flex-wrap gap-2 md:flex-col">
            <NavLink to="/admin/documents" className={navLinkClass}>
              文档管理
            </NavLink>
            <NavLink to="/admin/graph-reviews" className={navLinkClass}>
              知识图谱审核
            </NavLink>
            <NavLink to="/admin/terms" className={navLinkClass}>
              术语库管理
            </NavLink>
          </nav>
          <div className="flex flex-row flex-wrap gap-3 md:flex-col">
            <TenantSwitcher />
            <Link
              to="/"
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-center text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              返回前台
            </Link>
            <button
              type="button"
              onClick={logout}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              登出
            </button>
          </div>
        </aside>
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </TenantProvider>
  )
}
