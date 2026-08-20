import type { AddedFile, BuilderEntity, ColumnKeyPart, ConfirmedTermType } from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface EntityMappingEditorProps {
  entity: BuilderEntity
  files: AddedFile[]
  termTypes: ConfirmedTermType[]
  onChange: (next: BuilderEntity) => void
  onRemove: () => void
}

export function EntityMappingEditor({
  entity,
  files,
  termTypes,
  onChange,
  onRemove,
}: EntityMappingEditorProps) {
  const selectedFile = files.find((f) => f.id === entity.fileId) ?? null
  const columns = selectedFile?.columns ?? []
  const selectedTermType = termTypes.find((t) => t.value === entity.termType) ?? null

  return (
    <div className="flex flex-col gap-3 border-2 border-ink bg-paper p-3 shadow-brutal-sm">
      <div className="flex items-center justify-between">
        <span className="font-bold text-ink">实体映射</span>
        <button
          type="button"
          onClick={onRemove}
          className={`text-sm font-bold text-status-error underline ${focusRing}`}
        >
          删除
        </button>
      </div>
      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        实体类型
        <select
          value={entity.termType}
          onChange={(e) => onChange({ ...entity, termType: e.target.value, fieldMappings: {} })}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="">请选择</option>
          {termTypes.map((t) => (
            <option key={t.value} value={t.value}>
              {t.value}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        数据文件
        <select
          value={entity.fileId ?? ''}
          onChange={(e) => onChange({ ...entity, fileId: e.target.value || null, standardNameColumn: '' })}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="">请选择</option>
          {files.map((f) => (
            <option key={f.id} value={f.id}>
              {f.file.name}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        标准名称列
        <select
          value={entity.standardNameColumn}
          onChange={(e) => onChange({ ...entity, standardNameColumn: e.target.value })}
          disabled={columns.length === 0}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none disabled:opacity-50"
        >
          <option value="">{columns.length === 0 ? '请先选择数据文件' : '请选择'}</option>
          {columns.map((col) => (
            <option key={col} value={col}>
              {col}
            </option>
          ))}
        </select>
      </label>

      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">Node Key（唯一标识列）</span>
        {entity.nodeKeyParts.map((part, index) => (
          <div key={index} className="flex flex-col gap-1 border border-ink/40 p-2">
            {part.kind === 'column' ? (
              <>
                <select
                  value={part.column}
                  onChange={(e) => {
                    const next = [...entity.nodeKeyParts]
                    next[index] = { kind: 'column', column: e.target.value }
                    onChange({ ...entity, nodeKeyParts: next })
                  }}
                  disabled={columns.length === 0}
                  className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none disabled:opacity-50"
                >
                  <option value="">请选择列</option>
                  {columns.map((col) => (
                    <option key={col} value={col}>
                      {col}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => {
                    const next = [...entity.nodeKeyParts]
                    next[index] = { kind: 'allocated_code', scopeColumns: [], rawValueColumn: '' }
                    onChange({ ...entity, nodeKeyParts: next })
                  }}
                  className={`self-start text-xs font-bold text-ink underline ${focusRing}`}
                >
                  这一列没有现成唯一编码？
                </button>
              </>
            ) : (
              <>
                <label className="flex flex-col gap-1 text-xs font-bold text-ink">
                  作用域列（可多选）
                  <select
                    multiple
                    value={part.scopeColumns}
                    onChange={(e) => {
                      const selected = Array.from(e.target.selectedOptions).map((o) => o.value)
                      const next = [...entity.nodeKeyParts]
                      next[index] = { ...part, scopeColumns: selected }
                      onChange({ ...entity, nodeKeyParts: next })
                    }}
                    className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
                  >
                    {columns.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1 text-xs font-bold text-ink">
                  原始值列
                  <select
                    value={part.rawValueColumn}
                    onChange={(e) => {
                      const next = [...entity.nodeKeyParts]
                      next[index] = { ...part, rawValueColumn: e.target.value }
                      onChange({ ...entity, nodeKeyParts: next })
                    }}
                    className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
                  >
                    <option value="">请选择列</option>
                    {columns.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  onClick={() => {
                    const next = [...entity.nodeKeyParts]
                    next[index] = { kind: 'column', column: '' }
                    onChange({ ...entity, nodeKeyParts: next })
                  }}
                  className={`self-start text-xs font-bold text-ink underline ${focusRing}`}
                >
                  改回简单列引用
                </button>
              </>
            )}
            {entity.nodeKeyParts.length > 1 && (
              <button
                type="button"
                onClick={() => {
                  const next = entity.nodeKeyParts.filter((_, i) => i !== index)
                  onChange({ ...entity, nodeKeyParts: next })
                }}
                className={`self-start text-xs font-bold text-status-error underline ${focusRing}`}
              >
                删除这一列
              </button>
            )}
          </div>
        ))}
        <button
          type="button"
          onClick={() => {
            const columnPart: ColumnKeyPart = { kind: 'column', column: '' }
            onChange({ ...entity, nodeKeyParts: [...entity.nodeKeyParts, columnPart] })
          }}
          className={`self-start border-2 border-ink bg-card px-3 py-1.5 text-xs font-bold text-ink shadow-brutal-sm ${focusRing}`}
        >
          + 添加另一列（组成复合 key）
        </button>
      </div>

      {selectedTermType && selectedTermType.extra_fields.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-sm font-bold text-ink">属性字段映射</span>
          {selectedTermType.extra_fields.map((field) => (
            <label key={field.name} className="flex flex-col gap-1 text-xs font-bold text-ink">
              {field.name}（{field.value_type}）
              <select
                value={entity.fieldMappings[field.name] ?? ''}
                onChange={(e) => {
                  const nextMappings = { ...entity.fieldMappings }
                  if (e.target.value) {
                    nextMappings[field.name] = e.target.value
                  } else {
                    delete nextMappings[field.name]
                  }
                  onChange({ ...entity, fieldMappings: nextMappings })
                }}
                className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
              >
                <option value="">不映射</option>
                {columns.map((col) => (
                  <option key={col} value={col}>
                    {col}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
