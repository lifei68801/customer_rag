import { useState } from 'react'

const TENANT_STORAGE_KEY = 'admin_current_tenant'

export function useAdminTenant() {
  const [tenantId, setTenantIdState] = useState(
    () => sessionStorage.getItem(TENANT_STORAGE_KEY) ?? 'demo',
  )

  const setTenantId = (next: string) => {
    sessionStorage.setItem(TENANT_STORAGE_KEY, next)
    setTenantIdState(next)
  }

  return { tenantId, setTenantId }
}
