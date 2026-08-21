import { useState, type FormEvent } from 'react'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface ChatInputProps {
  disabled: boolean
  onSend: (question: string) => void
}

export function ChatInput({ disabled, onSend }: ChatInputProps) {
  const [value, setValue] = useState('')

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setValue('')
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex items-center gap-3 border-t border-subtle bg-card px-4 py-4"
    >
      <input
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入你的问题…"
        disabled={disabled}
        className={`flex-1 rounded-control border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:shadow-soft focus:outline-none disabled:opacity-50 ${focusRing}`}
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-soft transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
      >
        发送
      </button>
    </form>
  )
}
