import type { LucideIcon } from 'lucide-react'
import {
  Boxes,
  Database,
  FileText,
  GitPullRequestArrow,
  LayoutDashboard,
  Network,
  Scale,
  ScanSearch,
  Share2,
  Stethoscope,
  Table2,
  TriangleAlert,
  Unlink,
  UserRound,
  Wand2,
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
  // 登录后的落地页。唯一的**组织级**页面：它跨领域，不需要"当前租户"
  // （见下面的 NON_TENANT_ROUTE_KEYS）。
  dashboard: '/admin/dashboard',

  ontology: '/admin/ontology/ontology',
  ontologyGraph: '/admin/ontology/graph',
  guidedOntology: '/admin/ontology/guided',
  persona: '/admin/ontology/persona',

  documents: '/admin/import/documents',
  etl: '/admin/import/table',
  dbImport: '/admin/import/database',

  reviewRelations: '/admin/review/relations',
  reviewConflicts: '/admin/review/conflicts',
  reviewDuplicates: '/admin/review/duplicates',
  reviewDirtyEdges: '/admin/review/dirty-edges',

  terms: '/admin/browse/terms',
  dataGraph: '/admin/browse/graph',

  diagnostics: '/admin/logs/qa',
  errors: '/admin/logs/errors',

  // 这三个都不在侧边栏里，入口在左下角的账号菜单。
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
  // 第一代
  '/admin/graph-reviews': ADMIN_ROUTES.reviewRelations,
  '/admin/schema-etl': ADMIN_ROUTES.etl,
  // 第二代
  '/admin/data-entry/manual': ADMIN_ROUTES.terms,
  '/admin/data-entry/review': ADMIN_ROUTES.reviewRelations,
  '/admin/data-entry/etl': ADMIN_ROUTES.etl,
  '/admin/ontology': ADMIN_ROUTES.ontology,
  // 第三代（按工作阶段分的 model/ingest/review + 两个两段式的孤儿）。
  // 这一代活了一段时间，书签是真的存在的。
  '/admin/model/ontology': ADMIN_ROUTES.ontology,
  '/admin/model/graph': ADMIN_ROUTES.ontologyGraph,
  '/admin/model/guided': ADMIN_ROUTES.guidedOntology,
  // 数字人页是第三代末尾才加的，写这份重排计划时它还不存在——漏掉的话，
  // 刚发出去的那个后台链接立刻变死链。
  '/admin/model/persona': ADMIN_ROUTES.persona,
  '/admin/ingest/documents': ADMIN_ROUTES.documents,
  '/admin/ingest/etl': ADMIN_ROUTES.etl,
  '/admin/terms': ADMIN_ROUTES.terms,
  '/admin/diagnostics': ADMIN_ROUTES.diagnostics,
  // '/admin/browse/terms' 不在这里：它这次回来了，就是实体明细的正式路径。
  // 留一条指向自己的垫片会变成无限重定向。
}

export interface NavItem {
  path: string
  label: string
  icon: LucideIcon
}

export interface NavGroup {
  /** 同时是路径里的阶段段名——测试断言两者一致，防止分组和路径脱节。 */
  id: 'dashboard' | 'ontology' | 'import' | 'review' | 'browse' | 'logs'
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
    id: 'dashboard',
    label: '看板',
    items: [{ path: ADMIN_ROUTES.dashboard, label: '看板', icon: LayoutDashboard }],
  },
  {
    id: 'ontology',
    label: '本体创建',
    items: [
      { path: ADMIN_ROUTES.ontology, label: '本体结构', icon: Network },
      { path: ADMIN_ROUTES.ontologyGraph, label: '本体图', icon: Waypoints },
      { path: ADMIN_ROUTES.guidedOntology, label: '引导建模', icon: Wand2 },
      { path: ADMIN_ROUTES.persona, label: '数字人', icon: UserRound },
    ],
  },
  {
    id: 'import',
    label: '数据导入',
    items: [
      { path: ADMIN_ROUTES.documents, label: '文档导入', icon: FileText },
      { path: ADMIN_ROUTES.etl, label: '表格导入', icon: Table2 },
      { path: ADMIN_ROUTES.dbImport, label: '数据库导入', icon: Database },
    ],
  },
  {
    id: 'review',
    label: '数据审核',
    items: [
      { path: ADMIN_ROUTES.reviewRelations, label: '关系审核', icon: GitPullRequestArrow },
      { path: ADMIN_ROUTES.reviewConflicts, label: '属性冲突', icon: Scale },
      { path: ADMIN_ROUTES.reviewDuplicates, label: '疑似重复', icon: ScanSearch },
      { path: ADMIN_ROUTES.reviewDirtyEdges, label: '脏边与孤儿', icon: Unlink },
    ],
  },
  {
    id: 'browse',
    label: '结果预览',
    items: [
      { path: ADMIN_ROUTES.terms, label: '实体明细', icon: Boxes },
      { path: ADMIN_ROUTES.dataGraph, label: '图谱预览', icon: Share2 },
    ],
  },
  {
    id: 'logs',
    label: '日志明细',
    items: [
      { path: ADMIN_ROUTES.diagnostics, label: '问答明细', icon: Stethoscope },
      { path: ADMIN_ROUTES.errors, label: '报错明细', icon: TriangleAlert },
    ],
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
const ALL_NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items)

