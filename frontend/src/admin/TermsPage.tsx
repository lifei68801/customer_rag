import { useCallback, useEffect, useState } from 'react'
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
import { usePaginatedAdminList } from './usePaginatedAdminList'

const PAGE_SIZE = 20

type SourceFilter = 'all' | 'manual' | 'etl' | 'review' | 'unknown'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface ExtraFieldSpec {
  name: string
  value_type: string
}

interface TermDraft {
  standard_name: string
  aliases: string
  term_type: string
  // 表单里一律按字符串持有，提交时才按声明的 value_type 转换——受控 input
  // 的值本来就是字符串，过早转成 number 会让"正在输入的 12."这类中间态无法
  // 表达（转换结果 NaN 会把用户刚敲的字符吞掉）。
  extra_properties: Record<string, string>
}

function extraPropertiesToDraft(term: TermRecord): Record<string, string> {
  const draft: Record<string, string> = {}
  for (const [name, value] of Object.entries(term.extra_properties ?? {})) {
    draft[name] = Array.isArray(value) ? value.join('; ') : String(value)
  }
  return draft
}

function toDraft(term: TermRecord): TermDraft {
  return {
    standard_name: term.standard_name,
    aliases: term.aliases.join(', '),
    term_type: term.term_type,
    extra_properties: extraPropertiesToDraft(term),
  }
}

// 用 node_key 而不是 `term_type::standard_name` 拼接：标准名在同一 term_type
// 下已经允许重复（2026-08-30），拼接键会撞车——两条同名同类型的术语会共享
// 同一个 key，导致 editingKey/savingKey/deletingKey 同时命中两行，React 列表
// key 也会重复。node_key 是后端保证的身份键，不存在这个问题。
function termKey(term: { node_key: string }): string {
  return term.node_key
}

/** 按本体声明的 value_type 把表单字符串转回后端要的类型。转换规则跟
 * app/graphrag/schema_etl_row_processing.py::convert_field_value 保持一致，
 * 否则同一份数据经 ETL 写入和经这个表单编辑会得到不同的类型，后端的
 * InvalidExtraPropertyTypeError 校验只在其中一条路径上通过。
 * 空输入返回 undefined，调用方据此整个略去这个键——传 "" 会被后端按类型
 * 校验拒掉，而用户清空一个输入框的意图是"这个属性没有值"。 */
function coerceExtraProperty(raw: string, valueType: string): unknown {
  const trimmed = raw.trim()
  if (trimmed === '') return undefined
  if (valueType === 'number' || valueType === 'integer') {
    const parsed = Number(trimmed)
    // 转不动就原样回传字符串，让后端的类型校验给出明确的 400，而不是在
    // 前端悄悄变成 NaN 再序列化成 null。
    return Number.isNaN(parsed) ? trimmed : parsed
  }
  if (valueType === 'number[]') {
    return trimmed
      .split(';')
      .map((item) => item.trim())
      .filter((item) => item.length > 0)
      .map((item) => {
        const parsed = Number(item)
        return Number.isNaN(parsed) ? item : parsed
      })
  }
  return trimmed
}

