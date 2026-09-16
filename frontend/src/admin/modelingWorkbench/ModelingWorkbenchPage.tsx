import { useCallback, useEffect, useState } from 'react'
import { PAGE_TITLES } from '../../adminRoutes'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { useAdminAuth } from '../useAdminAuth'
import { useAdminTenant } from '../TenantContext'
import { useConfirm } from '../ConfirmContext'
import { useToast } from '../ToastContext'
import { nextStepHint } from './nextStep'
import { previewSkippedRelations, projectToDraftPayload, projectToEtlYaml } from './projectToDraft'
import { assignColumnAsField, assignColumnAsKey, setFieldAliases, setKeyAliases } from './columnAssign'
import { addManualClue, addManualTermType, renameTermType } from './skeletonEdits'
import {
  createWorkspace,
  exportSkill,
  fetchGrounding,
  fetchSkills,
  fetchWorkspace,
  previewApply,
  saveWorkspace,
} from './workspaceApi'
import type { DraftDiff, Grounding, ModelingWorkspace, SkillSummary, WorkspaceState } from './types'
import { ApplyPanel } from './panels/ApplyPanel'
import { DataPanel } from './panels/DataPanel'
import { SkeletonPanel } from './panels/SkeletonPanel'
import { StartPanel } from './panels/StartPanel'
import { UngroundedPanel } from './panels/UngroundedPanel'
import { panelClass, secondaryButtonClass } from './ui'

type Tab = 'skeleton' | 'data' | 'ungrounded' | 'apply'

const TAB_LABELS: { id: Tab; label: string }[] = [
  { id: 'skeleton', label: '骨架' },
  { id: 'data', label: '数据' },
  { id: 'ungrounded', label: '未落地' },
  { id: 'apply', label: '应用' },
]

/**
 * 建模工作台。替换原来的「引导建模」页，路由不变。
 *
 * 是工作台不是向导（spec 决策 12）：四个面板随时可切，因为真实的建模不是
 * 一条直线——用户会在"传了一张表、发现骨架少一个概念、回去加一条、再传下
 * 一张表"之间来回走。顶部一行"下一步建议"负责回答"现在最该做什么"。
 *
 * 每次改动立刻整份存回后端（PUT 带 updated_at 乐观锁），不做本地草稿：
 * 工作区是长期存在的，用户关掉页面一周后回来必须看到自己上次做到哪。
 */