/** 导航里没有的页面，标题写在这里。 */
const EXTRA_TITLES: Partial<Record<keyof typeof ADMIN_ROUTES, string>> = {
  // 这三个都不在侧边栏里，入口在左下角的账号菜单。账号页对 member 根本
  // 不存在，放进侧边栏会让两种角色看到不同的侧边栏，破坏"侧边栏是固定
  // 的"这个心智模型。
  accounts: '账号',
  tenants: '租户',
  settings: '设置',
}

export const PAGE_TITLES: Record<keyof typeof ADMIN_ROUTES, string> = Object.fromEntries(
  Object.entries(ADMIN_ROUTES).map(([key, path]) => {
    const explicit = EXTRA_TITLES[key as keyof typeof ADMIN_ROUTES]
    if (explicit) return [key, explicit]
    const inNav = ALL_NAV_ITEMS.find((i) => i.path === path)
    if (!inNav) {
      // 加载期就炸，且说清楚该干什么。此前这里是一个 `!`，同样会炸，
      // 但抛的是「Cannot read properties of undefined」——那句话不指向
      // 任何可执行的动作，读到的人得自己反推是标题表出了问题。
      throw new Error(
        `路由 ${key}（${path}）既不在侧边栏里，也没有在 EXTRA_TITLES 里写标题。` +
          `要么把它挂进 NAV_GROUPS，要么在 EXTRA_TITLES 里给它一个标题——` +
          `两者都没有的话，这一页会顶着一个空标题出现。`,
      )
    }
    return [key, inNav.label]
  }),
) as Record<keyof typeof ADMIN_ROUTES, string>

/**
 * 不依赖当前租户的路由。
 *
 * 前三个是账号级的：账号管理、租户管理、偏好设置。它们在「还没选定租户」
 * 时必须照常可用——尤其是租户管理页，把它一起挡住的话 admin 会被锁死：
 * 空态叫他去选一个租户，而唯一能新建或启用租户的页面盖着同一张空态。
 *
 * 看板是第四个，理由不同：它是**组织级**的，一屏列出这个账号能访问的所有
 * 领域，本来就不属于其中任何一个。而它又是登录后的落地页——归成租户内的话，
 * 新登录、还没切过租户的 admin（tenant_id 恒为 None）第一眼看到的是「请先
 * 选择一个租户」，而不是他的看板。
 */
export const NON_TENANT_ROUTE_KEYS = ['accounts', 'tenants', 'settings', 'dashboard'] as const

/**
 * 依赖当前租户的路由：读写的都是某一个租户里的数据。
 *
 * 显式列出来而不是"减去上面那份"，是为了让 adminRoutes.test.ts 能断言
 * 「每个路由都被归类了」——照搬后端 tests/api/test_admin_route_shapes.py
 * 的做法。新加一个页面忘了归类，测试会红，而不是默默走进错误的分支。
 */
export const TENANT_SCOPED_ROUTE_KEYS = [
  'ontology',
  'ontologyGraph',
  'guidedOntology',
  'persona',
  'documents',
  'etl',
  'dbImport',
  'reviewRelations',
  'reviewConflicts',
  'reviewDuplicates',
  'reviewDirtyEdges',
  'terms',
  'dataGraph',
  'diagnostics',
  'errors',
] as const

const NON_TENANT_PATHS: string[] = NON_TENANT_ROUTE_KEYS.map((key) => ADMIN_ROUTES[key])

/**
 * 这个后台路径需不需要一个当前租户？AdminLayout 用它决定渲染页面还是
 * 「请先选择一个租户」的空态。
 *
 * 默认值是 true，选的是安全的那边：没归类过的路径（新页面、404 兜底、
 * 旧书签垫片、/admin 落地分流）会被空态挡住——用户看得见、也够得着纠正
 * ——而不是拿 TenantContext 的兜底租户去读写别人的数据。反过来默认放行的
 * 话，漏归类的那一页会静默地在错误的租户里工作。
 */
export function routeRequiresTenant(pathname: string): boolean {
  return !NON_TENANT_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`))
}
