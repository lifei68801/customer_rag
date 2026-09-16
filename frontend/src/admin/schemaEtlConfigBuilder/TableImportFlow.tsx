import { useEffect, useRef, useState, type ReactNode } from 'react'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { CopyButton } from '../CopyButton'
import type { EtlMapping } from '../etlMappingApi'
import { buildConfigYaml } from './buildConfigYaml'
import { prefillMapping } from './prefillMapping'
import { scanPairs, type PairReport } from '../guidedOntology/columnStats'
import {
  addColumnToKey,
  dropField,
  findMappingConflicts,
  singleKeyColumns,
  type MappingConflict,
} from './mappingConflicts'
import {
  listSheetNames,
  readSourceHeader,
  readSourcePreview,
  sameEffectiveParseOptions,
  type SourceParseOptions,
} from './sourceParser'
import { storedParseOptionsFor } from './storedParseOptions'
import {
  draftFromOptions,
  optionsFromDraft,
  skippedRows,
  type ParseDraft,
} from './parseSettingsDraft'
import { EntityMappingEditor } from './EntityMappingEditor'
import { RelationMappingEditor } from './RelationMappingEditor'
import type {
  AddedFile,
  BuilderEntity,
  BuilderRelation,
  ConfirmedCombination,
  ConfirmedTermType,
} from './types'

/**
 * 预览给多少行。表头堆最深的真实样本（MUJI 的 SKU 主数据表）是第 6 行才到
 * 真表头，20 行足够让用户看清表头之上有什么、数据从哪里开始。
 */
const PREVIEW_ROWS = 20

/** 预览表格最多铺几列。113 列的表全铺出来，用户横着找不到边。 */
const PREVIEW_COLUMNS = 12

/** 一张表的解析设置在界面上的全部状态。 */
interface FileParseUi {
  /** 工作表下拉的选项。非 Excel 文件为空数组，那时不显示下拉。 */
  sheetNames: string[]
  /** 原始的前若干行，用户对着它挑表头行。 */
  preview: string[][]
  /** 输入框里的原样文字，可能是解析器不接受的中间态。 */
  draft: ParseDraft
  /** 当前文字翻不成解析选项时的那句话；能翻就为 null。 */
  error: string | null
  /** 这份设置是从存着的映射里回填的，不是缺省。 */
  fromStored: boolean
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface TableImportFlowProps {
  tenantId: string
  sessionToken: string
  /** 本体没确认时整条流程禁用。 */
  disabled: boolean
  /** 存好的映射。undefined=还在读，null=没有。 */
  mapping: EtlMapping | null | undefined
  /**
   * 跑批已提交。mappingSaved 说明这次用的映射有没有被记成这个本体的默认
   * 映射——默默替换掉默认映射的话，用户下次进来看到的"沿用上次"会跟他
   * 记忆里的不一样。
   */
  onSubmitted: (runId: string, mappingSaved: boolean) => void
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
  const [repairedRelations, setRepairedRelations] = useState<
    { relationType: string; fromSubject: string; toSubject: string }[]
  >([])
  const [droppedRelations, setDroppedRelations] = useState<
    { subject: string; relationType: string; object: string }[]
  >([])
  const [mappingExpanded, setMappingExpanded] = useState(false)
  const [yamlExpanded, setYamlExpanded] = useState(false)
  const [dryRun, setDryRun] = useState(false)
  const [allowLargeSweep, setAllowLargeSweep] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [pairReport, setPairReport] = useState<PairReport | null>(null)
  const [parseUi, setParseUi] = useState<Record<string, FileParseUi>>({})
  const [checking, setChecking] = useState(false)

  /** 预填时的映射快照，用来判断用户有没有改过。 */
  const prefilledRef = useRef<string>('')
  /**
   * 每张表最近一次重新解析的序号。用户连着改几下时，先发出的请求可能后回来，
   * 不按序号丢弃的话界面会停在一个用户已经改掉的列名上。
   */
  const parseSeqRef = useRef<Record<string, number>>({})

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
    setPairReport(null)
    setParseUi({})
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

  /**
   * 按第一张表把第二步填好。多表时其余表的列不参与预填——猜出来的跨表映射
   * 比空着更难纠正，那几张表的映射让用户自己加。所以这里只收那一张表，而
   * 不是整个文件列表：收列表的话，改第二张表的解析设置也会把第一张表的
   * 映射重推一遍。
   *
   * 列名变了就得重跑一遍：表头行一改，存着的映射可能对不上了，这条路径
   * prefillMapping 已经处理（storedMissingColumns）。
   */
  const applyPrefill = (primary: AddedFile) => {
    const prefill = prefillMapping({
      columns: primary.columns,
      summary: mapping?.summary ?? null,
      termTypes,
      combinations,
      fileId: primary.id,
    })
    setEntities(prefill.entities)
    setRelations(prefill.relations)
    setSource(prefill.source)
    setUnusedColumns(prefill.unusedColumns)
    setUnmatchedTermTypes(prefill.unmatchedTermTypes)
    setStoredMissingColumns(prefill.storedMissingColumns)
    setRepairedRelations(prefill.repairedRelations)
    setDroppedRelations(prefill.droppedRelations)
    prefilledRef.current = JSON.stringify({
      entities: prefill.entities,
      relations: prefill.relations,
    })
    // 沿用上次的映射默认折起来（用户已经确认过它一次），现推的建议默认
    // 展开——那份东西他还没看过。
    setMappingExpanded(prefill.source === 'suggested')
  }

  const handleChooseFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return
    setFileError(null)
    setReading(true)
    try {
      const added: AddedFile[] = []
      const ui: Record<string, FileParseUi> = {}
      for (const file of Array.from(fileList)) {
        // 存着的设置是用户自己配的，优先于缺省。没存过就是第 1 行——不猜，
        // 由用户对着下面的原始预览自己挑（按填充密度猜表头行这条路线已被
        // 定量证伪，见 2026-09-16-header-row-detection-design.md）。
        const stored = storedParseOptionsFor(mapping?.summary ?? null, file.name)
        const parseOptions: SourceParseOptions = stored ?? {}
        const id = crypto.randomUUID()
        added.push({
          id,
          file,
          columns: await readSourceHeader(file, parseOptions),
          parseOptions,
        })
        ui[id] = {
          sheetNames: await listSheetNames(file),
          preview: await readSourcePreview(file, PREVIEW_ROWS, parseOptions),
          draft: draftFromOptions(parseOptions),
          error: null,
          fromStored: stored !== null,
        }
      }
      setFiles(added)
      setParseUi(ui)
      parseSeqRef.current = {}
      applyPrefill(added[0])
    } catch (err) {
      setFileError(err instanceof Error ? err.message : '读取文件表头失败')
    } finally {
      setReading(false)
    }
  }

