import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from '../../adminApi'
import { useAdminAuth } from '../../useAdminAuth'
import { useAdminTenant } from '../../TenantContext'
import { useConfirm } from '../../ConfirmContext'
import { useToast } from '../../ToastContext'
import {
  addQuestion,
  answerInterview,
  deleteInterview,
  fetchInterview,
  previewDraft,
  saveInterview,
  startInterview,
} from './interviewApi'
import { addMissingAsRelation, addMissingAsTerm, projectSkeleton, setReview } from './skeletonEdits'
import type {
  DraftDiff,
  InterviewQuestionNeeds,
  InterviewSession,
  InterviewState,
  ReviewState,
  TurnReport,
} from './types'

// 样式常量本地声明、不 import `modelingWorkbench/ui.ts`：三种构建方式各自
// 独立（spec 决策 2），共用一份样式文件会在视觉上悄悄耦合两者。
const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const panelClass = 'rounded-panel border border-subtle bg-paper p-4'
const primaryButtonClass = `min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const secondaryButtonClass = `inline-flex min-h-[36px] cursor-pointer items-center rounded-control border border-subtle bg-paper px-3 py-1 text-sm font-bold text-ink transition hover:bg-interactive-hover active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const tagClass = 'rounded-control border border-subtle px-2 py-0.5 text-xs text-ink-soft'
const inputClass =
  'min-h-[40px] w-full rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink'

function constraintLabel(c: { subject: string; relation: string; object: string }): string {
  return `${c.subject} ${c.relation} ${c.object}`
}

export interface SmartCreatePanelProps {
  /**
   * 写入草稿成功后调用，把这次投影出去的三段数量报给壳页——壳页拿它自动
   * 切到「本体结构」并提示核实（design 增补，决策 11）。可选：本文件自己
   * 的测试直接渲染整个 App，不传这个 prop 也要能跑。
   */
  onApplied?: (counts: { termTypes: number; relationTypes: number; constraints: number }) => void
}

/**
 * 智能创建——本体建模页的「智能创建」tab（?way=smart）。
 *
 * 两段式（spec）：左边是访谈对话，右边是随访谈长出的骨架，下面是业务问题
 * 清单，最下面是写入草稿。跟另外两种构建方式（模板构建、本体结构）完全不
 * 共享状态——各自维护自己的会话/工作区，终点都是同一份草稿。
 *
 * 所有会改变审阅决定/骨架内容的动作（接受/拒绝、加进骨架、结束访谈）都走
 * `persist`（PUT 整份 state，乐观锁）；`answerInterview` 和 `addQuestion` 是
 * 例外——它们各自的端点会调 LLM、直接返回新会话，没有必要先读一遍再整份
 * 存回去。
 */
