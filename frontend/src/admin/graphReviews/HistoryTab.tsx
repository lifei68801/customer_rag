import { History } from 'lucide-react'
import { EmptyState } from '../EmptyState'
import { Pager } from '../Pager'
import { Skeleton } from '../Skeleton'
import { TaskStatusBadge } from '../TaskStatusBadge'
import { useAdminDensity } from '../DensityContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/** 已经处理过的一条审核记录。 */
export interface ResolvedReview {
  review_id: number
  subject_candidate: string
  object_candidate: string
  relation_type: string
  source: string
  evidence: string
  status: string
  resolved_at: string
  resolved_note: string | null
}

export type HistoryFilter = 'all' | 'approved' | 'rejected'

const FILTERS: { key: HistoryFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'approved', label: '已批准' },
  { key: 'rejected', label: '已驳回' },
]

interface HistoryTabProps {
  reviews: ResolvedReview[]
  loaded: boolean
  filter: HistoryFilter
  onFilterChange: (filter: HistoryFilter) => void
  page: number
  totalPages: number
  onPageChange: (page: number) => void
}

/**
 * 「已处理」这一档：筛选、列表、空态、翻页。
 *
 * 抽成组件的直接收益是消掉 `tab === 'history' &&` 这个条件——它此前在页面
 * 里重复了**五次**，每一段 JSX 各写一遍。读的人要把散在五处的片段在脑子里
 * 拼成"这一档长什么样"。
 *
 * 取数仍然在页面里：分页和筛选变化都要触发重新请求，而那两个状态还要参与
 * 页面级的「切走时回到第一页」等逻辑。这里只负责画。
 */
export function HistoryTab({
  reviews,
  loaded,
  filter,
  onFilterChange,
  page,
  totalPages,
  onPageChange,
}: HistoryTabProps) {
  const { density } = useAdminDensity()

  return (
    <>
      <div className="flex gap-2">
        {FILTERS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            onClick={() => onFilterChange(key)}
            aria-pressed={filter === key}
            className={`min-h-[44px] cursor-pointer rounded-control border border-subtle px-3 py-1.5 text-sm font-bold transition ${focusRing} ${
              filter === key ? 'bg-accent-primary text-on-accent' : 'bg-paper text-ink'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {!loaded && <Skeleton variant="card-list" count={3} />}

      {loaded &&
        reviews.map((review) => (
          <div
            key={review.review_id}
            className={`flex flex-col gap-1 rounded-card border border-subtle bg-card ${
              density === 'compact' ? 'p-2.5' : 'p-4'
            }`}
          >
            <p className="text-sm text-ink">
              {review.subject_candidate} —[{review.relation_type}]→ {review.object_candidate}
            </p>
            <p className="text-xs text-ink-soft">来源文档：{review.source || '（无记录）'}</p>
            {review.evidence && (
              <p className="border-l border-subtle pl-2 text-sm italic text-ink">
                原文引用："{review.evidence}"
              </p>
            )}
            <p className="flex flex-wrap items-center gap-2 text-xs text-ink-soft">
              <TaskStatusBadge
                tone={review.status === 'approved' ? 'success' : 'error'}
                label={review.status === 'approved' ? '已批准' : '已驳回'}
              />
              <span>
                {review.resolved_at}
                {review.resolved_note && ` · ${review.resolved_note}`}
              </span>
            </p>
          </div>
        ))}

      {loaded && reviews.length === 0 && (
        <EmptyState
          icon={History}
          title="还没有处理过的记录"
          action="在「待审核」标签里批准或驳回候选后，处理结果会出现在这里。"
        />
      )}

      {loaded && reviews.length > 0 && (
        <Pager page={page} totalPages={totalPages} onPageChange={onPageChange} />
      )}
    </>
  )
}
