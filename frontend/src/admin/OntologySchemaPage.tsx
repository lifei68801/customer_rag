import { Wand2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { buildOntologyDiff, type OntologyDiff } from './ontologyDiff'
import { fetchTermsSummary } from './termsApi'
import { useOntologyVersion } from './useOntologyVersion'
import { Link, useSearchParams } from 'react-router-dom'
import { ADMIN_ROUTES, MODEL_FROM_QUESTION_KEY, PAGE_TITLES } from '../adminRoutes'
import { ConstraintsTab } from './ontologySchema/ConstraintsTab'
import { RelationTypesTab } from './ontologySchema/RelationTypesTab'
import { TermTypesTab } from './ontologySchema/TermTypesTab'
import { focusRing } from './ontologySchema/shared'
import type { Constraint, RelationType, TermType } from './ontologyTypes'

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
      lines.push(
        `  ${sign(row.kind)} ${row.label}${row.impact ? `（${row.impact}）` : ''}${row.detail ? `：${row.detail}` : ''}`,
      )
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

/**
 * 本体结构页的三张表都只有**一档**选中。
 *
 * 实体明细页的批量删除分两档（本页 / 筛选条件下的全部），因为那里有
 * 分页也有筛选，两档的破坏力差两个数量级。这三张表是一次性全量渲染的
 * 十几行，没有分页也没有筛选——两档在这里指的是同一批行，给同一件事配
 * 两个按钮只会让用户先去想区别在哪、再猜错一个。
 *
 * 所以这里只用 useBulkSelection 的 'keys' 档：selectAllMatching 一次都
 * 不调用，BulkSelectionBar 拿到的 total 就是列出来的行数（它据此不给
 * 「改为选中全部」那个按钮），表头复选框勾的就是整张表。
 */
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

export function OntologySchemaPage() {
  const { sessionToken } = useAdminAuth()
  // 报错明细的「去建模」带着那个答不出来的问题跳过来。把它显示出来，
  // 用户才不用一边配本体一边回忆刚才问的是什么——不接住的话，那个参数
  // 就只是一段没人读的 URL，而入口处的注释在说一件不成立的事。
  const [searchParams] = useSearchParams()
  const fromQuestion = searchParams.get(MODEL_FROM_QUESTION_KEY)
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const [tab, setTab] = useState<Tab>('term-types')
  const [confirmed, setConfirmed] = useState<boolean | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)
  // 删分类被"还有实体在用"挡住时，后端会连挡路的术语一起报回来。光有一句
  // 人话，用户还得自己去实体明细里翻——把类型名留住，错误框里给一条筛好的
  // 链接。
  const [deleteBlockedTermType, setDeleteBlockedTermType] = useState<string | null>(null)
  const reportError = useCallback((msg: string | null) => {
    setPageError(msg)
    // 清错误就一起清掉链接：留着一条指向已经处理完的类型的链接，比没有链接
    // 更误导。
    if (msg === null) setDeleteBlockedTermType(null)
  }, [])
  // view/confirming 是页面级状态而不是各 tab 自己的本地状态——后端
  // confirm_ontology() 原子性地同时确认 tenant_relation_types（关系类型）和
  // term_type_relation_allowlist（约束）两张表（见 app/graphrag/
  // ontology_lifecycle.py），是同一个 schema 草稿生命周期的两个视图，不是
  // 关系类型 tab 独有的概念，所以"查看已确认版本"和"确认 schema"提到页面
  // 外层，两个 tab 共用同一份状态。
  const [view] = useOntologyVersion()
  const [confirming, setConfirming] = useState(false)
  // 确认成功后的常驻出口：toast 几秒后自己消失，用户抬头看页面时已经找
  // 不到刚才那句话了——常驻提示才是"接下来去哪"真正能被看到的地方。
  const [justConfirmed, setJustConfirmed] = useState(false)
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

  const missingCategories = readiness
    ? ([
        !readiness.termTypes && '实体类型',
        !readiness.relationTypes && '关系类型',
        !readiness.constraints && '约束',
      ].filter(Boolean) as string[])
    : []
  // 三个 tab 都在 schema 草稿的生命周期里，确认的前置条件也是同一套，
  // 所以这个原因跟当前在哪个 tab 无关。（此前这里还有一个"该分类直接生效，
  // 无需确认"的分支，是页面还有第四个 tab 时留下的，早已不可达。）
  const confirmDisabledReason =
    readiness === null
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

    // 按钮先变成"确认中…"，再去算差异。下面那三路请求里任何一路**挂住**
    // （不是失败——挂起没有 reject，catch 兜不住），confirm() 就永远不会被
    // 调用：按钮既不变灰也不改字，用户点了什么都没发生，只会再点一次，
    // 每点一次多发三路请求。/terms/summary 是全表 COUNT ... GROUP BY，
    // 是这三路里最可能慢的一路。
    setPageError(null)
    setConfirming(true)
    try {
      // 确认是不可逆的（旧的已确认版本会被换掉、无法恢复），所以先算出这次
      // 到底要改什么，摆在确认框里。只在点确认时拉已确认版，不拖慢页面首屏。
      let diff: OntologyDiff | null = null
      try {
        const [draftSnapshot, confirmedSnapshot, termCounts] = await Promise.all([
          loadSnapshot('draft'),
          loadSnapshot('confirmed'),
          // 拉不到实体分组统计不该挡住确认预览——按空对象处理，少一条数据
          // 影响提示，好过让下面两份快照的差异预览也一起废掉。
          fetchTermsSummary(sessionToken, tenantId).then(
            (groups) => Object.fromEntries(groups.map((g) => [g.term_type, g.total])),
            () => ({}) as Record<string, number>,
          ),
        ])
        diff = buildOntologyDiff(draftSnapshot, confirmedSnapshot, termCounts)
      } catch {
        // 算不出差异不该挡住确认——退回原来那句笼统的警告，但要让用户知道
        // 这次没能预览，而不是让他以为"没有变更"。
        diff = null
      }

      if (!(await confirm(describeConfirmDiff(tenantId, diff)))) {
        return
      }
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
      setJustConfirmed(true)
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

      {fromQuestion !== null && fromQuestion !== '' && (
        <p
          data-testid="from-question"
          className="rounded-card border border-subtle bg-card p-3 text-sm text-ink"
        >
          {`你从报错明细过来：「${fromQuestion}」这个问题答不出来。看看它涉及的概念在下面的实体类型和关系类型里有没有。`}
        </p>
      )}

      {/* 引导负责从零到一；三个 tab 负责后续微调。两条路径都留着，因为它们
          的用户和场景确实不同。

          replace_draft 是整份替换。readiness 还没查完（null）时不知道草稿
          是不是空的，这时既不能说"安全"也不能说"会覆盖"——猜错的代价是
          用户被白白吓退，或者手工建的东西没了却不知道是这一步干的，所以
          三态各自措辞，未知态用中性文案。 */}
      <Link
        to={ADMIN_ROUTES.guidedOntology}
        title={
          readiness === null
            ? '从一张业务表开始推导本体'
            : readiness.termTypes
              ? '从一张业务表开始重新推导本体——当前草稿会被整份覆盖'
              : '从一张业务表开始，平台会推荐一套本体草案'
        }
        className={`flex items-center gap-1.5 self-start rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
      >
        <Wand2 aria-hidden="true" className="h-4 w-4" />
        从表格开始引导建模
      </Link>

      {pageError && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {pageError}
          {deleteBlockedTermType !== null && (
            <>
              {' '}
              <Link
                to={`${ADMIN_ROUTES.terms}?term_type=${encodeURIComponent(deleteBlockedTermType)}`}
                className={`font-bold underline ${focusRing}`}
              >
                去实体明细处理这些实体
              </Link>
            </>
          )}
        </p>
      )}

      {justConfirmed && (
        <p data-testid="just-confirmed-notice" className="rounded-card border border-subtle bg-card px-3 py-2 text-sm text-ink">
          本体已确认。接下来把业务表的数据装进来——去
          <Link to={ADMIN_ROUTES.etl} className={`ml-1 font-bold underline ${focusRing}`}>
            表格导入
          </Link>
          。
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
            onError={reportError}
            onDeleteBlocked={setDeleteBlockedTermType}
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
            onError={reportError}
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
            onError={reportError}
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

