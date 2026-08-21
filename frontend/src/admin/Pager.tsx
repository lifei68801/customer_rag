import { getPageNumbers } from './pagination'
import { Tooltip } from './Tooltip'

interface PagerProps {
  page: number
  totalPages: number
  onPageChange: (page: number) => void
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const pageButtonClass = (active: boolean) =>
  `min-h-[36px] min-w-[36px] cursor-pointer rounded-control border border-subtle px-2 text-sm font-bold transition ${focusRing} ${
    active ? 'bg-accent-pink text-on-accent shadow-soft-sm' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

export function Pager({ page, totalPages, onPageChange }: PagerProps) {
  if (totalPages <= 1) return null

  return (
    <nav aria-label="分页" className="flex items-center gap-1.5">
      <Tooltip label="上一页">
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          aria-label="上一页"
          className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
        >
          ‹
        </button>
      </Tooltip>
      {getPageNumbers(page, totalPages).map((token, index) =>
        token === 'ellipsis' ? (
          <span key={`ellipsis-${index}`} className="px-1 text-ink-soft">
            …
          </span>
        ) : (
          <button
            key={token}
            type="button"
            onClick={() => onPageChange(token)}
            aria-current={token === page ? 'page' : undefined}
            className={pageButtonClass(token === page)}
          >
            {token}
          </button>
        ),
      )}
      <Tooltip label="下一页">
        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
          aria-label="下一页"
          className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
        >
          ›
        </button>
      </Tooltip>
    </nav>
  )
}
