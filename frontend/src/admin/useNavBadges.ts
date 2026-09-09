import { useEffect, useState } from 'react'
import { adminFetch } from './adminApi'
import { ADMIN_ROUTES } from '../adminRoutes'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

/** 一条导航徽标：数字，加上它是"有事等你"还是"这里有多大"。 */
export interface NavBadgeCount {
  count: number
  kind: 'todo' | 'scale'
}

/**
 * 每条导航后面的数字。
 *
 * 待审的东西不会自己冒出来说它在等——不点进去就不知道有没有。
 *
 * **kind 是数据的一部分，不是渲染位置的属性。** 此前"实体总数是规模不是
 * 待办"这件事写在侧边栏里那一段独立项的 JSX 上；六模块重排把实体明细并进
 * 「结果预览」组之后，那段 JSX 没有了，实体数立刻变成一个实心的待办徽标，
 * 而且被加进了组头的待办合计——侧边栏会显示「结果预览：20017 项待处理」。
 * 把 kind 挂在数据上，语义就不会因为这一项挪了个位置而丢掉。
 *
 * 拉不到时返回空对象，不是 0：显示 0 是在说"没有待办"，那是一句可能
 * 不实的断言。数字拉不到时沉默比编一个数好。
 */
export function useNavBadges(): Record<string, NavBadgeCount> {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [counts, setCounts] = useState<Record<string, NavBadgeCount>>({})

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    void (async () => {
      try {
        const res = await adminFetch(
          `/api/admin/${encodeURIComponent(tenantId)}/nav-badges`,
          sessionToken,
        )
        if (!res.ok) return
        const body = (await res.json()) as {
          pending_relations: number
          pending_duplicates: number
          pending_conflicts: number
          total_terms: number
        }
        if (cancelled) return
        setCounts({
          [ADMIN_ROUTES.reviewRelations]: { count: body.pending_relations, kind: 'todo' },
          [ADMIN_ROUTES.reviewDuplicates]: { count: body.pending_duplicates, kind: 'todo' },
          // 属性值冲突。漏掉它的话，看板的待审合计（tenant_stats.py 已经把
          // 三个队列都算进去了）会跟侧边栏「数据审核」组的合计对不上——
          // 同一个人同一屏看到两个互相矛盾的数字。
          [ADMIN_ROUTES.reviewConflicts]: { count: body.pending_conflicts, kind: 'todo' },
          // 「有 20017 条实体」不是一件等着你处理的事。
          [ADMIN_ROUTES.terms]: { count: body.total_terms, kind: 'scale' },
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
