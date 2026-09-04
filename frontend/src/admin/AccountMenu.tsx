import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Building2,
  Check,
  ChevronUp,
  LogOut,
  Settings,
  Users,
} from 'lucide-react'
import { ADMIN_ROUTES } from '../adminRoutes'
import { useTenants } from './useTenants'
import { useAdminAuth } from './useAdminAuth'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const itemClass = `flex min-h-[40px] w-full cursor-pointer items-center gap-2 rounded-control px-3 text-left text-sm text-ink transition hover:bg-interactive-hover ${focusRing}`

/**
 * 侧边栏左下角：当前租户 + 账号动作。
 *
 * 租户切换收进菜单，但**当前租户名常驻显示在触发按钮上**。这两件事必须
 * 一起做：租户是数据作用域，决定你看到的每一条数据和写操作落到哪里，
 * 看不到它的话用户会在错的租户里导一批数据，而那个错误不可撤销。名字
 * 一直在屏幕上，切换动作藏一层就没有代价了。
 *
 * click 触发而不是 hover：hover 菜单在触屏上打不开，而且左下角这个位置
 * 容易被路过——用户去点状态栏或滚动条时就会扫过。
 *
 * 这里只做"我是谁、我在哪个租户"以及去处。新建租户不在这儿——它是一次性
 * 的管理动作，归「租户管理」页；同一个动作留两个入口，改起来就得记得改
 * 两处。
 */
export function AccountMenu({
  onLogout,
  showManagementLinks = true,
}: {
  onLogout: () => void
  /**
   * 账号管理 / 租户管理这两项要不要渲染。
   *
   * 后台默认要（那是管知识库的地方）；前台传 false——把管理入口塞进问答
   * 界面，等于把建模→接入→审核这条流程的入口散回一个不属于它的页面。
   * 组件只有一份、两处渲染，内容按场景裁剪；照抄一份到前台的话两份会
   * 各自漂移。
   */
  showManagementLinks?: boolean
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const { options, current, tenantId, setTenantId } = useTenants()
  const { role, username, currentTenantId } = useAdminAuth()
  const isAdmin = role === 'admin'
  // 会话里到底有没有当前租户，只有 whoami 说了算。useTenants 给的 tenantId
  // 经过 TenantContext 的兜底值（'demo'），会话是空的时候它照样是个租户名——
  // 拿它来显示，界面就会一边说"请先选择一个租户"、一边声称"当前 demo"。
  // 显示的和生效的一旦脱钩，用户会以为自己已经在某个租户里了。
  const hasCurrentTenant = currentTenantId !== null
  const currentLabel = hasCurrentTenant ? current?.name ?? tenantId : '未选择租户'

  const close = () => setOpen(false)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    // 点菜单外面关掉。菜单浮在内容上方，不给一条退路就得回来点按钮。
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) close()
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
          aria-label="账号与租户"
          className="flex max-h-[70vh] flex-col gap-1 overflow-y-auto rounded-card border border-subtle bg-card p-1 shadow-lg"
        >
          {/* 租户区整块只对 admin 渲染。member 的租户是登录时绑定的，
              这里没有它可选的东西——不是把按钮藏起来，是这个能力对它
              不存在（后端会 403）。 */}
          {isAdmin && (
            <>
            <p className="px-3 pt-1 text-xs font-bold uppercase tracking-wide text-ink-faint">
              租户
            </p>
            {options.map((tenant) => (
              <button
                key={tenant.tenant_id}
                type="button"
                role="menuitemradio"
                aria-checked={hasCurrentTenant && tenant.tenant_id === tenantId}
                onClick={() => {
                  setTenantId(tenant.tenant_id)
                  close()
                }}
                className={itemClass}
              >
                {/* 勾不是唯一的信号：aria-checked 让屏幕阅读器也听得到当前是哪个。 */}
                <Check
                  aria-hidden="true"
                  className={`h-4 w-4 flex-shrink-0 ${
                    hasCurrentTenant && tenant.tenant_id === tenantId ? '' : 'invisible'
                  }`}
                />
                {tenant.name}
              </button>
            ))}

            </>
          )}

          <div role="separator" className="my-0.5 border-t border-subtle" />
          {isAdmin && showManagementLinks && (
            <Link
              to={ADMIN_ROUTES.accounts}
              role="menuitem"
              className={itemClass}
              onClick={close}
            >
              <Users aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
              账号管理
            </Link>
          )}
          {isAdmin && showManagementLinks && (
            <Link
              to={ADMIN_ROUTES.tenants}
              role="menuitem"
              className={itemClass}
              onClick={close}
            >
              <Building2 aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
              租户管理
            </Link>
          )}
          <Link to={ADMIN_ROUTES.settings} role="menuitem" className={itemClass} onClick={close}>
            <Settings aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
            设置
          </Link>

          {/* 登出是有代价的误触：再隔一条线 + 危险色，跟上面拉开距离。 */}
          <div role="separator" className="my-0.5 border-t border-subtle" />
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              close()
              onLogout()
            }}
            className={`${itemClass} text-status-error`}
          >
            <LogOut aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
            登出
          </button>
        </div>
      )}

      <button
        type="button"
        // 可访问名带上当前租户和账号：aria-label 会盖掉按钮里的可见文字，
        // 只写租户的话屏幕阅读器用户听不到自己是谁——而下面那两行正是为了
        // 让人看得出「我是谁、我在哪个租户」。
        aria-label={`账号与租户，${
          hasCurrentTenant ? `当前 ${currentLabel}` : currentLabel
        }，登录为 ${username}`}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => (open ? close() : setOpen(true))}
        className={`flex min-h-[44px] cursor-pointer items-center gap-2 rounded-control border border-subtle bg-paper px-3 text-sm text-ink transition hover:bg-interactive-hover ${focusRing}`}
      >
        <Building2 aria-hidden="true" className="h-4 w-4 flex-shrink-0 text-ink-soft" />
        {/* 租户名在主行：它是数据作用域，弄错了不会报错，只会安静地把数据
            写到别处。身份弄错则会立刻撞上权限错误。用户名在副行——登录系统
            做完了，界面上却看不出自己是谁，同样说不过去。 */}
        <span className="flex min-w-0 flex-1 flex-col text-left">
          <span className={`truncate font-bold ${hasCurrentTenant ? '' : 'text-ink-soft'}`}>
            {currentLabel}
          </span>
          <span className="truncate text-xs font-normal text-ink-soft">{username}</span>
        </span>
        <ChevronUp
          aria-hidden="true"
          className={`h-4 w-4 flex-shrink-0 transition-transform ${open ? '' : 'rotate-180'}`}
        />
      </button>
    </div>
  )
}
