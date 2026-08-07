import { Hero } from './components/Hero'
import { ChatWindow } from './components/ChatWindow'
import { ChatInput } from './components/ChatInput'
import { useAgentChat } from './hooks/useAgentChat'

function App() {
  const { messages, isSending, sendQuestion } = useAgentChat()

  return (
    <div className="flex min-h-screen flex-col bg-surface-base">
      <nav className="flex items-center justify-between border-b border-surface-border px-6 py-4">
        <span className="font-semibold text-content-primary">客服智能问答 Demo</span>
      </nav>
      <Hero />
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col">
        <ChatWindow messages={messages} />
        <ChatInput disabled={isSending} onSend={sendQuestion} />
      </main>
    </div>
  )
}

export default App
