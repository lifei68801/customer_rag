import type { Persona } from '../lib/personasApi'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface PersonaRailProps {
  personas: Persona[]
  activeTenantId: string | null
  onSelect: (tenantId: string) => void
  loading: boolean
  error: string | null
}

/**
 * 前台右栏：这个账号能访问的数字人。
 *
 * 只有一个数字人、且那一个恰好就是当前租户时才不渲染——一个选项的选择器
 * 不是选择器，是噪音，还会误导用户以为「还有别的，只是我没权限」。
 *
 * 但当前租户不在这个列表里时，哪怕列表只有一个数字人也必须渲染：那种情况
 * 只会发生在——账号被显式授权了别的租户、但会话当前挂着的租户（多半是
 * admin_users.tenant_id 那个回退值）不在授权范围内。此时这一栏是用户唯一
 * 能自己纠正过去的地方；连这唯一一个都藏起来，人是真的被锁死在一个只能
 * 看见 403、却无处可点的页面上，见 2026-09-08 全分支评审 Important 1。
 * 不做「自动切到列表第一个」——那是替用户做决定，本仓库明确不这么干
 * （理由见 admin/useTenants.ts）。
 *
 * 这个判断在组件内部而不是调用方，是为了让「什么时候不显示」只有一处定义。
 *
 * 拉取失败时说出来而不是渲染空栏：空栏和失败在界面上长得一样，
 * 而它们要用户做的事完全不同（一个是「你只有一个知识库」，一个是「重试」）。
 */
export function PersonaRail({
  personas,
  activeTenantId,
  onSelect,
  loading,
  error,
}: PersonaRailProps) {
  if (error !== null) {
    return (
      <aside
        aria-label="数字人"
        className="border-t border-subtle bg-card p-3 md:w-44 md:flex-shrink-0 md:border-l md:border-t-0"
      >
        <p role="status" className="text-sm text-ink-soft">
          {error}
        </p>
      </aside>
    )
  }
  if (loading) return null
  const currentIsListed = personas.some((persona) => persona.tenant_id === activeTenantId)
  if (personas.length <= 1 && currentIsListed) return null
  return (
    <aside
      aria-label="数字人"
      className="flex flex-col gap-1 border-t border-subtle bg-card p-3 md:w-44 md:flex-shrink-0 md:border-l md:border-t-0"
    >
      <p className="mb-1 px-1 text-xs font-bold uppercase tracking-wide text-ink-soft">数字人</p>
      {personas.map((persona) => {
        const isActive = persona.tenant_id === activeTenantId
        return (
          <button
            key={persona.tenant_id}
            type="button"
            aria-current={isActive ? 'true' : undefined}
            onClick={() => onSelect(persona.tenant_id)}
            className={`flex cursor-pointer items-center gap-2 rounded-control px-2 py-2 text-left text-sm transition ${focusRing} ${
              isActive
                ? 'bg-accent-primary font-bold text-on-accent'
                : 'text-ink hover:bg-interactive-hover'
            }`}
          >
            {/* 没配过脸时给一个占位符号而不是空白：空白让用户以为这一行坏了。 */}
            <span aria-hidden="true" className="text-lg leading-none">
              {persona.avatar || '◍'}
            </span>
            <span className="min-w-0 flex-1 truncate">{persona.name}</span>
          </button>
        )
      })}
    </aside>
  )
}
