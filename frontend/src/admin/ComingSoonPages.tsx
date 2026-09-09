import { Database, Scale, Share2, TriangleAlert, Unlink } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import { EmptyState } from './EmptyState'

/**
 * 六模块导航里已经挂上、但功能还没交付的那几页。
 *
 * 为什么先建占位而不是等功能做完再挂进导航：这次重排的目的就是把藏起来的
 * 东西摆出来，而"摆出来"和"能用"是两件事，用户需要看得见路线图。侧边栏里
 * 挂一个点进去空白的入口比不挂更糟——那读起来像页面坏了。
 *
 * 所以每一页都必须给出**一条现在就能走的替代路径**。只说「未上线」的话，
 * 用户点进来一无所获，跟空白页的区别只是多了一行字。
 */
function ComingSoon({
  icon,
  title,
  phase,
  plan,
  alternative,
}: {
  icon: LucideIcon
  title: string
  phase: string
  plan: string
  alternative: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{title}</h1>
      </div>
      <EmptyState
        icon={icon}
        title={`${title}还没上线`}
        action={
          <div className="flex flex-col gap-2">
            <p>
              它在实施计划的{phase}（<code className="font-mono text-xs">{plan}</code>）。
            </p>
            <p>{alternative}</p>
          </div>
        }
      />
    </div>
  )
}

const linkClass =
  'font-bold text-ink underline underline-offset-2 hover:text-accent-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function DatabaseImportPage() {
  return (
    <ComingSoon
      icon={Database}
      title={PAGE_TITLES.dbImport}
      phase="阶段六"
      plan="docs/superpowers/plans/2026-09-08-database-import.md"
      alternative={
        <>
          在那之前，数据库里的数据可以先导出成 CSV，走
          <Link to={ADMIN_ROUTES.etl} className={linkClass}>
            表格导入
          </Link>
          。
        </>
      }
    />
  )
}

export function ConflictReviewPage() {
  return (
    <ComingSoon
      icon={Scale}
      title={PAGE_TITLES.reviewConflicts}
      phase="阶段四"
      plan="docs/superpowers/plans/2026-09-08-review-split-and-conflicts.md"
      alternative={
        <>
          在那之前，同名实体的属性冲突会体现为
          <Link to={ADMIN_ROUTES.reviewDuplicates} className={linkClass}>
            疑似重复
          </Link>
          里的候选对，合并时可以逐字段挑。
        </>
      }
    />
  )
}

export function DirtyEdgesPage() {
  return (
    <ComingSoon
      icon={Unlink}
      title={PAGE_TITLES.reviewDirtyEdges}
      phase="阶段四"
      plan="docs/superpowers/plans/2026-09-08-review-split-and-conflicts.md"
      alternative={
        <>
          在那之前，指向不存在实体的边可以在
          <Link to={ADMIN_ROUTES.ontologyGraph} className={linkClass}>
            本体图
          </Link>
          里看出来——孤立的类型节点就是没有数据落进去的那些。
        </>
      }
    />
  )
}

export function DataGraphPage() {
  return (
    <ComingSoon
      icon={Share2}
      title={PAGE_TITLES.dataGraph}
      phase="阶段五"
      plan="docs/superpowers/plans/2026-09-08-graph-preview-and-error-log.md"
      alternative={
        <>
          在那之前，单个实体的邻域可以在
          <Link to={ADMIN_ROUTES.terms} className={linkClass}>
            实体明细
          </Link>
          里点开那一条查看，关系列表是全的，只是没有画成图。
        </>
      }
    />
  )
}

export function ErrorLogPage() {
  return (
    <ComingSoon
      icon={TriangleAlert}
      title={PAGE_TITLES.errors}
      phase="阶段五"
      plan="docs/superpowers/plans/2026-09-08-graph-preview-and-error-log.md"
      alternative={
        <>
          在那之前，答错和答不出来的问题可以在
          <Link to={ADMIN_ROUTES.diagnostics} className={linkClass}>
            问答明细
          </Link>
          里逐条回看，那里能追到是哪个实体没匹配上。
        </>
      }
    />
  )
}