  /**
   * 解析设置改了之后重读列名。
   *
   * 中间态不到这里来：输入框里的文字先经过 optionsFromDraft，翻不过去就停在
   * 上一次的列名并把拒绝的理由显示出来（见 handleDraftChange）。
   */
  const reparse = async (target: AddedFile, options: SourceParseOptions, seq: number) => {
    const sheetChanged = options.sheet !== target.parseOptions.sheet
    try {
      const columns = await readSourceHeader(target.file, options)
      const preview = sheetChanged
        ? await readSourcePreview(target.file, PREVIEW_ROWS, options)
        : null
      if (parseSeqRef.current[target.id] !== seq) return
      const updated: AddedFile = { ...target, columns, parseOptions: options }
      // 函数式更新，不拿渲染时那份 files 快照去 map：多表时另一张表的在途
      // 解析可能正好落在这中间，用快照会把它刚落下的改动盖掉。
      setFiles((prev) => prev.map((f) => (f.id === target.id ? updated : f)))
      if (preview !== null) {
        setParseUi((prev) => ({ ...prev, [target.id]: { ...prev[target.id], preview } }))
      }
      // 列名没变就不重推映射。重推一遍会把用户在编辑器里改过的东西悄悄换成
      // 预填的那份，而他只是改了个首数据行；折叠状态跟着被打断也是同一件事。
      // 只有第一张表的列参与预填，改其余表的解析设置不重推。
      const isPrefillSource = files[0]?.id === target.id
      const columnsChanged = columns.join(' ') !== target.columns.join(' ')
      if (isPrefillSource && columnsChanged) applyPrefill(updated)
    } catch (err) {
      if (parseSeqRef.current[target.id] !== seq) return
      // 读不出来就说出来。默默留着上一次的列名的话，用户以为自己选的工作表
      // 生效了，而页面上的列名其实是另一张表的。
      setParseUi((prev) => ({
        ...prev,
        [target.id]: {
          ...prev[target.id],
          error: err instanceof Error ? err.message : '按这份设置读不出列名',
        },
      }))
    }
  }

