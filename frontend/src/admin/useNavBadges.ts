import { useEffect, useState } from 'react'
import { adminFetch } from './adminApi'
import { ADMIN_ROUTES } from '../adminRoutes'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

/**
 * 每条导航后面的待办数。
 *
 * 待审的东西不会自己冒出来说它在等——不点进去就不知道有没有。
 *
 * 拉不到时返回空对象，不是 0：显示 0 是在说"没有待办"，那是一句可能
 * 不实的断言。数字拉不到时沉默比编一个数好。
 */
export function useNavBadges(): Record<string, number> {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [counts, setCounts] = useState<Record<string, number>>({})

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    void (async () => {
      try {
        const res = await adminFetch(
          `/api/admin/nav-badges?tenant_id=${encodeURIComponent(tenantId)}`,
          sessionToken,
        )
        if (!res.ok) return
        const body = (await res.json()) as {
          pending_relations: number
          pending_duplicates: number
        }
        if (cancelled) return
        setCounts({
          [ADMIN_ROUTES.reviewRelations]: body.pending_relations,
          [ADMIN_ROUTES.reviewDuplicates]: body.pending_duplicates,
        })
      } catch {
        // 徽标是锦上添花，拉不到就不显示，别让它把导航搞挂。
      }
    })()
    return () => {
      cancelled = true
    }
    // 换租户要重新拉：待办数是按租户算的。
  }, [sessionToken, tenantId])

  return counts
}
