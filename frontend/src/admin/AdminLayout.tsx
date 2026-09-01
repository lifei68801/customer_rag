import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { DensityProvider } from './DensityContext'
import { DensitySwitcher } from './DensitySwitcher'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
import { CommandPalette } from './CommandPalette'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `rounded-control border border-subtle px-3 py-2.5 text-sm font-bold transition ${focusRing} ${
    isActive ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink hover:bg-interactive-hover'
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
  //
  // SkinProvider/ConfirmProvider 现在都挂载在 main.tsx 的根节点（站点级
  // 能力，前台/后台共用），这里不再重复包一层。
  return (
    <TenantProvider>
      <DensityProvider>
        {/* 挂在两个 Provider 内部——命令面板要调用 TenantContext /
            DensityContext / SkinContext 的方法，挂在外面拿不到。 */}
        <CommandPalette />
        <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
          <aside className="flex flex-col gap-3 border-b border-subtle bg-card p-4 md:w-56 md:flex-shrink-0 md:flex-col md:justify-between md:border-b-0 md:border-r">
            <nav className="flex flex-row flex-wrap gap-2 md:flex-col">
              <NavLink to="/admin/ontology" className={navLinkClass}>
                本体管理
              </NavLink>
              <NavLink to="/admin/documents" className={navLinkClass}>
                文档管理
              </NavLink>
              <NavLink to="/admin/data-entry" className={navLinkClass}>
                数据加工
              </NavLink>
            </nav>
            <div className="flex flex-row flex-wrap gap-3 md:flex-col">
              <TenantSwitcher />
              <SkinSwitcher />
              <DensitySwitcher />
              {/* 快捷键不告诉用户等于不存在。用 kbd 而不是纯文本，让它看起来
                  就是个按键提示。 */}
              <p className="text-xs text-ink-soft">
                按
                <kbd className="mx-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono">
                  ⌘K
                </kbd>
                打开命令面板
              </p>
              <Link
                to="/"
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-2 text-center text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
              >
                返回前台
              </Link>
              <button
                type="button"
                onClick={logout}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
              >
                登出
              </button>
            </div>
          </aside>
          <main className="flex-1 overflow-y-auto p-6">
            <Outlet />
          </main>
        </div>
      </DensityProvider>
    </TenantProvider>
  )
}
