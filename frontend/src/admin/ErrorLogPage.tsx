import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, FileWarning, MessageSquareOff, TableProperties } from 'lucide-react'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const actionClass = `flex min-h-[36px] shrink-0 cursor-pointer items-center rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

/** 本体结构页上用这个参数带出「刚才答不出来的那个问题」。 */
export const MODEL_FROM_QUESTION_KEY = 'question'

type TabKey = 'documents' | 'etlRows' | 'qa'

interface DocumentFailure {
  job_id: string
  file_path: string
  last_error: string | null
  attempts: number
  updated_at: string
}

interface EtlSkippedRow {
  id: number
  run_id: string
  label: string
  source_file: string
  row_number: number
  reason: string
  created_at: string
}

interface QaFailure {
  id: number
  session_id: string
  question: string
  answer: string
  outcome: string
  created_at: string
}

interface Counts {
  documents: number
  etl_rows: number
  qa: number
}

const TABS: { key: TabKey; label: string; path: string; countKey: keyof Counts }[] = [
  { key: 'documents', label: '文档失败', path: 'documents', countKey: 'documents' },
  { key: 'etlRows', label: '表格跳行', path: 'etl-rows', countKey: 'etl_rows' },
  { key: 'qa', label: '问答未命中', path: 'qa', countKey: 'qa' },
]

/** 文件名。整条路径太长，而用户认的是文件名。 */
function basename(path: string): string {
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/**
 * 报错明细（spec D7）。
 *
 * 三个来源分成三个页签，**因为它们的修复动作完全不同**：文档失败去重试，
 * 表格跳行去改表格，问答未命中去建模。压成一个"错误列表"等于让用户自己猜
 * 该干什么，而这三件事往往落在三个不同的人手上。
 *
 * 每一页各自取数、各自记错误：一个来源挂了就把整页换成一句错误的话，
 * 另外两类的问题也跟着看不见了——而它们跟这次故障毫无关系。
 */
export function ErrorLogPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [tab, setTab] = useState<TabKey>('documents')
  const [counts, setCounts] = useState<Counts | null>(null)
  const [documents, setDocuments] = useState<DocumentFailure[] | null>(null)
  const [etlRows, setEtlRows] = useState<EtlSkippedRow[] | null>(null)
  const [qa, setQa] = useState<QaFailure[] | null>(null)
  // 每一页一条错误，不是全页一条。
  const [errors, setErrors] = useState<Partial<Record<TabKey, string>>>({})
  const [retrying, setRetrying] = useState<string | null>(null)

  useEffect(() => {
    document.title = `${PAGE_TITLES.errors} · 管理后台`
  }, [])

  const fetchJson = useCallback(
    async (suffix: string) => {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId ?? '')}/errors/${suffix}`,
        sessionToken ?? '',
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '没拉到'))
      }
      return response.json()
    },
    [sessionToken, tenantId],
  )

  const loadAll = useCallback(async () => {
    if (!sessionToken || !tenantId) return
    const nextErrors: Partial<Record<TabKey, string>> = {}
    const settle = async (key: TabKey, suffix: string, apply: (items: never[]) => void) => {
      try {
        const body = await fetchJson(suffix)
        apply(body.items)
      } catch (err) {
        // 记在这一页名下。整页共用一条的话，后失败的那个会盖掉先失败的，
        // 用户看到的错误跟他正在看的页签对不上。
        nextErrors[key] = err instanceof Error ? err.message : '没拉到'
        apply([])
      }
    }
    await Promise.all([
      settle('documents', 'documents', (items) => setDocuments(items as DocumentFailure[])),
      settle('etlRows', 'etl-rows', (items) => setEtlRows(items as EtlSkippedRow[])),
      settle('qa', 'qa', (items) => setQa(items as QaFailure[])),
      fetchJson('counts')
        .then((body: Counts) => setCounts(body))
        // 角标拉不到就不显示角标，**不编一个 0 出来**——0 的意思是"这一类
        // 没问题"，那是一句这时并不知道真假的话。
        .catch(() => setCounts(null)),
    ])
    setErrors(nextErrors)
  }, [fetchJson, sessionToken, tenantId])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  const handleRetry = async (jobId: string) => {
    if (!sessionToken || !tenantId) return
    setRetrying(jobId)
    try {
      // 复用文档页那个既有端点。重试到底允许在哪些状态下发生，判据在
      // ingestion_queue.retry_job 里，那里有它的依据——这一页重写一遍的话
      // 两处判断迟早会分叉。
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/documents/jobs/${encodeURIComponent(jobId)}/retry`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '重试没发出去'))
      }
      await loadAll()
    } catch (err) {
      setErrors((prev) => ({
        ...prev,
        documents: err instanceof Error ? err.message : '重试没发出去',
      }))
    } finally {
      setRetrying(null)
    }
  }

  const loading = documents === null && etlRows === null && qa === null
  const error = errors[tab]

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.errors}</h1>
        <p className="text-sm text-ink-soft">
          有什么坏了、该做什么。三类问题的修复动作不一样，所以分开列。
        </p>
      </div>

      <div role="tablist" aria-label="报错来源" className="flex flex-wrap gap-2">
        {TABS.map((item) => {
          const selected = tab === item.key
          const count = counts?.[item.countKey]
          return (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => setTab(item.key)}
              className={`flex min-h-[36px] cursor-pointer items-center gap-2 rounded-control border border-subtle px-3 text-sm font-bold transition ${focusRing} ${
                selected ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink'
              }`}
            >
              {item.label}
              {/* 0 也显示。这一页的三个数字是它的全部意义：「文档失败 0」是
                  用户需要看到的结论，不显示的话它和"还没拉到"长得一模一样。
                  拉不到时 count 是 undefined，那时才不显示——编一个 0 出来
                  是在说一句不知真假的话。 */}
              {count !== undefined && (
                <span
                  className={`rounded-chip px-1.5 py-0.5 text-xs ${
                    selected ? 'bg-on-accent/20' : 'border border-ink-soft text-ink-soft'
                  }`}
                >
                  {count.toLocaleString()}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {error !== undefined && (
        <p
          role="alert"
          className="rounded-card border border-subtle bg-card p-3 text-sm text-status-error-strong"
        >
          {error}
        </p>
      )}

      {loading && <Skeleton variant="card-list" count={3} />}

      {!loading && tab === 'documents' && (
        <DocumentsTab
          items={documents ?? []}
          hasError={errors.documents !== undefined}
          retrying={retrying}
          onRetry={handleRetry}
        />
      )}
      {!loading && tab === 'etlRows' && (
        <EtlRowsTab items={etlRows ?? []} hasError={errors.etlRows !== undefined} />
      )}
      {!loading && tab === 'qa' && <QaTab items={qa ?? []} hasError={errors.qa !== undefined} />}
    </div>
  )
}

const rowClass =
  'flex flex-wrap items-center justify-between gap-3 rounded-card border border-subtle bg-card p-3 text-sm'

function DocumentsTab({
  items,
  hasError,
  retrying,
  onRetry,
}: {
  items: DocumentFailure[]
  hasError: boolean
  retrying: string | null
  onRetry: (jobId: string) => void
}) {
  // 拉取失败时不说"没有失败的文档"——那是一句可能不实的断言，上面那条
  // 错误横幅已经说了发生了什么。
  if (items.length === 0 && !hasError) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="没有失败的文档"
        action={
          <>
            所有上传的文档都处理完了。新上传的文档在{' '}
            <Link to={ADMIN_ROUTES.documents} className="font-bold underline">
              文档导入
            </Link>{' '}
            里看进度。
          </>
        }
      />
    )
  }
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <li key={item.job_id} className={rowClass}>
          <span className="flex flex-wrap items-center gap-2">
            <FileWarning aria-hidden="true" className="h-4 w-4 shrink-0 text-ink-soft" />
            <span className="font-bold text-ink">{basename(item.file_path)}</span>
            {/* 具体错误，不是"失败了"。OCR 超时和格式不支持要做的事完全
                不同：一个重试，一个换文件。 */}
            <span className="text-ink-soft">
              · {item.last_error ?? '没有留下错误信息'} · 试过 {item.attempts} 次 ·{' '}
              {item.updated_at}
            </span>
          </span>
          <button
            type="button"
            className={actionClass}
            disabled={retrying !== null}
            onClick={() => onRetry(item.job_id)}
          >
            {retrying === item.job_id ? '重试中…' : '重试'}
          </button>
        </li>
      ))}
    </ul>
  )
}

function EtlRowsTab({ items, hasError }: { items: EtlSkippedRow[]; hasError: boolean }) {
  if (items.length === 0 && !hasError) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="没有被跳过的行"
        action={
          <>
            最近的导入没跳任何一行。跑批记录在{' '}
            <Link to={ADMIN_ROUTES.etl} className="font-bold underline">
              表格导入
            </Link>{' '}
            里。
          </>
        }
      />
    )
  }
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <li key={item.id} className={rowClass}>
          <span className="flex flex-wrap items-center gap-2">
            <TableProperties aria-hidden="true" className="h-4 w-4 shrink-0 text-ink-soft" />
            <span className="font-bold text-ink">{item.source_file}</span>
            {/* 行号必须写出来：只说原因的话，用户得在两万行里自己找那一行。 */}
            <span className="text-ink-soft">
              第 {item.row_number.toLocaleString()} 行 · {item.reason} · {item.label} ·{' '}
              {item.created_at}
            </span>
          </span>
          <span className="shrink-0 text-xs text-ink-soft">改表格后重新导入</span>
        </li>
      ))}
    </ul>
  )
}

function QaTab({ items, hasError }: { items: QaFailure[]; hasError: boolean }) {
  if (items.length === 0 && !hasError) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="没有答不出来的提问"
        action={
          <>
            最近的问答都给出了答案。逐条回看在{' '}
            <Link to={ADMIN_ROUTES.diagnostics} className="font-bold underline">
              问答明细
            </Link>{' '}
            里。
          </>
        }
      />
    )
  }
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <li key={item.id} className={rowClass}>
          <span className="flex flex-wrap items-center gap-2">
            <MessageSquareOff aria-hidden="true" className="h-4 w-4 shrink-0 text-ink-soft" />
            <span className="font-bold text-ink">「{item.question}」</span>
            <span className="text-ink-soft">
              · {item.outcome === 'no_match' ? '没有匹配到' : '处理时报错'} · {item.created_at}
            </span>
          </span>
          {item.outcome === 'no_match' ? (
            // 带上这个问题跳过去：到了本体页还要自己回忆刚才问的是什么，
            // 这个入口就白给了。
            <Link
              to={`${ADMIN_ROUTES.ontology}?${MODEL_FROM_QUESTION_KEY}=${encodeURIComponent(item.question)}`}
              className={actionClass}
            >
              去建模
            </Link>
          ) : (
            // 报错不是建模能解决的——那是服务端的问题，本体一个字都不用改。
            <Link to={`${ADMIN_ROUTES.diagnostics}?id=${item.id}`} className={actionClass}>
              问答明细
            </Link>
          )}
        </li>
      ))}
    </ul>
  )
}
