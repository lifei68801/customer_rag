import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { MessageSquareOff, Stethoscope } from 'lucide-react'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { termDetailLink } from './TermDetailPage'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface DiagnosticSummary {
  id: number
  session_id: string
  question: string
  answer: string
  created_at: string
}

interface ToolResult {
  tool_call_id: string
  name: string
  content: string
  content_truncated?: boolean
}

interface MentionedTerm {
  node_key: string
  standard_name: string | null
  term_type: string | null
}

interface DiagnosticDetail extends DiagnosticSummary {
  resolved_question: string | null
  used_sources: string[]
  tool_results: ToolResult[]
  mentioned_terms: MentionedTerm[]
}

const card = 'rounded-card border border-subtle bg-card p-4'
const sectionTitle = 'font-mono text-sm font-bold uppercase tracking-wide text-ink-soft'

/**
 * 问答诊断页。
 *
 * 「答错了 → 哪个实体不对」这条路的中间一跳。选一次真实的错误回答，看它
 * 用了哪些工具、匹配到哪些实体，每个实体链到详情页。
 *
 * 输入是历史会话而不是手动重跑：LLM 非确定性，重跑可能复现不出那个错误，
 * 你会对着一个正确的结果找不到问题。
 */
export function DiagnosticsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [list, setList] = useState<DiagnosticSummary[]>([])
  const [selected, setSelected] = useState<DiagnosticDetail | null>(null)
  const [listLoaded, setListLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 选中哪一条存在 URL 里。此前它只活在组件 state 里：刷新一下就散了，
  // 截图发给同事对方打开是空的，从这里点进实体详情也没法再回到这一条。
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('d')

  const base = `/api/admin/${encodeURIComponent(tenantId)}/diagnostics`

  const select = (id: number | null) => {
    const next = new URLSearchParams(params)
    if (id === null) next.delete('d')
    else next.set('d', String(id))
    setParams(next, { replace: true })
  }

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    setListLoaded(false)
    void (async () => {
      try {
        const response = await adminFetch(base, sessionToken)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '加载诊断记录失败'))
        }
        const data = (await response.json()) as { diagnostics: DiagnosticSummary[] }
        if (!cancelled) {
          setList(data.diagnostics)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载诊断记录失败')
      } finally {
        if (!cancelled) setListLoaded(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionToken, base])

  // 换租户时清掉选中项——上一个租户的诊断 id 在这个租户里查不到。跳过
  // 首次挂载：那不是切换，而且会把带着 ?d= 直接打开的链接当场清掉。
  const lastTenant = useRef(tenantId)
  useEffect(() => {
    if (lastTenant.current === tenantId) return
    lastTenant.current = tenantId
    select(null)
    setSelected(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId])

  // 详情跟着 URL 走，而不是跟着点击走：带着 ?d= 直接打开、后退回来，走的
  // 都是这一条路径，不需要各写一遍。
  useEffect(() => {
    if (!sessionToken) return
    if (!selectedId) {
      setSelected(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const response = await adminFetch(`${base}/${selectedId}`, sessionToken)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '加载诊断详情失败'))
        }
        const detail = (await response.json()) as DiagnosticDetail
        if (!cancelled) {
          setSelected(detail)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载诊断详情失败')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionToken, base, selectedId])

  return (
    <div data-testid="diagnostics" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.diagnostics}</h1>
        <p className="text-sm text-ink-soft">
          挑一次答得不对的问答，看它当时匹配到了哪些实体。
          用的是当时留下的快照，不是重新跑一遍——重跑可能复现不出那个错误。
        </p>
      </div>

      {error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        <section className="flex min-w-0 flex-1 flex-col gap-2">
          <h2 className={sectionTitle}>最近的问答</h2>
          {!listLoaded && <Skeleton variant="card-list" count={3} />}
          {listLoaded && list.length === 0 && (
            <EmptyState
              icon={MessageSquareOff}
              title="还没有诊断记录"
              action={
                <span>
                  诊断快照是从这个功能上线之后才开始记的，之前的问答没有留下。
                  去前台问几个问题，再回来这里看。
                </span>
              }
            />
          )}
          <ul className="flex flex-col gap-2">
            {list.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  onClick={() => select(item.id)}
                  aria-current={selected?.id === item.id}
                  className={`flex w-full cursor-pointer flex-col gap-1 rounded-card border px-4 py-3 text-left transition ${
                    selected?.id === item.id
                      ? 'border-accent-primary bg-card'
                      : 'border-subtle bg-card hover:bg-interactive-hover'
                  }`}
                >
                  <span className="font-bold text-ink">{item.question}</span>
                  {/* 答案也列出来：不点开就能认出是哪一次答错的。 */}
                  <span className="line-clamp-2 text-sm text-ink-soft">{item.answer}</span>
                  <span className="font-mono text-xs text-ink-faint">{item.created_at}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>

        {selected && (
          <section className="flex min-w-0 flex-1 flex-col gap-4">
            <h2 className={sectionTitle}>这次用到了什么</h2>

            <div className="flex flex-col gap-2">
              <h3 className="text-sm font-bold text-ink">匹配到的实体</h3>
              {selected.mentioned_terms.length === 0 ? (
                // 这不是「暂无数据」，是个结论。如果问题恰恰出在图谱上，
                // 这一句就是答案。
                <p className={`${card} text-sm text-ink`}>
                  这次问答没有用到图谱——没有匹配到任何实体，答案完全来自向量检索。
                  如果你期待它走图谱，问题在实体匹配那一步，不在实体本身。
                </p>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {selected.mentioned_terms.map((term) => (
                    <li key={term.node_key}>
                      <Link
                        {...termDetailLink(term.node_key, {
                          path: `${ADMIN_ROUTES.diagnostics}?d=${selected.id}`,
                          label: PAGE_TITLES.diagnostics,
                        })}
                        className="flex items-center gap-1.5 rounded-chip border border-subtle bg-card px-2.5 py-1 text-sm text-ink transition hover:bg-interactive-hover"
                      >
                        <span className="font-bold underline underline-offset-2">
                          {term.standard_name ?? term.node_key}
                        </span>
                        {term.term_type && (
                          <span className="text-xs text-ink-soft">{term.term_type}</span>
                        )}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <h3 className="text-sm font-bold text-ink">工具调用</h3>
              {selected.tool_results.length === 0 ? (
                <p className={`${card} text-sm text-ink-soft`}>这次没有调用任何工具。</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {selected.tool_results.map((tool) => (
                    <li key={tool.tool_call_id} className={`${card} flex flex-col gap-2`}>
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="font-mono text-sm font-bold text-ink">{tool.name}</code>
                        {tool.content_truncated && (
                          // 看到一段结果会默认那就是全部，据此得出「只匹配到
                          // 3 条」这样的结论。截断了必须说。
                          <span className="rounded-chip bg-status-error px-2 py-0.5 text-xs font-bold text-on-accent">
                            结果已截断
                          </span>
                        )}
                      </div>
                      <pre className="overflow-x-auto rounded-control bg-paper p-2 font-mono text-xs text-ink-soft">
                        {tool.content}
                      </pre>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {selected.used_sources.length > 0 && (
              <div className="flex flex-col gap-2">
                <h3 className="text-sm font-bold text-ink">引用的文档</h3>
                <ul className="flex flex-wrap gap-2">
                  {selected.used_sources.map((source) => (
                    <li
                      key={source}
                      className="rounded-chip border border-subtle bg-card px-2.5 py-1 font-mono text-xs text-ink-soft"
                    >
                      {source}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
        )}

        {!selected && listLoaded && list.length > 0 && (
          <section className="flex min-w-0 flex-1">
            <EmptyState
              icon={Stethoscope}
              title="选一次问答"
              action={<span>左边挑一条，这里会显示它当时匹配到的实体和调用的工具。</span>}
            />
          </section>
        )}
      </div>
    </div>
  )
}