function draftToRecord(draft: TermDraft, specs: ExtraFieldSpec[]): Omit<TermRecord, 'node_key'> {
  const extraProperties: Record<string, unknown> = {}
  for (const spec of specs) {
    const coerced = coerceExtraProperty(draft.extra_properties[spec.name] ?? '', spec.value_type)
    if (coerced !== undefined) extraProperties[spec.name] = coerced
  }
  // 该类型没有声明任何属性字段时整个略去这个键，走后端的"缺席=保留"语义。
  // 不能传 {}：那是显式清空，会抹掉历史遗留的属性值——_validate_categories
  // 的 existing_extra_property_keys 允许术语携带类型未声明的旧字段，这个
  // 表单看不见它们，也就不该替用户决定删掉它们。
  const extraPropertiesPatch = specs.length > 0 ? { extra_properties: extraProperties } : {}
  return {
    ...extraPropertiesPatch,
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
  const [termTypeOptions, setTermTypeOptions] = useState<string[]>([])
  const [extraFieldsByType, setExtraFieldsByType] = useState<Record<string, ExtraFieldSpec[]>>({})
  const [optionsLoaded, setOptionsLoaded] = useState(false)

  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all')
  // 输入框的即时值与真正拿去请求的值分开：每敲一个字就发一次请求，既浪费
  // 也会让结果乱序返回（后发的先到）。300ms 防抖后再请求。
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')

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
      .then((data: { term_types: { value: string; extra_fields?: ExtraFieldSpec[] }[] }) => {
        setTermTypeOptions(data.term_types.map((t) => t.value))
        setExtraFieldsByType(
          Object.fromEntries(data.term_types.map((t) => [t.value, t.extra_fields ?? []])),
        )
      })
      .catch((err) => {
        console.error('加载实体类型枚举失败', err)
        return null
      })
      .finally(() => setOptionsLoaded(true))
  }, [sessionToken, tenantId])

  const fetchPage = useCallback(
    async (page: number) => {
      if (!sessionToken) return { items: [], total: 0 }
      const data = await fetchTermsPage(
        sessionToken, tenantId, page, PAGE_SIZE,
        sourceFilter === 'all' ? undefined : sourceFilter,
        search,
      )
      return { items: data.terms, total: data.total }
    },
    [sessionToken, tenantId, sourceFilter, search],
  )
  const {
    items: terms, total, loaded, error, setError, page, setPage, refresh,
  } = usePaginatedAdminList(fetchPage)

  useEffect(() => {
    setPage(1)
  }, [tenantId, setPage])

  useEffect(() => {
    setPage(1)
  }, [sourceFilter, setPage])

  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput), 300)
    return () => clearTimeout(timer)
  }, [searchInput])

  // 换关键词要回第一页——否则搜出 3 条却停在第 5 页，界面是空的。
  useEffect(() => {
    setPage(1)
  }, [search, setPage])

  useEffect(() => {
    if (loaded && terms.length === 0 && page > 1) {
      setPage((p) => p - 1)
    }
  }, [loaded, terms.length, page])

  const handleStartEdit = (term: TermRecord) => {
    if (editingKey !== null) return
    setEditingKey(termKey(term))
    setEditDraft(toDraft(term))
  }

  const handleCancelEdit = () => {
    setEditingKey(null)
    setEditDraft(null)
  }

  const handleSaveEdit = async (originalTerm: TermRecord) => {
    if (!sessionToken || !editDraft || savingKey !== null) return
    if (!editDraft.standard_name.trim()) return
    setError(null)
    const key = termKey(originalTerm)
    setSavingKey(key)
    try {
      await updateTerm(
        sessionToken, tenantId, originalTerm.node_key,
        // 按编辑后选中的类型取字段声明——这次编辑可能同时改了 term_type，
        // 属性值该按新类型的声明来转换和取舍，不是按原类型。
        // node_key 确实进了请求体（updateTerm 的 term 参数类型是完整的
        // TermRecord，这里补上是为了满足那个类型）。后端不看它：寻址靠 URL
        // 路径，TermWriteRequest 没有 node_key 字段，多余的键会被 Pydantic v2
        // 忽略。
        { ...draftToRecord(editDraft, extraFieldsByType[editDraft.term_type] ?? []), node_key: originalTerm.node_key },
      )
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

  const handleDelete = async (term: TermRecord) => {
    if (!sessionToken || deletingKey !== null) return
    if (!(await confirm(`确定要删除术语「${term.standard_name}」吗？此操作不可撤销。`))) return
    setError(null)
    const key = termKey(term)
    setDeletingKey(key)
    try {
      await deleteTerm(sessionToken, tenantId, term.node_key)
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
      <h1 className="font-mono text-xl font-semibold text-ink">实体列表</h1>

      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="term-search" className="text-sm font-bold text-ink">
          搜索
        </label>
        <input
          id="term-search"
          type="search"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          placeholder="按名称或别名"
          aria-describedby="term-search-hint"
          className={`w-56 rounded-control border border-subtle bg-paper px-3 py-2 text-ink focus:outline-none ${focusRing}`}
        />
        {searchInput && (
          <button
            type="button"
            onClick={() => setSearchInput('')}
            className={`rounded-control border border-subtle bg-paper px-3 py-2 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
          >
            清除
          </button>
        )}
        <span id="term-search-hint" className="text-xs text-ink-soft">
          搜标准名和别名，不区分大小写
        </span>
      </div>

      <div className="flex items-center gap-2">
        <label htmlFor="source-filter" className="text-sm font-bold text-ink">
          来源
        </label>
        <select
          id="source-filter"
          value={sourceFilter}
          onChange={(event) => setSourceFilter(event.target.value as SourceFilter)}
          className={`rounded-control border border-subtle bg-paper px-3 py-2 text-ink focus:outline-none ${focusRing}`}
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
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink"
        >
          {error}
        </p>
      )}

      {!loaded && <Skeleton variant="table-rows" count={5} />}
      {loaded &&
        terms.map((term) => {
          const key = termKey(term)
          const isEditing = editingKey === key
          return (
            <div
              key={key}
              className={`flex flex-col gap-3 rounded-card border border-subtle bg-card ${
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
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(term)}
                      disabled={editingKey !== null || deletingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {deletingKey === key ? '删除中…' : '删除'}
                    </button>
                  </div>
                </div>
              )}
              {!isEditing && Object.keys(term.extra_properties ?? {}).length > 0 && (
                <dl className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                  {Object.entries(term.extra_properties ?? {}).map(([name, value]) => (
                    <div key={name} className="flex gap-1">
                      <dt className="text-ink-soft">{name}</dt>
                      <dd className="font-bold text-ink">
                        {Array.isArray(value) ? value.join('; ') : String(value)}
                      </dd>
                    </div>
                  ))}
                </dl>
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
                      className={`min-w-[10rem] flex-1 rounded-control border border-subtle bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
                    />
                    <input
                      value={editDraft.aliases}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, aliases: event.target.value } : prev))
                      }
                      placeholder="别名（逗号分隔）"
                      aria-label={`别名（${term.standard_name}）`}
                      className={`min-w-[10rem] flex-1 rounded-control border border-subtle bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
                    />
                    <select
                      value={editDraft.term_type}
                      onChange={(event) =>
                        setEditDraft((prev) => (prev ? { ...prev, term_type: event.target.value } : prev))
                      }
                      aria-label={`类型（${term.standard_name}）`}
                      className={`min-w-[8rem] flex-1 rounded-control border border-subtle bg-paper px-3 py-2 text-ink focus:outline-none ${focusRing}`}
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
                  {(extraFieldsByType[editDraft.term_type] ?? []).length > 0 && (
                    <div className="flex flex-wrap gap-3">
                      {(extraFieldsByType[editDraft.term_type] ?? []).map((spec) => (
                        <label key={spec.name} className="flex min-w-[10rem] flex-1 flex-col gap-1">
                          <span className="text-sm text-ink-soft">
                            {spec.name}
                            <span className="ml-1 text-xs">({spec.value_type})</span>
                          </span>
                          <input
                            value={editDraft.extra_properties[spec.name] ?? ''}
                            onChange={(event) =>
                              setEditDraft((prev) =>
                                prev
                                  ? {
                                      ...prev,
                                      extra_properties: {
                                        ...prev.extra_properties,
                                        [spec.name]: event.target.value,
                                      },
                                    }
                                  : prev,
                              )
                            }
                            placeholder={spec.value_type === 'number[]' ? '分号分隔，如 1.5; 2.0' : ''}
                            aria-label={`${spec.name}（${term.standard_name}）`}
                            className={`rounded-control border border-subtle bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
                          />
                        </label>
                      ))}
                    </div>
                  )}
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleSaveEdit(term)}
                      disabled={!editDraft.standard_name.trim() || savingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                    >
                      {savingKey === key ? '保存中…' : '保存'}
                    </button>
                    <button
                      type="button"
                      onClick={handleCancelEdit}
                      disabled={savingKey !== null}
                      className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
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
