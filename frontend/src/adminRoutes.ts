import type { LucideIcon } from 'lucide-react'
import {
  Boxes,
  FileText,
  GitPullRequestArrow,
  Network,
  ScanSearch,
  Table2,
  Waypoints,
} from 'lucide-react'

/**
 * 管理后台的路由表与导航结构，单一事实来源。
 *
 * 路径按**工作阶段**分段（ingest → model → review → browse），跟侧边栏
 * 分组一一对应。此前是按对象类型分（本体/文档/数据加工），叶子最深藏到
 * 第四层——「疑似重复」在「数据加工 › 文档抽取 › 疑似重复」，「本体图」在
 * 「本体管理 › 约束 › 图」，侧边栏上一个字都看不到。
 *
 * 集中在一处而不是散在 App.tsx、AdminLayout、⌘K 命令表、空状态链接里：
 * 那样每加一个页面都要记得改四个地方，漏一个就是"页面存在但没人找得到"。
 * 有了这份表，adminRoutes.test.ts 能断言"七个目的地全部出现在侧边栏"。
 */
export const ADMIN_ROUTES = {
  documents: '/admin/ingest/documents',
  etl: '/admin/ingest/etl',
  ontology: '/admin/model/ontology',
  ontologyGraph: '/admin/model/graph',
  reviewRelations: '/admin/review/relations',
  reviewDuplicates: '/admin/review/duplicates',
  terms: '/admin/browse/terms',
} as const

/**
 * 旧路径 → 新路径。两代都保留，且**每条一跳直达**。
 *
 * 第一代（`/admin/terms` 等）在 2026-08 那次重组时已经改过一次，指向的是
 * 第二代的 `/admin/data-entry/*`；这次如果只改第二代，旧书签会变成两跳
 * 链式跳转。链式的问题不只是多一跳——"这条垫片指向哪"要顺着链子读，
 * 而且中间那一代哪天删掉就会断。
 */
export const LEGACY_REDIRECTS: Record<string, string> = {
  // 第一代
  '/admin/terms': ADMIN_ROUTES.terms,
  '/admin/graph-reviews': ADMIN_ROUTES.reviewRelations,
  '/admin/schema-etl': ADMIN_ROUTES.etl,
  // 第二代
  '/admin/data-entry/manual': ADMIN_ROUTES.terms,
  '/admin/data-entry/review': ADMIN_ROUTES.reviewRelations,
  '/admin/data-entry/etl': ADMIN_ROUTES.etl,
  // 本体页这次单独改名
  '/admin/ontology': ADMIN_ROUTES.ontology,
}

export interface NavItem {
  path: string
  label: string
  icon: LucideIcon
}

export interface NavGroup {
  /** 同时是路径里的阶段段名——测试断言两者一致，防止分组和路径脱节。 */
  id: 'ingest' | 'model' | 'review' | 'browse'
  label: string
  items: NavItem[]
}

/** 顺序即工作顺序：先接入数据，再定义本体，再审核抽取结果，最后浏览。 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'ingest',
    label: '接入数据',
    items: [
      { path: ADMIN_ROUTES.documents, label: '文档上传', icon: FileText },
      { path: ADMIN_ROUTES.etl, label: '表格导入', icon: Table2 },
    ],
  },
  {
    id: 'model',
    label: '建模',
    items: [
      { path: ADMIN_ROUTES.ontology, label: '本体结构', icon: Network },
      { path: ADMIN_ROUTES.ontologyGraph, label: '本体图', icon: Waypoints },
    ],
  },
  {
    id: 'review',
    label: '审核',
    items: [
      { path: ADMIN_ROUTES.reviewRelations, label: '待审关系', icon: GitPullRequestArrow },
      { path: ADMIN_ROUTES.reviewDuplicates, label: '疑似重复', icon: ScanSearch },
    ],
  },
  {
    id: 'browse',
    label: '浏览',
    items: [{ path: ADMIN_ROUTES.terms, label: '实体列表', icon: Boxes }],
  },
]

/** 当前 URL 落在哪个分组里——侧边栏用它决定默认展开哪一组。 */
export function groupIdForPath(pathname: string): NavGroup['id'] | null {
  return NAV_GROUPS.find((group) => group.items.some((i) => pathname.startsWith(i.path)))?.id ?? null
}

/**
 * 页面标题，取自侧边栏的名字。
 *
 * 手写第二份就会重演上一次：导航改名时改了标签、忘了标题，用户点「待审
 * 关系」落到一个叫「文档抽取」的页面上，第一反应是自己点错了。
 *
 * 标题里不带租户。它已经在侧边栏顶部常驻，每个页面再说一遍是噪音——
 * 而且原先三个页面带、一个不带，四个页面三种写法。
 */
export const PAGE_TITLES: Record<keyof typeof ADMIN_ROUTES, string> = Object.fromEntries(
  Object.entries(ADMIN_ROUTES).map(([key, path]) => [
    key,
    NAV_GROUPS.flatMap((g) => g.items).find((i) => i.path === path)!.label,
  ]),
) as Record<keyof typeof ADMIN_ROUTES, string>
