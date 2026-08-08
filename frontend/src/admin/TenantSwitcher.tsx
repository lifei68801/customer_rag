import { useAdminTenant } from './TenantContext'

export function TenantSwitcher() {
  const { tenantId, setTenantId } = useAdminTenant()

  return (
    <select
      value={tenantId}
      onChange={(event) => setTenantId(event.target.value)}
      className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
    >
      <option value="demo">demo</option>
    </select>
  )
}
