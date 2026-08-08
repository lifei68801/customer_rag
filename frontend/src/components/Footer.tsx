export function Footer() {
  return (
    <footer className="border-t-2 border-ink bg-ink px-6 py-8 text-accent-yellow/60">
      <div className="mx-auto flex max-w-3xl flex-col items-center gap-2 text-center">
        <span className="font-bold text-accent-yellow">客服智能问答 Demo</span>
        <p className="font-mono text-xs uppercase tracking-widest">
          检索增强生成 · 术语知识图谱 · 多轮对话记忆
        </p>
        <p className="font-mono text-xs uppercase tracking-widest">
          内部技术演示，非生产环境
        </p>
      </div>
    </footer>
  )
}
