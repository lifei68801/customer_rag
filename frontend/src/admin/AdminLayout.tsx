import { Building2, ChevronDown, Menu, SquareArrowOutUpRight, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Navigate, Outlet, useLocation } from 'react-router-dom'
import { NAV_GROUPS, groupIdForPath, routeRequiresTenant } from '../adminRoutes'
import { useAdminAuth } from './useAdminAuth'
import { DensityProvider } from './DensityContext'
import { TenantProvider } from './TenantContext'
import { CommandPalette } from './CommandPalette'
import { AccountMenu } from './AccountMenu'
import { useNavGroups } from './useNavGroups'
import { VersionSwitcher } from './VersionSwitcher'
import { NavBadge } from './NavBadge'
import { useNavBadges } from './useNavBadges'
import { commandPaletteHint } from './shortcutHint'
import { EmptyState } from './EmptyState'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/**
 * 导航项的样式。
 *
 * 只有当前项是实心的，其余安静。此前每一项都是 `border + bg-paper +
 * font-bold` 的描边实心按钮——十六个这样的块竖着堆，全都在喊，结果"我现在
 * 在哪"反而看不出来。侧边栏的工作是回答这个问题，不是把每个链接都做成 CTA。
 *
 * 当前项另外加一条左侧竖条：不靠颜色单独表意（§color-not-only），用了
 * 反色的人和把界面调成高对比的人都还能看出来。
 */
