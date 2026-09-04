import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ArrowLeft, ArrowRight, Trash2, Unlink } from 'lucide-react'
import { ADMIN_ROUTES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useConfirm } from './ConfirmContext'
import { useToast } from './ToastContext'
import { deleteTermRelation } from './termsApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface TermRelation {
  direction: 'in' | 'out'
  relation_type: string
  node_key: string
  standard_name: string
  term_type: string | null
}

interface TermDetail {
  node_key: string
  standard_name: string
  aliases: string[]
  term_type: string
  extra_properties: Record<string, unknown>
  source: string
  // null = 读取失败，[] = 确实没有关系。这两者不能混：混为一谈的话，
  // Neo4j 挂掉时每个实体都会被报成孤立的。
  relations: TermRelation[] | null
}

/** 详情页的链接。node_key 形如「公司:可口可乐」，冒号和中文都要编码。 */
export function termDetailPath(nodeKey: string): string {
  return `${ADMIN_ROUTES.terms}/${encodeURIComponent(nodeKey)}`
}

/**
 * 用户是从哪儿点进详情页的。
 *
 * 返回键写死指向实体列表的话，从问答诊断点进来的人会被送到一个跟当前
 * 调查无关的页面，还得回头在几十条问答里找回刚才那一条。
 */
export interface TermOrigin {
  path: string
  label: string
}

const DEFAULT_ORIGIN: TermOrigin = { path: ADMIN_ROUTES.terms, label: '实体列表' }

/**
 * 详情页链接的 to + state。来路走 router state 而不是 query：URL 干净，
 * 而且丢了也只是退回默认去处（分享出去的链接就是这种情况），不会变成
 * 一个没有出口的页面。
 */
export function termDetailLink(nodeKey: string, origin?: TermOrigin) {
  return { to: termDetailPath(nodeKey), state: origin ? { origin } : undefined }
}

/**
 * 一条关系边的人话写法：「主语 -类型-> 宾语」。
 *
 * 删边不可逆，按钮和确认框都得用它——一排只写「删除」的按钮，用户（和
 * 读屏软件）分不出按下去掉的是哪条。direction 是相对当前实体说的，
 * 'in' 表示对端才是主语，写反了指的就是另一条边。
 */
export function relationLabel(relation: TermRelation, termStandardName: string): string {
  const other = relation.standard_name || relation.node_key
  return relation.direction === 'out'
    ? `${termStandardName} -${relation.relation_type}-> ${other}`
    : `${other} -${relation.relation_type}-> ${termStandardName}`
}

/** 同一对实体之间可能有多种关系类型，key 得把类型和方向都算进去。 */
function relationKey(relation: TermRelation): string {
  return `${relation.direction}:${relation.relation_type}:${relation.node_key}`
}

const card = 'rounded-card border border-subtle bg-card p-4'
const sectionTitle = 'font-mono text-sm font-bold uppercase tracking-wide text-ink-soft'

/**
 * 实体详情页。
 *
 * 存在的理由是**关系**：一个实体有没有用，取决于它连着谁。这在列表行里
 * 放不下，而它正是 GraphRAG 的核心——孤立实体占着存储却从不被命中。
 *
 * 独立 URL 也是刚需：问答诊断页要能直接链过来，同事之间要能发链接。
 */
