import { Hero } from './components/Hero'
import { ChatWindow } from './components/ChatWindow'
import { ChatInput } from './components/ChatInput'
import { Footer } from './components/Footer'
import { useAgentChat } from './hooks/useAgentChat'

function App() {
  const { messages, isSending, sendQuestion, resetConversation } = useAgentChat()

  return (
    <div className="flex min-h-screen flex-col bg-paper">
      <div className="border-b-2 border-ink bg-ink px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-accent-yellow">
        检索增强生成 + 知识图谱驱动的客服问答演示
      </div>
      <nav className="flex items-center justify-between border-b-2 border-ink bg-accent-yellow px-6 py-4">
        <span className="font-bold text-ink">客服智能问答 Demo</span>
        <button
          type="button"
          onClick={resetConversation}
          disabled={messages.length === 0}
          className="border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
        >
          重新开始对话
        </button>
      </nav>
      <Hero />
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col">
        <ChatWindow messages={messages} />
        <ChatInput disabled={isSending} onSend={sendQuestion} />
      </main>
      <Footer />
    </div>
  )
}

export default App