const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-2.5 rounded-control px-3 py-2 text-sm transition ${focusRing} ${
    isActive
      ? 'border-l-2 border-accent-primary bg-accent-primary/10 pl-[10px] font-medium text-accent-primary'
      : 'font-normal text-ink-soft hover:bg-interactive-hover hover:text-ink'
  }`

/**
 * 侧边栏导航。
 *
 * 单独成一个组件是因为它要用 useAdminTenant（徽标按租户算）——而
 * TenantProvider 是 AdminLayout 自己渲染的，同一个组件体里拿不到自己
 * 提供的 context。
 */
/**
 * 换页之后把焦点挪到主内容区。
 *
 * SPA 换页时浏览器不动焦点——它还停在刚点过的那条导航链接上。于是用键盘
 * 或读屏的人每进一个页面，都要从侧边栏第 N 项开始，重新 Tab 穿过剩下的
 * 十几条链接才够得着内容；读屏软件也不会播报"页面变了"。
 *
 * 只在 pathname 变化时动，不在首次挂载时动：刚进来就抢焦点会打断用户自己
 * 的操作（比如他正要点账号菜单）。
 */
function useFocusMainOnNavigate(pathname: string) {
  const mainRef = useRef<HTMLElement>(null)
  const firstRender = useRef(true)

  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    mainRef.current?.focus()
  }, [pathname])

  return mainRef
}

function AdminNav() {
  const { pathname, search } = useLocation()
  const { isExpanded, toggle } = useNavGroups(pathname)
  const currentGroup = groupIdForPath(pathname)
  const badges = useNavBadges()

  return (
            <nav aria-label="后台导航" className="flex flex-col gap-1">
              {NAV_GROUPS.map((group) => {
                const expanded = isExpanded(group)
                // 只把 todo 加进组头合计。规模数（实体总数）混进来的话，
                // 收起的「结果预览」会显示「20017 项待处理」——而那不是
                // 任何人要处理的东西，它还会把审核那两个真待办的数字淹掉。
                const groupCount = group.items.reduce(
                  (sum, i) => sum + (badges[i.path]?.kind === 'todo' ? badges[i.path].count : 0),
                  0,
                )
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
                    {/* 版本切换器只在**当前就在建模组**时出现，不是"这个组
                        展开着"就出现——它只对本体结构和本体图有意义。此前
                        这两件事碰巧等价（只有当前组会展开），分组默认全展开
                        之后就不再等价了。 */}
                    {group.id === 'ontology' && currentGroup === 'ontology' && <VersionSwitcher />}
                    {expanded &&
                      group.items.map((item) => (
                        <NavLink
                          key={item.path}
                          // 只有本体创建组内部带上 version：它对别的组没有意义，
                          // 带着跑只会让 URL 说谎——看起来那些页面也有版本概念。
                          to={{ pathname: item.path, search: group.id === 'ontology' ? search : '' }}
                          className={navLinkClass}
                        >
                          <item.icon aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
                          {item.label}
                          <NavBadge
                            label={item.label}
                            count={badges[item.path]?.count}
                            kind={badges[item.path]?.kind}
                          />
                        </NavLink>
                      ))}
                  </div>
                )
              })}
            </nav>
  )
}

/**
 * 还没选定租户时，依赖租户的页面落在这里。
 *
 * admin 的 tenant_id 恒为 null，当前租户要显式切过一次才有值。此前
 * useTenants 会替他选一个（租户列表里的第一个），用户从没选过、也没被告知
 * ——他以为自己在主数据租户里，实际在示例数据租户里，直到某个操作被一条
 * 从没见过的数据挡住。选择权交回去之后，这一屏就是那个选择的落点。
 *
 * 拦在布局层而不是改十几个页面：每一页各写一遍空态，漏掉的那一页会拿
 * TenantContext 的兜底租户去取数——正是要修的那个静默失败。
 */
function NoTenantNotice() {
  return (
    <EmptyState
      icon={Building2}
      title="请先选择一个租户"
      action="你的账号可以访问多个租户，请先选一个再继续——切换器在左下角的账号块里。"
    />
  )
}

export function AdminLayout() {
  const { status, logout, currentTenantId } = useAdminAuth()
  const { pathname } = useLocation()
  const mainRef = useFocusMainOnNavigate(pathname)
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

  // 会话状态未知时（whoami 还没回来）两个方向都不能走：渲染后台会让
  // Cookie 已失效的人看到一屏取不到数的界面，跳登录页则会把还登录着的人
  // 一脚踢出去。
  if (status === 'loading') {
    return null
  }
  if (status === 'anonymous') {
    return <Navigate to="/admin/login" replace />
  }

  // TenantProvider 必须包住侧边栏（租户下拉框）和 <Outlet />（各页面）两者，
  // 它们才共用同一份租户状态；只包其中一边等于没修。
  //
  // 顶栏在最外层、跨满宽度，和前台那条对齐；侧边栏从它下面开始。窄屏
  // （<768px）下侧边栏收成抽屉，由顶栏左端的汉堡开关——固定 w-56 的侧边栏
  // 在手机宽度下会把主内容区挤到不到 150px，必须横向滚动才看得全。
  //
  // SkinProvider/ConfirmProvider 现在都挂载在 main.tsx 的根节点（站点级
  // 能力，前台/后台共用），这里不再重复包一层。
  return (
    <TenantProvider>
      <DensityProvider>
        {/* 挂在两个 Provider 内部——命令面板要调用 TenantContext /
            DensityContext / SkinContext 的方法，挂在外面拿不到。 */}
        <CommandPalette />
        {/* 跳到主内容：平时看不见，Tab 一下就出现在左上角。
            没有它的话，用键盘的人每换一页都要从侧边栏第一条开始，穿过
            十六条导航链接才够得着内容。放在最前面，它必须是第一个可聚焦
            元素，否则就失去意义。 */}
        <a
          href="#main-content"
          className={`sr-only z-50 rounded-control bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent focus:not-sr-only focus:absolute focus:left-3 focus:top-3 ${focusRing}`}
        >
          跳到主内容
        </a>
        <div className="flex min-h-dvh flex-col bg-paper">
          {/* 顶栏跨满整个宽度，和前台那条同一个位置、同一个形状。右端是
              「返回前台」，正对前台右端的「管理后台」——两个方向的入口落
              在屏幕上的同一个点，回去这件事不用重新找。它常驻在每一页，
              没有哪一页是走进去出不来的。 */}
          <header
            data-testid="admin-topbar"
            className="flex items-center justify-between gap-2 border-b border-subtle bg-card px-4 py-3 md:px-6 md:py-4"
          >
            <div className="flex items-center gap-2">
              {/* 抽屉开关只在窄屏有意义——宽屏侧边栏常驻。 */}
              <button
                type="button"
                aria-label="导航菜单"
                aria-expanded={drawerOpen}
                onClick={() => setDrawerOpen((v) => !v)}
                className={`flex min-h-[44px] min-w-[44px] cursor-pointer items-center justify-center rounded-control border border-subtle bg-paper text-ink transition active:scale-95 md:hidden ${focusRing}`}
              >
                {drawerOpen ? <X aria-hidden="true" className="h-5 w-5" /> : <Menu aria-hidden="true" className="h-5 w-5" />}
              </button>
              <span className="font-mono font-semibold text-ink">管理后台</span>
            </div>
            <Link
              to="/"
              className={`flex min-h-[44px] cursor-pointer items-center gap-1.5 rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              <SquareArrowOutUpRight aria-hidden="true" className="h-4 w-4" />
              返回前台
            </Link>
          </header>
          <div className="flex flex-1 flex-col md:flex-row">
            {/* data-open 是语义状态，显示与否交给断点：宽屏 md:flex 永远展开，
                窄屏由它决定。用 hidden 而不是不渲染，是为了让宽屏那份始终在
                DOM 里——否则跨断点缩放窗口时侧边栏的展开状态会被重置。 */}
            <aside
              data-open={drawerOpen}
              className={`flex-col gap-3 border-b border-subtle bg-card p-4 md:flex md:w-56 md:flex-shrink-0 md:justify-between md:border-b-0 md:border-r ${
                drawerOpen ? 'flex' : 'hidden'
              }`}
            >
              <AdminNav />
              <div className="flex flex-col gap-3">
                {/* 快捷键不告诉用户等于不存在。用 kbd 而不是纯文本，让它看起来
                    就是个按键提示。修饰键按平台算——监听两个键都认，写死
                    ⌘K 的话 Windows 用户照着按不出来，然后以为功能坏了。 */}
                <p className="text-xs text-ink-soft">
                  按
                  <kbd
                    data-testid="command-palette-hint"
                    className="mx-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono"
                  >
                    {commandPaletteHint()}
                  </kbd>
                  打开命令面板
                </p>
                {/* 当前租户 + 账号动作。租户名常驻在按钮上——它是数据作用域，
                    看不到它的话用户会在错的租户里导数据。 */}
                <AccountMenu onLogout={logout} />
              </div>
            </aside>
            <main
              id="main-content"
              ref={mainRef}
              // tabIndex={-1}：让它能被脚本聚焦，但不进 Tab 顺序（进了的话
              // 每页多一次无意义的停留）。
              tabIndex={-1}
              className={`min-w-0 flex-1 overflow-y-auto p-6 ${focusRing}`}
            >
              {/* 侧边栏和账号块留在原位（上面那个 aside 不受这个分支影响）
                  ——租户切换器就在账号块里，把它一起挡住等于没有退路。 */}
              {currentTenantId === null && routeRequiresTenant(pathname) ? (
                <NoTenantNotice />
              ) : (
                <Outlet />
              )}
            </main>
          </div>
        </div>
      </DensityProvider>
    </TenantProvider>
  )
}
