import { useCallback, useEffect, useState } from 'react'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { createTerm, deleteTerm, fetchTerms, updateTerm, type TermRecord } from './termsApi'
import { adminFetch } from './adminApi'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface TermDraft {
  standard_name: string
  aliases: string
  term_type: string
  product_line: string
}

function toDraft(term: TermRecord): TermDraft {
  return {
    standard_name: term.standard_name,
    aliases: term.aliases.join(', '),
    term_type: term.term_type,
    product_line: term.product_line,
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
    product_line: draft.product_line.trim(),
  }
}

const emptyDraft: TermDraft = { standard_name: '', aliases: '', term_type: '', product_line: '' }

export function TermsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [terms, setTerms] = useState<TermRecord[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [termTypeOptions, setTermTypeOptions] = useState<string[]>([])
  const [productLineOptions, setProductLineOptions] = useState<string[]>([])
  const [optionsLoaded, setOptionsLoaded] = useState(false)

  const [newDraft, setNewDraft] = useState<TermDraft>(emptyDraft)
  const [creating, setCreating] = useState(false)

  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<TermDraft | null>(null)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [deletingKey, setDeletingKey] = useState<string | null>(null)

  useEffect(() => {
    document.title = '术语库管理 · 管理后台'
  }, [])

  useEffect(() => {
    if (!sessionToken) return
    setOptionsLoaded(false)
    Promise.all([
      adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken)
        .then((res) => res.json())
        .then((data: { term_types: { value: string }[] }) =>
          setTermTypeOptions(data.term_types.map((t) => t.value)),
        )
        .catch((err) => {
          console.error('加载实体类型枚举失败', err)
          return null
        }),
      adminFetch('/api/admin/ontology/product-lines', sessionToken)
        .then((res) => res.json())
        .then((data: { product_lines: string[] }) => setProductLineOptions(data.product_lines))
        .catch((err) => {
          console.error('加载产品线枚举失败', err)
          return null
        }),
    ]).finally(() => setOptionsLoaded(true))
  }, [sessionToken, tenantId])

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      const data = await fetchTerms(sessionToken, tenantId)
      setTerms(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载术语表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId])

  useEffect(() => {
    refresh().catch((err) => {
      console.error('术语表刷新失败', err)
    })
  }, [refresh])

  const handleCreate = async () => {
    if (!sessionToken || creating) return
    if (!newDraft.standard_name.trim()) return
    setError(null)
    setCreating(true)
    try {
      await createTerm(sessionToken, tenantId, draftToRecord(newDraft))
      setNewDraft(emptyDraft)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '新增术语失败')
    } finally {
      setCreating(false)
    }
  }

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
    if (!window.confirm(`确定要删除术语「${standardName}」吗？此操作不可撤销。`)) return
    setError(null)
    setDeletingKey(standardName)
    try {
      await deleteTerm(sessionToken, tenantId, standardName)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除术语失败')
    } finally {
      setDeletingKey(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-bold text-ink">术语库管理</h1>

      <div className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
        <p className="text-sm font-bold text-ink">新增术语</p>
        <div className="flex flex-wrap gap-3">
          <input
            value={newDraft.standard_name}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, standard_name: event.target.value }))
            }
            placeholder="标准名"
            aria-label="标准名"
            className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <input
            value={newDraft.aliases}
            onChange={(event) => setNewDraft((prev) => ({ ...prev, aliases: event.target.value }))}
            placeholder="别名（逗号分隔）"
            aria-label="别名"
            className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
          />
          <select
            value={newDraft.term_type}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, term_type: event.target.value }))
            }
            aria-label="类型"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
          >
            <option value="">（无类型）</option>
            {termTypeOptions.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select
            value={newDraft.product_line}
            onChange={(event) =>
              setNewDraft((prev) => ({ ...prev, product_line: event.target.value }))
            }
            aria-label="产品线"
            className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
          >
            <option value="">（无产品线）</option>
            {productLineOptions.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={handleCreate}
          disabled={!newDraft.standard_name.trim() || creating}
          className={`min-h-[44px] cursor-pointer self-start border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {creating ? '新增中…' : '新增术语'}
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

      {!loaded && <p className="text-ink-soft">加载中…</p>}
      {loaded &&
        terms.map((term) => {
          const isEditing = editingKey === term.standard_name
          return (
            <div
              key={term.standard_name}
              className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal-sm"
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
                      · {term.term_type || '（无类型）'} · {term.product_line || '（无产品线）'}
                    </span>
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleStartEdit(term)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(term.standard_name)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
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
                      className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                    />
                    <input
                      value={editDraft.aliases}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, aliases: event.target.value } : prev))
                      }
                      placeholder="别名（逗号分隔）"
                      aria-label={`别名（${term.standard_name}）`}
                      className="min-w-[10rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                    />
                    <select
                      value={editDraft.term_type}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, term_type: event.target.value } : prev))
                      }
                      aria-label={`类型（${term.standard_name}）`}
                      className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
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
                    <select
                      value={editDraft.product_line}
                      onChange={(event) =>
                        setEditDraft((prev) =>
                          prev ? { ...prev, product_line: event.target.value } : prev,
                        )
                      }
                      aria-label={`产品线（${term.standard_name}）`}
                      className="min-w-[8rem] flex-1 border-2 border-ink bg-paper px-3 py-2 text-ink focus:shadow-brutal focus:outline-none"
                    >
                      <option value="">（无产品线）</option>
                      {optionsLoaded && editDraft.product_line && !productLineOptions.includes(editDraft.product_line) && (
                        <option value={editDraft.product_line}>{editDraft.product_line}（不在当前本体枚举中）</option>
                      )}
                      {productLineOptions.map((value) => (
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
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {savingKey === term.standard_name ? '保存中…' : '保存'}
                    </button>
                    <button
                      type="button"
                      onClick={handleCancelEdit}
                      disabled={savingKey !== null}
                      className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
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
        <p className="text-ink-soft">还没有任何术语，用上面的表单新增一个。</p>
      )}
    </div>
  )
}
