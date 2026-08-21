import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { deleteTerm, fetchTermsPage, updateTerm, type TermRecord } from './termsApi'
import { useToast } from './ToastContext'
import { adminFetch } from './adminApi'
import { Pager } from './Pager'

const PAGE_SIZE = 20

type SourceFilter = 'all' | 'manual' | 'etl' | 'review' | 'unknown'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface TermDraft {
  standard_name: string
  aliases: string
  term_type: string
}

function toDraft(term: TermRecord): TermDraft {
  return {
    standard_name: term.standard_name,
    aliases: term.aliases.join(', '),
    term_type: term.term_type,
  }
}

function draftToRecord(draft: TermDraft): TermRecord {
  return {
    standard_name: draft.standard_name.trim(),
    aliases: draft.aliases
      .split(',')
      .map((alias) => alias.trim())
      .filter((alias) => alias.length > 0),
    term_type: draft.term_type.trim(),
    // 占位值：本页面创建入口已下线（见 Task 3 brief），draftToRecord 现在
    // 只服务于编辑场景。后端 update_term 永不用 payload 覆盖已有 source
    // （见 terms_store.py），所以这个占位不会污染已有数据。
    source: 'manual',
  }
}

export function TermsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [terms, setTerms] = useState<TermRecord[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)

  const [termTypeOptions, setTermTypeOptions] = useState<string[]>([])
  const [optionsLoaded, setOptionsLoaded] = useState(false)

  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all')

  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<TermDraft | null>(null)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [deletingKey, setDeletingKey] = useState<string | null>(null)

  useEffect(() => {
    document.title = '实体列表 · 管理后台'
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    setOptionsLoaded(false)
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken)
      .then((res) => res.json())
      .then((data: { term_types: { value: string }[] }) =>
        setTermTypeOptions(data.term_types.map((t) => t.value)),
      )
      .catch((err) => {
        console.error('加载实体类型枚举失败', err)
        return null
      })
      .finally(() => setOptionsLoaded(true))
  }, [sessionToken, tenantId])

  // 快速连续翻页会同时有多个请求在途；每次发起请求前递增请求序号，响应回来
  // 时只有序号仍是"最新"的那一个才允许写入 state——旧请求的响应即使后到，
  // 也不会覆盖新请求已经写入的数据。（照抄 GraphReviewsPage.tsx 的模式。）
  const refreshRequestIdRef = useRef(0)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const requestId = ++refreshRequestIdRef.current
    try {
      const data = await fetchTermsPage(
        sessionToken, tenantId, page, PAGE_SIZE,
        sourceFilter === 'all' ? undefined : sourceFilter,
      )
      if (requestId !== refreshRequestIdRef.current) return
      setTerms(data.terms)
      setTotal(data.total)
    } catch (err) {
      if (requestId !== refreshRequestIdRef.current) return
      setError(err instanceof Error ? err.message : '加载术语表失败')
    } finally {
      if (requestId === refreshRequestIdRef.current) {
        setLoaded(true)
      }
    }
  }, [sessionToken, tenantId, page, sourceFilter])

  useEffect(() => {
    refresh().catch((err) => {
      console.error('术语表刷新失败', err)
    })
  }, [refresh])

  useEffect(() => {
    setPage(1)
  }, [tenantId])

  useEffect(() => {
    setPage(1)
  }, [sourceFilter])

  useEffect(() => {
    if (loaded && terms.length === 0 && page > 1) {
      setPage((p) => p - 1)
    }
  }, [loaded, terms.length, page])

  const handleStartEdit = (term: TermRecord) => {
    if (editingKey !== null) return
    setEditingKey(term.standard_name)
    setEditDraft(toDraft(term))
  }

  const handleCancelEdit = () => {
    setEditingKey(null)
    setEditDraft(null)
  }

  const handleSaveEdit = async (originalStandardName: string) => {
    if (!sessionToken || !editDraft || savingKey !== null) return
    if (!editDraft.standard_name.trim()) return
    setError(null)
    setSavingKey(originalStandardName)
    try {
      await updateTerm(sessionToken, tenantId, originalStandardName, draftToRecord(editDraft))
      showToast('已保存')
      setEditingKey(null)
      setEditDraft(null)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新术语失败')
    } finally {
      setSavingKey(null)
    }
  }

  const handleDelete = async (standardName: string) => {
    if (!sessionToken || deletingKey !== null) return
    if (!(await confirm(`确定要删除术语「${standardName}」吗？此操作不可撤销。`))) return
    setError(null)
    setDeletingKey(standardName)
    try {
      await deleteTerm(sessionToken, tenantId, standardName)
      showToast('已删除实体')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除术语失败')
    } finally {
      setDeletingKey(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">实体列表</h1>

      <div className="flex items-center gap-2">
        <label htmlFor="source-filter" className="text-sm font-bold text-ink">
          来源
        </label>
        <select
          id="source-filter"
          value={sourceFilter}
          onChange={(event) => setSourceFilter(event.target.value as SourceFilter)}
          className={`rounded-control border border-subtle bg-paper px-3 py-2 text-ink focus:shadow-soft focus:outline-none ${focusRing}`}
        >
          <option value="all">全部</option>
          <option value="manual">手工</option>
          <option value="etl">表格导入</option>
          <option value="review">文档抽取</option>
          <option value="unknown">未知（历史数据）</option>
        </select>
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink shadow-soft-sm"
        >
          {error}
        </p>
      )}

      {!loaded && <Skeleton variant="table-rows" count={5} />}
      {loaded &&
        terms.map((term) => {
          const isEditing = editingKey === term.standard_name
          return (
            <div
              key={term.standard_name}
              className={`flex flex-col gap-3 rounded-card border border-subtle bg-card shadow-soft-sm ${
                density === 'compact' ? 'p-2.5' : 'p-4'
              }`}
            >
              {!isEditing && (
                <div className="flex items-center justify-between gap-3">
                  <span className="text-ink">
                    <span className="font-bold">{term.standard_name}</span>
                    {term.aliases.length > 0 && (
                      <span className="text-ink-soft">（别名：{term.aliases.join('、')}）</span>
                    )}
                    <span className="text-ink-soft">
                      {' '}
                      · {term.term_type || '（无类型）'}
                    </span>
                    <span className="ml-2 rounded-chip border border-ink-soft px-1.5 py-0.5 text-xs text-ink-soft">
                      来源：{
                        { manual: '手工', etl: '表格导入', review: '文档抽取', unknown: '未知' }[
                          term.source
                        ] ?? term.source
                      }
                    </span>
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleStartEdit(term)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(term.standard_name)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {deletingKey === term.standard_name ? '删除中…' : '删除'}
                    </button>
                  </div>
                </div>
              )}
              {isEditing && editDraft && (
                <>
                  <div className="flex flex-wrap gap-3">
                    <input
                      value={editDraft.standard_name}
                      onChange={(event) =>
                        setEditDraft((prev) =>
                          prev ? { ...prev, standard_name: event.target.value } : prev,
                        )
                      }
                      placeholder="标准名"
                      aria-label={`标准名（${term.standard_name}）`}
                      className={`min-w-[10rem] flex-1 rounded-control border border-subtle bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-soft focus:outline-none ${focusRing}`}
                    />
                    <input
                      value={editDraft.aliases}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, aliases: event.target.value } : prev))
                      }
                      placeholder="别名（逗号分隔）"
                      aria-label={`别名（${term.standard_name}）`}
                      className={`min-w-[10rem] flex-1 rounded-control border border-subtle bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-soft focus:outline-none ${focusRing}`}
                    />
                    <select
                      value={editDraft.term_type}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, term_type: event.target.value } : prev))
                      }
                      aria-label={`类型（${term.standard_name}）`}
                      className={`min-w-[8rem] flex-1 rounded-control border border-subtle bg-paper px-3 py-2 text-ink focus:shadow-soft focus:outline-none ${focusRing}`}
                    >
                      <option value="">（无类型）</option>
                      {optionsLoaded && editDraft.term_type && !termTypeOptions.includes(editDraft.term_type) && (
                        <option value={editDraft.term_type}>{editDraft.term_type}（不在当前本体枚举中）</option>
                      )}
                      {termTypeOptions.map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleSaveEdit(term.standard_name)}
                      disabled={!editDraft.standard_name.trim() || savingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-pink px-4 py-2 font-bold text-ink shadow-soft transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {savingKey === term.standard_name ? '保存中…' : '保存'}
                    </button>
                    <button
                      type="button"
                      onClick={handleCancelEdit}
                      disabled={savingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      取消
                    </button>
                  </div>
                </>
              )}
            </div>
          )
        })}
      {loaded && !error && terms.length === 0 && (
        <p className="text-ink-soft">
          还没有任何实体。实体创建只能通过「
          <Link to="/admin/data-entry/etl" className="font-bold underline">
            表格导入
          </Link>
          」或「
          <Link to="/admin/data-entry/review" className="font-bold underline">
            文档抽取
          </Link>
          」完成。
        </p>
      )}
      {loaded && terms.length > 0 && (
        <Pager page={page} totalPages={Math.max(1, Math.ceil(total / PAGE_SIZE))} onPageChange={setPage} />
      )}
    </div>
  )
}
