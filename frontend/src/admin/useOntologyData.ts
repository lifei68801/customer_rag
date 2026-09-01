import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import type { FanoutEntry } from './ontologyGraph/buildScene'
import type { Constraint, RelationType, TermType, ViewMode } from './ontologyTypes'

/**
 * 拉一个租户的本体草稿数据。
 *
 * 从 ConstraintsTab 里抽出来的：本体图变成独立页面之后，图和约束表要看
 * 同一份数据，逻辑留在其中一方就得复制一遍。这两个视图不是"同一个组件的
 * 两种形态"——图是只读全局视图，表是编辑器——但它们取的数是同一份。
 *
 * `withGraphOverlay` 控制要不要顺带拉扇出。扇出要逐条查 Neo4j，比约束表
 * 本身慢得多，表格视图不该被它拖住首屏；扇出失败也不算失败，图照常渲染，
 * 只是不标红——没有红边好过标错红边。
 *
 * `reloadKey` 变化时重新拉一次，给"确认 schema 之后要看到新数据"这类场景
 * 用。首次加载 hook 自己会触发：把它留给调用方的话，每个新调用方都得记得
 * 调一次，而忘了的表现是页面一直转圈、不报错——本体图刚上线时就是这样，
 * 请求全部 200，页面永远是骨架屏。
 */
export function useOntologyData({
  sessionToken,
  tenantId,
  view,
  withGraphOverlay,
  reloadKey = 0,
  onError,
}: {
  sessionToken: string | null
  tenantId: string
  view: ViewMode
  withGraphOverlay: boolean
  reloadKey?: number
  onError: (msg: string | null) => void
}) {
  const [constraints, setConstraints] = useState<Constraint[]>([])
  const [termTypes, setTermTypes] = useState<string[]>([])
  const [draftRelationTypes, setDraftRelationTypes] = useState<string[]>([])
  const [loaded, setLoaded] = useState(false)
  const [fanout, setFanout] = useState<FanoutEntry[]>([])
  const [entityCounts, setEntityCounts] = useState<Record<string, number>>({})

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      const checkoutResponse = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`,
        sessionToken,
        { method: 'POST' },
      )
      if (!checkoutResponse.ok) {
        const body = await checkoutResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, 'schema 草稿初始化失败'))
      }
      const [constraintsRes, termTypesRes, relationTypesRes] = await Promise.all([
        adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=${view}`,
          sessionToken,
        ),
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=draft`, sessionToken),
        // 下拉框的 relation_type 数据源固定拉草稿——不管当前 view 是不是切到已确认，
        // 新增约束这个动作本身只能作用于草稿（后端 add_allowed_combination 也是
        // 校验草稿关系类型），与后端 ontology_constraints.py::_validate_references
        // 的既有校验口径保持一致。
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=draft`, sessionToken),
      ])
      const constraintsData = (await constraintsRes.json()) as { constraints: Constraint[] }
      const termTypesData = (await termTypesRes.json()) as { term_types: TermType[] }
      const relationTypesData = (await relationTypesRes.json()) as { relation_types: RelationType[] }
      setConstraints(constraintsData.constraints)
      setTermTypes(termTypesData.term_types.map((t) => t.value))
      setDraftRelationTypes(relationTypesData.relation_types.map((r) => r.relation_type))
      setLoaded(true)
    } catch (err) {
      // Promise.all 里任一并发请求失败都会在这里被捕获。
      onError(err instanceof Error ? err.message : '约束列表刷新失败')
      setLoaded(true)
    }
  }, [sessionToken, tenantId, view, onError])

  useEffect(() => {
    refresh().catch((err) => console.error('本体数据加载失败', err))
  }, [refresh, reloadKey])

  useEffect(() => {
    if (!withGraphOverlay || !sessionToken) return
    let cancelled = false
    void (async () => {
      try {
        const res = await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/graph-overlay?status=${view}`,
          sessionToken,
        )
        if (!res.ok) return
        const body = (await res.json()) as {
          fanout: FanoutEntry[]
          entity_counts: Record<string, number>
        }
        if (!cancelled) {
          setFanout(body.fanout)
          setEntityCounts(body.entity_counts ?? {})
        }
      } catch {
        // 探测失败就不标红，不打断图的渲染——图本身的价值不依赖扇出。
      }
    })()
    return () => {
      cancelled = true
    }
  }, [withGraphOverlay, sessionToken, tenantId, view, constraints])

  return { constraints, termTypes, draftRelationTypes, fanout, entityCounts, loaded, refresh }
}
