import { Database, TriangleAlert } from 'lucide-react'
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

export function ErrorLogPage() {
  return (
    <ComingSoon
      icon={TriangleAlert}
      title={PAGE_TITLES.errors}
      phase="阶段五"
      plan="docs/superpowers/plans/2026-09-08-graph-preview-and-error-log.md"
      alternative={
        <>
          在那之前，
          <Link to={ADMIN_ROUTES.diagnostics} className={linkClass}>
            问答明细
          </Link>
          里能逐条回看问答记录，包括改写后的问题和用到的检索结果。
          <strong className="font-bold">但它筛不出「哪几次答坏了」</strong>
          ——那张表只记问题和答案，没有任何一列表示成败，答不出来和答得好
          长得一模一样。要找出错的只能自己一条条读。
        </>
      }
    />
  )
}