export function SmartCreatePanel({ onApplied }: SmartCreatePanelProps = {}) {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()

  const [session, setSession] = useState<InterviewSession | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [draft, setDraft] = useState('')
  const [questionDraft, setQuestionDraft] = useState('')
  const [diff, setDiff] = useState<DraftDiff | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [turnReport, setTurnReport] = useState<TurnReport | null>(null)

  const reportError = useCallback((err: unknown, fallback: string) => {
    setError(err instanceof Error ? err.message : fallback)
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    ;(async () => {
      try {
        const loadedSession = await fetchInterview(tenantId, sessionToken)
        if (!cancelled) setSession(loadedSession)
      } catch (err) {
        if (!cancelled) reportError(err, '读取访谈失败')
      } finally {
        if (!cancelled) setLoaded(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId, reportError])

  const persist = async (next: InterviewState) => {
    if (!sessionToken || !session) return
    setBusy(true)
    setError(null)
    try {
      setSession(await saveInterview(tenantId, sessionToken, next, session.updated_at))
      // 骨架改了，之前算的差异就过期了——留着会让用户照着一份旧差异写入。
      setDiff(null)
    } catch (err) {
      reportError(err, '保存访谈失败')
    } finally {
      setBusy(false)
    }
  }

  const handleStart = async () => {
    if (!sessionToken) return
    setBusy(true)
    setError(null)
    try {
      setSession(await startInterview(tenantId, sessionToken))
      setTurnReport(null)
    } catch (err) {
      reportError(err, '开始访谈失败')
    } finally {
      setBusy(false)
    }
  }

  const handleAnswer = async () => {
    if (!sessionToken || !session) return
    const answer = draft.trim()
    if (!answer) return
    setBusy(true)
    setError(null)
    try {
      const result = await answerInterview(tenantId, sessionToken, answer, session.updated_at)
      setSession(result.session)
      setTurnReport(result.turn)
      setDraft('')
      setDiff(null)
    } catch (err) {
      // 409（InterviewConflictError）跟别的错误在这里不需要分支：两句提示都
      // 是"刷新/重试"，用户看到的文案本来就一样。
      reportError(err, '提交回答失败')
    } finally {
      setBusy(false)
    }
  }

  const handleFinish = () => {
    if (!session) return
    void persist({ ...session.state, done: true })
  }

  const handleReview = (kind: 'term' | 'relation' | 'constraint', key: string, review: ReviewState) => {
    if (!session) return
    void persist(setReview(session.state, kind, key, review))
  }

  const handleAddQuestion = async () => {
    if (!sessionToken || !session) return
    const text = questionDraft.trim()
    if (!text) return
    setBusy(true)
    setError(null)
    try {
      const result = await addQuestion(tenantId, sessionToken, text, session.updated_at)
      setSession(result.session)
      setQuestionDraft('')
    } catch (err) {
      reportError(err, '添加业务问题失败')
    } finally {
      setBusy(false)
    }
  }

  const handleAddMissing = (name: string, questionText: string, needs: InterviewQuestionNeeds) => {
    if (!session) return
    // 按问题条目自己记录的归属判断加成实体还是关系——而不是拿名字形状猜
    // （"SKU""VIP"这类全大写的实体名会被猜成关系类型）。needs 是
    // infer_needs 反推时就分好类的，比事后用命名规则去猜靠谱。
    const next = needs.relation_types.includes(name)
      ? addMissingAsRelation(session.state, name, questionText)
      : addMissingAsTerm(session.state, name, questionText)
    if (next !== session.state) void persist(next)
  }

  const scrollToTurn = (index: number) => {
    // jsdom 没实现 scrollIntoView，测试环境里这个方法可能不存在——可选调用
    // 而不是假设它一定在，免得点这颗按钮时在测试里炸出一个无关的 TypeError。
    document.getElementById(`turn-${index}`)?.scrollIntoView?.({ block: 'center' })
  }

  const handlePreview = async () => {
    if (!sessionToken || !session) return
    setBusy(true)
    setError(null)
    try {
      setDiff(await previewDraft(tenantId, sessionToken, projectSkeleton(session.state)))
    } catch (err) {
      reportError(err, '计算差异失败')
    } finally {
      setBusy(false)
    }
  }

  const handleApply = async () => {
    if (!sessionToken || !session) return
    const payload = projectSkeleton(session.state)
    if (payload.term_types.length === 0) {
      setError('骨架里一个已接受的实体类型都没有，写入草稿没有意义。先去「骨架」里接受几条。')
      return
    }
    setBusy(true)
    setError(null)
    // 跳过「看看会改什么」直接点「写入草稿」时 diff 还是 null：这里必须
    // 现算一份，否则删除项既不会出现在红框里、也不会触发下面的确认框，
    // 整份替换就静默删掉了草稿里已有的东西（跟模板构建同一条规则）。
    let effectiveDiff = diff
    if (effectiveDiff === null) {
      try {
        effectiveDiff = await previewDraft(tenantId, sessionToken, payload)
        setDiff(effectiveDiff)
      } catch (err) {
        reportError(err, '计算差异失败')
        setBusy(false)
        return
      }
    }
    const removed = [
      ...effectiveDiff.removed_term_types,
      ...effectiveDiff.removed_relation_types,
      ...effectiveDiff.removed_constraints,
    ]
    if (
      removed.length > 0 &&
      !(await confirm({
        message: `写入后这些会从草稿里消失：${removed.join('、')}。`,
        confirmLabel: '继续写入',
      }))
    ) {
      setBusy(false)
      return
    }
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/draft/replace`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // 智能创建没接触过任何数据表，猜一份 ETL 映射一跑就错（spec 决策
          // 8）：传 null，replace_draft 对 null 的语义是"这次不改映射"。
          body: JSON.stringify({ ...payload, etl_mapping: null }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '写入草稿失败'))
      }
      // 不再额外弹 toast：写入成功之后 onApplied 会把壳页切到「本体结构」
      // 并挂一条常驻的 role="status" 提示（说清楚下一步该核实什么）。toast
      // 转瞬即逝还要跟那条提示抢同一个 role，两条通知同时存在会让读屏
      // 重复/交叉播报同一件事；常驻的那条信息量更大，留它就够了。
      setDiff(null)
      onApplied?.({
        termTypes: payload.term_types.length,
        relationTypes: payload.relation_types.length,
        constraints: payload.constraints.length,
      })
    } catch (err) {
      reportError(err, '写入草稿失败')
    } finally {
      setBusy(false)
    }
  }

  const handleRestart = async () => {
    if (!sessionToken || !session) return
    if (
      !(await confirm({
        message: '重新开始会删掉这个租户的访谈记录和骨架，已经写入草稿的本体不受影响。',
        confirmLabel: '删掉重来',
      }))
    ) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      await deleteInterview(tenantId, sessionToken)
      setSession(null)
      setDiff(null)
      setTurnReport(null)
      showToast('访谈已重新开始')
    } catch (err) {
      reportError(err, '删除访谈失败')
    } finally {
      setBusy(false)
    }
  }

  if (!loaded) {
    return <p className="text-sm text-ink-soft">加载中…</p>
  }

  if (session === null) {
    return (
      <div className={`${panelClass} flex flex-col gap-3`}>
        <p className="text-sm text-ink-soft">
          回答几个问题，让模型先猜一版骨架的实体和关系，再用真实业务问题校准，全程可以审阅每一条。
        </p>
        {error && (
          <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
            {error}
          </p>
        )}
        <button type="button" className={primaryButtonClass} disabled={busy} onClick={() => void handleStart()}>
          开始访谈
        </button>
      </div>
    )
  }

  const { state } = session

  return (
    <div className="flex flex-col gap-6">
      {error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 访谈：对话流 + 输入 */}
        <section className={`${panelClass} flex flex-col gap-3`} aria-label="访谈">
          <h2 className="font-mono text-sm font-semibold text-ink">访谈</h2>
          <div className="flex max-h-96 flex-col gap-2 overflow-y-auto">
            {state.turns.map((turn, index) => (
              <div
                key={index}
                id={`turn-${index}`}
                className={
                  turn.role === 'assistant'
                    ? 'rounded-card bg-card px-3 py-2 text-sm text-ink'
                    : 'self-end rounded-card bg-ink px-3 py-2 text-sm text-paper'
                }
              >
                {turn.text}
              </div>
            ))}
            {turnReport && (turnReport.note || turnReport.dropped.length > 0) && (
              <div role="status" className="text-sm text-ink-soft">
                {turnReport.note && <p>{turnReport.note}</p>}
                {turnReport.dropped.length > 0 && (
                  // 逐条列出后端给的理由——"有 N 条被丢弃"这种计数对用户
                  // 没用，理由本身（比如"实体类型 X 没有给出理由，丢弃"）
                  // 才是他要看的。
                  <ul className="list-disc pl-5">
                    {turnReport.dropped.map((reason, index) => (
                      <li key={index}>{reason}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
          {state.done ? (
            <p className="text-sm text-ink-soft">访谈已结束。骨架仍可以继续审阅。</p>
          ) : (
            <div className="flex flex-col gap-2">
              <label className="text-sm text-ink" htmlFor="smart-answer-input">
                回答
              </label>
              <textarea
                id="smart-answer-input"
                className={inputClass}
                rows={2}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  className={primaryButtonClass}
                  disabled={busy || draft.trim() === ''}
                  onClick={() => void handleAnswer()}
                >
                  回答
                </button>
                <button type="button" className={secondaryButtonClass} disabled={busy} onClick={handleFinish}>
                  结束访谈
                </button>
              </div>
            </div>
          )}
        </section>

        {/* 骨架：三节，每条标"这是猜的"、理由、出自哪一轮、接受/拒绝 */}
        <section className={`${panelClass} flex flex-col gap-4`} aria-label="骨架">
          <h2 className="font-mono text-sm font-semibold text-ink">骨架</h2>

          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-bold text-ink">实体类型</h3>
            {state.skeleton.term_types.length === 0 && (
              <p className="text-sm text-ink-soft">还没有候选实体类型。</p>
            )}
            <ul className="flex flex-col gap-2">
              {state.skeleton.term_types.map((t) => (
                <li key={t.value} className="rounded-card border border-subtle p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold text-ink">{t.value}</span>
                    <span className={tagClass}>这是猜的</span>
                    {t.review === 'accepted' && <span className={tagClass}>已接受</span>}
                    {t.review === 'rejected' && <span className={tagClass}>已拒绝</span>}
                  </div>
                  <p className="text-sm text-ink-soft">{t.rationale}</p>
                  <div className="mt-1 flex flex-wrap gap-2">
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      onClick={() => scrollToTurn(t.from_turn)}
                    >
                      出自第 {t.from_turn} 轮
                    </button>
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={busy}
                      onClick={() => handleReview('term', t.value, 'accepted')}
                    >
                      接受 {t.value}
                    </button>
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={busy}
                      onClick={() => handleReview('term', t.value, 'rejected')}
                    >
                      拒绝 {t.value}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>

          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-bold text-ink">关系类型</h3>
            {state.skeleton.relation_types.length === 0 && (
              <p className="text-sm text-ink-soft">还没有候选关系类型。</p>
            )}
            <ul className="flex flex-col gap-2">
              {state.skeleton.relation_types.map((r) => (
                <li key={r.relation_type} className="rounded-card border border-subtle p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold text-ink">{r.relation_type}</span>
                    <span className={tagClass}>这是猜的</span>
                    {r.review === 'accepted' && <span className={tagClass}>已接受</span>}
                    {r.review === 'rejected' && <span className={tagClass}>已拒绝</span>}
                  </div>
                  <p className="text-sm text-ink-soft">{r.rationale}</p>
                  <div className="mt-1 flex flex-wrap gap-2">
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      onClick={() => scrollToTurn(r.from_turn)}
                    >
                      出自第 {r.from_turn} 轮
                    </button>
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={busy}
                      onClick={() => handleReview('relation', r.relation_type, 'accepted')}
                    >
                      接受 {r.relation_type}
                    </button>
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={busy}
                      onClick={() => handleReview('relation', r.relation_type, 'rejected')}
                    >
                      拒绝 {r.relation_type}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>

          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-bold text-ink">约束</h3>
            {state.skeleton.constraints.length === 0 && (
              <p className="text-sm text-ink-soft">还没有候选约束。</p>
            )}
            <ul className="flex flex-col gap-2">
              {state.skeleton.constraints.map((c) => {
                const label = constraintLabel(c)
                const key = `${c.subject}|${c.relation}|${c.object}`
                return (
                  <li key={key} className="rounded-card border border-subtle p-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-bold text-ink">{label}</span>
                      <span className={tagClass}>这是猜的</span>
                      {c.review === 'accepted' && <span className={tagClass}>已接受</span>}
                      {c.review === 'rejected' && <span className={tagClass}>已拒绝</span>}
                    </div>
                    <p className="text-sm text-ink-soft">{c.rationale}</p>
                    <div className="mt-1 flex flex-wrap gap-2">
                      <button
                        type="button"
                        className={secondaryButtonClass}
                        onClick={() => scrollToTurn(c.from_turn)}
                      >
                        出自第 {c.from_turn} 轮
                      </button>
                      <button
                        type="button"
                        className={secondaryButtonClass}
                        disabled={busy}
                        onClick={() => handleReview('constraint', key, 'accepted')}
                      >
                        接受 {label}
                      </button>
                      <button
                        type="button"
                        className={secondaryButtonClass}
                        disabled={busy}
                        onClick={() => handleReview('constraint', key, 'rejected')}
                      >
                        拒绝 {label}
                      </button>
                    </div>
                  </li>
                )
              })}
            </ul>
          </div>
        </section>
      </div>

      {/* 问题清单 */}
      <section className={`${panelClass} flex flex-col gap-3`} aria-label="问题清单">
        <h2 className="font-mono text-sm font-semibold text-ink">问题清单</h2>
        <p className="text-sm text-ink-soft">确认本体后可以拿这些问题去问答页验收。</p>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <div className="flex flex-1 flex-col gap-1">
            <label className="text-sm text-ink" htmlFor="smart-question-input">
              业务问题
            </label>
            <input
              id="smart-question-input"
              className={inputClass}
              value={questionDraft}
              onChange={(event) => setQuestionDraft(event.target.value)}
            />
          </div>
          <button
            type="button"
            className={secondaryButtonClass}
            disabled={busy || questionDraft.trim() === ''}
            onClick={() => void handleAddQuestion()}
          >
            加一条
          </button>
        </div>
        <ul className="flex flex-col gap-2">
          {state.questions.map((q, index) => (
            <li key={index} className="rounded-card border border-subtle p-2">
              <p className="text-sm text-ink">{q.text}</p>
              <p className="text-sm text-ink-soft">
                需要实体：{q.needs.term_types.join('、') || '无'}；需要关系：
                {q.needs.relation_types.join('、') || '无'}
              </p>
              {q.missing.length > 0 && (
                <p className="text-sm text-ink-soft">
                  缺：{q.missing.join('、')}
                  <span className="ml-2 inline-flex flex-wrap gap-2">
                    {q.missing.map((name) => (
                      <button
                        key={name}
                        type="button"
                        className={secondaryButtonClass}
                        disabled={busy}
                        onClick={() => handleAddMissing(name, q.text, q.needs)}
                      >
                        把 {name} 加进骨架
                      </button>
                    ))}
                  </span>
                </p>
              )}
            </li>
          ))}
        </ul>
      </section>

      {/* 写入 */}
      <section className={`${panelClass} flex flex-col gap-3`} aria-label="写入">
        <h2 className="font-mono text-sm font-semibold text-ink">写入草稿</h2>
        {diff && (
          <div className="text-sm text-ink-soft">
            {diff.added_term_types.length + diff.added_relation_types.length + diff.added_constraints.length >
              0 && <p>会新增：{[...diff.added_term_types, ...diff.added_relation_types, ...diff.added_constraints].join('、')}</p>}
            {diff.removed_term_types.length + diff.removed_relation_types.length + diff.removed_constraints.length >
              0 && (
              <p className="text-status-error">
                会删掉：
                {[...diff.removed_term_types, ...diff.removed_relation_types, ...diff.removed_constraints].join(
                  '、',
                )}
              </p>
            )}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <button type="button" className={secondaryButtonClass} disabled={busy} onClick={() => void handlePreview()}>
            看看会改什么
          </button>
          <button type="button" className={primaryButtonClass} disabled={busy} onClick={() => void handleApply()}>
            写入草稿
          </button>
          <button type="button" className={secondaryButtonClass} disabled={busy} onClick={() => void handleRestart()}>
            重新开始
          </button>
        </div>
      </section>
    </div>
  )
}
