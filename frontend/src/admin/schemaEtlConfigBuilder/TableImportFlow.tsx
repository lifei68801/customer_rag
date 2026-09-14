import { useEffect, useRef, useState, type ReactNode } from 'react'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { CopyButton } from '../CopyButton'
import type { EtlMapping } from '../etlMappingApi'
import { buildConfigYaml } from './buildConfigYaml'
import { prefillMapping } from './prefillMapping'
import { readTableHeaderColumns } from './tableHeader'
import { EntityMappingEditor } from './EntityMappingEditor'
import { RelationMappingEditor } from './RelationMappingEditor'
import type {
  AddedFile,
  BuilderEntity,
  BuilderRelation,
  ConfirmedCombination,
  ConfirmedTermType,
} from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface TableImportFlowProps {
  tenantId: string
  sessionToken: string
  /** 本体没确认时整条流程禁用。 */
  disabled: boolean
  /** 存好的映射。undefined=还在读，null=没有。 */
  mapping: EtlMapping | null | undefined
  onSubmitted: (runId: string) => void
  /**
   * 每加一，就把第二步的映射编辑器展开一次。
   *
   * 跑批失败详情里的「去改映射」要打开它。用布尔量的话，用户自己收起来之后
   * 那个布尔仍是 true，再点一次「去改映射」就什么都不会发生。
   */
  openMappingSignal: number
}

/**
 * 表格导入的主流程：**选表 → 看映射 → 运行**，一条线，一个主按钮。
 *
 * ## 它替掉了什么
 *
 * 此前这一页有三个长得一样、语义完全不同的文件输入框：主表单的「数据文件」
 * （要导入的数据）、映射向导第一步的文件框（只为读表头）、「高级」里的
 * config.yaml + 数据文件。还有两个并列的主按钮——「开始运行」和「确认并开始
 * 运行」，做的事不同而外观一致。用户读不出该走哪条。
 *
 * 更要命的是顺序：运行按钮在最上面，映射折在下面的面板里，而那个面板里还要
 * 再传一次表。于是点运行时用户根本没见过映射。真实事故就是这么发生的——
 * 邮编挂在「客户名」下，同名客户邮编不同，ETL 拒绝写入 530 个 node_key，
 * 而界面上没有任何一处提前显示过这件事。
 *
 * ## 现在的顺序
 *
 * 先有表，才谈映射：选完文件就地读表头，第二步**已经填好**——有存着的映射
 * 且列对得上就沿用它，否则按已确认本体现推一份（见 prefillMapping）。填好的
 * 东西全都能改。第三步才是唯一的主按钮。
 *
 * ## 提交时带不带 config
 *
 * 沿用存着的那份、且用户一个字没改 → **不带** config，让后端用存好的映射。
 * 带一份 config 会被后端当权威，所以这里绝不能"顺手也带上"。其余情况一律
 * 带上当前编辑器的内容。
 */
