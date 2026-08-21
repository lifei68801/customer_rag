import { useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { CopyButton } from '../CopyButton'
import { buildConfigYaml } from './buildConfigYaml'
import { readCsvHeaderColumns } from './csvHeader'
import { EntityMappingEditor } from './EntityMappingEditor'
import { RelationMappingEditor } from './RelationMappingEditor'
import type { AddedFile, BuilderEntity, BuilderRelation, ConfirmedCombination, ConfirmedTermType } from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface SchemaEtlConfigBuilderProps {
  tenantId: string
  sessionToken: string
  disabled: boolean
  onSubmitted: () => void
}

export function SchemaEtlConfigBuilder({
  tenantId,
  sessionToken,
  disabled,
  onSubmitted,
}: SchemaEtlConfigBuilderProps) {
  const [termTypes, setTermTypes] = useState<ConfirmedTermType[]>([])
  const [combinations, setCombinations] = useState<ConfirmedCombination[]>([])
  const [files, setFiles] = useState<AddedFile[]>([])
  const [entities, setEntities] = useState<BuilderEntity[]>([])
  const [relations, setRelations] = useState<BuilderRelation[]>([])
  const [fileError, setFileError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // 切换租户时，之前添加的文件/实体映射/关系映射都是针对旧租户的本体选出来的
  // termType/relationType，必须清空——否则用户在向导展开状态下切换租户，
  // buildConfigYaml 会拿新租户的 tenantId 拼上旧租户选出来的 termType，
  // 生成一份看似合法、实际本体不匹配的配置。跟 SchemaEtlPage.tsx 里
  // "查看示例数据" 区块切换租户时清空 sampleFiles 的处理是同一个模式。
  useEffect(() => {
    setFiles([])
    setEntities([])
    setRelations([])
    setFileError(null)
    setSubmitError(null)
  }, [tenantId])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const [termTypesRes, combinationsRes] = await Promise.all([
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken),
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=confirmed`, sessionToken),
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

  const handleAddFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return
    setFileError(null)
    try {
      const newFiles: AddedFile[] = []
      for (const file of Array.from(fileList)) {
        const columns = await readCsvHeaderColumns(file)
        newFiles.push({ id: crypto.randomUUID(), file, columns })
      }
      setFiles((prev) => [...prev, ...newFiles])
    } catch (err) {
      setFileError(err instanceof Error ? err.message : '读取文件表头失败')
    }
  }

  const handleAddEntity = () => {
    setEntities((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        termType: '',
        fileId: null,
        standardNameColumn: '',
        nodeKeyParts: [{ kind: 'column', column: '' }],
        fieldMappings: {},
      },
    ])
  }

  const handleAddRelation = () => {
    setRelations((prev) => [
      ...prev,
      { id: crypto.randomUUID(), fileId: null, subjectTermType: '', relationType: '', objectTermType: '' },
    ])
  }

  return (
    <div className="flex flex-col gap-4 border-t-2 border-ink p-4">
      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">1. 添加数据文件</span>
        <input
          type="file"
          accept=".csv"
          multiple
          disabled={disabled}
          onChange={(e) => {
            handleAddFiles(e.target.files).catch((err) => console.error(err))
            e.target.value = ''
          }}
          className="text-ink"
        />
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
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">2. 配置实体映射</span>
        {entities.map((entity) => (
          <EntityMappingEditor
            key={entity.id}
            entity={entity}
            files={files}
            termTypes={termTypes}
            onChange={(next) => setEntities((prev) => prev.map((e) => (e.id === next.id ? next : e)))}
            onRemove={() => setEntities((prev) => prev.filter((e) => e.id !== entity.id))}
          />
        ))}
        <button
          type="button"
          onClick={handleAddEntity}
          disabled={disabled || files.length === 0}
          className={`self-start border border-subtle bg-card px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          + 添加实体映射
        </button>
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">3. 配置关系映射（可选）</span>
        {relations.map((relation) => (
          <RelationMappingEditor
            key={relation.id}
            relation={relation}
            files={files}
            combinations={combinations}
            onChange={(next) => setRelations((prev) => prev.map((r) => (r.id === next.id ? next : r)))}
            onRemove={() => setRelations((prev) => prev.filter((r) => r.id !== relation.id))}
          />
        ))}
        <button
          type="button"
          onClick={handleAddRelation}
          disabled={disabled || files.length === 0}
          className={`self-start border border-subtle bg-card px-3 py-1.5 text-sm font-bold text-ink shadow-soft-sm disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          + 添加关系映射
        </button>
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">4. 预览并提交</span>
        <pre className="max-h-80 overflow-auto border border-subtle bg-card p-3 text-xs text-ink">
          {buildConfigYaml({ tenantId, entities, relations, files })}
        </pre>
        <CopyButton getText={() => buildConfigYaml({ tenantId, entities, relations, files })} />
        {submitError && (
          <p role="alert" className="text-sm text-ink">
            {submitError}
          </p>
        )}
        <button
          type="button"
          disabled={disabled || submitting || entities.length === 0}
          onClick={async () => {
            setSubmitError(null)
            setSubmitting(true)
            try {
              const yamlText = buildConfigYaml({ tenantId, entities, relations, files })
              const formData = new FormData()
              formData.append('config', new Blob([yamlText], { type: 'text/yaml' }), 'config.yaml')
              for (const f of files) {
                formData.append('data_files', f.file)
              }
              const response = await adminFetch(
                `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
                sessionToken,
                { method: 'POST', body: formData },
              )
              if (!response.ok) {
                const body = await response.json().catch(() => ({}))
                throw new Error(extractErrorDetail(body, '启动失败'))
              }
              setFiles([])
              setEntities([])
              setRelations([])
              onSubmitted()
            } catch (err) {
              setSubmitError(err instanceof Error ? err.message : '启动失败')
            } finally {
              setSubmitting(false)
            }
          }}
          className={`min-h-[44px] cursor-pointer self-start border border-subtle bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-soft transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {submitting ? '提交中…' : '确认并开始运行'}
        </button>
      </div>
    </div>
  )
}
