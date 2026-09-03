import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { ADMIN_ROUTES } from '../adminRoutes'
import { adminFetch } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

/**
 * /admin 的落地分流。
 *
 * 侧边栏分组的顺序表达的是依赖(建模 → 接入 → 审核):ETL 会拒绝未确认
 * 本体的租户(admin_schema_etl_routes.py::_build_sample_files),文档管线
 * 在本体未确认时会跳过图谱抽取(ingestion/pipeline.py)。此前索引路由静态
 * 跳文档上传,跳过了第一阶段——新租户第一屏是一个主能力被禁用的页面,而
 * 修复动作要他自己去另一个分组里找。
 *
 * 静态选任何一个都必然对一半人是错的:落在本体结构对老租户是错的,他们
 * 天天来传文档。这条依赖本来就是状态相关的。
 *
 * 三态:状态未知时既不跳转也不空白——在不知情时跳任何一边,都是对用户
 * 断言一件可能为假的事。
 */
export function AdminLanding() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [confirmed, setConfirmed] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!sessionToken) return
    const load = async () => {
      try {
        const res = await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/status`,
          sessionToken,
        )
        const body = (await res.json()) as { confirmed: boolean }
        if (!cancelled) setConfirmed(body.confirmed)
      } catch {
        // 读不到状态时落在本体结构页:那一页对两种租户都是完全可用的,而
        // 文档上传页在本体未确认时主能力是禁用的。读失败时选代价小的那边。
        if (!cancelled) setConfirmed(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId])

  if (confirmed === null) {
    return (
      <div data-testid="admin-landing-loading" className="text-sm text-ink-soft">
        正在确认这个租户走到哪一步…
      </div>
    )
  }
  return <Navigate to={confirmed ? ADMIN_ROUTES.documents : ADMIN_ROUTES.ontology} replace />
}
