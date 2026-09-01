import { ChevronDown, Menu, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Navigate, Outlet, useLocation } from 'react-router-dom'
import { NAV_GROUPS, NAV_STANDALONE } from '../adminRoutes'
import { useAdminAuth } from './useAdminAuth'
import { DensityProvider } from './DensityContext'
import { DensitySwitcher } from './DensitySwitcher'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
import { CommandPalette } from './CommandPalette'
import { useNavGroups } from './useNavGroups'
import { VersionSwitcher } from './VersionSwitcher'
import { NavBadge } from './NavBadge'
import { useNavBadges } from './useNavBadges'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-2 rounded-control border border-subtle px-3 py-2.5 text-sm font-bold transition ${focusRing} ${
    isActive ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

/**
 * 侧边栏导航。
 *
 * 单独成一个组件是因为它要用 useAdminTenant（徽标按租户算）——而
 * TenantProvider 是 AdminLayout 自己渲染的，同一个组件体里拿不到自己
 * 提供的 context。
 */
function AdminNav() {
  const { pathname, search } = useLocation()
  const { isExpanded, toggle } = useNavGroups(pathname)
  const badges = useNavBadges()

  return (
            <nav aria-label="后台导航" className="flex flex-col gap-1">
              {NAV_GROUPS.map((group) => {
                const expanded = isExpanded(group)
                const groupCount = group.items.reduce((sum, i) => sum + (badges[i.path] ?? 0), 0)
                return (
                  <div key={group.id} className="flex flex-col gap-1">
                    <button
                      type="button"
                      aria-expanded={expanded}
                      onClick={() => toggle(group)}
                      className={`flex min-h-[36px] cursor-pointer items-center justify-between rounded-control px-2 text-xs font-bold uppercase tracking-wide text-ink-soft transition hover:bg-interactive-hover ${focusRing}`}
                    >
                      {group.label}
                      {/* 组头上是组内的合计。收起时这是唯一的提醒——不展开
                          就看不到待办，等于没提醒。 */}
                      {!expanded && <NavBadge label={group.label} count={groupCount} />}
                      <ChevronDown
                        aria-hidden="true"
                        className={`ml-2 h-3.5 w-3.5 flex-shrink-0 transition-transform ${expanded ? '' : '-rotate-90'}`}
                      />
                    </button>
                    {expanded && group.id === 'model' && <VersionSwitcher />}
                    {expanded &&
                      group.items.map((item) => (
                        <NavLink
                          key={item.path}
                          // 只有建模组内部带上 version：它对别的组没有意义，
                          // 带着跑只会让 URL 说谎——看起来那些页面也有版本概念。
                          to={{ pathname: item.path, search: group.id === 'model' ? search : '' }}
                          className={navLinkClass}
                        >
                          <item.icon aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
                          {item.label}
                          <NavBadge label={item.label} count={badges[item.path]} />
                        </NavLink>
                      ))}
                  </div>
                )
              })}
              {/* 分隔线之下是流程外的目的地。它不归任何组，因为它不是
                  「最后一步」，是每一步的落点。 */}
              <div className="my-1 border-t border-subtle" />
              {NAV_STANDALONE.map((item) => (
                <NavLink key={item.path} to={item.path} className={navLinkClass}>
                  <item.icon aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
                  {item.label}
                  {/* 规模，不是待办——顺带回答了「这个租户到底有多少数据」，
                      这个问题此前必须点进去才知道。 */}
                  <NavBadge label={item.label} count={badges[item.path]} kind="scale" />
                </NavLink>
              ))}
            </nav>
  )
}

export function AdminLayout() {
  const { sessionToken, logout } = useAdminAuth()
  const { pathname } = useLocation()
  const [drawerOpen, setDrawerOpen] = useState(false)

  // 换页面就关：抽屉的用途是选一个去处，选完还挡着等于每次都要多点一下。
  // 监听 pathname 而不是只在链接上挂 onClick——页面内部的跳转（空状态里的
  // 链接、⌘K）同样算选完了去处。
  useEffect(() => setDrawerOpen(false), [pathname])

  // 抽屉盖住内容时，键盘用户需要一条不用找关闭按钮的退路。
  useEffect(() => {
    if (!drawerOpen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawerOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [drawerOpen])

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
          {/* 窄屏专用的开关条。宽屏上侧边栏常驻，不需要它。 */}
          <div className="flex items-center gap-2 border-b border-subtle bg-card p-3 md:hidden">
            <button
              type="button"
              aria-label="导航菜单"
              aria-expanded={drawerOpen}
              onClick={() => setDrawerOpen((v) => !v)}
              className={`flex min-h-[44px] min-w-[44px] cursor-pointer items-center justify-center rounded-control border border-subtle bg-paper text-ink transition active:scale-95 ${focusRing}`}
            >
              {drawerOpen ? <X aria-hidden="true" className="h-5 w-5" /> : <Menu aria-hidden="true" className="h-5 w-5" />}
            </button>
            <span className="font-mono text-sm font-bold text-ink">管理后台</span>
          </div>
          {/* data-open 是语义状态，显示与否交给断点：宽屏 md:flex 永远展开，
              窄屏由它决定。用 hidden 而不是不渲染，是为了让宽屏那份始终在
              DOM 里——否则跨断点缩放窗口时侧边栏的展开状态会被重置。 */}
          <aside
            data-open={drawerOpen}
            className={`flex-col gap-3 border-b border-subtle bg-card p-4 md:flex md:w-56 md:flex-shrink-0 md:justify-between md:border-b-0 md:border-r ${
              drawerOpen ? 'flex' : 'hidden'
            }`}
          >
            {/* 租户排在最上面：它决定后面看到的每一条数据。排在导航下面的
                话，用户会先挑页面、再发现自己在错的租户里，得重来一次。 */}
            <TenantSwitcher />
            <AdminNav />
            <div className="flex flex-row flex-wrap gap-3 md:flex-col">
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
