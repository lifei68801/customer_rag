const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface GuidedQuestionsProps {
  tagline: string
  questions: string[]
  onAsk: (question: string) => void
}

/**
 * 空会话时的引导：这个数字人是谁、可以问它什么。
 *
 * 一条问题都没有时只渲染人设那一行，不渲染问题区——一个只有边框没有内容
 * 的区块，读起来像「加载失败」。人设和问题都没有时整块返回 null。
 *
 * 这里的每一条都保证点了能答出来：手写那批保存时跑过实体匹配（见后端
 * question_validation.py），自动那批只用图里真有边的类型组合（见后端
 * guided_questions.py）。前端不做二次判断——判断的依据（本体、图）都在
 * 后端手里，前端复制一份只会得到一份会过期的判断。
 */
export function GuidedQuestions({ tagline, questions, onAsk }: GuidedQuestionsProps) {
  if (!tagline && questions.length === 0) return null
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-3 p-6">
      {tagline && <p className="text-sm text-ink-soft">{tagline}</p>}
      {questions.length > 0 && (
        <div data-testid="guided-questions" className="flex flex-wrap gap-2">
          {questions.map((question) => (
            <button
              key={question}
              type="button"
              onClick={() => onAsk(question)}
              className={`cursor-pointer rounded-chip border border-subtle bg-card px-3 py-1.5 text-sm text-ink transition hover:bg-interactive-hover active:scale-95 ${focusRing}`}
            >
              {question}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
