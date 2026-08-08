import { useCallback, useEffect, useState } from 'react'
import { adminFetch } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './useAdminTenant'

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

export function GraphReviewsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [tab, setTab] = useState<Tab>('pending')
  const [pending, setPending] = useState<PendingReview[]>([])
  const [history, setHistory] = useState<ResolvedReview[]>([])
  const [drafts, setDrafts] = useState<Record<number, { subject: string; object: string }>>({})
  const [error, setError] = useState<string | null>(null)
  const [processingId, setProcessingId] = useState<number | null>(null)

  const refreshPending = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch(
        `/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=pending`,
        sessionToken,
      )
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? '加载待审核列表失败')
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
    }
  }, [sessionToken, tenantId])

  const refreshHistory = useCallback(async () => {
    if (!sessionToken) return
    try {
      const [approvedRes, rejectedRes] = await Promise.all([
        adminFetch(
          `/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=approved`,
          sessionToken,
        ),
        adminFetch(
          `/admin/graph-reviews?tenant_id=${encodeURIComponent(tenantId)}&status=rejected`,
          sessionToken,
        ),
      ])
      if (!approvedRes.ok || !rejectedRes.ok) {
        throw new Error('加载历史记录失败')
      }
      const approved = (await approvedRes.json()) as { reviews: ResolvedReview[] }
      const rejected = (await rejectedRes.json()) as { reviews: ResolvedReview[] }
      setHistory(
        [...approved.reviews, ...rejected.reviews].sort((a, b) =>
          b.resolved_at.localeCompare(a.resolved_at),
        ),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载历史记录失败')
    }
  }, [sessionToken, tenantId])

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
    const draft = drafts[reviewId]
    if (!draft?.subject || !draft?.object) return
    setError(null)
    setProcessingId(reviewId)
    try {
      const response = await adminFetch(`/admin/graph-reviews/${reviewId}/approve`, sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: tenantId,
          subject_standard_name: draft.subject,
          object_standard_name: draft.object,
        }),
      })
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? '批准失败')
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
    setError(null)
    setProcessingId(reviewId)
    try {
      const response = await adminFetch(`/admin/graph-reviews/${reviewId}/reject`, sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId }),
      })
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail ?? '驳回失败')
      }
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
          className={`min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition ${
            tab === 'pending' ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
          }`}
        >
          待审核
        </button>
        <button
          type="button"
          onClick={() => setTab('history')}
          className={`min-h-[44px] cursor-pointer border-2 border-ink px-4 py-2 text-sm font-bold transition ${
            tab === 'history' ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink'
          }`}
        >
          历史记录
        </button>
      </div>

      {error && (
        <p className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm">
          {error}
        </p>
      )}

      {tab === 'pending' &&
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
                className="flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
              />
            </div>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => handleApprove(review.review_id)}
                disabled={
                  !drafts[review.review_id]?.subject ||
                  !drafts[review.review_id]?.object ||
                  processingId === review.review_id
                }
                className="min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
              >
                批准
              </button>
              <button
                type="button"
                onClick={() => handleReject(review.review_id)}
                disabled={processingId === review.review_id}
                className="min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
              >
                驳回
              </button>
            </div>
          </div>
        ))}
      {tab === 'pending' && pending.length === 0 && (
        <p className="text-ink-soft">当前没有待审核的候选关系。</p>
      )}

      {tab === 'history' &&
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
      {tab === 'history' && history.length === 0 && (
        <p className="text-ink-soft">还没有处理过的记录。</p>
      )}
    </div>
  )
}
