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
        className="w-full border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-soft focus:outline-none"
      />
      {isOpen && (suggestions.length > 0 || showCreateNew) && (
        <ul className="absolute z-10 mt-1 w-full border-2 border-ink bg-paper shadow-soft-sm">
          {suggestions.map((term) => {
            const matchedAlias = term.standard_name.includes(query)
              ? null
              : term.aliases.find((alias) => alias.includes(query))
            return (
              <li key={term.standard_name}>
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
                className="block w-full cursor-pointer border-t-2 border-ink px-3 py-2 text-left text-sm font-bold text-ink hover:bg-interactive-hover"
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
