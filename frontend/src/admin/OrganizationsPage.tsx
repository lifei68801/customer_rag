import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Building2 } from 'lucide-react'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'

interface Organization {
  org_id: string
  name: string
  status: string
  tenant_ids: string[]
}

interface Tenant {
  tenant_id: string
  name: string
  status: string
}

const card = 'rounded-card border border-subtle bg-card p-4'
const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft ${focusRing}`

/**
 * 组织管理（spec 裁决补充）。
 *
 * 组织只是租户之上的一层归拢，不参与任何隔离判据——隔离依旧是 tenant_id。
 * 这一页做两件事：建组织、把租户挂进去。后端 API 阶段一就有了，但一直没有
 * 页面调它，存量租户因此 org_id 全空，看板"按组织分组"的能力用不上。
 *
 * **没挂组织的租户单独列一组**：不列的话用户找不到它们，也就永远挂不上。
 */
export function OrganizationsPage() {
  const { sessionToken, role } = useAdminAuth()
  const isAdmin = role === 'admin'
  const [organizations, setOrganizations] = useState<Organization[] | null>(null)
  const [tenants, setTenants] = useState<Tenant[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    document.title = `${PAGE_TITLES.organizations} · 管理后台`
  }, [])

  const load = useCallback(async () => {
    if (!sessionToken || !isAdmin) return
    try {
      const [orgResponse, tenantResponse] = await Promise.all([
        adminFetch('/api/admin/organizations', sessionToken),
        // 停用的租户也列：它仍然属于某个组织，看不到就没法调整它的归属。
        adminFetch('/api/admin/tenants?include_disabled=true', sessionToken),
      ])
      if (!orgResponse.ok) {
        const body = await orgResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '组织列表没拉到'))
      }
      if (!tenantResponse.ok) {
        const body = await tenantResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '租户列表没拉到'))
      }
      setOrganizations(((await orgResponse.json()) as { organizations: Organization[] }).organizations)
      setTenants(((await tenantResponse.json()) as { tenants: Tenant[] }).tenants)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '没拉到')
      setOrganizations((prev) => prev ?? [])
      setTenants((prev) => prev ?? [])
    }
  }, [sessionToken, isAdmin])

  useEffect(() => {
    void load()
  }, [load])

  if (!isAdmin) {
    return (
      <div data-testid="no-permission" className="flex flex-col gap-2">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.organizations}</h1>
        {/* 不用 404：404 会让人以为链接坏了而反复重试。 */}
        <p className="text-sm text-ink-soft">这个页面只有管理员能用。</p>
      </div>
    )
  }

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken) return
    setBusy(true)
    setError(null)
    try {
      const response = await adminFetch('/api/admin/organizations', sessionToken, {
        method: 'POST',
        body: JSON.stringify({ org_id: newId.trim(), name: newName.trim() }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '没建成'))
      }
      setNewId('')
      setNewName('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '没建成')
    } finally {
      setBusy(false)
    }
  }

  const handleStatus = async (org: Organization, next: 'active' | 'disabled') => {
    if (!sessionToken) return
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/organizations/${encodeURIComponent(org.org_id)}/${
          next === 'disabled' ? 'disable' : 'enable'
        }`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '没改成'))
      }
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '没改成')
    }
  }

  const handleDelete = async (org: Organization) => {
    if (!sessionToken) return
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/organizations/${encodeURIComponent(org.org_id)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        // 后端那句话原样显示：「还有 2 个租户，先把它们移出去」比一句
        // 「删除失败」有用得多——它直接说出了下一步该做什么。
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '没删掉'))
      }
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '没删掉')
    }
  }

  const handleAssign = async (tenant: Tenant, orgId: string) => {
    if (!sessionToken) return
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/tenants/${encodeURIComponent(tenant.tenant_id)}/organization`,
        sessionToken,
        {
          method: 'PUT',
          // **空选项发 null，不是空串。** 后端把 null 当"移出组织"；空串会被
          // 当成一个叫 "" 的组织，挂上去之后这个租户在任何一张卡里都找不到。
          body: JSON.stringify({ org_id: orgId === '' ? null : orgId }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '没挂上'))
      }
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '没挂上')
    }
  }

  const loading = organizations === null || tenants === null
  const tenantById = new Map((tenants ?? []).map((t) => [t.tenant_id, t]))
  const assigned = new Set((organizations ?? []).flatMap((o) => o.tenant_ids))
  const unassigned = (tenants ?? []).filter((t) => !assigned.has(t.tenant_id))

  const renderTenant = (tenant: Tenant, currentOrg: string) => (
    <li key={tenant.tenant_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <span className="text-ink">
        {tenant.name}
        <span className="text-ink-soft"> · {tenant.tenant_id}</span>
        {tenant.status !== 'active' && <span className="text-ink-soft">（已停用）</span>}
      </span>
      <label className="flex items-center gap-2 text-ink-soft">
        所属组织
        <select
          aria-label={`${tenant.name} 所属组织`}
          className={inputClass}
          value={currentOrg}
          onChange={(event) => void handleAssign(tenant, event.target.value)}
        >
          <option value="">无</option>
          {(organizations ?? []).map((org) => (
            // 停用的组织**留在列表里但不可选**，不是整个删掉：删掉的话，
            // 已经挂在里面的租户会显示成「无」——看起来像被移出去了，而它
            // 其实还在里面。当前就在这个组织里的那一项不禁用，否则浏览器
            // 渲染不出选中值。
            <option
              key={org.org_id}
              value={org.org_id}
              disabled={org.status !== 'active' && org.org_id !== currentOrg}
            >
              {org.name}
              {org.status !== 'active' ? '（已停用）' : ''}
            </option>
          ))}
        </select>
      </label>
    </li>
  )

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.organizations}</h1>
        <p className="text-sm text-ink-soft">
          把多个领域归到一个客户名下。组织只是归拢，不参与隔离——数据依旧按租户隔开。
        </p>
      </div>

      {error !== null && (
        <p role="alert" className="rounded-card border border-subtle bg-card p-3 text-sm text-status-error-strong">
          {error}
        </p>
      )}

      <form className={`${card} flex flex-wrap items-end gap-3`} onSubmit={handleCreate}>
        <label className="flex flex-col gap-1 text-sm text-ink">
          组织 ID
          <input className={inputClass} value={newId} onChange={(e) => setNewId(e.target.value)} placeholder="muji" />
        </label>
        <label className="flex flex-col gap-1 text-sm text-ink">
          组织名称
          <input className={inputClass} value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="无印良品" />
        </label>
        <button type="submit" className={buttonClass} disabled={busy || !newId.trim() || !newName.trim()}>
          新建组织
        </button>
      </form>

      {loading && <Skeleton variant="card-list" count={2} />}

      {!loading && (organizations ?? []).length === 0 && unassigned.length === 0 && (
        <EmptyState icon={Building2} title="还没有组织，也没有租户" action="先在上面建一个组织，再去租户管理建租户。" />
      )}

      {!loading &&
        (organizations ?? []).map((org) => (
          <section key={org.org_id} data-testid={`org-${org.org_id}`} className={`${card} flex flex-col gap-3`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-bold text-ink">
                {org.name}
                <span className="text-ink-soft"> · {org.org_id}</span>
                {org.status !== 'active' && <span className="text-ink-soft">（已停用）</span>}
              </h2>
              <div className="flex gap-2">
                {/* 停用只做一件事：不能再往它下面挂新租户。已经挂着的不动，
                    那些租户也照常工作——隔离是 tenant_id 一维，组织只是归拢。 */}
                <button
                  type="button"
                  className={buttonClass}
                  onClick={() =>
                    void handleStatus(org, org.status === 'active' ? 'disabled' : 'active')
                  }
                >
                  {org.status === 'active' ? '停用' : '启用'}
                </button>
                <button type="button" className={buttonClass} onClick={() => void handleDelete(org)}>
                  删除
                </button>
              </div>
            </div>
            {org.tenant_ids.length === 0 ? (
              <p className="text-sm text-ink-soft">这个组织下还没有租户。从下面「未归入组织」里挑一个挂进来。</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {org.tenant_ids.map((id) =>
                  renderTenant(tenantById.get(id) ?? { tenant_id: id, name: id, status: 'active' }, org.org_id),
                )}
              </ul>
            )}
          </section>
        ))}

      {!loading && unassigned.length > 0 && (
        <section data-testid="org-unassigned" className={`${card} flex flex-col gap-3`}>
          <h2 className="text-sm font-bold text-ink">未归入组织</h2>
          <ul className="flex flex-col gap-2">{unassigned.map((t) => renderTenant(t, ''))}</ul>
        </section>
      )}
    </div>
  )
}
