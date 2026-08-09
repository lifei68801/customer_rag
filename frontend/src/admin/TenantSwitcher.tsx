import { useAdminTenant } from './TenantContext'

export function TenantSwitcher() {
  const { tenantId, setTenantId } = useAdminTenant()

  return (
    <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
      切换租户
      <select
        value={tenantId}
        onChange={(event) => setTenantId(event.target.value)}
        aria-label="切换租户"
        className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
      >
        <option value="demo">demo</option>
      </select>
    </label>
  )
}