  const handleDraftChange = (fileId: string, patch: Partial<ParseDraft>) => {
    const target = files.find((f) => f.id === fileId)
    const current = parseUi[fileId]
    if (!target || !current) return
    const draft = { ...current.draft, ...patch }
    const result = optionsFromDraft(draft)
    setParseUi((prev) => ({
      ...prev,
      [fileId]: { ...prev[fileId], draft, error: result.ok ? null : result.message },
    }))
    // 翻不过去就到此为止：列名保持上一次的，拒绝的理由已经显示出来了。把中间
    // 态硬喂给解析器只会抛异常，喂一个"猜"出来的替代值则会让用户看到他没选过
    // 的列名。
    if (!result.ok) return
    const seq = (parseSeqRef.current[fileId] ?? 0) + 1
    parseSeqRef.current[fileId] = seq
    reparse(target, result.options, seq).catch((err) => console.error(err))
  }

  useEffect(() => {
    if (openMappingSignal > 0) setMappingExpanded(true)
  }, [openMappingSignal])

  // 身份键定下来之后才扫得了「这个键的每个值是不是只对应一个属性值」。
  //
  // 扫描按**身份键列 × 表里所有列**建对，而不是只按当前挂着的那几个属性——
  // 用户在编辑器里把某一列挂过来时，结论要立刻有，不能每改一次就重扫一遍
  // 整张表。键变了才重扫（keySignature 变化），改属性不重扫。
  const keySignature = singleKeyColumns(entities).join(' ')
  useEffect(() => {
    const file = files[0]
    const hostColumns = keySignature === '' ? [] : keySignature.split(' ')
    if (!file || hostColumns.length === 0) {
      setPairReport(null)
      return
    }
    let cancelled = false
    setChecking(true)
    scanPairs(file.file, { hostColumns, attributeColumns: file.columns })
      .then((report) => {
        if (!cancelled) setPairReport(report)
      })
      .catch((err) => {
        // 扫不动就不给结论。**不能**当成"没冲突"——那等于告诉用户这份映射
        // 验过了。跑批那一端的阀仍然在，最坏是退回原来的行为。
        console.error('单值检测失败', err)
        if (!cancelled) setPairReport(null)
      })
      .finally(() => {
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [files, keySignature])

  const conflicts = findMappingConflicts(entities, pairReport)

  const edited = source !== null && JSON.stringify({ entities, relations }) !== prefilledRef.current
  // 按本体自动改过关系时，**不能**再走"沿用存着的那份"——那条路径提交时不带
  // config，后端会拿存着的旧映射去跑，界面上改过的东西一条都不生效。这正是
  // 用户连撞两次的那个坑：跑批"成功"，图里一条公司边都没有。
  const repairedByOntology = repairedRelations.length > 0 || droppedRelations.length > 0
  // 解析设置改过也算改过，哪怕列名一个字没变。改首数据行、换一张列名恰好相同
  // 的工作表，映射本身还沿用得上，但 sources: 段是新的——走"沿用存着的那份"
  // 就不上传 config，后端会按存着的旧选项去读，说明行被当成数据导进去；而列名
  // 一致，提交时的 client_columns 对账也发现不了。
  const parseOptionsEdited = files.some(
    (f) =>
      !sameEffectiveParseOptions(
        f.parseOptions,
        storedParseOptionsFor(mapping?.summary ?? null, f.file.name) ?? {},
      ),
  )
  const usesStoredMapping =
    source === 'stored' && !edited && !repairedByOntology && !parseOptionsEdited

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
      // 把页面上看到的列名一并发给后端对账。前端本地解析、后端跑批解析，两份
      // 规则会悄悄分叉——9c71cf9 已经让它们分叉过一次。不对账的话，用户拿到的
      // 结果跟他在界面上看到的不一样，而且没有任何提示。
      formData.append(
        'client_columns',
        JSON.stringify(Object.fromEntries(files.map((f) => [f.file.name, f.columns]))),
      )
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
      const { run_id, mapping_saved_as_default } = (await response.json()) as {
        run_id: string
        mapping_saved_as_default?: boolean
      }
      onSubmitted(run_id, mapping_saved_as_default === true)
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
        {files.length > 0 && (
          <div data-testid="parse-settings" className="flex flex-col gap-3">
            {files.map((f) =>
              parseUi[f.id] ? (
                <ParseSettingsPanel
                  key={f.id}
                  file={f}
                  ui={parseUi[f.id]}
                  showFileName={files.length > 1}
                  disabled={disabled}
                  onChange={(patch) => handleDraftChange(f.id, patch)}
                />
              ) : null,
            )}
          </div>
        )}
        {source === null ? (
          <p className="text-sm text-ink-soft">还没有选文件。</p>
        ) : (
          <>
            {(repairedRelations.length > 0 || droppedRelations.length > 0) && (
              <div
                role="status"
                data-testid="relations-reconciled"
                className="flex flex-col gap-1 rounded-card border border-accent-secondary bg-paper px-3 py-2 text-sm text-ink"
              >
                <p className="font-bold">存着的映射是本体改动之前配的，已按当前本体调整：</p>
                {repairedRelations.map((r) => (
                  <p key={`${r.relationType}-${r.fromSubject}`}>
                    <code className="font-mono text-xs">{r.relationType}</code> 的主体从{' '}
                    <code className="font-mono text-xs">{r.fromSubject}</code> 改成{' '}
                    <code className="font-mono text-xs">{r.toSubject}</code>
                  </p>
                ))}
                {droppedRelations.map((r) => (
                  <p key={`${r.subject}-${r.relationType}-${r.object}`}>
                    去掉了{' '}
                    <code className="font-mono text-xs">
                      {r.subject} —{r.relationType}→ {r.object}
                    </code>
                    ：当前本体里没有这个组合，导入时也会被跳过。
                  </p>
                ))}
                <p className="text-xs text-ink-soft">
                  不调整的话，这些关系会在导入时被静默跳过——跑批照样报告成功，而图里一条边都没有。
                </p>
              </div>
            )}

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

            {checking && <p className="text-xs text-ink-soft">正在检查这份映射跑不跑得通…</p>}
            {conflicts.map((conflict) => (
              <ConflictNotice
                key={`${conflict.entityId}-${conflict.field}`}
                conflict={conflict}
                onAddToKey={() =>
                  setEntities((prev) =>
                    prev.map((e) => (e.id === conflict.entityId ? addColumnToKey(e, conflict.column) : e)),
                  )
                }
                onDrop={() =>
                  setEntities((prev) =>
                    prev.map((e) => (e.id === conflict.entityId ? dropField(e, conflict.field) : e)),
                  )
                }
              />
            ))}

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
 * 一张表的「解析设置」：选工作表、挑表头行、说明首数据行，旁边摆着这张表
 * 原始的前 20 行。
 *
 * 表头行**不做自动探测**，缺省就是第 1 行。按填充密度找稳定数据块那条路线
 * 被定量证伪（结论见 docs/superpowers/specs/2026-09-16-header-row-detection-design.md），
 * 而且猜错的表头行不会被跑批前的列名对账拦住——前后端会一致地用同一个错误
 * 行号，数据以"成功"导入成垃圾。所以这里给的是原始预览，由用户自己挑；解析
 * 设置随映射一起存下来，每种文件形状只需要挑一次。
 */
function ParseSettingsPanel({
  file,
  ui,
  showFileName,
  disabled,
  onChange,
}: {
  file: AddedFile
  ui: FileParseUi
  showFileName: boolean
  disabled: boolean
  onChange: (patch: Partial<ParseDraft>) => void
}) {
  const skip = skippedRows(file.parseOptions)
  // 高亮的是**已经生效**的那一行，不是输入框里的文字：文字非法时列名没有跟着
  // 变，高亮跟着变的话，预览会指向一行其实没被当成表头的内容。
  const highlightedRow = file.parseOptions.headerRow ?? 1
  const totalWidth = ui.preview.reduce((max, row) => Math.max(max, row.length), 0)
  const width = Math.min(PREVIEW_COLUMNS, totalWidth)

  return (
    <div className="flex flex-col gap-2 rounded-card border border-subtle bg-paper px-3 py-2 text-sm text-ink">
      <div className="flex flex-wrap items-end gap-3">
        <span className="font-bold">
          解析设置{showFileName ? `：${file.file.name}` : ''}
        </span>
        {ui.sheetNames.length > 0 && (
          <label className="flex flex-col gap-1 text-xs text-ink-soft">
            <span className="font-bold text-ink">工作表</span>
            <select
              value={
                typeof ui.draft.sheet === 'number'
                  ? (ui.sheetNames[ui.draft.sheet] ?? '')
                  : (ui.draft.sheet ?? '')
              }
              disabled={disabled}
              onChange={(e) => onChange({ sheet: e.target.value === '' ? undefined : e.target.value })}
              className={`min-h-[36px] rounded-control border border-subtle bg-card px-2 text-sm text-ink ${focusRing}`}
            >
              <option value="">第一张表（{ui.sheetNames[0]}）</option>
              {ui.sheetNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-1 text-xs text-ink-soft">
          <span className="font-bold text-ink">表头行</span>
          <input
            type="number"
            min={1}
            value={ui.draft.headerRow}
            disabled={disabled}
            onChange={(e) => onChange({ headerRow: e.target.value })}
            className={`min-h-[36px] w-24 rounded-control border border-subtle bg-card px-2 text-sm text-ink ${focusRing}`}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-ink-soft">
          <span className="font-bold text-ink">首数据行</span>
          <input
            type="number"
            min={1}
            placeholder="紧跟表头"
            value={ui.draft.firstDataRow}
            disabled={disabled}
            onChange={(e) => onChange({ firstDataRow: e.target.value })}
            className={`min-h-[36px] w-24 rounded-control border border-subtle bg-card px-2 text-sm text-ink ${focusRing}`}
          />
        </label>
        <span className="text-xs text-ink-soft">
          {ui.fromStored ? '沿用上次配置' : '缺省：第 1 行表头'}，读到 {file.columns.length} 列
        </span>
      </div>

      {/* 填大了会安静地少读数据，不说出来的话用户看不出自己丢了几行。 */}
      {skip && (
        <p className="text-xs text-ink-soft">
          将跳过第 {skip.from}~{skip.to} 行（表头和数据之间的说明行）。
        </p>
      )}

      {ui.error && (
        <p role="alert" data-testid="parse-settings-error" className="text-xs text-ink">
          {ui.error}下面的列名还是上一次生效的那一份。
        </p>
      )}

      <div className="max-h-56 overflow-auto rounded-card border border-subtle bg-card">
        <table className="min-w-full text-xs">
          <tbody>
            {ui.preview.map((row, index) => (
              <tr
                key={index}
                className={index + 1 === highlightedRow ? 'bg-accent-secondary font-bold' : ''}
              >
                <td className="whitespace-nowrap px-2 py-1 text-ink-soft">
                  {index + 1}
                  {index + 1 === highlightedRow ? ' 表头' : ''}
                </td>
                {Array.from({ length: width }, (_, col) => (
                  <td key={col} className="max-w-40 truncate px-2 py-1 text-ink">
                    {row[col] ?? ''}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {totalWidth > width && (
        <p className="text-xs text-ink-soft">预览只铺了前 {width} 列，这张表共 {totalWidth} 列。</p>
      )}
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

/**
 * 一条「这份映射跑下去会被拒」的预警，连着两条**不用改本体**就能做的出路。
 *
 * 只报问题不给出路的话，用户能做的只有重新上传同一个文件再失败一次——
 * 这一页此前正是这样：跑批失败后甩出一句"530 个 node_key 被算出了不同的
 * 值"，而界面上没有任何地方能改那件事。
 *
 * 第三条出路（把这个字段在本体里挪到另一个实体下）要去「本体结构」页，
 * 不在这里做：表格导入页偷偷改本体，会让"已确认本体"这件事失去意义。
 */
function ConflictNotice({
  conflict,
  onAddToKey,
  onDrop,
}: {
  conflict: MappingConflict
  onAddToKey: () => void
  onDrop: () => void
}) {
  return (
    <div
      role="alert"
      data-testid={`mapping-conflict-${conflict.field}`}
      className="flex flex-col gap-2 rounded-card border border-status-error bg-paper p-3 text-sm text-ink"
    >
      <p>
        <span className="font-bold">这份映射跑下去会被拒绝写入。</span>{' '}
        <code className="font-mono text-xs">{conflict.column}</code> 挂在{' '}
        <span className="font-bold">{conflict.termType}</span> 下，而它的身份键{' '}
        <code className="font-mono text-xs">{conflict.hostColumn}</code>{' '}
        并不唯一：<code className="font-mono text-xs">{conflict.violation.hostValue}</code> 出现在{' '}
        {conflict.violation.rowCount} 行里，
        <code className="font-mono text-xs">{conflict.column}</code> 有{' '}
        {conflict.violation.distinctCount} 个不同的值（{conflict.violation.samples.join('、')}…）。
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onAddToKey}
          className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-card px-3 text-sm font-bold text-ink ${focusRing}`}
        >
          把「{conflict.column}」也算进身份键
        </button>
        <button
          type="button"
          onClick={onDrop}
          className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-card px-3 text-sm font-bold text-ink ${focusRing}`}
        >
          不导入这一列
        </button>
      </div>
      <p className="text-xs text-ink-soft">
        算进身份键的代价：同一个「{conflict.violation.hostValue}」的两条记录会成为两个不同的节点。
        如果这一列本来就该挂在别的实体下（比如订单），去「本体结构」把这个字段挪过去，再回来重新选文件。
      </p>
    </div>
  )
}
