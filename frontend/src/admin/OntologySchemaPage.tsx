import { Boxes, ShieldCheck, Spline, Waypoints } from 'lucide-react'
import { EmptyState } from './EmptyState'
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { buildOntologyDiff, type OntologyDiff } from './ontologyDiff'
import { useOntologyData } from './useOntologyData'
import { useOntologyVersion } from './useOntologyVersion'
import { Link } from 'react-router-dom'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import type {
  Constraint,
  ExtraFieldSpec,
  RelationType,
  TermType,
  ViewMode,
} from './ontologyTypes'

type Tab = 'term-types' | 'relation-types' | 'constraints'

/**
 * 把差异渲染成确认框里的一段文字。
 *
 * 确认框是纯文本的（ConfirmContext 只接受 string），所以这里用紧凑的行式
 * 排版而不是表格。变更多时截断——确认框不是变更清单，它的职责是让用户在
 * 按下不可逆按钮前知道"大概要改什么、规模多大"。
 */
const MAX_DIFF_LINES = 12

function describeConfirmDiff(tenantId: string, diff: OntologyDiff | null): string {
  // 用 fromCharCode(10) 而不是字面转义拼换行：这段文案要经过确认框的纯文本
  // 渲染，写成字面量在多层字符串处理里很容易被吃掉一层。
  const NL = String.fromCharCode(10)
  const tail =
    NL + NL +
    `确认后，当前草稿将成为新的已确认版本，旧的已确认版本会被换掉、无法恢复。确认要确认租户「${tenantId}」吗？`
  if (diff === null) {
    return `无法预览本次变更（读取已确认版本失败）。` + tail
  }
  if (diff.total === 0) {
    return `草稿与已确认版本没有差异——这次确认不会改变任何内容。` + tail
  }
  const sign = (kind: string) => (kind === 'added' ? '+' : kind === 'removed' ? '-' : '~')
  const lines: string[] = []
  const push = (title: string, rows: OntologyDiff['termTypes']) => {
    if (rows.length === 0) return
    lines.push(`${title}（${rows.length}）`)
    for (const row of rows) {
      lines.push(`  ${sign(row.kind)} ${row.label}${row.detail ? `：${row.detail}` : ''}`)
    }
  }
  push('实体类型', diff.termTypes)
  push('关系类型', diff.relationTypes)
  push('约束', diff.constraints)

  const shown = lines.slice(0, MAX_DIFF_LINES)
  const omitted = lines.length - shown.length
  return (
    `本次确认将改动 ${diff.total} 处：` + NL + shown.join(NL) +
    (omitted > 0 ? NL + `  ...另有 ${omitted} 行未列出` : '') +
    tail
  )
}

const VALUE_TYPES = ['string', 'number', 'integer', 'number[]'] as const

