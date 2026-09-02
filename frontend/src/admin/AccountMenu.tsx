import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  Building2,
  Check,
  ChevronUp,
  LogOut,
  Plus,
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
 */
export function AccountMenu({ onLogout }: { onLogout: () => void }) {
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const { options, current, tenantId, setTenantId, create, error, setError } = useTenants()
  const { role, username } = useAdminAuth()
  const isAdmin = role === 'admin'

  const close = () => {
    setOpen(false)
    setCreating(false)
    setError(null)
  }

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

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !newId.trim() || !newName.trim()) return
    setBusy(true)
    try {
      await create(newId.trim(), newName.trim())
      setNewId('')
      setNewName('')
      close()
    } catch (err) {
      setError(err instanceof Error ? err.message : '新建租户失败')
    } finally {
      setBusy(false)
    }
  }

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
                aria-checked={tenant.tenant_id === tenantId}
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
                    tenant.tenant_id === tenantId ? '' : 'invisible'
                  }`}
                />
                {tenant.name}
              </button>
            ))}

            {!creating && (
              <button
                type="button"
                role="menuitem"
                onClick={() => setCreating(true)}
                className={itemClass}
              >
                <Plus aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
                新建租户
              </button>
            )}
            {creating && (
              // 表单就地展开，菜单不关——还没填完就关掉等于让用户重来。
              <form
                onSubmit={handleCreate}
                className="flex flex-col gap-2 rounded-control bg-paper p-2"
              >
                <input
                  value={newId}
                  onChange={(event) => setNewId(event.target.value)}
                  placeholder="tenant_id"
                  aria-label="新租户 ID"
                  autoFocus
                  className={`rounded-control border border-subtle bg-card px-2 py-1.5 text-xs text-ink placeholder:text-ink-soft ${focusRing}`}
                />
                <input
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder="显示名"
                  aria-label="新租户显示名"
                  className={`rounded-control border border-subtle bg-card px-2 py-1.5 text-xs text-ink placeholder:text-ink-soft ${focusRing}`}
                />
                <div className="flex gap-2">
                  <button
                    type="submit"
                    disabled={busy || !newId.trim() || !newName.trim()}
                    className={`min-h-[32px] flex-1 cursor-pointer rounded-control border border-subtle bg-accent-primary px-2 text-xs font-bold text-on-accent transition disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    {busy ? '创建中…' : '创建'}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setCreating(false)
                      setError(null)
                    }}
                    disabled={busy}
                    className={`min-h-[32px] cursor-pointer rounded-control border border-subtle bg-card px-2 text-xs font-bold text-ink transition ${focusRing}`}
                  >
                    取消
                  </button>
                </div>
              </form>
            )}
            {error && (
              <p role="alert" className="px-3 text-xs text-status-error">
                {error}
              </p>
            )}
            </>
          )}

          <div role="separator" className="my-0.5 border-t border-subtle" />
          {isAdmin && (
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
        // 可访问名带上当前租户：屏幕阅读器用户不看颜色也知道自己在哪。
        aria-label={`账号与租户，当前 ${current?.name ?? tenantId}`}
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
          <span className="truncate font-bold">{current?.name ?? tenantId}</span>
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