export function TermDetailPage() {
  const { nodeKey = '' } = useParams()
  const { state } = useLocation()
  const origin = (state as { origin?: TermOrigin } | null)?.origin ?? DEFAULT_ORIGIN
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const [term, setTerm] = useState<TermDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  // 删边失败要单独说，不能复用 error——那个一亮整页就只剩一条报错，
  // 属性和别名都看不见了，而它们其实还好好的。
  const [relationError, setRelationError] = useState<string | null>(null)
  const [deletingRelation, setDeletingRelation] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken || !nodeKey) return
    setLoaded(false)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(nodeKey)}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载实体失败'))
      }
      setTerm((await response.json()) as TermDetail)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载实体失败')
      setTerm(null)
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, nodeKey])

  const handleDeleteRelation = async (relation: TermRelation) => {
    if (!sessionToken || !term || deletingRelation !== null) return
    const label = relationLabel(relation, term.standard_name)
    if (!(await confirm(`确定要删除关系「${label}」吗？此操作不可撤销。`))) return
    setRelationError(null)
    setDeletingRelation(relationKey(relation))
    try {
      await deleteTermRelation(sessionToken, tenantId, term.node_key, {
        direction: relation.direction,
        relationType: relation.relation_type,
        otherNodeKey: relation.node_key,
      })
      showToast('已删除关系')
      await refresh()
    } catch (err) {
      setRelationError(err instanceof Error ? err.message : '删除关系失败')
    } finally {
      setDeletingRelation(null)
    }
  }

  useEffect(() => {
    refresh().catch((err) => console.error('加载实体失败', err))
  }, [refresh])

  if (!loaded) return <Skeleton variant="card-list" count={3} />

  if (error || !term) {
    return (
      <div className="flex flex-col gap-4">
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {error ?? '加载实体失败'}
        </p>
        <Link to={origin.path} className="self-start font-bold text-ink underline">
          返回{origin.label}
        </Link>
      </div>
    )
  }

  const outgoing = term.relations?.filter((r) => r.direction === 'out') ?? []
  const incoming = term.relations?.filter((r) => r.direction === 'in') ?? []
  const properties = Object.entries(term.extra_properties)

  return (
    <div data-testid="term-detail" className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Link
          data-testid="term-back-link"
          to={origin.path}
          className="flex items-center gap-1 self-start text-sm text-ink-soft hover:text-ink"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          {origin.label}
        </Link>
        <h1 className="font-mono text-xl font-semibold text-ink">{term.standard_name}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded-chip border border-subtle bg-paper px-2 py-0.5 font-bold text-ink">
            {term.term_type}
          </span>
          <span className="rounded-chip border border-subtle bg-paper px-2 py-0.5 text-ink-soft">
            来源 {term.source}
          </span>
          {/* node_key 是身份，standard_name 只是展示名（ADR-0003）。排查时
              需要看到真正的身份，不然两个同名实体分不出来。 */}
          <code className="rounded-chip border border-subtle bg-paper px-2 py-0.5 font-mono text-ink-soft">
            {term.node_key}
          </code>
        </div>
      </div>

      <section className="flex flex-col gap-2">
        <h2 className={sectionTitle}>别名</h2>
        <div className={card}>
          {term.aliases.length === 0 ? (
            <p className="text-sm text-ink-soft">
              没有别名。问答里用其他说法提到这个实体时不会被命中。
            </p>
          ) : (
            <ul className="flex flex-wrap gap-2">
              {term.aliases.map((alias) => (
                <li
                  key={alias}
                  className="rounded-chip border border-subtle bg-paper px-2 py-0.5 text-sm text-ink"
                >
                  {alias}
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className={sectionTitle}>属性</h2>
        <div className={card}>
          {properties.length === 0 ? (
            <p className="text-sm text-ink-soft">这个类型没有定义额外字段。</p>
          ) : (
            <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-[minmax(8rem,auto)_1fr]">
              {properties.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-mono text-sm text-ink-soft">{key}</dt>
                  <dd className="text-sm text-ink">{String(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className={sectionTitle}>图谱关系</h2>
        {term.relations === null ? (
          // 读取失败跟「确实没有关系」必须分开说。混为一谈的话，Neo4j 挂掉
          // 时每个实体都会被报成孤立的，而那是个严重得多的结论。
          <p role="alert" className={`${card} text-sm text-ink`}>
            无法读取图谱关系（图数据库暂时不可用）。上面的属性来自另一个存储，
            仍然是准确的。
          </p>
        ) : term.relations.length === 0 ? (
          <EmptyState
            icon={Unlink}
            title="这个实体是孤立的，没有任何关系"
            action={
              <span>
                孤立实体对检索基本无用——它占着存储却不会被图谱查询命中。
                通常是本体约束里缺了对应的组合，或者 ETL 映射没接上关联列。
              </span>
            }
          />
        ) : (
          <div className="flex flex-col gap-4">
            {relationError && (
              <p
                role="alert"
                className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink"
              >
                {relationError}
              </p>
            )}
            <RelationGroup
              title="它指向"
              icon={ArrowRight}
              relations={outgoing}
              emptyHint="没有从这个实体出发的关系。"
              origin={origin}
              termStandardName={term.standard_name}
              onDelete={handleDeleteRelation}
              deletingRelation={deletingRelation}
            />
            <RelationGroup
              title="指向它"
              icon={ArrowLeft}
              relations={incoming}
              emptyHint="没有指向这个实体的关系。"
              origin={origin}
              termStandardName={term.standard_name}
              onDelete={handleDeleteRelation}
              deletingRelation={deletingRelation}
            />
          </div>
        )}
      </section>
    </div>
  )
}

function RelationGroup({
  title,
  icon: Icon,
  relations,
  emptyHint,
  origin,
  termStandardName,
  onDelete,
  deletingRelation,
}: {
  title: string
  icon: typeof ArrowRight
  relations: TermRelation[]
  emptyHint: string
  // 沿着关系一路点下去，返回键还得指回最初的起点——每跳一次就把来路
  // 换成上一个实体的话，回去要点的次数和跳的次数一样多。
  origin: TermOrigin
  termStandardName: string
  onDelete: (relation: TermRelation) => void
  deletingRelation: string | null
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <h3 className="flex items-center gap-1.5 text-sm font-bold text-ink">
        <Icon aria-hidden="true" className="h-4 w-4 text-ink-soft" />
        {title}
      </h3>
      {relations.length === 0 ? (
        <p className="text-sm text-ink-soft">{emptyHint}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {relations.map((relation) => (
            <li
              key={relationKey(relation)}
              className="flex flex-wrap items-center gap-2 rounded-card border border-subtle bg-card px-3 py-2 text-sm"
            >
              <span className="rounded-chip bg-accent-secondary px-2 py-0.5 text-xs font-bold text-on-accent">
                {relation.relation_type}
              </span>
              <Link
                {...termDetailLink(relation.node_key, origin)}
                className="font-bold text-ink underline underline-offset-2 hover:text-accent-primary"
              >
                {relation.standard_name}
              </Link>
              {relation.term_type && (
                <span className="text-xs text-ink-soft">{relation.term_type}</span>
              )}
              <button
                type="button"
                // 无障碍名字里带上整条边——一排都叫"删除"的按钮，读屏软件
                // 念出来完全一样，而这个操作删错了收不回来。
                aria-label={`删除关系 ${relationLabel(relation, termStandardName)}`}
                title="删除这条关系"
                disabled={deletingRelation !== null}
                onClick={() => onDelete(relation)}
                className="ml-auto cursor-pointer rounded-control border border-subtle bg-card p-1.5 text-ink-soft transition hover:text-status-error-strong disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Trash2 aria-hidden="true" className="h-4 w-4" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
