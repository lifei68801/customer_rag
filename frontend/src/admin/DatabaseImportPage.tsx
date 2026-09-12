import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Database } from 'lucide-react'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { PasswordPrompt } from './dbImport/PasswordPrompt'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft ${focusRing}`

interface DbSource {
  source_id: string
  name: string
  driver: string
  host: string
  port: number
  database: string
  username: string
  query: string
  mapping: Record<string, unknown>
  last_sync_at: string | null
  last_sync_rows: number | null
}

interface PreviewResult {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
}

/** 连接信息。**没有 password**——它单独走，永远不进这个会被保存的对象。 */
interface ConnectionDraft {
  driver: string
  host: string
  port: string
  database: string
  username: string
}

const EMPTY_CONNECTION: ConnectionDraft = {
  driver: 'mysql',
  host: '',
  port: '',
  database: '',
  username: '',
}

type Step = 'connection' | 'query' | 'mapping'

/**
 * 数据库导入（spec D3）。
 *
 * 向导四步压成三屏：填连接 + 测连通 → 写 SQL 并预览 → 配列映射并保存。
 * **测连通之前不让往下走**：没测就往下的话，用户会在写完 SQL、配完映射之后
 * 才发现连不上，前面两步全白做。
 *
 * 密码在这一页只活在两个地方，都是短命的：向导里那个用来测连通/预览的输入框，
 * 和重新同步时弹出来的密码框。**保存数据源的请求体里没有它**，也不写任何
 * 浏览器存储。
 */
export function DatabaseImportPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [sources, setSources] = useState<DbSource[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [wizardOpen, setWizardOpen] = useState(false)
  const [step, setStep] = useState<Step>('connection')
  const [connection, setConnection] = useState<ConnectionDraft>(EMPTY_CONNECTION)
  const [password, setPassword] = useState('')
  const [connectionOk, setConnectionOk] = useState(false)
  const [query, setQuery] = useState('')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [name, setName] = useState('')
  const [termType, setTermType] = useState('')
  const [nameColumn, setNameColumn] = useState('')
  const [keyColumn, setKeyColumn] = useState('')
  const [busy, setBusy] = useState(false)

  //: 正在等密码的那个数据源。null 表示没在等。
  const [syncing, setSyncing] = useState<DbSource | null>(null)
  //: 已确认本体里的实体类型。null = 还没拉到（拉取失败也是 null）。
  const [termTypeOptions, setTermTypeOptions] = useState<string[] | null>(null)

  useEffect(() => {
    document.title = `${PAGE_TITLES.dbImport} · 管理后台`
  }, [])

  // 实体类型必须是已确认本体里有的：ETL 写入时按 status='confirmed' 校验，
  // 这里填一个不存在的类型，要等到导入真的跑起来才报错，而那时用户已经
  // 配完了整个数据源。
  useEffect(() => {
    if (!sessionToken || !tenantId) return
    let cancelled = false
    adminFetch(
      `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`,
      sessionToken,
    )
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
      .then((data: { term_types: { value: string }[] }) => {
        if (!cancelled) setTermTypeOptions(data.term_types.map((t) => t.value))
      })
      .catch(() => {
        // 拉不到就退回自由文本输入，而不是把这一步锁死——本体服务暂时不可用
        // 不该让用户配不了数据源。下面的输入框会把这件事说出来。
        if (!cancelled) setTermTypeOptions(null)
      })
    return () => {
      cancelled = true
    }
  }, [sessionToken, tenantId])

  const call = useCallback(
    async (path: string, init?: RequestInit) => {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId ?? '')}/db-import/${path}`,
        sessionToken ?? '',
        init,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '这一步没成功'))
      }
      return response.json()
    },
    [sessionToken, tenantId],
  )

  const loadSources = useCallback(async () => {
    if (!sessionToken || !tenantId) return
    try {
      const body = await call('sources')
      setSources(body.items as DbSource[])
    } catch (err) {
      setSources([])
      setError(err instanceof Error ? err.message : '数据源列表没拉到')
    }
  }, [call, sessionToken, tenantId])

  useEffect(() => {
    void loadSources()
  }, [loadSources])

  const connectionBody = () => ({
    driver: connection.driver,
    host: connection.host,
    port: Number(connection.port || 0),
    database: connection.database,
    username: connection.username,
  })

  const handleTestConnection = async () => {
    setBusy(true)
    setError(null)
    try {
      await call('test-connection', {
        method: 'POST',
        body: JSON.stringify({ ...connectionBody(), password }),
      })
      setConnectionOk(true)
    } catch (err) {
      // 后端那句话原样显示：「连不上 10.0.0.5:3306」比「连接失败」有用得多，
      // 用户据此判断是地址写错了还是网络不通。它保证不含密码。
      setConnectionOk(false)
      setError(err instanceof Error ? err.message : '连不上')
    } finally {
      setBusy(false)
    }
  }

  const handlePreview = async () => {
    setBusy(true)
    setError(null)
    try {
      const body = await call('preview', {
        method: 'POST',
        body: JSON.stringify({ ...connectionBody(), password, query }),
      })
      setPreview(body as PreviewResult)
    } catch (err) {
      setPreview(null)
      setError(err instanceof Error ? err.message : '预览没跑出来')
    } finally {
      setBusy(false)
    }
  }

  const handleSave = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // **请求体里没有 password。** 后端有 extra="forbid" 兜底，但前端本来
      // 就不该发——发出去它就进了访问日志，而那份日志我们不控制留存。
      await call('sources', {
        method: 'POST',
        body: JSON.stringify({
          ...connectionBody(),
          name,
          query,
          mapping: {
            term_type: termType,
            standard_name_parts: [nameColumn],
            node_key_parts: [{ column: keyColumn }],
            field_mappings: {},
          },
        }),
      })
      closeWizard()
      await loadSources()
    } catch (err) {
      setError(err instanceof Error ? err.message : '没保存成功')
    } finally {
      setBusy(false)
    }
  }

  const closeWizard = () => {
    setWizardOpen(false)
    setStep('connection')
    setConnection(EMPTY_CONNECTION)
    // 关向导就把密码丢掉，不留在内存里等着下一次。
    setPassword('')
    setConnectionOk(false)
    setQuery('')
    setPreview(null)
    setName('')
    setTermType('')
    setNameColumn('')
    setKeyColumn('')
  }

  const handleSync = async (source: DbSource, syncPassword: string) => {
    setSyncing(null)
    setBusy(true)
    setError(null)
    try {
      // 密码只在请求体里。绝不拼进 URL——URL 会进浏览器历史、进服务端访问
      // 日志、进 Referer 头。
      await call(`sources/${encodeURIComponent(source.source_id)}/sync`, {
        method: 'POST',
        body: JSON.stringify({ password: syncPassword }),
      })
      await loadSources()
    } catch (err) {
      setError(err instanceof Error ? err.message : '同步没成功')
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (source: DbSource) => {
    setError(null)
    try {
      await call(`sources/${encodeURIComponent(source.source_id)}`, { method: 'DELETE' })
      await loadSources()
    } catch (err) {
      setError(err instanceof Error ? err.message : '没删掉')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.dbImport}</h1>
        <p className="text-sm text-ink-soft">
          从数据库直接拉数据进图谱。连接和列映射存下来可重跑，
          <strong className="font-bold">密码不保存</strong>——每次同步现填。
        </p>
      </div>

      {error !== null && (
        <p
          role="alert"
          className="rounded-card border border-subtle bg-card p-3 text-sm text-status-error-strong"
        >
          {error}
        </p>
      )}

      {!wizardOpen && (
        <div>
          <button type="button" className={buttonClass} onClick={() => setWizardOpen(true)}>
            新建数据源
          </button>
        </div>
      )}

      {wizardOpen && (
        <div className="flex flex-col gap-4 rounded-card border border-subtle bg-card p-4">
          {step === 'connection' && (
            <div className="flex flex-col gap-3">
              <h2 className="text-sm font-bold text-ink">第一步：连上去</h2>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="flex flex-col gap-1 text-sm text-ink">
                  数据库类型
                  <select
                    className={inputClass}
                    value={connection.driver}
                    onChange={(e) => setConnection({ ...connection, driver: e.target.value })}
                  >
                    <option value="mysql">MySQL</option>
                    <option value="postgresql">PostgreSQL</option>
                  </select>
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  主机
                  <input
                    className={inputClass}
                    value={connection.host}
                    onChange={(e) => setConnection({ ...connection, host: e.target.value })}
                    placeholder="10.0.0.5"
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  端口
                  <input
                    className={inputClass}
                    value={connection.port}
                    onChange={(e) => setConnection({ ...connection, port: e.target.value })}
                    placeholder="3306"
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  库名
                  <input
                    className={inputClass}
                    value={connection.database}
                    onChange={(e) => setConnection({ ...connection, database: e.target.value })}
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  账号
                  <input
                    className={inputClass}
                    value={connection.username}
                    onChange={(e) => setConnection({ ...connection, username: e.target.value })}
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  密码
                  <input
                    type="password"
                    autoComplete="off"
                    className={inputClass}
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value)
                      // 改了密码就得重测：拿着"上一个密码测通过"的结论往下
                      // 走，用户会在保存之后才发现同步连不上。
                      setConnectionOk(false)
                    }}
                  />
                </label>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  className={buttonClass}
                  disabled={busy}
                  onClick={handleTestConnection}
                >
                  测连通
                </button>
                {/* 没测通不让往下走：不然用户会在写完 SQL、配完映射之后才
                    发现连不上，前面两步全白做。 */}
                <button
                  type="button"
                  className={buttonClass}
                  disabled={!connectionOk}
                  onClick={() => setStep('query')}
                >
                  下一步
                </button>
                <button type="button" className={buttonClass} onClick={closeWizard}>
                  取消
                </button>
              </div>
              {connectionOk && <p className="text-sm text-ink">连通了。</p>}
            </div>
          )}

          {step === 'query' && (
            <div className="flex flex-col gap-3">
              <h2 className="text-sm font-bold text-ink">第二步：写查询</h2>
              <label className="flex flex-col gap-1 text-sm text-ink">
                查询（只允许 SELECT）
                <textarea
                  className={`${inputClass} min-h-[6rem] font-mono`}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="SELECT id, name FROM goods"
                />
              </label>
              <div className="flex gap-2">
                <button type="button" className={buttonClass} disabled={busy} onClick={handlePreview}>
                  预览
                </button>
                <button
                  type="button"
                  className={buttonClass}
                  disabled={preview === null}
                  onClick={() => setStep('mapping')}
                >
                  下一步
                </button>
                <button type="button" className={buttonClass} onClick={closeWizard}>
                  取消
                </button>
              </div>
              {preview !== null && (
                <div className="flex flex-col gap-2">
                  {preview.truncated && (
                    <p role="status" className="text-sm text-ink">
                      只取了前 {preview.row_count} 行看看，实际可能更多。
                    </p>
                  )}
                  {/* 列名要摆出来：下一步的列映射照着它配，只给行数的话
                      用户得自己回数据库里查列名。 */}
                  <div className="overflow-x-auto">
                    <table className="min-w-full text-left text-sm">
                      <thead>
                        <tr>
                          {preview.columns.map((column) => (
                            <th key={column} className="border-b border-subtle px-2 py-1 font-bold text-ink">
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {preview.rows.slice(0, 10).map((row, index) => (
                          <tr key={index}>
                            {row.map((cell, cellIndex) => (
                              <td key={cellIndex} className="border-b border-subtle px-2 py-1 text-ink">
                                {String(cell)}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}

          {step === 'mapping' && (
            <form className="flex flex-col gap-3" onSubmit={handleSave}>
              <h2 className="text-sm font-bold text-ink">第三步：配列映射</h2>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="flex flex-col gap-1 text-sm text-ink">
                  数据源名字
                  <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
                </label>
                {/* 提示语放在 label 外面：放里面的话，这个字段的可访问名字
                    会变成"实体类型 + 整段提示"，读屏软件念一长串，测试也
                    按名字找不到它。 */}
                <div className="flex flex-col gap-1 text-sm text-ink">
                  <label className="flex flex-col gap-1">
                    实体类型
                    {termTypeOptions === null || termTypeOptions.length === 0 ? (
                      <input
                        className={inputClass}
                        value={termType}
                        onChange={(e) => setTermType(e.target.value)}
                        placeholder="产品"
                        disabled={termTypeOptions?.length === 0}
                      />
                    ) : (
                      <select
                        className={inputClass}
                        value={termType}
                        onChange={(e) => setTermType(e.target.value)}
                      >
                        <option value="">请选择实体类型</option>
                        {termTypeOptions.map((value) => (
                          <option key={value} value={value}>
                            {value}
                          </option>
                        ))}
                      </select>
                    )}
                  </label>
                  {termTypeOptions === null && (
                    // 拉不到已确认类型时退回自由文本，并说清代价——不说的话
                    // 用户以为随便填都行，而 ETL 会在跑起来之后才拒。
                    <span className="text-xs text-ink-soft">
                      没能拉到已确认的实体类型，这里暂时按自由文本处理。填一个
                      本体里没有的类型，导入跑起来时会被拒。
                    </span>
                  )}
                  {termTypeOptions?.length === 0 && (
                    <span className="text-xs text-ink-soft">
                      这个领域还没有已确认的实体类型。先去「本体结构」建一个并
                      确认，再回来配数据源。
                    </span>
                  )}
                </div>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  展示名列
                  <input
                    className={inputClass}
                    value={nameColumn}
                    onChange={(e) => setNameColumn(e.target.value)}
                    placeholder={preview?.columns[1] ?? 'name'}
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm text-ink">
                  唯一键列
                  <input
                    className={inputClass}
                    value={keyColumn}
                    onChange={(e) => setKeyColumn(e.target.value)}
                    placeholder={preview?.columns[0] ?? 'id'}
                  />
                </label>
              </div>
              <div className="flex gap-2">
                <button type="submit" className={buttonClass} disabled={busy}>
                  保存数据源
                </button>
                <button type="button" className={buttonClass} onClick={closeWizard}>
                  取消
                </button>
              </div>
            </form>
          )}
        </div>
      )}

      {sources === null && <Skeleton variant="card-list" count={2} />}

      {sources !== null && sources.length === 0 && !wizardOpen && (
        <EmptyState
          icon={Database}
          title="还没有数据源"
          action="点「新建数据源」填一次连接信息和列映射，之后每次点「重新同步」就能把最新数据拉进来。"
        />
      )}

      {sources !== null && sources.length > 0 && (
        <ul className="flex flex-col gap-2">
          {sources.map((source) => (
            <li
              key={source.source_id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-subtle bg-card p-3 text-sm"
            >
              <span className="flex flex-wrap items-center gap-2">
                <Database aria-hidden="true" className="h-4 w-4 shrink-0 text-ink-soft" />
                <span className="font-bold text-ink">{source.name}</span>
                <span className="text-ink-soft">
                  · {source.driver} · {source.host}
                  {source.port ? `:${source.port}` : ''}/{source.database}
                </span>
                {/* 时间和行数一起说。只有时间的话，用户不知道那次同步是
                    成功导了数据还是导了个空；从没同步过则明说，不显示
                    「0 行」——那是两件完全不同的事。 */}
                <span className="text-ink-soft">
                  ·{' '}
                  {source.last_sync_at === null
                    ? '还没同步过'
                    : `上次同步 ${source.last_sync_at} · ${(source.last_sync_rows ?? 0).toLocaleString()} 行`}
                </span>
              </span>
              <span className="flex gap-2">
                <button
                  type="button"
                  className={buttonClass}
                  disabled={busy}
                  onClick={() => setSyncing(source)}
                >
                  重新同步
                </button>
                <button type="button" className={buttonClass} onClick={() => void handleDelete(source)}>
                  删除
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {syncing !== null && (
        <PasswordPrompt
          sourceName={syncing.name}
          onSubmit={(value) => void handleSync(syncing, value)}
          onCancel={() => setSyncing(null)}
        />
      )}
    </div>
  )
}
