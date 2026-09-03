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
        // 只有 401(adminFetch 内部显式 throw)或请求本身失败(网络错误、
        // res.json() 解析非法 JSON)才会走到这里。实测过一种容易被当作
        // "同一条路径"的情形:后端返回 500 但带一个解析得出的 JSON body
        // (例如 {"detail": "..."})——这种响应不会抛错,会走上面的 try
        // 分支,body.confirmed 是 undefined(falsy),同样落到本体结构页,
        // 但走的是 try 分支而不是这里。这里落到本体结构页,是因为那一页
        // 对两种租户都完全可用,而文档上传页在本体未确认时主能力是禁用
        // 的——读失败时选代价小的那边。
        if (!cancelled) setConfirmed(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId])

  // 这个分支不只是渲染加载文案。实测过:删掉它、把 null 当 false 处理后,
  // 组件首帧就会用初始值 null(falsy)去 <Navigate> 并卸载自己——随后
  // effect 完成时的 setConfirmed(true) 落在一个已经不在树上的组件上,
  // 不会再生效。这个分支同时让组件在 effect 完成前保持挂载,状态才有
  // 地方落。
  if (confirmed === null) {
    return (
      <div data-testid="admin-landing-loading" className="text-sm text-ink-soft">
        正在确认这个租户走到哪一步…
      </div>
    )
  }
  return <Navigate to={confirmed ? ADMIN_ROUTES.documents : ADMIN_ROUTES.ontology} replace />
}
