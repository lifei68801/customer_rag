import { useState, type FormEvent } from 'react'

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
      className="flex items-center gap-3 border-t border-surface-border bg-surface-raised px-4 py-4"
    >
      <input
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入你的问题…"
        disabled={disabled}
        className="flex-1 rounded-card border border-surface-border bg-surface-base px-4 py-2.5 text-content-primary placeholder:text-content-secondary focus:border-accent focus:outline-none disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="rounded-card bg-accent px-5 py-2.5 font-medium text-white transition hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-50"
      >
        发送
      </button>
    </form>
  )
}
