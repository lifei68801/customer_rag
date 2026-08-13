import { useState } from 'react'
import type { GraphTerm } from './termsApi'

interface StandardNameInputProps {
  value: string
  onChange: (value: string) => void
  terms: GraphTerm[]
  placeholder: string
  ariaLabel: string
}

const MAX_SUGGESTIONS = 8

export function StandardNameInput({
  value,
  onChange,
  terms,
  placeholder,
  ariaLabel,
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
        className="w-full border-2 border-ink bg-paper px-3 py-2 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
      />
      {isOpen && suggestions.length > 0 && (
        <ul className="absolute z-10 mt-1 w-full border-2 border-ink bg-paper shadow-brutal-sm">
          {suggestions.map((term) => {
            const matchedAlias = term.standard_name.includes(query)
              ? null
              : term.aliases.find((alias) => alias.includes(query))
            return (
              <li key={term.standard_name}>
                <button
                  type="button"
                  // 鼠标在这里按下时先阻止默认行为，输入框就不会因此失焦——
                  // 不然 input 的 onBlur 会抢在这个按钮的 onClick 之前触发，
                  // 下拉列表在点击生效前就被卸载掉，选不中任何建议
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    onChange(term.standard_name)
                    setIsOpen(false)
                  }}
                  className="block w-full cursor-pointer px-3 py-2 text-left text-sm text-ink hover:bg-card"
                >
                  {term.standard_name}
                  {matchedAlias && (
                    <span className="text-ink-soft">（别名：{matchedAlias}）</span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
