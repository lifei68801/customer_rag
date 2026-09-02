import type { LucideIcon } from 'lucide-react'
import {
  Boxes,
  FileText,
  GitPullRequestArrow,
  Network,
  ScanSearch,
  Stethoscope,
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
  ontology: '/admin/model/ontology',
  ontologyGraph: '/admin/model/graph',
  // 首次建模的入口，从本体结构页进入；不常驻导航，见 NAV_GROUPS 上方
  // 的注释和 adminRoutes.test.ts 里的 NOT_IN_NAV。
  guidedOntology: '/admin/model/guided',
  documents: '/admin/ingest/documents',
  etl: '/admin/ingest/etl',
  reviewRelations: '/admin/review/relations',
  reviewDuplicates: '/admin/review/duplicates',
  // 两段式：实体列表不属于任何阶段，路径里留一个「browse」段就是个孤儿
  // ——侧边栏没有那个组，URL 里却有。形状本身说清楚它不在流程里。
  terms: '/admin/terms',
  // 问答诊断：从「这次答错了」反查到「哪个实体不对」。跟实体列表一样是
  // 流程外的——它不是某一步，是出问题时才来的地方。
  diagnostics: '/admin/diagnostics',
  // 账号设置。不在任何导航分组里——它不是流程的一站，是账号级的偏好。
  accounts: '/admin/accounts',
  tenants: '/admin/tenants',
  settings: '/admin/settings',
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
  // 第一代。'/admin/terms' 不在这里——它现在就是实体列表的正式路径，旧
  // 书签直接命中；留一条指向自己的垫片会变成无限重定向。
  '/admin/graph-reviews': ADMIN_ROUTES.reviewRelations,
  '/admin/schema-etl': ADMIN_ROUTES.etl,
  // 第二代
  '/admin/data-entry/manual': ADMIN_ROUTES.terms,
  '/admin/data-entry/review': ADMIN_ROUTES.reviewRelations,
  '/admin/data-entry/etl': ADMIN_ROUTES.etl,
  // 本体页这次单独改名
  '/admin/ontology': ADMIN_ROUTES.ontology,
  // 只活了一天的第三代：实体列表曾经归在「浏览」组下。
  '/admin/browse/terms': ADMIN_ROUTES.terms,
}

export interface NavItem {
  path: string
  label: string
  icon: LucideIcon
}

export interface NavGroup {
  /** 同时是路径里的阶段段名——测试断言两者一致，防止分组和路径脱节。 */
  id: 'model' | 'ingest' | 'review'
  label: string
  items: NavItem[]
}

/**
 * 流程三段，顺序即依赖顺序。
 *
 * 建模排在最前面不是偏好：ETL 会拒绝未确认本体的租户
 * （admin_schema_etl_routes.py:129），文档管线会跳过图谱抽取
 * （ingestion/pipeline.py:108）。把接入排在前面等于教用户走一条产品会
 * 拒绝的路——新用户第一站就撞墙。
 *
 * 有人会说接入是高频、建模是低频，高频该排前面。不该：这里表达的是依赖
 * 和心智模型，不是使用频率。频率问题由 ⌘K 和待办徽标解决，熟练用户根本
 * 不靠侧边栏找路。
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'model',
    label: '建模',
    items: [
      { path: ADMIN_ROUTES.ontology, label: '本体结构', icon: Network },
      { path: ADMIN_ROUTES.ontologyGraph, label: '本体图', icon: Waypoints },
    ],
  },
  {
    id: 'ingest',
    label: '接入数据',
    items: [
      { path: ADMIN_ROUTES.documents, label: '文档上传', icon: FileText },
      { path: ADMIN_ROUTES.etl, label: '表格导入', icon: Table2 },
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
]

/**
 * 不属于任何阶段的目的地。
 *
 * 建模、接入、审核是流程步骤，有先后；实体列表是结果视图，任何一步之后
 * 都可能用到——塞进流程末尾会让人以为它是「最后一步」，而它其实是每一步
 * 的落点。
 *
 * Palantir Foundry 也是这么分的：Ontology Manager 管定义（object types /
 * link types），Object Explorer 查实例，是两个独立应用而不是一个侧边栏
 * 里的两个分组。分界是「定义 vs 实例」，不是流程第几步。
 */
export const NAV_STANDALONE: NavItem[] = [
  { path: ADMIN_ROUTES.terms, label: '实体列表', icon: Boxes },
  { path: ADMIN_ROUTES.diagnostics, label: '问答诊断', icon: Stethoscope },
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
const ALL_NAV_ITEMS: NavItem[] = [
  ...NAV_GROUPS.flatMap((g) => g.items),
  ...NAV_STANDALONE,
]

/** 导航里没有的页面，标题写在这里。 */
const EXTRA_TITLES: Partial<Record<keyof typeof ADMIN_ROUTES, string>> = {
  // 这两个都不在侧边栏里，入口在左下角的账号菜单。账号页对 member 根本
  // 不存在，放进侧边栏会让两种角色看到不同的侧边栏，破坏"侧边栏是固定
  // 的"这个心智模型。
  accounts: '账号',
  tenants: '租户',
  settings: '设置',
  // 入口不在侧边栏，也就不在 ALL_NAV_ITEMS 里；标题只能在这里手写一份。
  guidedOntology: '引导建模',
}

export const PAGE_TITLES: Record<keyof typeof ADMIN_ROUTES, string> = Object.fromEntries(
  Object.entries(ADMIN_ROUTES).map(([key, path]) => [
    key,
    EXTRA_TITLES[key as keyof typeof ADMIN_ROUTES] ??
      ALL_NAV_ITEMS.find((i) => i.path === path)!.label,
  ]),
) as Record<keyof typeof ADMIN_ROUTES, string>
