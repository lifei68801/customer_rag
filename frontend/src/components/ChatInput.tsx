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
      className="flex items-center gap-3 border-t-2 border-ink bg-card px-4 py-4"
    >
      <input
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入你的问题…"
        disabled={disabled}
        className="flex-1 border-2 border-ink bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="cursor-pointer border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
      >
        发送
      </button>
    </form>
  )
}
