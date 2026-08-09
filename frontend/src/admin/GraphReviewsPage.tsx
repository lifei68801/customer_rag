import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface PendingReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  reason: string
  suggested_subject_standard_name: string | null
  suggested_object_standard_name: string | null
}

interface ResolvedReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
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

  useEffect(() => {
    document.title = '知识图谱审核 · 管理后台'
  }, [])

  // 切换历史筛选条件时，先假定"还没加载"，避免在新数据到达前继续展示上一个
  // 筛选条件的结果、却顶着新筛选条件的标签，看起来像是查询结果。
  useEffect(() => {
    setHistoryLoaded(false)
  }, [historyFilter])

  const refreshPending = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch(
        `/api/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=pending`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载待审核列表失败'))
      }
      const data = (await response.json()) as { reviews: PendingReview[] }
      setPending(data.reviews)
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
      setError(err instanceof Error ? err.message : '加载待审核列表失败')
    } finally {
      setPendingLoaded(true)
    }
  }, [sessionToken, tenantId])

  const refreshHistory = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch(
        `/api/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=${historyFilter}`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载历史记录失败'))
      }
      const data = (await response.json()) as { reviews: ResolvedReview[] }
      setHistory(data.reviews)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载历史记录失败')
    } finally {
      setHistoryLoaded(true)
    }
  }, [sessionToken, tenantId, historyFilter])

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
    if (processingId !== null) return
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
    if (processingId !== null) return
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
      {tab === 'pending' &&
        pendingLoaded &&
        pending.map((review) => (
          <div
            key={review.review_id}
            className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
          >
            <p className="text-sm text-ink-soft">
              候选：{review.subject_candidate} —[{review.relation_type}]→{' '}
              {review.object_candidate}（原因：{review.reason}）
            </p>
            <div className="flex gap-3">
              <input
                value={drafts[review.review_id]?.subject ?? ''}
                onChange={(event) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      subject: event.target.value,
                    },
                  }))
                }
                placeholder="subject 标准名"
                aria-label="subject 标准名"
                className="flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
              />
              <input
                value={drafts[review.review_id]?.object ?? ''}
                onChange={(event) =>
                  setDrafts((prev) => ({
                    ...prev,
                    [review.review_id]: {
                      ...prev[review.review_id],
                      object: event.target.value,
                    },
                  }))
                }
                placeholder="object 标准名"
                aria-label="object 标准名"
                className="flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
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
      {tab === 'pending' && pendingLoaded && pending.length === 0 && (
        <p className="text-ink-soft">当前没有待审核的候选关系。</p>
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
            <p className="text-xs text-ink-soft">
              {review.status === 'approved' ? '已批准' : '已驳回'} · {review.resolved_at}
              {review.resolved_note && ` · ${review.resolved_note}`}
            </p>
          </div>
        ))}
      {tab === 'history' && historyLoaded && history.length === 0 && (
        <p className="text-ink-soft">还没有处理过的记录。</p>
      )}
    </div>
  )
}
