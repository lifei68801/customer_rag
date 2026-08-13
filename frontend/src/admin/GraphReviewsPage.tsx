import { useCallback, useEffect, useRef, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { Pager } from './Pager'
import { StandardNameInput } from './StandardNameInput'
import { fetchGraphTerms, type GraphTerm } from './termsApi'

const PAGE_SIZE = 20

interface PendingReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  source: string
  evidence: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
}

interface ResolvedReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  source: string
  evidence: string
  status: string
  resolved_at: string
  resolved_note: string | null
}

type Tab = 'pending' | 'history'
type HistoryFilter = 'all' | 'approved' | 'rejected'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function GraphReviewsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [tab, setTab] = useState<Tab>('pending')
  const [historyFilter, setHistoryFilter] = useState<HistoryFilter>('all')
  const [pending, setPending] = useState<PendingReview[]>([])
  const [pendingLoaded, setPendingLoaded] = useState(false)
  const [history, setHistory] = useState<ResolvedReview[]>([])
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const [drafts, setDrafts] = useState<Record<number, { subject: string; object: string }>>({})
  const [rejectNotes, setRejectNotes] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  // 任意一条正在批准/驳回时，全部行的按钮一起禁用——不是只禁用被点的那一行。
  // 只锁一行的话，点另一行会因为下面的 processingId 二次校验静默 return，
  // 但那一行看起来还是"启用"的，等于按钮看着能点、点了却没反应。
  const [processingId, setProcessingId] = useState<number | null>(null)
  const [graphTerms, setGraphTerms] = useState<GraphTerm[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [batchRejectNote, setBatchRejectNote] = useState('')
  const [batchProcessing, setBatchProcessing] = useState(false)
  const [batchResult, setBatchResult] = useState<{
    success: number
    failures: { id: number; error: string }[]
  } | null>(null)
  const [pendingPage, setPendingPage] = useState(1)
  const [pendingTotal, setPendingTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(1)
  const [historyTotal, setHistoryTotal] = useState(0)

  useEffect(() => {
    document.title = '知识图谱审核 · 管理后台'
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    fetchGraphTerms(sessionToken)
      .then(setGraphTerms)
      .catch((err) => {
        console.error('加载术语表失败', err)
      })
  }, [sessionToken])

  // 切换租户时两个 tab 的页码都要回到第一页——不然停留在深页码切租户，
  // 新租户数据少的话会连续触发好几轮"清空自动退页"，页面在几个请求
  // 之间来回闪烁；数据多的话则是静默停在中间某一页，用户看不出发生了
  // 什么。
  useEffect(() => {
    setPendingPage(1)
    setHistoryPage(1)
  }, [tenantId])

  // 切换历史筛选条件时，先假定"还没加载"，避免在新数据到达前继续展示上一个
  // 筛选条件的结果、却顶着新筛选条件的标签，看起来像是查询结果。
  useEffect(() => {
    setHistoryLoaded(false)
    setHistoryPage(1)
  }, [historyFilter])

  // 批准/驳回把当前页清空后（比如最后一页只剩一条、处理完变空），自动
  // 退回上一页，而不是让用户停留在一个明明还有数据、只是 offset 超出
  // 范围而显示"没有记录"的空页面。
  useEffect(() => {
    if (pendingLoaded && pending.length === 0 && pendingPage > 1) {
      setPendingPage((page) => page - 1)
    }
  }, [pendingLoaded, pending.length, pendingPage])

  useEffect(() => {
    if (historyLoaded && history.length === 0 && historyPage > 1) {
      setHistoryPage((page) => page - 1)
    }
  }, [historyLoaded, history.length, historyPage])

  // 快速连续翻页会同时有多个请求在途；每次发起请求前递增各自的序号，
  // 响应回来时只有序号仍是"最新"的那一个才允许写入 state——旧请求的
  // 响应即使后到，也不会覆盖新请求已经写入的数据。
  const pendingRequestIdRef = useRef(0)
  const historyRequestIdRef = useRef(0)

  const refreshPending = useCallback(async () => {
    if (!sessionToken) return
    const requestId = ++pendingRequestIdRef.current
    try {
      const response = await adminFetch(
        `/api/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=pending&page=${pendingPage}&page_size=${PAGE_SIZE}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载待审核列表失败'))
      }
      const data = (await response.json()) as { reviews: PendingReview[]; total: number }
      if (requestId !== pendingRequestIdRef.current) return
      setPending(data.reviews)
      setPendingTotal(data.total)
      setDrafts(
        Object.fromEntries(
          data.reviews.map((review) => [
            review.review_id,
            {
              subject: review.suggested_subject_standard_name ?? '',
              object: review.suggested_object_standard_name ?? '',
            },
          ]),
        ),
      )
    } catch (err) {
      if (requestId !== pendingRequestIdRef.current) return
      setError(err instanceof Error ? err.message : '加载待审核列表失败')
    } finally {
      if (requestId === pendingRequestIdRef.current) {
        setPendingLoaded(true)
      }
    }
  }, [sessionToken, tenantId, pendingPage])

  useEffect(() => {
    setSelectedIds(new Set())
    setBatchResult(null)
  }, [pending])

  const selectedReviews = pending.filter((review) => selectedIds.has(review.review_id))
  const allOnPageSelected =
    pending.length > 0 && pending.every((review) => selectedIds.has(review.review_id))
  const canBatchApprove =
    selectedReviews.length > 0 &&
    selectedReviews.every(
      (review) => drafts[review.review_id]?.subject && drafts[review.review_id]?.object,
    )

  const toggleSelected = (reviewId: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(reviewId)) {
        next.delete(reviewId)
      } else {
        next.add(reviewId)
      }
      return next
    })
  }

  const toggleSelectAllOnPage = () => {
    setSelectedIds(allOnPageSelected ? new Set() : new Set(pending.map((r) => r.review_id)))
  }

  const refreshHistory = useCallback(async () => {
    if (!sessionToken) return
    const requestId = ++historyRequestIdRef.current
    try {
      const response = await adminFetch(
        `/api/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=${historyFilter}&page=${historyPage}&page_size=${PAGE_SIZE}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载历史记录失败'))
      }
      const data = (await response.json()) as { reviews: ResolvedReview[]; total: number }
      if (requestId !== historyRequestIdRef.current) return
      setHistory(data.reviews)
      setHistoryTotal(data.total)
    } catch (err) {
      if (requestId !== historyRequestIdRef.current) return
      setError(err instanceof Error ? err.message : '加载历史记录失败')
    } finally {
      if (requestId === historyRequestIdRef.current) {
        setHistoryLoaded(true)
      }
    }
  }, [sessionToken, tenantId, historyFilter, historyPage])

  useEffect(() => {
    setError(null)
    if (tab === 'pending') {
      refreshPending().catch((err) => {
        console.error('待审核列表刷新失败', err)
      })
    } else {
      refreshHistory().catch((err) => {
        console.error('历史记录刷新失败', err)
      })
    }
  }, [tab, refreshPending, refreshHistory])

  const handleApprove = async (reviewId: number) => {
    if (!sessionToken) return
    // UI 上按钮已经用 disabled 挡了，这里再查一次 processingId 是双保险：
    // disabled 只挡鼠标/键盘触发，挡不住代码里其它路径直接调这个函数。
    if (processingId !== null || batchProcessing) return
    const draft = drafts[reviewId]
    if (!draft?.subject || !draft?.object) return
    setError(null)
    setProcessingId(reviewId)
    try {
      const response = await adminFetch(`/api/admin/graph-reviews/${reviewId}/approve`, sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: tenantId,
          subject_standard_name: draft.subject,
          object_standard_name: draft.object,
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '批准失败'))
      }
      await refreshPending()
    } catch (err) {
      setError(err instanceof Error ? err.message : '批准失败')
    } finally {
      setProcessingId(null)
    }
  }

  const handleReject = async (reviewId: number) => {
    if (!sessionToken) return
    if (processingId !== null || batchProcessing) return
    setError(null)
    setProcessingId(reviewId)
    try {
      const response = await adminFetch(`/api/admin/graph-reviews/${reviewId}/reject`, sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId, note: rejectNotes[reviewId] || null }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '驳回失败'))
      }
      setRejectNotes((prev) => {
        const next = { ...prev }
        delete next[reviewId]
        return next
      })
      await refreshPending()
    } catch (err) {
      setError(err instanceof Error ? err.message : '驳回失败')
    } finally {
      setProcessingId(null)
    }
  }

  const handleBatchApprove = async () => {
    if (!sessionToken || batchProcessing || processingId !== null || !canBatchApprove) return
    setBatchProcessing(true)
    setBatchResult(null)
    setError(null)
    const failures: { id: number; error: string }[] = []
    let success = 0
    for (const review of selectedReviews) {
      const draft = drafts[review.review_id]
      try {
        const response = await adminFetch(
          `/api/admin/graph-reviews/${review.review_id}/approve`,
          sessionToken,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              tenant_id: tenantId,
              subject_standard_name: draft.subject,
              object_standard_name: draft.object,
            }),
          },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '批准失败'))
        }
        success += 1
      } catch (err) {
        failures.push({
          id: review.review_id,
          error: err instanceof Error ? err.message : '批准失败',
        })
      }
    }
    setBatchResult({ success, failures })
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleBatchReject = async () => {
    if (!sessionToken || batchProcessing || processingId !== null) return
    if (selectedReviews.length === 0) return
    setBatchProcessing(true)
    setBatchResult(null)
    setError(null)
    const failures: { id: number; error: string }[] = []
    let success = 0
    for (const review of selectedReviews) {
      try {
        const response = await adminFetch(
          `/api/admin/graph-reviews/${review.review_id}/reject`,
          sessionToken,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tenant_id: tenantId, note: batchRejectNote || null }),
          },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '驳回失败'))
        }
        success += 1
      } catch (err) {
        failures.push({
          id: review.review_id,
          error: err instanceof Error ? err.message : '驳回失败',
        })
      }
    }
    setBatchResult({ success, failures })
    setBatchRejectNote('')
    setBatchProcessing(false)
    await refreshPending()
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">知识图谱审核（租户：{tenantId}）</h1>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => setTab('pending')}
          className={`min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition ${focusRing} ${
            tab === 'pending' ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
          }`}
        >
          待审核
        </button>
        <button
          type="button"
          onClick={() => setTab('history')}
          className={`min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition ${focusRing} ${
            tab === 'history' ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
          }`}
        >
          历史记录
        </button>
      </div>

      {error && (
        <p
          role="alert"
          className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm"
        >
          {error}
        </p>
      )}

      {tab === 'pending' && !pendingLoaded && <p className="text-ink-soft">加载中…</p>}
      {tab === 'pending' && pendingLoaded && pending.length > 0 && (
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={allOnPageSelected}
            onChange={toggleSelectAllOnPage}
            aria-label="全选本页"
          />
          全选本页（{pending.length} 条）
        </label>
      )}
      {tab === 'pending' &&
        pendingLoaded &&
        pending.map((review) => (
          <div
            key={review.review_id}
            className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
          >
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                type="checkbox"
                checked={selectedIds.has(review.review_id)}
                onChange={() => toggleSelected(review.review_id)}
                aria-label={`选中候选 ${review.review_id}`}
              />
              选中批量处理
            </label>
            <p className="text-sm text-ink-soft">
              候选：{review.subject_candidate} —[{review.relation_type}]→{' '}
              {review.object_candidate}（原因：{review.reason}）
            </p>
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l-2 border-ink pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
            <div className="flex gap-3">
              <StandardNameInput
                value={drafts[review.review_id]?.subject ?? ''}
                onChange={(value) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      subject: value,
                    },
                  }))
                }
                terms={graphTerms}
                placeholder="subject 标准名"
                ariaLabel="subject 标准名"
              />
              <StandardNameInput
                value={drafts[review.review_id]?.object ?? ''}
                onChange={(value) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      object: value,
                    },
                  }))
                }
                terms={graphTerms}
                placeholder="object 标准名"
                ariaLabel="object 标准名"
              />
            </div>
            <textarea
              value={rejectNotes[review.review_id] ?? ''}
              onChange={(event) =>
                setRejectNotes((prev) => ({ ...prev, [review.review_id]: event.target.value }))
              }
              placeholder="驳回备注（可选，仅驳回时提交）"
              aria-label="驳回备注"
              rows={2}
              className="border-2 border-ink bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
            />
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => handleApprove(review.review_id)}
                disabled={
                  !drafts[review.review_id]?.subject ||
                  !drafts[review.review_id]?.object ||
                  processingId !== null
                }
                className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {processingId === review.review_id ? '批准中…' : '批准'}
              </button>
              <button
                type="button"
                onClick={() => handleReject(review.review_id)}
                disabled={processingId !== null}
                className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {processingId === review.review_id ? '驳回中…' : '驳回'}
              </button>
            </div>
          </div>
        ))}
      {tab === 'pending' && selectedReviews.length > 0 && (
        <div className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
          <p className="text-sm font-bold text-ink">已选中 {selectedReviews.length} 条</p>
          <textarea
            value={batchRejectNote}
            onChange={(event) => setBatchRejectNote(event.target.value)}
            placeholder="批量驳回备注（可选，应用到本次选中的所有记录）"
            aria-label="批量驳回备注"
            rows={2}
            className="border-2 border-ink bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <div className="flex gap-3">
            <button
              type="button"
              onClick={handleBatchApprove}
              disabled={!canBatchApprove || batchProcessing || processingId !== null}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {batchProcessing ? '批量处理中…' : '批量通过'}
            </button>
            <button
              type="button"
              onClick={handleBatchReject}
              disabled={batchProcessing || processingId !== null}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {batchProcessing ? '批量处理中…' : '批量驳回'}
            </button>
          </div>
          {!canBatchApprove && (
            <p className="text-xs text-ink-soft">
              批量通过要求选中的每一条都已填好 subject/object 标准名。
            </p>
          )}
          {batchResult && (
            <p className="text-sm text-ink">
              成功 {batchResult.success} 条
              {batchResult.failures.length > 0 &&
                `，失败 ${batchResult.failures.length} 条：${batchResult.failures
                  .map((f) => `#${f.id} ${f.error}`)
                  .join('；')}`}
            </p>
          )}
        </div>
      )}
      {tab === 'pending' && pendingLoaded && pending.length === 0 && (
        <p className="text-ink-soft">当前没有待审核的候选关系。</p>
      )}
      {tab === 'pending' && pendingLoaded && pending.length > 0 && (
        <Pager
          page={pendingPage}
          totalPages={Math.max(1, Math.ceil(pendingTotal / PAGE_SIZE))}
          onPageChange={setPendingPage}
        />
      )}

      {tab === 'history' && (
        <div className="flex gap-2">
          {(['all', 'approved', 'rejected'] as const).map((filter) => (
            <button
              key={filter}
              type="button"
              onClick={() => setHistoryFilter(filter)}
              className={`min-h-[44px] cursor-pointer border-2 border-ink px-3 py-1.5 text-sm font-bold transition ${focusRing} ${
                historyFilter === filter
                  ? 'bg-accent-pink text-ink shadow-brutal-sm'
                  : 'bg-paper text-ink'
              }`}
            >
              {filter === 'all' ? '全部' : filter === 'approved' ? '已批准' : '已驳回'}
            </button>
          ))}
        </div>
      )}

      {tab === 'history' && !historyLoaded && <p className="text-ink-soft">加载中…</p>}
      {tab === 'history' &&
        historyLoaded &&
        history.map((review) => (
          <div
            key={review.review_id}
            className="flex flex-col gap-1 border-2 border-ink bg-card p-4 shadow-brutal-sm"
          >
            <p className="text-sm text-ink">
              {review.subject_candidate} —[{review.relation_type}]→ {review.object_candidate}
            </p>
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l-2 border-ink pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
            <p className="text-xs text-ink-soft">
              {review.status === 'approved' ? '已批准' : '已驳回'} · {review.resolved_at}
              {review.resolved_note && ` · ${review.resolved_note}`}
            </p>
          </div>
        ))}
      {tab === 'history' && historyLoaded && history.length === 0 && (
        <p className="text-ink-soft">还没有处理过的记录。</p>
      )}
      {tab === 'history' && historyLoaded && history.length > 0 && (
        <Pager
          page={historyPage}
          totalPages={Math.max(1, Math.ceil(historyTotal / PAGE_SIZE))}
          onPageChange={setHistoryPage}
        />
      )}
    </div>
  )
}