export function TableImportFlow({
  tenantId,
  sessionToken,
  disabled,
  mapping,
  onSubmitted,
  openMappingSignal,
}: TableImportFlowProps) {
  const [termTypes, setTermTypes] = useState<ConfirmedTermType[]>([])
  const [combinations, setCombinations] = useState<ConfirmedCombination[]>([])
  const [files, setFiles] = useState<AddedFile[]>([])
  const [reading, setReading] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)
  const [entities, setEntities] = useState<BuilderEntity[]>([])
  const [relations, setRelations] = useState<BuilderRelation[]>([])
  const [source, setSource] = useState<'stored' | 'suggested' | null>(null)
  const [unusedColumns, setUnusedColumns] = useState<string[]>([])
  const [unmatchedTermTypes, setUnmatchedTermTypes] = useState<string[]>([])
  const [storedMissingColumns, setStoredMissingColumns] = useState<string[] | null>(null)
  const [mappingExpanded, setMappingExpanded] = useState(false)
  const [yamlExpanded, setYamlExpanded] = useState(false)
  const [dryRun, setDryRun] = useState(false)
  const [allowLargeSweep, setAllowLargeSweep] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  /** 预填时的映射快照，用来判断用户有没有改过。 */
  const prefilledRef = useRef<string>('')

  // 切换租户时上一租户的文件和映射全部作废：留着的话 buildConfigYaml 会拿
  // 新租户的 tenantId 拼上旧租户的实体类型，生成一份看似合法、实际本体不
  // 匹配的配置。
  useEffect(() => {
    setFiles([])
    setEntities([])
    setRelations([])
    setSource(null)
    setFileError(null)
    setSubmitError(null)
  }, [tenantId])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const [termTypesRes, combinationsRes] = await Promise.all([
        adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`,
          sessionToken,
        ),
        adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=confirmed`,
          sessionToken,
        ),
      ])
      if (cancelled) return
      const termTypesData = (await termTypesRes.json()) as { term_types: ConfirmedTermType[] }
      const combinationsData = (await combinationsRes.json()) as { constraints: ConfirmedCombination[] }
      if (cancelled) return
      setTermTypes(termTypesData.term_types)
      setCombinations(combinationsData.constraints)
    }
    load().catch((err) => console.error('加载本体数据失败', err))
    return () => {
      cancelled = true
    }
  }, [tenantId, sessionToken])

  const handleChooseFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return
    setFileError(null)
    setReading(true)
    try {
      const added: AddedFile[] = []
      for (const file of Array.from(fileList)) {
        added.push({ id: crypto.randomUUID(), file, columns: await readTableHeaderColumns(file) })
      }
      setFiles(added)
      // 按第一张表预填。多表时其余表的列不参与预填——猜出来的跨表映射比
      // 空着更难纠正，那几张表的映射让用户自己加。
      const prefill = prefillMapping({
        columns: added[0].columns,
        summary: mapping?.summary ?? null,
        termTypes,
        combinations,
        fileId: added[0].id,
      })
      setEntities(prefill.entities)
      setRelations(prefill.relations)
      setSource(prefill.source)
      setUnusedColumns(prefill.unusedColumns)
      setUnmatchedTermTypes(prefill.unmatchedTermTypes)
      setStoredMissingColumns(prefill.storedMissingColumns)
      prefilledRef.current = JSON.stringify({
        entities: prefill.entities,
        relations: prefill.relations,
      })
      // 沿用上次的映射默认折起来（用户已经确认过它一次），现推的建议默认
      // 展开——那份东西他还没看过。
      setMappingExpanded(prefill.source === 'suggested')
    } catch (err) {
      setFileError(err instanceof Error ? err.message : '读取文件表头失败')
    } finally {
      setReading(false)
    }
  }

  useEffect(() => {
    if (openMappingSignal > 0) setMappingExpanded(true)
  }, [openMappingSignal])

  const edited = source !== null && JSON.stringify({ entities, relations }) !== prefilledRef.current
  const usesStoredMapping = source === 'stored' && !edited

  const handleRun = async () => {
    if (files.length === 0) {
      setSubmitError('先选要导入的数据文件。')
      return
    }
    setSubmitting(true)
    setSubmitError(null)
    try {
      const formData = new FormData()
      if (!usesStoredMapping) {
        const yamlText = buildConfigYaml({ tenantId, entities, relations, files })
        formData.append('config', new Blob([yamlText], { type: 'text/yaml' }), 'config.yaml')
      }
      for (const f of files) formData.append('data_files', f.file)
      formData.append('dry_run', String(dryRun))
      formData.append('allow_large_sweep', String(allowLargeSweep))
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
        sessionToken,
        { method: 'POST', body: formData },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '启动失败'))
      }
      const { run_id } = (await response.json()) as { run_id: string }
      onSubmitted(run_id)
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : '启动失败')
    } finally {
      setSubmitting(false)
    }
  }

  const mappingHint = (() => {
    if (source === null) return '选完文件后，这里会按本体自动填好'
    if (usesStoredMapping) return '沿用上次配好的映射'
    if (source === 'stored') return '在上次配好的映射上改过'
    return '按已确认本体现推的建议，可以改'
  })()

  return (
    <div data-testid="table-import-flow" className="flex flex-col gap-3">
      <Step index={1} title="选择数据文件" done={files.length > 0}>
        <label className="flex flex-col gap-1 text-sm text-ink">
          <span className="font-bold">数据文件（CSV/TSV/XLSX/XLS，可多选）</span>
          <input
            type="file"
            accept=".csv,.tsv,.xlsx,.xls"
            multiple
            disabled={disabled}
            onChange={(e) => {
              handleChooseFiles(e.target.files).catch((err) => console.error(err))
              e.target.value = ''
            }}
            className="text-sm text-ink"
          />
        </label>
        {reading && <p className="text-sm text-ink-soft">正在读取表头…</p>}
        {fileError && (
          <p role="alert" className="text-sm text-ink">
            {fileError}
          </p>
        )}
        {files.length > 0 && (
          <ul className="flex flex-col gap-1 text-sm text-ink">
            {files.map((f) => (
              <li key={f.id}>
                {f.file.name}（{f.columns.length} 列）
              </li>
            ))}
          </ul>
        )}
      </Step>

      <Step index={2} title="字段映射" done={source !== null} hint={mappingHint}>
        {source === null ? (
          <p className="text-sm text-ink-soft">还没有选文件。</p>
        ) : (
          <>
            {storedMissingColumns && (
              <p
                role="status"
                data-testid="stored-mapping-not-reused"
                className="rounded-card border border-accent-secondary bg-paper px-3 py-2 text-sm text-ink"
              >
                没有沿用上次配好的映射：它用到的{' '}
                <code className="font-mono">{storedMissingColumns.join('、')}</code> 在这张表里不存在。
                下面是按本体重新推的一份。
              </p>
            )}

            {!mappingExpanded && (
              <div data-testid="mapping-overview" className="flex flex-col gap-1.5 text-sm">
                {entities.map((entity) => (
                  <p key={entity.id} className="text-ink">
                    <span className="font-bold">{entity.termType}</span>
                    <span className="text-ink-soft"> ← </span>
                    <code className="font-mono text-xs">
                      {entity.nodeKeyParts
                        .map((p) =>
                          p.kind === 'column' ? p.column : `按「${p.rawValueColumn}」分配编号`,
                        )
                        .join(' + ')}
                    </code>
                    {Object.keys(entity.fieldMappings).length > 0 && (
                      <span className="text-ink-soft">
                        ，＋{Object.keys(entity.fieldMappings).length} 个属性
                      </span>
                    )}
                  </p>
                ))}
                {relations.length > 0 && (
                  <p className="text-xs text-ink-soft">
                    关系：
                    {relations
                      .map((r) => `${r.subjectTermType} —${r.relationType}→ ${r.objectTermType}`)
                      .join('；')}
                  </p>
                )}
              </div>
            )}

            {mappingExpanded && (
              <div className="flex flex-col gap-3">
                {entities.map((entity) => (
                  <EntityMappingEditor
                    key={entity.id}
                    entity={entity}
                    files={files}
                    termTypes={termTypes}
                    onChange={(next) =>
                      setEntities((prev) => prev.map((e) => (e.id === next.id ? next : e)))
                    }
                    onRemove={() => setEntities((prev) => prev.filter((e) => e.id !== entity.id))}
                  />
                ))}
                <button
                  type="button"
                  onClick={() =>
                    setEntities((prev) => [
                      ...prev,
                      {
                        id: crypto.randomUUID(),
                        termType: '',
                        fileId: files[0]?.id ?? null,
                        standardNameColumn: '',
                        nodeKeyParts: [{ kind: 'column', column: '' }],
                        fieldMappings: {},
                      },
                    ])
                  }
                  disabled={disabled}
                  className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-card px-3 py-1.5 text-sm font-bold text-ink disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  ＋ 添加实体映射
                </button>

                {relations.map((relation) => (
                  <RelationMappingEditor
                    key={relation.id}
                    relation={relation}
                    files={files}
                    combinations={combinations}
                    onChange={(next) =>
                      setRelations((prev) => prev.map((r) => (r.id === next.id ? next : r)))
                    }
                    onRemove={() => setRelations((prev) => prev.filter((r) => r.id !== relation.id))}
                  />
                ))}
                <button
                  type="button"
                  onClick={() =>
                    setRelations((prev) => [
                      ...prev,
                      {
                        id: crypto.randomUUID(),
                        fileId: files[0]?.id ?? null,
                        subjectTermType: '',
                        relationType: '',
                        objectTermType: '',
                      },
                    ])
                  }
                  disabled={disabled}
                  className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-card px-3 py-1.5 text-sm font-bold text-ink disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  ＋ 添加关系映射
                </button>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-3 pt-1">
              <button
                type="button"
                data-testid="toggle-mapping-editor"
                onClick={() => setMappingExpanded((prev) => !prev)}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink ${focusRing}`}
              >
                {mappingExpanded ? '收起' : '改这份映射'}
              </button>
              {unusedColumns.length > 0 && (
                <span className="text-xs text-ink-soft">没用到的列：{unusedColumns.join('、')}</span>
              )}
              {unmatchedTermTypes.length > 0 && (
                <span className="text-xs text-ink-soft">
                  这张表里没有对应列的实体类型：{unmatchedTermTypes.join('、')}
                </span>
              )}
            </div>

            {/* YAML 是实现细节，不该是这一步的主角。此前它以一个撑满视野的
                <pre> 占着向导第四步的位置，用户被迫读自己不需要读的东西。 */}
            <div className="flex flex-col gap-2">
              <button
                type="button"
                onClick={() => setYamlExpanded((prev) => !prev)}
                className={`self-start text-xs font-bold text-ink-soft underline ${focusRing}`}
              >
                {yamlExpanded ? '收起配置文件（YAML）' : '查看这份映射的配置文件（YAML）'}
              </button>
              {yamlExpanded && (
                <>
                  <pre className="max-h-80 overflow-auto rounded-card border border-subtle bg-paper p-3 text-xs text-ink">
                    {buildConfigYaml({ tenantId, entities, relations, files })}
                  </pre>
                  <CopyButton
                    getText={() => buildConfigYaml({ tenantId, entities, relations, files })}
                  />
                </>
              )}
            </div>
          </>
        )}
      </Step>

      <Step index={3} title="开始导入" done={false}>
        <label className="flex flex-wrap items-center gap-2 text-sm font-bold text-ink">
          <input
            type="checkbox"
            checked={dryRun}
            disabled={disabled}
            onChange={(e) => setDryRun(e.target.checked)}
          />
          预演（terms 和 Neo4j 零写入）
          <span className="font-normal text-ink-soft">
            只报告将要移除多少实体；预演只覆盖实体侧，关系侧无法预演。
          </span>
        </label>
        {/* 这个开关此前只存在于「高级」那个折叠面板里，而那个表单的
            config.yaml 是必填的——跑批被大规模清理阀挡住之后，走引导路径的
            用户手上没有 YAML，等于没有重试的办法。 */}
        <label className="flex flex-wrap items-center gap-2 text-sm font-bold text-ink">
          <input
            type="checkbox"
            checked={allowLargeSweep}
            disabled={disabled}
            onChange={(e) => setAllowLargeSweep(e.target.checked)}
          />
          允许大规模清理
          <span className="font-normal text-ink-soft">
            本次移除比例超过安全阈值时也继续。默认不勾。
          </span>
        </label>
        {submitError && (
          <p role="alert" className="text-sm text-ink">
            {submitError}
          </p>
        )}
        <button
          type="button"
          data-testid="run-import"
          onClick={() => {
            handleRun().catch((err) => console.error(err))
          }}
          disabled={disabled || submitting || files.length === 0 || entities.length === 0}
          className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-5 py-2.5 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {submitting ? '提交中…' : '开始导入'}
        </button>
      </Step>
    </div>
  )
}

/**
 * 一步。序号是真的步骤指示器，不是一个写着"1."的小标题——用户要看得出自己
 * 走到哪、还剩什么。做完的那步序号填实心，剩下的是空心。
 */
function Step({
  index,
  title,
  hint,
  done,
  children,
}: {
  index: number
  title: string
  hint?: string
  done: boolean
  children: ReactNode
}) {
  return (
    <section className="flex flex-col gap-2 rounded-panel border border-subtle bg-card p-4">
      <h3 className="flex flex-wrap items-baseline gap-2">
        <span
          aria-hidden="true"
          className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
            done ? 'bg-accent-primary text-on-accent' : 'border border-subtle bg-paper text-ink-soft'
          }`}
        >
          {index}
        </span>
        <span className="font-bold text-ink">{title}</span>
        {hint && <span className="text-xs font-normal text-ink-soft">{hint}</span>}
      </h3>
      {children}
    </section>
  )
}
