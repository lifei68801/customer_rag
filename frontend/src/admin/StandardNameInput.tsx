import { useState } from 'react'
import type { GraphTerm } from './termsApi'

interface StandardNameInputProps {
  value: string
  onChange: (value: string) => void
  terms: GraphTerm[]
  placeholder: string
  ariaLabel: string
  onCreateNew?: (query: string) => void
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const MAX_SUGGESTIONS = 8

export function StandardNameInput({
  value,
  onChange,
  terms,
  placeholder,
  ariaLabel,
  onCreateNew,
}: StandardNameInputProps) {
  const [isOpen, setIsOpen] = useState(false)

  const query = value.trim()
  const suggestions = query
    ? terms
        .filter(
          (term) =>
            term.standard_name.includes(query) ||
            term.aliases.some((alias) => alias.includes(query)),
        )
        .slice(0, MAX_SUGGESTIONS)
    : []
  // 跨类型重名（同一个 standard_name 同时属于两个不同 term_type）在这份
  // 列表里会产生两条文字完全相同的建议——只在当前实际展示的建议里确实
  // 出现重复时才需要额外的类型后缀区分它们，不重复的建议维持原样，不
  // 平白增加视觉噪音。
  const duplicateStandardNames = new Set(
    suggestions
      .map((term) => term.standard_name)
      .filter((name, index, all) => all.indexOf(name) !== index),
  )
  const showCreateNew = Boolean(onCreateNew) && query.length > 0 && suggestions.length === 0

  return (
    <div className="relative flex-1">
      <input
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
          setIsOpen(true)
        }}
        onFocus={() => setIsOpen(true)}
        onBlur={() => setIsOpen(false)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        className={`w-full rounded-control border border-subtle bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-soft focus:outline-none ${focusRing}`}
      />
      {isOpen && (suggestions.length > 0 || showCreateNew) && (
        <ul className="absolute z-10 mt-1 w-full overflow-hidden rounded-modal border border-subtle bg-paper shadow-soft-sm">
          {suggestions.map((term) => {
            const matchedAlias = term.standard_name.includes(query)
              ? null
              : term.aliases.find((alias) => alias.includes(query))
            const isDuplicateName = duplicateStandardNames.has(term.standard_name)
            return (
              <li key={`${term.term_type}::${term.standard_name}`}>
                <button
                  type="button"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    onChange(term.standard_name)
                    setIsOpen(false)
                  }}
                  className="block w-full cursor-pointer px-3 py-2 text-left text-sm text-ink hover:bg-interactive-hover"
                >
                  {term.standard_name}
                  {isDuplicateName && (
                    <span className="text-ink-soft">（类型：{term.term_type}）</span>
                  )}
                  {matchedAlias && (
                    <span className="text-ink-soft">（别名：{matchedAlias}）</span>
                  )}
                </button>
              </li>
            )
          })}
          {showCreateNew && (
            <li>
              <button
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onCreateNew?.(query)
                  setIsOpen(false)
                }}
                className="block w-full cursor-pointer border-t border-subtle px-3 py-2 text-left text-sm font-bold text-ink hover:bg-interactive-hover"
              >
                + 创建为新实体"{query}"
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
