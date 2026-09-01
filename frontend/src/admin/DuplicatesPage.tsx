import { DuplicateTermSuggestionsTab } from './DuplicateTermSuggestionsTab'

/**
 * 疑似重复的独立页面。
 *
 * 此前它是「数据加工 › 文档抽取 › 疑似重复」的第四层 tab——侧边栏上看不
 * 到，得先知道它在那儿才找得到。它跟「待审关系」是并列的审核任务，不是
 * 后者的子项，所以给它自己的地址。
 *
 * 页面本体一行都没搬：DuplicateTermSuggestionsTab 本来就自己拿 auth 和
 * 租户、自己加载数据，是个完整的东西，只是过去被挂错了地方。
 */
export function DuplicatesPage() {
  return (
    <div data-testid="duplicates-page" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">疑似重复</h1>
        <p className="text-sm text-ink-soft">
          系统认为可能指向同一个实体的条目。合并前请确认它们真的是一回事——合并不可撤销。
        </p>
      </div>
      <DuplicateTermSuggestionsTab />
    </div>
  )
}