export function ModelingWorkbenchPage() {
  const { sessionToken, username } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const [workspace, setWorkspace] = useState<ModelingWorkspace | null>(null)
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [grounding, setGrounding] = useState<Grounding | null>(null)
  const [diff, setDiff] = useState<DraftDiff | null>(null)
  const [tab, setTab] = useState<Tab>('skeleton')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)

  // WorkspaceConflictError 不需要单独分支：后端 409 的 detail 本来就带着
  // "刷新后重试"，跟下面 fallback 分支做的事完全一样。
  const reportError = useCallback((err: unknown, fallback: string) => {
    setError(err instanceof Error ? err.message : fallback)
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    let cancelled = false
    ;(async () => {
      try {
        const [loadedWorkspace, loadedSkills, loadedGrounding] = await Promise.all([
          fetchWorkspace(tenantId, sessionToken),
          fetchSkills(tenantId, sessionToken),
          fetchGrounding(tenantId, sessionToken),
        ])
        if (cancelled) return
        setWorkspace(loadedWorkspace)
        setSkills(loadedSkills)
        setGrounding(loadedGrounding)
      } catch (err) {
        if (!cancelled) {
          reportError(err, '加载建模工作区失败')
          // 加载失败跟"还没有工作区"是两回事：不标出来的话，下面的渲染
          // 条件会把失败呈现成 StartPanel 的空白起步页，用户会以为自己
          // 从没建过工作区，而不是这次请求没成功。
          setLoadFailed(true)
        }
      } finally {
        if (!cancelled) setLoaded(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId, reportError])

  const persist = async (next: WorkspaceState) => {
    if (!sessionToken || !workspace) return
    setBusy(true)
    setError(null)
    try {
      setWorkspace(await saveWorkspace(tenantId, sessionToken, next, workspace.updated_at))
      // 应用之前的任何改动都会让上一次算的 diff 过时；留着它会让用户照着
      // 一份旧差异点「写入草稿」。
      setDiff(null)
    } catch (err) {
      reportError(err, '保存建模工作区失败')
    } finally {
      setBusy(false)
    }
  }

  const handleStart = async (skillName: string | null) => {
    if (!sessionToken) return
    setBusy(true)
    setError(null)
    try {
      setWorkspace(await createWorkspace(tenantId, sessionToken, skillName))
      setTab('skeleton')
    } catch (err) {
      reportError(err, '创建建模工作区失败')
    } finally {
      setBusy(false)
    }
  }

  const handleReview = (
    kind: 'term' | 'relation',
    key: string,
    review: 'accepted' | 'rejected',
  ) => {
    if (!workspace) return
    const state = workspace.state
    void persist(
      kind === 'term'
        ? {
            ...state,
            term_types: state.term_types.map((t) => (t.value === key ? { ...t, review } : t)),
          }
        : {
            ...state,
            relation_types: state.relation_types.map((r) =>
              r.relation_type === key ? { ...r, review } : r,
            ),
          },
    )
  }

  const handleRename = (from: string, to: string) => {
    if (!workspace) return
    // renameTermType 原样返回同一个 state 有三种原因：空名、重名、名字没变。
    // 单独判断"名字没变"，不然它会跟"重名"共用同一句提示，让用户误以为
    // 自己打错字重复了，其实只是没改。
    if (to.trim() === from) {
      setError('名字没变。')
      return
    }
    const next = renameTermType(workspace.state, from, to)
    if (next === workspace.state) {
      setError(`改名没生效：新名字不能为空，也不能跟已有的实体类型重名（${to}）。`)
      return
    }
    void persist(next)
  }

  const handleAddClue = (termValue: string, note: string) => {
    if (!workspace) return
    const next = addManualClue(
      workspace.state,
      termValue,
      note,
      username ?? '',
      new Date().toISOString(),
    )
    if (next !== workspace.state) void persist(next)
  }

  const handleAddTerm = (value: string) => {
    if (!workspace) return
    const next = addManualTermType(workspace.state, value)
    if (next === workspace.state) {
      setError(`没有新增：名字不能为空，也不能跟已有的实体类型重名（${value}）。`)
      return
    }
    void persist(next)
  }

  const handleSetKeyAliases = (termValue: string, aliases: string[]) => {
    if (!workspace) return
    const next = setKeyAliases(workspace.state, termValue, aliases)
    if (next !== workspace.state) void persist(next)
  }

  const handleSetFieldAliases = (termValue: string, fieldName: string, aliases: string[]) => {
    if (!workspace) return
    const next = setFieldAliases(workspace.state, termValue, fieldName, aliases)
    if (next !== workspace.state) void persist(next)
  }

  const handlePromote = (file: string, column: string) => {
    if (!workspace) return
    const state = workspace.state
    if (state.term_types.some((t) => t.value === column)) return
    void persist({
      ...state,
      term_types: [
        ...state.term_types,
        {
          value: column,
          display_name: column,
          provenance: 'data',
          review: 'pending',
          standard_name_value_type: 'string',
          extra_fields: [],
          // 这一列自己就是它的别名——下次扫同一张表还能对上
          key_aliases: [column],
          field_aliases: {},
          clues: [],
          data_match: {
            source_file: file,
            key_columns: [column],
            field_columns: {},
            matched_by: 'manual',
          },
        },
      ],
      unmatched_columns: {
        ...state.unmatched_columns,
        [file]: (state.unmatched_columns[file] ?? []).filter((name) => name !== column),
      },
    })
  }

  const handleAssign = (file: string, column: string, termValue: string, as: 'key' | 'field') => {
    if (!workspace) return
    const next =
      as === 'key'
        ? assignColumnAsKey(workspace.state, file, column, termValue)
        : assignColumnAsField(workspace.state, file, column, termValue)
    if (next === workspace.state) {
      setError(
        as === 'field'
          ? `${termValue} 还没在 ${file} 里有键列，先把它的键列指到这张表。`
          : `没法把 ${column} 指给 ${termValue}：它不存在或已被拒绝。`,
      )
      return
    }
    void persist(next)
  }

  const handlePreview = async () => {
    if (!sessionToken || !workspace) return
    setBusy(true)
    setError(null)
    try {
      setDiff(await previewApply(tenantId, sessionToken, projectToDraftPayload(workspace.state)))
    } catch (err) {
      reportError(err, '计算差异失败')
    } finally {
      setBusy(false)
    }
  }

  const handleApply = async () => {
    if (!sessionToken || !workspace) return
    const payload = projectToDraftPayload(workspace.state)
    if (payload.term_types.length === 0) {
      setError('工作区里一个已接受的实体类型都没有，写入草稿没有意义。先去「骨架」面板接受几条。')
      return
    }
    setBusy(true)
    setError(null)
    // 跳过「看看会改什么」直接点「写入草稿」时 diff 还是 null：这里必须
    // 现算一份，否则删除项既不会出现在红框里、也不会触发下面的确认框，
    // 整份替换就静默删掉了草稿里手工加的东西。
    let effectiveDiff = diff
    if (effectiveDiff === null) {
      try {
        effectiveDiff = await previewApply(tenantId, sessionToken, payload)
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
      const mapping = projectToEtlYaml(workspace.state, tenantId)
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/draft/replace`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...payload,
            etl_mapping: mapping
              ? { config_yaml: mapping.yaml, source_file_name: mapping.fileName }
              : null,
          }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '写入草稿失败'))
      }
      showToast('已写入本体草稿')
      // 刻意不调 /confirm：确认是不可逆的，工作台不替用户做这个决定
      setGrounding(await fetchGrounding(tenantId, sessionToken))
      setDiff(null)
    } catch (err) {
      reportError(err, '写入草稿失败')
    } finally {
      setBusy(false)
    }
  }

  const handleExport = async () => {
    if (!sessionToken) return
    setBusy(true)
    setError(null)
    try {
      // skill 名要求 ^[a-z][a-z0-9_]{0,63}$，而租户 ID 允许数字、连字符开头：
      // 固定的字母前缀保证首字符合法，截断保证长度，否则这类租户导出必 400。
      const skillName = `domain_${tenantId.toLowerCase().replace(/[^a-z0-9_]/g, '_')}`.slice(0, 64)
      const text = await exportSkill(tenantId, sessionToken, skillName, `${tenantId} 导出的领域模板`)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/yaml;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `${tenantId}-skill.yaml`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      showToast('已导出领域模板，人工审阅后才能提交进代码仓')
    } catch (err) {
      reportError(err, '导出领域模板失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.guidedOntology}</h1>
        <p className="text-sm text-ink-soft">{nextStepHint(workspace, diff)}</p>
      </div>

      {error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}

      {!loaded && <p className="text-sm text-ink-soft">加载中…</p>}

      {loaded && !loadFailed && workspace === null && (
        <StartPanel skills={skills} busy={busy} onStart={handleStart} />
      )}

      {loaded && workspace !== null && (
        <>
          <div className={`${panelClass} flex flex-wrap gap-2`}>
            {TAB_LABELS.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                aria-pressed={tab === id}
                className={secondaryButtonClass}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === 'skeleton' && (
            <SkeletonPanel
              state={workspace.state}
              grounding={grounding}
              busy={busy}
              onReview={handleReview}
              onRename={handleRename}
              onAddClue={handleAddClue}
              onAddTerm={handleAddTerm}
              onSetKeyAliases={handleSetKeyAliases}
              onSetFieldAliases={handleSetFieldAliases}
            />
          )}
          {tab === 'data' && (
            <DataPanel
              state={workspace.state}
              busy={busy}
              onMerged={(next) => void persist(next)}
              onPromote={handlePromote}
              onAssign={handleAssign}
            />
          )}
          {tab === 'ungrounded' && (
            <UngroundedPanel state={workspace.state} grounding={grounding} />
          )}
          {tab === 'apply' && (
            <ApplyPanel
              diff={diff}
              skippedRelations={previewSkippedRelations(workspace.state)}
              busy={busy}
              onPreview={handlePreview}
              onApply={handleApply}
              onExport={handleExport}
            />
          )}
        </>
      )}
    </div>
  )
}
