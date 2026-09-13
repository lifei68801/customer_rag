import { useCallback, useEffect, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ADMIN_ROUTES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export interface Domain {
  tenant_id: string
  name: string
  org_id: string | null
  org_name: string | null
}

interface DomainStats {
  tenant_id: string
  term_count: number
  edge_count: number
  document_count: number
  pending_review_count: number
  sheet_row_count: number
  stale_question_count: number
}

/**
 * 一个领域一张卡。**自己取数、自己出骨架屏、自己失败。**
 *
 * 这是 spec D5 裁决二的落点：清单端点不带数字，每张卡各自请求自己的统计。
 * 由 DashboardPage 统一 `Promise.all` 再一次性渲染的话，第一张卡也要等最慢
 * 的那个领域算完——而每个领域的边计数都是一次图查询。
 *
 * 失败也是各自的：一个领域的图谱查不通，不该让整个看板变成一个错误页。
 * 别的领域的数字是好的，凭什么一起看不到。
 */
export function DomainCard({ domain }: { domain: Domain }) {
  const { sessionToken } = useAdminAuth()
  const { switchTenant } = useAdminTenant()
  const navigate = useNavigate()
  const [stats, setStats] = useState<DomainStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [going, setGoing] = useState(false)

  const load = useCallback(async () => {
    if (!sessionToken) return
    setError(null)
    setStats(null)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(domain.tenant_id)}/dashboard/stats`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '统计没算出来'))
      }
      setStats((await response.json()) as DomainStats)
    } catch (err) {
      setError(err instanceof Error ? err.message : '统计没算出来')
    }
  }, [sessionToken, domain.tenant_id])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * 去这个领域的某一页。
   *
   * **先把当前租户切过去，切成功了才跳。** 看板是跨领域的：用户当前挂着
   * A、点的是 B 那张卡，直接跳的话他落在目标页上看到的是 A 的数据——数字
   * 是 B 的、内容是 A 的，而界面全程不说话。
   *
   * 切换失败时不跳（switchTenant 内部已经弹了 toast 说原因）：跳过去只会
   * 让他在错误的租户里操作。
   */
  const goTo = async (path: string) => {
    setGoing(true)
    try {
      if (await switchTenant(domain.tenant_id)) navigate(path)
    } finally {
      setGoing(false)
    }
  }

  const shell = (children: React.ReactNode) => (
    <section
      data-testid={`domain-card-${domain.tenant_id}`}
      className="flex flex-col gap-3 rounded-card border border-subtle bg-card p-4"
    >
      {/* 领域名就是进入这个领域的入口。
          此前卡上只有条件按钮：空领域给"去导入"、有待审给"去审核"、有失效
          问题给"去修"。于是**配好了、正常跑着的**领域（实体>0、待审=0、
          没有失效问题）一个按钮都没有——点不进去。落地页最该做的那件事，
          恰恰对状态最好的那些领域缺失。
          进入的落点是实体明细：卡上这几个数字说的就是这个领域里有什么，
          点进去自然是去看它们。 */}
      <h3 className="font-mono text-sm font-semibold">
        <button
          type="button"
          disabled={going}
          onClick={() => void goTo(ADMIN_ROUTES.terms)}
          className={`flex w-full cursor-pointer items-center justify-between gap-2 text-left text-ink transition hover:text-accent-primary disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {domain.name}
          <ChevronRight aria-hidden="true" className="h-4 w-4 flex-shrink-0 text-ink-soft" />
        </button>
      </h3>
      {children}
    </section>
  )

  if (error !== null) {
    return shell(
      <div className="flex flex-col items-start gap-2">
        <p role="status" className="text-sm text-status-error-strong">
          {error}
        </p>
        {/* 只说坏了不给出路等于只做了一半。 */}
        <button type="button" className={buttonClass} onClick={() => void load()}>
          重试
        </button>
      </div>,
    )
  }

  if (stats === null) {
    return shell(
      // 骨架屏而不是白屏：领域名已经在了，卡片轮廓也在，只有数字还没到。
      // 一屏空白读起来像页面没加载出来。
      <div
        data-testid={`domain-card-${domain.tenant_id}-skeleton`}
        aria-hidden="true"
        className="grid grid-cols-2 gap-3"
      >
        {[0, 1, 2, 3].map((i) => (
          <div
              key={i}
              className="h-10 animate-pulse rounded-control bg-interactive-hover motion-reduce:animate-none"
            />
        ))}
      </div>,
    )
  }

  // 判据是「一个实体都没有」，不是「四个数字全为 0」。
  //
  // 「文档 3、实体 0」是一个常见状态而不是边角情况：本体没确认时文档管线
  // 会跳过图谱抽取（ingestion/pipeline.py），传进去的文档不会变成实体。
  // 按"全为 0"判的话这一档落进 else 分支，而待审又是 0——那张卡上一个按钮
  // 都没有，用户看到三个 0 和一个 3，不知道该干嘛。
  //
  // 这正是导航重排删掉 AdminLanding 之后要由看板接住的引导：那个落地分流
  // 原本就是按"本体确认了没有"分的。文档已经传了的话下一步是去建本体
  // （抽取卡在那儿），一个文档都没有的话下一步才是导入。
  const hasNoEntities = stats.term_count === 0
  const nextStep = stats.document_count > 0
    ? { label: '去确认本体', to: ADMIN_ROUTES.ontology, why: '传进来的文档还没变成实体——本体没确认时抽取会跳过。' }
    : { label: '去导入数据', to: ADMIN_ROUTES.documents, why: '这个领域还没有数据。' }

  return shell(
    <>
      {/* 规模和待办分开排，两个理由。
          一是它们不是一类东西：前四个说"这里有多少东西"，待审说"有多少事
          等着你"。项目在侧边栏徽标里早就区分了 todo 和 count
          （useNavBadges），这里此前没区分——五个数字长得一模一样。
          二是五个数字塞进两列，第五个独占半行留个洞。四个填满 2×2。 */}
      <dl className="grid grid-cols-2 gap-3">
        <Stat label="实体" value={stats.term_count} />
        <Stat label="关系" value={stats.edge_count} />
        <Stat label="文档" value={stats.document_count} />
        {/* 表格/数据库导入进来的实体行数，是「实体」的子集。少了它用户分不清
            两万个实体里多少是表格导进来的、多少是文档抽出来的——而这两条
            路径的修法完全不同。 */}
        <Stat label="表格行" value={stats.sheet_row_count} />
      </dl>
      <dl className="flex items-baseline gap-2 border-t border-subtle pt-3">
        <dt className="text-xs text-ink-soft">待审</dt>
        <dd
          className={`font-mono text-lg font-semibold tabular-nums ${
            stats.pending_review_count > 0 ? 'text-status-error-strong' : 'text-ink-soft'
          }`}
        >
          {stats.pending_review_count.toLocaleString()}
        </dd>
        {/* 不靠颜色单独表意：0 的时候把"没有事等着你"直接说出来。 */}
        {stats.pending_review_count === 0 && (
          <span className="text-xs text-ink-soft">没有待处理的</span>
        )}
      </dl>

      {/* 失效的手写引导问题（spec 前台硬规矩之二：失效了必须有人知道）。
          只在数字人配置页能看见的话，要用户主动去翻——那正是「默默消失」。
          0 时不显示：恒显示的话用户很快就不看它了。 */}
      {stats.stale_question_count > 0 && (
        <button
          type="button"
          className={buttonClass}
          disabled={going}
          onClick={() => void goTo(ADMIN_ROUTES.persona)}
        >
          {stats.stale_question_count} 条引导问题失效，去修
        </button>
      )}

      {hasNoEntities ? (
        // 0 只说明"这里是空的"，不说明该干什么。
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-ink-soft">{nextStep.why}</p>
          <button
            type="button"
            className={buttonClass}
            disabled={going}
            onClick={() => void goTo(nextStep.to)}
          >
            {nextStep.label}
          </button>
        </div>
      ) : (
        stats.pending_review_count > 0 && (
          // 待办为 0 时不给入口：那不是一件等着你做的事，点进去是个空队列。
          <button
            type="button"
            className={buttonClass}
            disabled={going}
            onClick={() => void goTo(ADMIN_ROUTES.reviewRelations)}
          >
            去处理 {stats.pending_review_count} 项待审
          </button>
        )
      )}
    </>,
  )
}

const buttonClass = `min-h-[36px] cursor-pointer self-start rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-ink-soft">{label}</dt>
      {/* 千分位：1204883 读不出来是一百二十万还是十二万。tabular-nums 让
          四个数字的位数对齐，扫一眼就能比大小。 */}
      <dd className="font-mono text-lg font-semibold tabular-nums text-ink">
        {value.toLocaleString()}
      </dd>
    </div>
  )
}
