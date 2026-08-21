import type { AddedFile, BuilderRelation, ConfirmedCombination } from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface RelationMappingEditorProps {
  relation: BuilderRelation
  files: AddedFile[]
  combinations: ConfirmedCombination[]
  onChange: (next: BuilderRelation) => void
  onRemove: () => void
}

function combinationKey(c: { subject_term_type: string; relation_type: string; object_term_type: string }): string {
  return `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`
}

export function RelationMappingEditor({
  relation,
  files,
  combinations,
  onChange,
  onRemove,
}: RelationMappingEditorProps) {
  const selectedKey = combinationKey({
    subject_term_type: relation.subjectTermType,
    relation_type: relation.relationType,
    object_term_type: relation.objectTermType,
  })

  return (
    <div className="flex flex-col gap-3 rounded-card border border-subtle bg-paper p-3 shadow-soft-sm">
      <div className="flex items-center justify-between">
        <span className="font-bold text-ink">关系映射</span>
        <button
          type="button"
          onClick={onRemove}
          className={`text-sm font-bold text-status-error underline ${focusRing}`}
        >
          删除
        </button>
      </div>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        关系组合
        <select
          value={relation.subjectTermType ? selectedKey : ''}
          onChange={(e) => {
            if (!e.target.value) {
              onChange({ ...relation, subjectTermType: '', relationType: '', objectTermType: '' })
              return
            }
            const [subject, rel, object] = e.target.value.split('|')
            onChange({ ...relation, subjectTermType: subject, relationType: rel, objectTermType: object })
          }}
          className={`rounded-control border border-subtle bg-card px-2 py-1.5 text-ink focus:shadow-soft focus:outline-none ${focusRing}`}
        >
          <option value="">请选择</option>
          {combinations.map((c) => (
            <option key={combinationKey(c)} value={combinationKey(c)}>
              {c.subject_term_type} —{c.relation_type}→ {c.object_term_type}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        数据文件
        <select
          value={relation.fileId ?? ''}
          onChange={(e) => onChange({ ...relation, fileId: e.target.value || null })}
          className={`rounded-control border border-subtle bg-card px-2 py-1.5 text-ink focus:shadow-soft focus:outline-none ${focusRing}`}
        >
          <option value="">请选择</option>
          {files.map((f) => (
            <option key={f.id} value={f.id}>
              {f.file.name}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}
