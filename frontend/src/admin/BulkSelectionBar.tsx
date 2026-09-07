import type { BulkDeleteFilters, BulkDeleteResult } from './bulkDelete'
import { describeBulkDeleteFilters, summarizeBulkDeleteResult } from './bulkDelete'
import type { BulkSelectionApi } from './useBulkSelection'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface BulkSelectionBarProps {
  /** 这一段列表的身份。平铺列表用 'flat'，分组视图里用各自的实体类型。 */
  scopeId: string
  /** 这一段当前列出来的那些行的 key，按显示顺序。表头复选框勾的就是它们。 */
  listedKeys: string[]
  /** 当前筛选条件下的总条数——「全部」那一档要删的就是这么多。 */
  total: number
  /** 当前筛选条件。确认框要把它念出来，请求体里发的也是它。 */
  filters: BulkDeleteFilters
  /** 被删对象的量词说法，比如「实体」。 */
  noun: string
  selection: BulkSelectionApi
  onDelete: (scopeId: string) => void
  deleting: boolean
}

/**
 * 两级全选的工具条：表头复选框 + 升级到「全部」的提示条 + 删除按钮。
 *
 * 「已选中本页 50 条，[改为选中筛选条件下的全部 4712 条]」这条提示是这套
 * 交互的关键：本页全选和全量全选的破坏力差两个数量级，界面上必须一眼可辨，
 * 而不是同一个复选框在不同上下文里悄悄换了意思。
 *
 * 8 个删除点共用这一个组件，所以这里不认识实体、文档或账号——它只认识
 * 「列出来的这些 key」「筛选条件下共几条」「当前选中是哪一档」。
 */
export function BulkSelectionBar({
  scopeId,
  listedKeys,
  total,
  filters,
  noun,
  selection,
  onDelete,
  deleting,
}: BulkSelectionBarProps) {
  const { selection: state } = selection
  const active = state.kind !== 'none' && state.scopeId === scopeId
  const isAll = active && state.kind === 'all'
  const pageAllChecked =
    listedKeys.length > 0 && listedKeys.every((key) => selection.isSelected(scopeId, key))
  const selectedCount = selection.countIn(scopeId)
  // 本页就是全部时不给「改为选中全部」——同一件事给两个按钮，用户会以为
  // 它们不一样，然后花时间去想区别在哪。
  const canEscalate = !isAll && total > listedKeys.length

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={pageAllChecked}
            onChange={() => selection.togglePage(scopeId, listedKeys)}
            aria-label={`选中本页 ${listedKeys.length} 条${noun}`}
            className={`h-4 w-4 cursor-pointer ${focusRing}`}
          />
          <span>选中本页（{listedKeys.length}）</span>
        </label>
        {active && (
          <>
            <button
              type="button"
              onClick={() => onDelete(scopeId)}
              disabled={deleting}
              className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-3 text-sm font-bold text-white transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {deleting ? '删除中…' : `删除选中的 ${selectedCount.toLocaleString()} 条`}
            </button>
            <button
              type="button"
              onClick={selection.clear}
              disabled={deleting}
              className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              取消选中
            </button>
          </>
        )}
      </div>
      {active && (
        <p
          data-testid={`bulk-selection-notice-${scopeId}`}
          className={`flex flex-wrap items-center gap-2 rounded-card border px-3 py-2 text-sm text-ink ${
            // 两档状态视觉上必须分得开：全量那一档是危险色，本页那一档是普通提示。
            isAll ? 'border-status-error bg-card font-bold' : 'border-subtle bg-card'
          }`}
        >
          {isAll ? (
            <span>
              已选中筛选条件（{describeBulkDeleteFilters(filters)}）下的全部{' '}
              {total.toLocaleString()} 条{noun}，不只是本页这 {listedKeys.length} 条。
            </span>
          ) : (
            <span>
              已选中本页 {selectedCount.toLocaleString()} 条{noun}。
            </span>
          )}
          {canEscalate && (
            <button
              type="button"
              onClick={() => selection.selectAllMatching(scopeId, filters, total)}
              className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink underline transition hover:bg-interactive-hover ${focusRing}`}
            >
              改为选中筛选条件下的全部 {total.toLocaleString()} 条
            </button>
          )}
          {isAll && (
            <button
              type="button"
              onClick={() => selection.togglePage(scopeId, listedKeys)}
              className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink underline transition hover:bg-interactive-hover ${focusRing}`}
            >
              改回只选本页 {listedKeys.length} 条
            </button>
          )}
        </p>
      )}
    </div>
  )
}

/**
 * 批量删除的结果面板：删掉几条，以及**逐条**列出没删掉的那几条和原因。
 *
 * 只弹一句「删除完成」是这个项目的头号反模式——删 100 条其中 3 条被图谱边
 * 挡住时，用户必须能看见是哪 3 条、为什么，否则他会以为清干净了。所以失败
 * 明细留在页面上（不是一闪而过的 toast），直到他自己关掉。
 */
export function BulkDeleteOutcome({
  result,
  noun,
  onDismiss,
}: {
  result: BulkDeleteResult
  noun: string
  onDismiss: () => void
}) {
  const hasFailures = result.failures.length > 0
  return (
    <div
      role="status"
      data-testid="bulk-delete-outcome"
      className={`flex flex-col gap-2 rounded-card border px-3 py-2 text-sm text-ink ${
        hasFailures ? 'border-status-error bg-card' : 'border-subtle bg-card'
      }`}
    >
      <p className="font-bold">{summarizeBulkDeleteResult(result, noun)}</p>
      {hasFailures && (
        <ul className="flex flex-col gap-1">
          {result.failures.map((failure) => (
            <li key={failure.key} className="flex flex-wrap gap-2">
              <span className="font-mono font-bold">{failure.key}</span>
              <span className="text-ink-soft">{failure.reason}</span>
            </li>
          ))}
        </ul>
      )}
      <div>
        <button
          type="button"
          onClick={onDismiss}
          className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
        >
          知道了
        </button>
      </div>
    </div>
  )
}