const STANDARD_NAME_VALUE_TYPES = ['string', 'number', 'integer'] as const

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const tabButtonClass = (active: boolean) =>
  `rounded-control border border-subtle px-3 py-2 text-sm font-bold transition ${focusRing} ${
    active ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

/**
 * 确认按钮可用时的 title。
 *
 * 导出是为了让测试能绑住这个文案本身——测试文件里抄一份常量，源码改了
 * 测试照样绿，那种测试等于没写。
 */
export const CONFIRM_IRREVERSIBLE_HINT = '不可逆：旧的已确认版本会被换掉，无法恢复'

const emptyTermTypeDraft = (): TermType => ({ value: '', extra_fields: [], standard_name_value_type: 'string' })
const emptyRelationTypeDraft = (): RelationType => ({
  relation_type: '',
  example_phrase: '',
  description: '',
  allow_chain_query: false,
})

export function OntologySchemaPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const [tab, setTab] = useState<Tab>('term-types')
  const [confirmed, setConfirmed] = useState<boolean | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)
  // view/confirming 是页面级状态而不是各 tab 自己的本地状态——后端
  // confirm_ontology() 原子性地同时确认 tenant_relation_types（关系类型）和
  // term_type_relation_allowlist（约束）两张表（见 app/graphrag/
  // ontology_lifecycle.py），是同一个 schema 草稿生命周期的两个视图，不是
  // 关系类型 tab 独有的概念，所以"查看已确认版本"和"确认 schema"提到页面
  // 外层，两个 tab 共用同一份状态。
  const [view] = useOntologyVersion()
  const [confirming, setConfirming] = useState(false)
  // 确认成功后用来"踢"一下当前挂载的 tab 重新拉取数据——两个 tab 互斥挂载
  // （tab === 'relation-types' 时约束 tab 是卸载状态，反之亦然），只需要让
  // 当前挂载的那个重新 refresh；另一个 tab 下次挂载时自己的 useEffect 会
  // 用新数据初始化，不需要额外处理。
  const [confirmVersion, setConfirmVersion] = useState(0)
  // 确认 schema 的前置条件：实体类型、关系类型（草稿）、约束（草稿）三者
  // 都至少有一条，否则确认了也只是把空/不完整的 schema 定版，ETL 和知识
  // 图谱抽取都用不了。三个 tab 互斥挂载，任何一个 tab 单独维护自己的
  // "是否有数据"都不足以判断"三者是否都满足"，所以在页面级单独查一份。
  const [readiness, setReadiness] = useState<{
    termTypes: boolean
    relationTypes: boolean
    constraints: boolean
  } | null>(null)
  const [readinessVersion, setReadinessVersion] = useState(0)
  const bumpReadiness = useCallback(() => setReadinessVersion((v) => v + 1), [])

  useEffect(() => {
    document.title = '本体管理 · 管理后台'
  }, [])

  const refreshStatus = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/ontology/${encodeURIComponent(tenantId)}/status`,
      sessionToken,
    )
    const data = (await response.json()) as { confirmed: boolean }
    setConfirmed(data.confirmed)
  }, [sessionToken, tenantId])

  useEffect(() => {
    refreshStatus().catch((err) => console.error('查询 schema 确认状态失败', err))
  }, [refreshStatus])

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    const loadReadiness = async () => {
      setReadiness(null)
      try {
        // checkout 幂等：哪怕用户从没点开过关系类型/约束 tab，这里也要保证
        // 草稿存在——extraction 模式租户依赖 checkout 自动播种默认关系类型，
        // 不 checkout 直接查 status=draft，会把"还没打开过那个 tab"误判成
        // "没有关系类型"。
        await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`,
          sessionToken,
          { method: 'POST' },
        )
        const [termTypesRes, relationTypesRes, constraintsRes] = await Promise.all([
          adminFetch(
            `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=draft`,
            sessionToken,
          ),
          adminFetch(
            `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=draft`,
            sessionToken,
          ),
          adminFetch(
            `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=draft`,
            sessionToken,
          ),
        ])
        const termTypesData = (await termTypesRes.json()) as { term_types: TermType[] }
        const relationTypesData = (await relationTypesRes.json()) as { relation_types: RelationType[] }
        const constraintsData = (await constraintsRes.json()) as { constraints: Constraint[] }
        if (cancelled) return
        setReadiness({
          termTypes: termTypesData.term_types.length > 0,
          relationTypes: relationTypesData.relation_types.length > 0,
          constraints: constraintsData.constraints.length > 0,
        })
      } catch (err) {
        if (!cancelled) console.error('检查 schema 确认前置条件失败', err)
      }
    }
    loadReadiness()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId, readinessVersion])

  const isLifecycleTab = tab === 'term-types' || tab === 'relation-types' || tab === 'constraints'
  const missingCategories = readiness
    ? ([
        !readiness.termTypes && '实体类型',
        !readiness.relationTypes && '关系类型',
        !readiness.constraints && '约束',
      ].filter(Boolean) as string[])
    : []
  const confirmDisabledReason = !isLifecycleTab
    ? '该分类直接生效，无需确认'
    : readiness === null
      ? '检查前置条件中…'
      : missingCategories.length > 0
        ? `还缺少：${missingCategories.join('、')}（各至少一条）`
        : null
  const confirmDisabled = confirming || confirmDisabledReason !== null

  /** 拉一份 status 下的本体三件套，供 diff 用。 */
  const loadSnapshot = async (status: 'draft' | 'confirmed') => {
    const [tt, rt, cs] = await Promise.all(
      ['term-types', 'relation-types', 'constraints'].map((ep) =>
        adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/${ep}?status=${status}`,
          sessionToken!,
        ),
      ),
    )
    const [ttBody, rtBody, csBody] = await Promise.all([tt.json(), rt.json(), cs.json()])
    return {
      termTypes: ttBody.term_types ?? [],
      relationTypes: rtBody.relation_types ?? [],
      constraints: csBody.constraints ?? [],
    }
  }

  const handleConfirm = async () => {
    if (!sessionToken || confirmDisabled) return

    // 确认是不可逆的（旧的已确认版本会被换掉、无法恢复），所以先算出这次
    // 到底要改什么，摆在确认框里。只在点确认时拉已确认版，不拖慢页面首屏。
    let diff: OntologyDiff | null = null
    try {
      const [draftSnapshot, confirmedSnapshot] = await Promise.all([
        loadSnapshot('draft'),
        loadSnapshot('confirmed'),
      ])
      diff = buildOntologyDiff(draftSnapshot, confirmedSnapshot)
    } catch {
      // 算不出差异不该挡住确认——退回原来那句笼统的警告，但要让用户知道
      // 这次没能预览，而不是让他以为"没有变更"。
      diff = null
    }

    if (!(await confirm(describeConfirmDiff(tenantId, diff)))) {
      return
    }
    setPageError(null)
    setConfirming(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/confirm`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '确认失败'))
      }
      showToast('已确认')
      await refreshStatus()
      setConfirmVersion((v) => v + 1)
      // 确认会把草稿行的 status 原地改成 confirmed（不再是 draft），
      // checkout_draft 下次调用时才会把已确认版本重新复制回草稿——这里
      // 主动"踢"一次前置条件检查，让它带着新的 checkout 重新算一遍，不然
      // 确认成功的瞬间 status=draft 的关系类型/约束会短暂查到 0 条，误报
      // "还缺少关系类型/约束"。
      bumpReadiness()
    } catch (err) {
      setPageError(err instanceof Error ? err.message : '确认失败')
    } finally {
      setConfirming(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* 页头只有标题。草稿/已确认这个轴归侧边栏的版本切换器——同一个轴在
          两个地方各摆一份控件，用户会以为它们管的不是同一件事。 */}
      <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.ontology}</h1>

      {pageError && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {pageError}
        </p>
      )}

      <nav data-testid="ontology-tabs" className="flex flex-row flex-wrap gap-2">
        <button
          type="button"
          className={tabButtonClass(tab === 'term-types')}
          onClick={() => {
            setTab('term-types')
            setPageError(null)
          }}
        >
          实体类型
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'relation-types')}
          onClick={() => {
            setTab('relation-types')
            setPageError(null)
          }}
        >
          关系类型
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'constraints')}
          onClick={() => {
            setTab('constraints')
            setPageError(null)
          }}
        >
          约束
        </button>
      </nav>

      {/* 已确认视图是只读快照。快照压根不存在时，三个空列表和"确认过但
          是空的"长得一模一样——不说清楚，用户会掉头去查数据哪儿去了。 */}
      {view === 'confirmed' && confirmed === false && (
        <p
          data-testid="never-confirmed-notice"
          className="rounded-card border border-subtle bg-card px-3 py-2 text-sm text-ink"
        >
          这个租户还没确认过 schema，已确认版本是空的。下面看到的空列表不是数据丢了——
          切回侧边栏的「草稿」编辑，录完再确认。
        </p>
      )}

      <div data-testid="ontology-tab-panel" className="flex flex-col gap-6">
        {tab === 'term-types' && (
          <TermTypesTab
            key={tenantId}
            sessionToken={sessionToken}
            tenantId={tenantId}
            onError={setPageError}
            view={view}
            confirmVersion={confirmVersion}
            onDataChanged={bumpReadiness}
          />
        )}
        {tab === 'relation-types' && (
          <RelationTypesTab
            key={tenantId}
            sessionToken={sessionToken}
            tenantId={tenantId}
            onError={setPageError}
            view={view}
            confirmVersion={confirmVersion}
            onDataChanged={bumpReadiness}
          />
        )}
        {tab === 'constraints' && (
          <ConstraintsTab
            key={tenantId}
            sessionToken={sessionToken}
            tenantId={tenantId}
            onError={setPageError}
            view={view}
            confirmVersion={confirmVersion}
            onDataChanged={bumpReadiness}
          />
        )}
      </div>

      {/* 确认动作跟着录入信息走：用户是在这下面录完的，让他回到页面顶部
          去点，中间隔着一整页内容。只在草稿视图出现——已确认是只读快照，
          那里摆一个点不动的按钮，只会让人怀疑自己哪一步做错了。 */}
      {view === 'draft' && (
        <div className="flex flex-col gap-1 border-t border-subtle pt-4">
          <button
            type="button"
            onClick={handleConfirm}
            disabled={confirmDisabled}
            // 禁用时说明为什么点不了，可用时说明点下去会发生什么。颜色
            // 不能是唯一的信号——色觉障碍的用户看到的是两个灰按钮。
            title={confirmDisabledReason ?? CONFIRM_IRREVERSIBLE_HINT}
            // 危险色而不是成功色。这个动作确实会"成功"，但它的效果是
            // 「旧的已确认版本会被换掉、无法恢复」。确认弹窗把后果写得
            // 很清楚，可用户在点开弹窗之前就已经形成了预期。
            className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-status-error px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
          >
            {confirming ? '确认中…' : '确认 schema'}
          </button>
          <span className="text-xs text-ink-soft">
            {confirming ? '确认中…' : (confirmDisabledReason ?? CONFIRM_IRREVERSIBLE_HINT)}
          </span>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 实体类型 tab
// ---------------------------------------------------------------------------

function TermTypesTab({
  sessionToken,
  tenantId,
  onError,
  view,
  confirmVersion,
  onDataChanged,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  view: ViewMode
  confirmVersion: number
  onDataChanged: () => void
}) {
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [items, setItems] = useState<TermType[]>([])
  const [loaded, setLoaded] = useState(false)
  const [editingValue, setEditingValue] = useState<string | null>(null)
  const [draft, setDraft] = useState<TermType>(emptyTermTypeDraft())
  const [creating, setCreating] = useState(false)
  const [savingValue, setSavingValue] = useState<string | null>(null)
  const [deletingValue, setDeletingValue] = useState<string | null>(null)
  const [migratingFrom, setMigratingFrom] = useState<string | null>(null)
  const [migrateTarget, setMigrateTarget] = useState('')
  const [migrating, setMigrating] = useState(false)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      // checkout 对用户透明——每次进这个 tab / 切换视图前，先确保草稿存在，
      // 幂等操作，已有草稿时后端直接跳过（见 app/graphrag/ontology_lifecycle.py
      // ::checkout_draft 的说明）。实体类型的编辑/删除严格只作用于 status='draft'
      // 行（见 ontology_categories.py::update_term_type/delete_term_type），没有
      // 草稿就编辑会报"草稿里不存在分类"，所以这里跟关系类型/约束 tab 一样
      // 主动 checkout，不能只依赖页面级那次（有竞态）。
      const checkoutResponse = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`,
        sessionToken,
        { method: 'POST' },
      )
      if (!checkoutResponse.ok) {
        const body = await checkoutResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, 'schema 草稿初始化失败'))
      }
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=${view}`,
        sessionToken,
      )
      const data = (await response.json()) as { term_types: TermType[] }
      setItems(data.term_types)
    } catch (err) {
      onError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, view, onError])

  useEffect(() => {
    refresh().catch((err) => console.error('实体类型列表刷新失败', err))
  }, [refresh, confirmVersion])

  const startEdit = (item: TermType) => {
    setEditingValue(item.value)
    setDraft({ ...item, extra_fields: item.extra_fields.map((f) => ({ ...f })) })
  }

  const startCreate = () => {
    setEditingValue('')
    setDraft(emptyTermTypeDraft())
  }

  const cancelEdit = () => {
    setEditingValue(null)
    setDraft(emptyTermTypeDraft())
  }

  const addField = () => {
    setDraft((prev) => ({ ...prev, extra_fields: [...prev.extra_fields, { name: '', value_type: 'string' }] }))
  }

  const updateField = (index: number, patch: Partial<ExtraFieldSpec>) => {
    setDraft((prev) => ({
      ...prev,
      extra_fields: prev.extra_fields.map((f, i) => (i === index ? { ...f, ...patch } : f)),
    }))
  }

  const removeField = (index: number) => {
    setDraft((prev) => ({ ...prev, extra_fields: prev.extra_fields.filter((_, i) => i !== index) }))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || editingValue === null) return
    const isCreate = editingValue === ''
    onError(null)
    if (isCreate) setCreating(true)
    else setSavingValue(editingValue)
    try {
      const url = isCreate
        ? `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`
        : `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/${encodeURIComponent(editingValue)}`
      const response = await adminFetch(url, sessionToken, {
        method: isCreate ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, isCreate ? '新增实体类型失败' : '更新实体类型失败'))
      }
      cancelEdit()
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setCreating(false)
      setSavingValue(null)
    }
  }

  const handleDelete = async (value: string) => {
    if (!sessionToken || deletingValue !== null) return
    if (!(await confirm(`确定要删除实体类型「${value}」吗？此操作不可撤销。`))) return
    onError(null)
    setDeletingValue(value)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/${encodeURIComponent(value)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除实体类型失败'))
      }
      showToast('已删除实体类型')
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setDeletingValue(null)
    }
  }

  const handleMigrate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || migratingFrom === null || migrating) return
    if (
      !(await confirm(
        `这会把租户「${tenantId}」所有 term_type 为「${migratingFrom}」的真实术语和图谱节点批量改成「${migrateTarget}」，不可逆。确定要继续吗？`,
      ))
    ) {
      return
    }
    onError(null)
    setMigrating(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/migrate`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_type: migratingFrom, new_type: migrateTarget }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '迁移实体类型失败'))
      }
      const data = (await response.json()) as { terms_migrated: number; graph_nodes_migrated: number }
      showToast(`已迁移 ${data.terms_migrated} 条术语、${data.graph_nodes_migrated} 个图谱节点`)
      setMigratingFrom(null)
      setMigrateTarget('')
    } catch (err) {
      onError(err instanceof Error ? err.message : '迁移实体类型失败')
    } finally {
      setMigrating(false)
    }
  }

  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'

  return (
    <div className="flex flex-col gap-4">
      {!loaded && <Skeleton variant="table-rows" count={4} />}
      {loaded && items.length === 0 && editingValue === null && (
        <EmptyState
          icon={Boxes}
          title={`还没有任何${view === 'draft' ? '草稿' : '已确认的'}实体类型`}
          action={
            view === 'draft'
              ? '点击下方「+ 新增实体类型」创建一个。实体类型是本体的骨架，关系和约束都建立在它之上。'
              : '草稿确认之后，已确认的实体类型会出现在这里。'
          }
        />
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle bg-card">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-subtle bg-paper text-ink">
                <th className={cellPadding}>类型名</th>
                <th className={cellPadding}>属性字段数</th>
                <th className={cellPadding}>自身取值类型</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.value} className="border-b border-subtle text-ink last:border-b-0">
                  <td className={cellPadding}>{item.value}</td>
                  <td className={cellPadding}>{item.extra_fields.length}</td>
                  <td className={cellPadding}>{item.standard_name_value_type}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => startEdit(item)}
                        disabled={editingValue !== null}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => {
                          setMigratingFrom(item.value)
                          setMigrateTarget('')
                        }}
                        disabled={migrating}
                      >
                        迁移实体类型…
                      </button>
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleDelete(item.value)}
                        disabled={deletingValue !== null || editingValue !== null}
                      >
                        {deletingValue === item.value ? '删除中…' : '删除'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {view === 'draft' && (
        <p className="text-xs text-ink-soft">
          直接改名后旧类型不再有草稿行可选，「迁移实体类型」用不了；要迁移真实数据，
          请新增一个类型作为新条目、确认草稿后用「迁移实体类型」把旧类型迁到新类型，再删除旧条目。
        </p>
      )}

      {view === 'draft' && editingValue === null && (
        <button
          type="button"
          onClick={startCreate}
          className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          + 新增实体类型
        </button>
      )}

      {view === 'draft' && editingValue !== null && (
        <form onSubmit={submit} className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4">
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            类型名
            <input
              type="text"
              required
              value={draft.value}
              onChange={(e) => setDraft((prev) => ({ ...prev, value: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            自身取值类型
            <select
              value={draft.standard_name_value_type}
              onChange={(e) => setDraft((prev) => ({ ...prev, standard_name_value_type: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            >
              {STANDARD_NAME_VALUE_TYPES.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <span className="text-xs font-normal text-ink-soft">
              这个类型的实例本身代表什么类型的值（比如"销量""收入"这类每个取值都是独立节点的类型，
              应该声明成 number，才能用"大于/小于"做区间查询；大多数类型（产品名、公司名…）保持默认的 string 即可）
            </span>
          </label>

          <div className="flex flex-col gap-2">
            <span className="text-sm font-bold text-ink">属性字段</span>
            {draft.extra_fields.map((field, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2">
                <input
                  type="text"
                  placeholder="字段名"
                  aria-label="字段名"
                  required
                  value={field.name}
                  onChange={(e) => updateField(index, { name: e.target.value })}
                  className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
                />
                <select
                  value={field.value_type}
                  aria-label="字段类型"
                  onChange={(e) => updateField(index, { value_type: e.target.value })}
                  className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
                >
                  {VALUE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => removeField(index)}
                  className={`font-bold text-status-error underline ${focusRing}`}
                >
                  删除
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={addField}
              className={`self-start rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              + 添加字段
            </button>
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || savingValue !== null}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {creating || savingValue !== null ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}

      {migratingFrom !== null && (
        <form
          onSubmit={handleMigrate}
          className="flex flex-col gap-3 rounded-panel border border-status-error bg-card p-4"
        >
          <p className="text-sm text-ink">
            把租户「{tenantId}」所有 term_type 为「{migratingFrom}」的真实术语和图谱节点迁移成：
          </p>
          <select
            required
            value={migrateTarget}
            onChange={(e) => setMigrateTarget(e.target.value)}
            aria-label="迁移目标类型"
            className={`rounded-control border border-subtle bg-paper px-2 py-1.5 font-mono text-ink focus:outline-none ${focusRing}`}
          >
            <option value="">请选择新类型</option>
            {items
              .filter((item) => item.value !== migratingFrom)
              .map((item) => (
                <option key={item.value} value={item.value}>
                  {item.value}
                </option>
              ))}
          </select>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={migrating}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 py-2 text-sm font-bold text-white transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {migrating ? '迁移中…' : '确认迁移'}
            </button>
            <button
              type="button"
              onClick={() => {
                setMigratingFrom(null)
              }}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 关系类型 tab
// ---------------------------------------------------------------------------

function RelationTypesTab({
  sessionToken,
  tenantId,
  onError,
  view,
  confirmVersion,
  onDataChanged,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  view: ViewMode
  confirmVersion: number
  onDataChanged: () => void
}) {
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [items, setItems] = useState<RelationType[]>([])
  const [loaded, setLoaded] = useState(false)
  const [editingType, setEditingType] = useState<string | null>(null)
  const [draft, setDraft] = useState<RelationType>(emptyRelationTypeDraft())
  const [creating, setCreating] = useState(false)
  const [savingType, setSavingType] = useState<string | null>(null)
  const [deletingType, setDeletingType] = useState<string | null>(null)
  const [migratingFrom, setMigratingFrom] = useState<string | null>(null)
  const [migrateTarget, setMigrateTarget] = useState('')
  const [migrating, setMigrating] = useState(false)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      // checkout 对用户透明——每次进这个 tab / 切换视图前，先确保草稿存在，
      // 幂等操作，已有草稿时后端直接跳过（见 app/graphrag/ontology_lifecycle.py
      // ::checkout_draft 的说明）。
      const checkoutResponse = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`,
        sessionToken,
        { method: 'POST' },
      )
      if (!checkoutResponse.ok) {
        const body = await checkoutResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, 'schema 草稿初始化失败'))
      }
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=${view}`,
        sessionToken,
      )
      const data = (await response.json()) as { relation_types: RelationType[] }
      setItems(data.relation_types)
    } catch (err) {
      onError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, view, onError])

  useEffect(() => {
    refresh().catch((err) => console.error('关系类型列表刷新失败', err))
  }, [refresh, confirmVersion])

  const startEdit = (item: RelationType) => {
    setEditingType(item.relation_type)
    setDraft({ ...item })
  }

  const startCreate = () => {
    setEditingType('')
    setDraft(emptyRelationTypeDraft())
  }

  const cancelEdit = () => {
    setEditingType(null)
    setDraft(emptyRelationTypeDraft())
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || editingType === null) return
    const isCreate = editingType === ''
    onError(null)
    if (isCreate) setCreating(true)
    else setSavingType(editingType)
    try {
      const url = isCreate
        ? `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types`
        : `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/${encodeURIComponent(editingType)}`
      const response = await adminFetch(url, sessionToken, {
        method: isCreate ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          relation_type: draft.relation_type,
          example_phrase: draft.example_phrase,
          description: draft.description,
          allow_chain_query: draft.allow_chain_query,
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, isCreate ? '新增关系类型失败' : '更新关系类型失败'))
      }
      cancelEdit()
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setCreating(false)
      setSavingType(null)
    }
  }

  const handleDelete = async (relationType: string) => {
    if (!sessionToken || deletingType !== null) return
    if (!(await confirm(`确定要删除关系类型「${relationType}」吗？此操作不可撤销。`))) return
    onError(null)
    setDeletingType(relationType)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/${encodeURIComponent(relationType)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除关系类型失败'))
      }
      showToast('已删除关系类型')
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setDeletingType(null)
    }
  }

  const handleMigrate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || migratingFrom === null || migrating) return
    if (
      !(await confirm(
        `这会遍历租户「${tenantId}」在 Neo4j 图谱里所有类型为「${migratingFrom}」的边，批量改成「${migrateTarget}」，不可逆。确定要继续吗？`,
      ))
    ) {
      return
    }
    onError(null)
    setMigrating(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/migrate`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_type: migratingFrom, new_type: migrateTarget }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '迁移图谱边失败'))
      }
      const data = (await response.json()) as { migrated_count: number }
      showToast(`已迁移 ${data.migrated_count} 条边`)
      setMigratingFrom(null)
      setMigrateTarget('')
    } catch (err) {
      onError(err instanceof Error ? err.message : '迁移图谱边失败')
    } finally {
      setMigrating(false)
    }
  }

  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'

  return (
    <div className="flex flex-col gap-4">
      {!loaded && <Skeleton variant="table-rows" count={4} />}
      {loaded && items.length === 0 && (
        <EmptyState
          icon={Spline}
          title={`还没有任何${view === 'draft' ? '草稿' : '已确认的'}关系类型`}
          action={
            view === 'draft'
              ? '点击下方「+ 新增关系类型」创建一个。关系类型定义实体之间可以有哪些连接。'
              : '草稿确认之后，已确认的关系类型会出现在这里。'
          }
        />
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle bg-card">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-subtle bg-paper text-ink">
                <th className={cellPadding}>关系类型</th>
                <th className={cellPadding}>示例短语</th>
                <th className={cellPadding}>说明</th>
                <th className={cellPadding}>支持链式查询</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.relation_type} className="border-b border-subtle text-ink last:border-b-0">
                  <td className={`${cellPadding} font-mono text-xs`}>{item.relation_type}</td>
                  <td className={cellPadding}>{item.example_phrase}</td>
                  <td className={cellPadding}>{item.description || '-'}</td>
                  <td className={cellPadding}>{item.allow_chain_query ? '是' : '否'}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => startEdit(item)}
                        disabled={editingType !== null}
                      >
                        改名/编辑
                      </button>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => {
                          setMigratingFrom(item.relation_type)
                          setMigrateTarget('')
                        }}
                        disabled={migrating}
                      >
                        迁移图谱边…
                      </button>
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleDelete(item.relation_type)}
                        disabled={deletingType !== null}
                      >
                        {deletingType === item.relation_type ? '删除中…' : '删除'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {view === 'draft' && (
        <p className="text-xs text-ink-soft">
          改名只影响草稿定义，已确认图谱里的历史边不会自动变，需要用「迁移图谱边」处理。
        </p>
      )}

      {view === 'draft' && editingType === null && (
        <button
          type="button"
          onClick={startCreate}
          className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          + 新增关系类型
        </button>
      )}

      {view === 'draft' && editingType !== null && (
        <form onSubmit={submit} className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4">
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            关系类型名（大写字母/数字/下划线）
            <input
              type="text"
              required
              value={draft.relation_type}
              onChange={(e) => setDraft((prev) => ({ ...prev, relation_type: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 font-mono text-ink focus:outline-none ${focusRing}`}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            示例短语
            <input
              type="text"
              required
              value={draft.example_phrase}
              onChange={(e) => setDraft((prev) => ({ ...prev, example_phrase: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            说明
            <input
              type="text"
              value={draft.description}
              onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            />
          </label>
          <label className="flex items-center gap-2 text-sm font-bold text-ink">
            <input
              type="checkbox"
              checked={draft.allow_chain_query}
              onChange={(e) => setDraft((prev) => ({ ...prev, allow_chain_query: e.target.checked }))}
            />
            支持链式查询
          </label>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || savingType !== null}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {creating || savingType !== null ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}

      {migratingFrom !== null && (
        <form
          onSubmit={handleMigrate}
          className="flex flex-col gap-3 rounded-panel border border-status-error bg-card p-4"
        >
          <p className="text-sm text-ink">
            把租户「{tenantId}」图谱里所有类型为「{migratingFrom}」的边迁移成：
          </p>
          <select
            required
            value={migrateTarget}
            onChange={(e) => setMigrateTarget(e.target.value)}
            aria-label="迁移目标类型"
            className={`rounded-control border border-subtle bg-paper px-2 py-1.5 font-mono text-ink focus:outline-none ${focusRing}`}
          >
            <option value="">请选择新类型</option>
            {items
              .filter((item) => item.relation_type !== migratingFrom)
              .map((item) => (
                <option key={item.relation_type} value={item.relation_type}>
                  {item.relation_type}
                </option>
              ))}
          </select>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={migrating}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 py-2 text-sm font-bold text-white transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {migrating ? '迁移中…' : '确认迁移'}
            </button>
            <button
              type="button"
              onClick={() => {
                setMigratingFrom(null)
              }}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 约束 tab
// ---------------------------------------------------------------------------

function ConstraintsTab({
  sessionToken,
  tenantId,
  onError,
  view,
  confirmVersion,
  onDataChanged,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  view: ViewMode
  confirmVersion: number
  onDataChanged: () => void
}) {
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [subject, setSubject] = useState('')
  const [relationType, setRelationType] = useState('')
  const [object, setObject] = useState('')
  const [adding, setAdding] = useState(false)
  const [removingKey, setRemovingKey] = useState<string | null>(null)
  // 约束本质是 (主语类型, 关系, 宾语类型) 的边表——图和表是同一份数据的两种
  // 呈现。默认给表：新增/删除都在表上操作，图是只读的全局视图。

  const { constraints, termTypes, draftRelationTypes, loaded, refresh } =
    useOntologyData({
      sessionToken,
      tenantId,
      view,
      withGraphOverlay: false,
      reloadKey: confirmVersion,
      onError,
    })

  const constraintKey = (c: Constraint) => `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`

  const handleAdd = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || !subject || !relationType || !object || adding) return
    onError(null)
    setAdding(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            subject_term_type: subject,
            relation_type: relationType,
            object_term_type: object,
          }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新增约束失败'))
      }
      showToast('已添加约束')
      setSubject('')
      setRelationType('')
      setObject('')
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '新增约束失败')
    } finally {
      setAdding(false)
    }
  }

  const handleRemove = async (constraint: Constraint) => {
    if (!sessionToken || removingKey !== null) return
    const key = constraintKey(constraint)
    if (!(await confirm('确定要删除这条约束吗？'))) return
    onError(null)
    setRemovingKey(key)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints`,
        sessionToken,
        {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(constraint),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除约束失败'))
      }
      showToast('已删除约束')
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除约束失败')
    } finally {
      setRemovingKey(null)
    }
  }

  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'

  return (
    <div className="flex flex-col gap-4">
      {loaded && (
        <Link
          to={ADMIN_ROUTES.ontologyGraph}
          className={`inline-flex min-h-[36px] items-center gap-1.5 self-start rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
        >
          <Waypoints aria-hidden="true" className="h-4 w-4" />
          在本体图中查看
        </Link>
      )}
      {!loaded && <Skeleton variant="table-rows" count={3} />}
      {loaded && constraints.length === 0 && (
        <EmptyState
          icon={ShieldCheck}
          title={`还没有任何${view === 'draft' ? '草稿' : '已确认的'}约束`}
          action={
            view === 'draft'
              ? '在下方表单里添加一个。约束声明「哪个实体类型可以经哪种关系连到哪个实体类型」，是本体图上的边。'
              : '草稿确认之后，已确认的约束会出现在这里。'
          }
        />
      )}
      {constraints.length > 0 && (
        <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle bg-card">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-subtle bg-paper text-ink">
                <th className={cellPadding}>主体类型</th>
                <th className={cellPadding}>关系类型</th>
                <th className={cellPadding}>客体类型</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {constraints.map((c) => (
                <tr key={constraintKey(c)} className="border-b border-subtle text-ink last:border-b-0">
                  <td className={cellPadding}>{c.subject_term_type}</td>
                  <td className={`${cellPadding} font-mono text-xs`}>{c.relation_type}</td>
                  <td className={cellPadding}>{c.object_term_type}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleRemove(c)}
                        disabled={removingKey !== null}
                      >
                        {removingKey === constraintKey(c) ? '删除中…' : '删除'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {view === 'draft' && (
        <form
          onSubmit={handleAdd}
          className="flex flex-wrap items-end gap-3 rounded-panel border border-subtle bg-card p-4"
        >
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            主体类型
            <select
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            >
              <option value="">请选择</option>
              {termTypes.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            关系类型（草稿）
            <select
              required
              value={relationType}
              onChange={(e) => setRelationType(e.target.value)}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            >
              <option value="">请选择</option>
              {draftRelationTypes.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            客体类型
            <select
              required
              value={object}
              onChange={(e) => setObject(e.target.value)}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            >
              <option value="">请选择</option>
              {termTypes.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            disabled={adding}
            className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
          >
            {adding ? '添加中…' : '+ 添加约束'}
          </button>
        </form>
      )}
    </div>
  )
}
