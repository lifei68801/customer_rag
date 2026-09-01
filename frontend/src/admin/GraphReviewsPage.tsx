import { useCallback, useEffect, useState } from 'react'
import { EmptyState } from './EmptyState'
import { History } from 'lucide-react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { Pager } from './Pager'
import { StandardNameInput } from './StandardNameInput'
import { TaskStatusBadge } from './TaskStatusBadge'
import { fetchGraphTerms, createTerm, type GraphTerm } from './termsApi'
import { useLatestRequestGuard } from './useLatestRequestGuard'

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
  subject_type_candidate: string | null
  object_type_candidate: string | null
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

interface CreateEntityDraft {
  reviewId: number
  field: 'subject' | 'object'
  standardName: string
  termType: string
  step: 'form' | 'confirm'
  submitting: boolean
  error: string | null
}

type Tab = 'pending' | 'history'
type HistoryFilter = 'all' | 'approved' | 'rejected'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function GraphReviewsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const showToast = useToast()
  const { density } = useAdminDensity()
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
  // 键盘分诊的焦点行。审核是"同一个动作重复几百次"的场景，鼠标在按钮之间
  // 往返是纯损耗——j/k 移动、a 批准、r 驳回、x 选中，手不用离开键盘。
  const [focusedIndex, setFocusedIndex] = useState(0)
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
  const [termTypeOptions, setTermTypeOptions] = useState<string[]>([])
  const [createDraft, setCreateDraft] = useState<CreateEntityDraft | null>(null)
  // 值是评审员在内联创建弹窗里实际确认过的 term_type（不是布尔值）——
  // 批准时要用这个真实类型覆盖 LLM 猜的 subject_type_candidate/
  // object_type_candidate，见 handleApprove/handleBatchApprove 里
  // resolveApprovalTermType 的说明。
  const [justCreated, setJustCreated] = useState<Record<number, { subject?: string; object?: string }>>({})
  // 审核员在"歧义选择器"（见 candidateTermTypesFor）里显式选中的类型——
  // 跟 justCreated 是两回事：justCreated 只在走过内联创建实体流程时才
  // 有值，这里是"标准名已经存在、但同名不同类型有 2 条以上，需要手动
  // 挑一条"的场景。两者共用同一个 resolveApprovalTermType 优先级链。
  const [ambiguityPick, setAmbiguityPick] = useState<Record<number, { subject?: string; object?: string }>>({})

  useEffect(() => {
    document.title = '文档抽取 · 管理后台'
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    fetchGraphTerms(sessionToken, tenantId)
      .then(setGraphTerms)
      .catch((err) => {
        console.error('加载术语表失败', err)
        setError(err instanceof Error ? err.message : '加载术语表失败，标准名自动补全不可用')
      })
  }, [sessionToken, tenantId])

  useEffect(() => {
    if (!sessionToken) return
    adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken)
      .then((res) => res.json())
      .then((data: { term_types: { value: string }[] }) =>
        setTermTypeOptions(data.term_types.map((t) => t.value)),
      )
      .catch((err) => {
        console.error('加载实体类型枚举失败', err)
        return null
      })
  }, [sessionToken, tenantId])

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

  // 请求序号保护逻辑收在 useLatestRequestGuard 里（跟 DocumentsPage.tsx
  // 共用同一个原语）；pending/history 各自独立的派生状态（drafts、共享
  // error 横幅）比较多，不套完整的 usePaginatedAdminList，只换掉计数器
  // 这一部分。
  const pendingGuard = useLatestRequestGuard()
  const historyGuard = useLatestRequestGuard()

  const refreshPending = useCallback(async () => {
    if (!sessionToken) return
    const requestId = pendingGuard.next()
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
      if (!pendingGuard.isLatest(requestId)) return
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
      if (!pendingGuard.isLatest(requestId)) return
      setError(err instanceof Error ? err.message : '加载待审核列表失败')
    } finally {
      if (pendingGuard.isLatest(requestId)) {
        setPendingLoaded(true)
      }
    }
  }, [sessionToken, tenantId, pendingPage, pendingGuard])

  // 只清选中状态，不清 batchResult——batchResult 要留到用户看到汇总为止。
  // 批量提交结束后会调用 refreshPending()，那会产生一个新的 pending 数组
  // 引用，如果这个 effect 也把 batchResult 清掉，汇总面板会在填完的瞬间
  // 就随着 selectedReviews 归零一起消失，用户根本来不及看。
  useEffect(() => {
    setSelectedIds(new Set())
  }, [pending])

  // 批准请求里 subject_term_type/object_term_type 优先用评审员通过内联
  // 创建实体流程实际确认过的类型（justCreated[reviewId][field]）——不能
  // 直接信 LLM 抽取阶段给出的 candidateType（subject_type_candidate/
  // object_type_candidate）。原因跟 handleOpenCreateEntity 里的注释一样：
  // candidateType 只是未经校验的猜测值。如果评审员用内联创建给这一侧建了
  // 一个跟 LLM 猜测不同类型的新实体（比如 LLM 猜"产品"，评审员实际建的是
  // "类目"），批准时继续传 LLM 的猜测值会让后端按错误的类型消歧，解析到
  // 一个同名但完全无关的已有术语，而不是评审员刚创建的那一个——这正是
  // 本次改动要修的 bug（见 2026-08-23 最终评审 Finding I2）。没有走过
  // 内联创建流程的一侧（justCreated 里没有对应记录）行为不变，继续退回
  // candidateType。
  //
  // 注意：这两个函数（以及下面的 candidateTermTypesFor）必须定义在
  // canBatchApprove 之前——canBatchApprove 是一个 const，赋值表达式里
  // 立即调用了 selectedReviews.every(...)，而回调里又同步调用这两个
  // 函数；如果把函数定义放在 canBatchApprove 之后（哪怕只是同一个组件
  // 函数体里靠后几行），只要有勾选项（selectedReviews.length > 0）触发
  // 渲染，这里的调用点就会先于函数自身的 const 声明执行，落入 TDZ
  // （temporal dead zone），直接抛 ReferenceError 把整个页面渲染崩掉。
  // tsc 不会报这类跨闭包的执行顺序错误，只能在这里用注释提醒：改动这段
  // 代码时不要把函数定义挪到 canBatchApprove 后面。
  const resolveApprovalTermType = (
    reviewId: number,
    field: 'subject' | 'object',
    candidateType: string | null | undefined,
  ): string | undefined => {
    const confirmedType = justCreated[reviewId]?.[field]
    if (confirmedType) return confirmedType
    const pickedType = ambiguityPick[reviewId]?.[field]
    if (pickedType) return pickedType
    return candidateType ?? undefined
  }

  // 给定当前输入框里的候选名，算出它在 graphTerms 里实际匹配到的
  // term_type 集合（按 standard_name 或任一 alias 匹配，逻辑对应后端
  // app/graphrag/ontology.py::find_candidate_term_types）。返回 2 个及
  // 以上说明这个名字有歧义，需要审核员显式选一个类型；返回 0 或 1 个
  // 不需要选择器——0 个交给既有的"创建为新实体"流程，1 个直接唯一解析。
  const candidateTermTypesFor = (name: string): string[] => {
    const trimmed = name.trim()
    if (!trimmed) return []
    const types = new Set(
      graphTerms
        .filter((term) => term.standard_name === trimmed || term.aliases.includes(trimmed))
        .map((term) => term.term_type),
    )
    return [...types].sort()
  }

  const selectedReviews = pending.filter((review) => selectedIds.has(review.review_id))
  const allOnPageSelected =
    pending.length > 0 && pending.every((review) => selectedIds.has(review.review_id))
  const canBatchApprove =
    selectedReviews.length > 0 &&
    selectedReviews.every(
      (review) =>
        review.reason !== 'invalid_relation_type' &&
        review.reason !== 'not_in_confirmed_ontology' &&
        drafts[review.review_id]?.subject &&
        drafts[review.review_id]?.object &&
        !(
          candidateTermTypesFor(drafts[review.review_id]?.subject ?? '').length >= 2 &&
          !(justCreated[review.review_id]?.subject ?? ambiguityPick[review.review_id]?.subject)
        ) &&
        !(
          candidateTermTypesFor(drafts[review.review_id]?.object ?? '').length >= 2 &&
          !(justCreated[review.review_id]?.object ?? ambiguityPick[review.review_id]?.object)
        ),
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

  // 键盘分诊。只在待审列表非空、且没有弹窗打开时生效——弹窗里 a/r/x 是
  // 正常输入字符，不能被当成快捷键吞掉。输入框聚焦时同理。
  useEffect(() => {
    if (pending.length === 0 || createDraft) return
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing =
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      if (typing || event.metaKey || event.ctrlKey || event.altKey) return

      const clamp = (index: number) => Math.max(0, Math.min(index, pending.length - 1))
      const current = pending[clamp(focusedIndex)]

      switch (event.key) {
        case 'j':
          event.preventDefault()
          setFocusedIndex((i) => clamp(i + 1))
          break
        case 'k':
          event.preventDefault()
          setFocusedIndex((i) => clamp(i - 1))
          break
        case 'a':
          if (!current || processingId !== null || batchProcessing) return
          event.preventDefault()
          void handleApprove(current.review_id)
          break
        case 'r':
          if (!current || processingId !== null || batchProcessing) return
          event.preventDefault()
          void handleReject(current.review_id)
          break
        case 'x':
          if (!current) return
          event.preventDefault()
          toggleSelected(current.review_id)
          break
        default:
          break
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [pending, focusedIndex, createDraft, processingId, batchProcessing])

  // 列表变短（批准/驳回后那一条被移出）时把焦点收回范围内，否则焦点会
  // 停在一个不存在的行上，看起来像"焦点消失了"。
  useEffect(() => {
    setFocusedIndex((i) => Math.max(0, Math.min(i, pending.length - 1)))
  }, [pending.length])

  const refreshHistory = useCallback(async () => {
    if (!sessionToken) return
    const requestId = historyGuard.next()
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
      if (!historyGuard.isLatest(requestId)) return
      setHistory(data.reviews)
      setHistoryTotal(data.total)
    } catch (err) {
      if (!historyGuard.isLatest(requestId)) return
      setError(err instanceof Error ? err.message : '加载历史记录失败')
    } finally {
      if (historyGuard.isLatest(requestId)) {
        setHistoryLoaded(true)
      }
    }
  }, [sessionToken, tenantId, historyFilter, historyPage, historyGuard])

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
    const review = pending.find((r) => r.review_id === reviewId)
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
          subject_term_type: resolveApprovalTermType(
            reviewId, 'subject', review?.subject_type_candidate,
          ),
          object_term_type: resolveApprovalTermType(
            reviewId, 'object', review?.object_type_candidate,
          ),
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '批准失败'))
      }
      showToast('已批准')
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
      showToast('已驳回')
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
              subject_term_type: resolveApprovalTermType(
                review.review_id, 'subject', review.subject_type_candidate,
              ),
              object_term_type: resolveApprovalTermType(
                review.review_id, 'object', review.object_type_candidate,
              ),
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
    if (success > 0) showToast(`已批准 ${success} 条`)
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
    if (success > 0) showToast(`已驳回 ${success} 条`)
    setBatchRejectNote('')
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleOpenCreateEntity = (reviewId: number, field: 'subject' | 'object', query: string) => {
    const review = pending.find((r) => r.review_id === reviewId)
    const suggestedType =
      (field === 'subject' ? review?.subject_type_candidate : review?.object_type_candidate) ?? ''
    // suggestedType 是 LLM 抽取阶段给出的建议值，从未针对该租户当前已确认
    // 的实体类型枚举校验过——如果不在 termTypeOptions 里，<select> 会渲染出
    // 一个没有匹配项的值（看起来空白/不对），但"下一步"按钮的禁用条件只看
    // !createDraft.termType（非空字符串就算通过），用户仍能继续、最终在
    // 提交时收到一个莫名其妙的 400。这里做同款 clamp：只有确认在当前枚举
    // 里才采用建议值，否则退回空字符串，让下拉框的实际值和按钮的可点状态
    // 不会互相矛盾。
    const clampedSuggestedType = termTypeOptions.includes(suggestedType) ? suggestedType : ''
    setCreateDraft({
      reviewId,
      field,
      standardName: query,
      termType: clampedSuggestedType,
      step: 'form',
      submitting: false,
      error: null,
    })
  }

  const handleSubmitCreateEntity = async () => {
    if (!sessionToken || !createDraft) return
    if (createDraft.step === 'form') {
      if (!createDraft.termType) return
      setCreateDraft({ ...createDraft, step: 'confirm', error: null })
      return
    }
    // step === 'confirm'：真正提交
    setCreateDraft({ ...createDraft, submitting: true, error: null })
    try {
      await createTerm(sessionToken, tenantId, {
        standard_name: createDraft.standardName,
        aliases: [],
        term_type: createDraft.termType,
        source: 'review',
      })
      const { reviewId, field, standardName } = createDraft
      setDrafts((prev) => ({
        ...prev,
        [reviewId]: { ...prev[reviewId], [field]: standardName },
      }))
      setJustCreated((prev) => ({
        ...prev,
        [reviewId]: { ...prev[reviewId], [field]: createDraft.termType },
      }))
      showToast('已创建实体候选')
      setCreateDraft(null)
      // 创建成功后立即重新拉取本页 graphTerms，让同页其它引用同一新实体
      // 的候选行也能立刻搜到它——见 spec 决策 A.4。
      const refreshedTerms = await fetchGraphTerms(sessionToken, tenantId)
      setGraphTerms(refreshedTerms)
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建实体失败'
      // 撞名冲突（TermNameConflictError 映射成 400）时，提示直接从下拉选择，
      // 并自动重新拉取一次 graphTerms——见 spec 决策 A.8。
      setCreateDraft({
        ...createDraft,
        step: 'confirm',
        submitting: false,
        error: `${message}，请刷新后从下拉列表中选择已有项`,
      })
      fetchGraphTerms(sessionToken, tenantId).then(setGraphTerms).catch(() => {})
    }
  }

  const handleCancelCreateEntity = () => setCreateDraft(null)

  // Escape 关闭弹窗——提交中（submitting）时不响应，避免用户中途关闭后
  // fetch 仍在飞行、回调打在已经不存在的 UI 状态上。只在弹窗打开时挂
  // 监听，卸载/关闭时清理，不用全局常驻监听器。
  useEffect(() => {
    if (!createDraft) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !createDraft.submitting) {
        handleCancelCreateEntity()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [createDraft])

  return (
    <div className="flex flex-col gap-6">
      <h1 className="font-mono text-xl font-semibold text-ink">文档抽取（租户：{tenantId}）</h1>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => setTab('pending')}
          className={`min-h-[44px] cursor-pointer rounded-control border border-subtle px-4 py-2 text-sm font-bold transition ${focusRing} ${
            tab === 'pending' ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink'
          }`}
        >
          待审核
        </button>
        <button
          type="button"
          onClick={() => setTab('history')}
          className={`min-h-[44px] cursor-pointer rounded-control border border-subtle px-4 py-2 text-sm font-bold transition ${focusRing} ${
            tab === 'history' ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink'
          }`}
        >
          历史记录
        </button>
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink"
        >
          {error}
        </p>
      )}

      {createDraft && (
        <div
          className="fixed inset-0 z-20 flex items-center justify-center bg-black/40 p-4"
          // 只在点击落在遮罩本身（不是冒泡自弹窗内部内容）时关闭——同一个
          // preventClose-while-submitting 规则也用在这里，跟 Escape 一致。
          onClick={(event) => {
            if (event.target === event.currentTarget && !createDraft.submitting) {
              handleCancelCreateEntity()
            }
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-entity-dialog-title"
            className="flex w-full max-w-md flex-col gap-3 rounded-modal border border-subtle bg-paper p-5"
          >
            {createDraft.step === 'form' && (
              <>
                <p id="create-entity-dialog-title" className="text-sm font-bold text-ink">
                  创建为新实体
                </p>
                <p className="text-sm text-ink-soft">标准名：{createDraft.standardName}</p>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  实体类型
                  <select
                    value={createDraft.termType}
                    onChange={(event) =>
                      setCreateDraft({ ...createDraft, termType: event.target.value })
                    }
                    className={`rounded-control border border-subtle bg-paper px-3 py-2 text-ink focus:outline-none ${focusRing}`}
                  >
                    <option value="">（请选择）</option>
                    {termTypeOptions.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={handleSubmitCreateEntity}
                    disabled={!createDraft.termType}
                    className="min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 font-bold text-on-accent disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    下一步
                  </button>
                  <button
                    type="button"
                    onClick={handleCancelCreateEntity}
                    className="min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 font-bold text-ink"
                  >
                    取消
                  </button>
                </div>
              </>
            )}
            {createDraft.step === 'confirm' && (
              <>
                <p id="create-entity-dialog-title" className="text-sm font-bold text-ink">
                  确认创建
                </p>
                <p className="text-sm text-ink">
                  标准名：{createDraft.standardName}
                  <br />
                  实体类型：{createDraft.termType}
                </p>
                {createDraft.error && (
                  <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
                    {createDraft.error}
                  </p>
                )}
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={handleSubmitCreateEntity}
                    disabled={createDraft.submitting}
                    className="min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 font-bold text-on-accent disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {createDraft.submitting ? '创建中…' : '确认创建'}
                  </button>
                  <button
                    type="button"
                    onClick={handleCancelCreateEntity}
                    disabled={createDraft.submitting}
                    className="min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 font-bold text-ink disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    取消
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {tab === 'pending' && !pendingLoaded && <Skeleton variant="card-list" count={3} />}
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
      {tab === 'pending' && pendingLoaded && pending.length > 0 && (
        <p className="text-xs text-ink-soft">
          键盘：
          <kbd className="mx-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono">j</kbd>
          <kbd className="mr-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono">k</kbd>
          移动 ·
          <kbd className="mx-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono">a</kbd>
          批准 ·
          <kbd className="mx-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono">r</kbd>
          驳回 ·
          <kbd className="mx-1 rounded-chip border border-subtle bg-paper px-1.5 py-0.5 font-mono">x</kbd>
          选中。当前行左侧有色条标记。
        </p>
      )}
      {tab === 'pending' &&
        pendingLoaded &&
        pending.map((review) => {
          const subjectAmbiguous =
            candidateTermTypesFor(drafts[review.review_id]?.subject ?? '').length >= 2 &&
            !(justCreated[review.review_id]?.subject ?? ambiguityPick[review.review_id]?.subject)
          const objectAmbiguous =
            candidateTermTypesFor(drafts[review.review_id]?.object ?? '').length >= 2 &&
            !(justCreated[review.review_id]?.object ?? ambiguityPick[review.review_id]?.object)
          const isFocused = pending[focusedIndex]?.review_id === review.review_id
          return (
          <div
            key={review.review_id}
            data-focused={isFocused || undefined}
            // 焦点用左侧色条标记，不改边框——边框变色会让整行轻微位移，
            // 连按 j 时整个列表会抖。
            className={`flex flex-col gap-3 rounded-card border border-subtle bg-card ${
              isFocused ? 'border-l-4 border-l-accent-primary' : 'border-l-4 border-l-transparent'
            } ${
              density === 'compact' ? 'p-2.5' : 'p-4'
            }`}
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
            {review.reason === 'invalid_relation_type' && (
              <p className="text-xs text-status-error">
                关系类型不合法，无法批准，请驳回。
              </p>
            )}
            {review.reason === 'not_in_confirmed_ontology' && (
              <p className="text-xs text-status-error">
                关系类型或实体类型组合不在已确认本体范围内，无法批准，请驳回。
              </p>
            )}
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l border-subtle pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
            <div className="flex gap-3">
              <div className="flex flex-1 items-center gap-2">
                <StandardNameInput
                  value={drafts[review.review_id]?.subject ?? ''}
                  onChange={(value) => {
                    setDrafts((prev) => ({
                      ...prev,
                      [review.review_id]: {
                        ...prev[review.review_id],
                        subject: value,
                      },
                    }))
                    setAmbiguityPick((prev) => ({
                      ...prev,
                      [review.review_id]: { ...prev[review.review_id], subject: undefined },
                    }))
                    setJustCreated((prev) => ({
                      ...prev,
                      [review.review_id]: { ...prev[review.review_id], subject: undefined },
                    }))
                  }}
                  terms={graphTerms}
                  placeholder="subject 标准名"
                  ariaLabel="subject 标准名"
                  onCreateNew={(query) => handleOpenCreateEntity(review.review_id, 'subject', query)}
                />
                {justCreated[review.review_id]?.subject && (
                  <span className="rounded-chip border border-status-success px-1.5 py-0.5 text-xs text-status-success">
                    新建
                  </span>
                )}
                {(() => {
                  const candidateTypes = candidateTermTypesFor(drafts[review.review_id]?.subject ?? '')
                  if (candidateTypes.length < 2 || justCreated[review.review_id]?.subject) return null
                  return (
                    <select
                      value={ambiguityPick[review.review_id]?.subject ?? ''}
                      onChange={(event) =>
                        setAmbiguityPick((prev) => ({
                          ...prev,
                          [review.review_id]: { ...prev[review.review_id], subject: event.target.value },
                        }))
                      }
                      aria-label="subject 类型（有歧义，需选择）"
                      className={`rounded-control border border-status-error bg-paper px-2 py-1 text-xs text-ink focus:outline-none ${focusRing}`}
                    >
                      <option value="">（歧义，请选类型）</option>
                      {candidateTypes.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  )
                })()}
              </div>
              <div className="flex flex-1 items-center gap-2">
                <StandardNameInput
                  value={drafts[review.review_id]?.object ?? ''}
                  onChange={(value) => {
                    setDrafts((prev) => ({
                      ...prev,
                      [review.review_id]: {
                        ...prev[review.review_id],
                        object: value,
                      },
                    }))
                    setAmbiguityPick((prev) => ({
                      ...prev,
                      [review.review_id]: { ...prev[review.review_id], object: undefined },
                    }))
                    setJustCreated((prev) => ({
                      ...prev,
                      [review.review_id]: { ...prev[review.review_id], object: undefined },
                    }))
                  }}
                  terms={graphTerms}
                  placeholder="object 标准名"
                  ariaLabel="object 标准名"
                  onCreateNew={(query) => handleOpenCreateEntity(review.review_id, 'object', query)}
                />
                {justCreated[review.review_id]?.object && (
                  <span className="rounded-chip border border-status-success px-1.5 py-0.5 text-xs text-status-success">
                    新建
                  </span>
                )}
                {(() => {
                  const candidateTypes = candidateTermTypesFor(drafts[review.review_id]?.object ?? '')
                  if (candidateTypes.length < 2 || justCreated[review.review_id]?.object) return null
                  return (
                    <select
                      value={ambiguityPick[review.review_id]?.object ?? ''}
                      onChange={(event) =>
                        setAmbiguityPick((prev) => ({
                          ...prev,
                          [review.review_id]: { ...prev[review.review_id], object: event.target.value },
                        }))
                      }
                      aria-label="object 类型（有歧义，需选择）"
                      className={`rounded-control border border-status-error bg-paper px-2 py-1 text-xs text-ink focus:outline-none ${focusRing}`}
                    >
                      <option value="">（歧义，请选类型）</option>
                      {candidateTypes.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  )
                })()}
              </div>
            </div>
            <textarea
              value={rejectNotes[review.review_id] ?? ''}
              onChange={(event) =>
                setRejectNotes((prev) => ({ ...prev, [review.review_id]: event.target.value }))
              }
              placeholder="驳回备注（可选，仅驳回时提交）"
              aria-label="驳回备注"
              rows={2}
              className={`rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
            />
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => handleApprove(review.review_id)}
                disabled={
                  !drafts[review.review_id]?.subject ||
                  !drafts[review.review_id]?.object ||
                  review.reason === 'invalid_relation_type' ||
                  review.reason === 'not_in_confirmed_ontology' ||
                  subjectAmbiguous ||
                  objectAmbiguous ||
                  processingId !== null ||
                  batchProcessing
                }
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {processingId === review.review_id ? '批准中…' : '批准'}
              </button>
              <button
                type="button"
                onClick={() => handleReject(review.review_id)}
                disabled={processingId !== null || batchProcessing}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
              >
                {processingId === review.review_id ? '驳回中…' : '驳回'}
              </button>
            </div>
          </div>
          )
        })}
      {tab === 'pending' && (selectedReviews.length > 0 || batchResult) && (
        <div className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4">
          {selectedReviews.length > 0 && (
            <>
              <p className="text-sm font-bold text-ink">已选中 {selectedReviews.length} 条</p>
              <textarea
                value={batchRejectNote}
                onChange={(event) => setBatchRejectNote(event.target.value)}
                placeholder="批量驳回备注（可选，应用到本次选中的所有记录）"
                aria-label="批量驳回备注"
                rows={2}
                className={`rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
              />
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={handleBatchApprove}
                  disabled={!canBatchApprove || batchProcessing || processingId !== null}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {batchProcessing ? '批量处理中…' : '批量通过'}
                </button>
                <button
                  type="button"
                  onClick={handleBatchReject}
                  disabled={batchProcessing || processingId !== null}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {batchProcessing ? '批量处理中…' : '批量驳回'}
                </button>
              </div>
              {!canBatchApprove && (
                <p className="text-xs text-ink-soft">
                  批量通过要求选中的每一条都已填好 subject/object 标准名，且没有未解决的类型歧义（红框选择器）。
                </p>
              )}
            </>
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
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle px-3 py-1.5 text-sm font-bold transition ${focusRing} ${
                historyFilter === filter
                  ? 'bg-accent-primary text-on-accent'
                  : 'bg-paper text-ink'
              }`}
            >
              {filter === 'all' ? '全部' : filter === 'approved' ? '已批准' : '已驳回'}
            </button>
          ))}
        </div>
      )}

      {tab === 'history' && !historyLoaded && <Skeleton variant="card-list" count={3} />}
      {tab === 'history' &&
        historyLoaded &&
        history.map((review) => (
          <div
            key={review.review_id}
            className={`flex flex-col gap-1 rounded-card border border-subtle bg-card ${
              density === 'compact' ? 'p-2.5' : 'p-4'
            }`}
          >
            <p className="text-sm text-ink">
              {review.subject_candidate} —[{review.relation_type}]→ {review.object_candidate}
            </p>
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l border-subtle pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
            <p className="flex flex-wrap items-center gap-2 text-xs text-ink-soft">
              <TaskStatusBadge
                tone={review.status === 'approved' ? 'success' : 'error'}
                label={review.status === 'approved' ? '已批准' : '已驳回'}
              />
              <span>
                {review.resolved_at}
                {review.resolved_note && ` · ${review.resolved_note}`}
              </span>
            </p>
          </div>
        ))}
      {tab === 'history' && historyLoaded && history.length === 0 && (
        <EmptyState
          icon={History}
          title="还没有处理过的记录"
          action="在「待审核」标签里批准或驳回候选后，处理结果会出现在这里。"
        />
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
