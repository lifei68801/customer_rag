import { describe, expect, it } from 'vitest'
import {
  ADMIN_ROUTES,
  LEGACY_REDIRECTS,
  NAV_GROUPS,
  NAV_STANDALONE,
  groupIdForPath,
  routeRequiresTenant,
  NON_TENANT_ROUTE_KEYS,
  TENANT_SCOPED_ROUTE_KEYS,
} from './adminRoutes'

/**
 * 路由表和导航结构的契约测试。
 *
 * 这些是**可断言的行为**而不是视觉——路径长什么样、旧书签跳到哪、侧边栏
 * 有哪些叶子。视觉仍然靠人眼看，但这一层错了人眼未必看得出来（一条垫片
 * 指错地方，只有恰好用那个旧书签的人会撞上）。
 */

describe('新路由表', () => {
  it('七个工作流目的地，加上流程外的诊断页、账号页和设置页', () => {
    expect(ADMIN_ROUTES).toEqual({
      ontology: '/admin/model/ontology',
      ontologyGraph: '/admin/model/graph',
      guidedOntology: '/admin/model/guided',
      documents: '/admin/ingest/documents',
      etl: '/admin/ingest/etl',
      reviewRelations: '/admin/review/relations',
      reviewDuplicates: '/admin/review/duplicates',
      terms: '/admin/terms',
      diagnostics: '/admin/diagnostics',
      // 账号页和设置页都不在侧边栏里，入口在左下角的账号菜单。账号页对
      // member 根本不存在——放进侧边栏会让两种角色看到不同的侧边栏。
      accounts: '/admin/accounts',
      tenants: '/admin/tenants',
      settings: '/admin/settings',
    })
  })

  it('流程内的路径带阶段段，流程外的不带', () => {
    // 实体列表是两段式的 /admin/terms：它不属于任何阶段，路径里留一个
    // 「browse」段就是个孤儿——侧边栏没有那个组，URL 里却有。路径形状
    // 本身要说清楚「这个页面不在流程里」。
    const inFlow = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path))
    for (const path of inFlow) {
      expect(path).toMatch(/^\/admin\/(model|ingest|review)\/[a-z]+$/)
    }
    for (const item of NAV_STANDALONE) {
      expect(item.path).toMatch(/^\/admin\/[a-z]+$/)
    }
  })
})

describe('旧路径垫片', () => {
  it('历史路径全部覆盖', () => {
    // 第一代（data-entry 之前）+ 第二代（data-entry/*）+ 单独改名的
    // ontology + 短命的 /admin/browse/terms。
    //
    // '/admin/terms' 不在这里：它现在就是实体列表的正式路径，旧书签直接
    // 命中，不需要垫片——垫片指向自己会变成无限重定向。
    expect(Object.keys(LEGACY_REDIRECTS).sort()).toEqual([
      '/admin/browse/terms',
      '/admin/data-entry/etl',
      '/admin/data-entry/manual',
      '/admin/data-entry/review',
      '/admin/graph-reviews',
      '/admin/ontology',
      '/admin/schema-etl',
    ])
  })

  it('没有指向自己的垫片', () => {
    for (const [from, to] of Object.entries(LEGACY_REDIRECTS)) {
      expect(from, '垫片指向自己会无限重定向').not.toBe(to)
    }
  })

  it('全部一跳直达，不链式跳转', () => {
    // 链式（旧 → 更旧 → 新）会让浏览器多跳一次，也会让"这条垫片指向哪"
    // 变得要顺着链子读。每条都必须直接落在新路径上。
    const destinations = new Set<string>(Object.values(ADMIN_ROUTES))
    for (const [from, to] of Object.entries(LEGACY_REDIRECTS)) {
      expect(destinations.has(to), `${from} 指向了非终点 ${to}`).toBe(true)
    }
  })

  it('垫片的终点跟它历史上的语义一致', () => {
    expect(LEGACY_REDIRECTS['/admin/browse/terms']).toBe(ADMIN_ROUTES.terms)
    expect(LEGACY_REDIRECTS['/admin/data-entry/manual']).toBe(ADMIN_ROUTES.terms)
    expect(LEGACY_REDIRECTS['/admin/graph-reviews']).toBe(ADMIN_ROUTES.reviewRelations)
    expect(LEGACY_REDIRECTS['/admin/data-entry/review']).toBe(ADMIN_ROUTES.reviewRelations)
    expect(LEGACY_REDIRECTS['/admin/schema-etl']).toBe(ADMIN_ROUTES.etl)
    expect(LEGACY_REDIRECTS['/admin/data-entry/etl']).toBe(ADMIN_ROUTES.etl)
    expect(LEGACY_REDIRECTS['/admin/ontology']).toBe(ADMIN_ROUTES.ontology)
  })
})

describe('导航分组', () => {
  it('三个阶段，顺序即依赖顺序', () => {
    // 建模在最前面不是偏好：ETL 会拒绝未确认本体的租户
    // （admin_schema_etl_routes.py），文档管线会跳过图谱抽取
    // （ingestion/pipeline.py）。把接入排在前面等于教用户走一条产品会
    // 拒绝的路——新用户第一站就撞墙。
    expect(NAV_GROUPS.map((g) => g.id)).toEqual(['model', 'ingest', 'review'])
  })

  it('每个叶子都指向路由表里的真实路径', () => {
    const known = new Set<string>(Object.values(ADMIN_ROUTES))
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        expect(known.has(item.path), `${item.label} 指向未知路径 ${item.path}`).toBe(true)
      }
    }
  })

  it('每个工作流目的地都在侧边栏，一个都不藏', () => {
    // 这条是这次重构的目的：此前「疑似重复」和「本体图」在第四层，侧边栏
    // 上一个字都看不到。任何新增页面如果忘了挂进导航，这里会失败。
    //
    // 例外必须逐条写明理由。列成具名常量而不是内联的 filter，是为了让
    // "再加一个例外"这件事有阻力——它本该是罕见的。
    const NOT_IN_NAV: Record<string, string> = {
      settings: '账号级偏好，不是流程的一站；入口在底部账号菜单',
      accounts: '对 member 根本不存在；放进侧边栏会让两种角色看到不同的侧边栏',
      tenants: '同上，admin 专属；入口在账号菜单',
      guidedOntology: '首次建模的入口，从本体结构页进入；不是常驻目的地',
    }
    const inNav = [
      ...NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path)),
      ...NAV_STANDALONE.map((i) => i.path),
    ].sort()
    const shouldBeInNav = Object.entries(ADMIN_ROUTES)
      .filter(([key]) => !(key in NOT_IN_NAV))
      .map(([, path]) => path)
      .sort()
    expect(inNav).toEqual(shouldBeInNav)
  })

  it('每个叶子的所属分组与它的路径段一致', () => {
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        expect(item.path.startsWith(`/admin/${group.id}/`)).toBe(true)
      }
    }
  })
})

describe('流程外的独立项', () => {
  it('实体列表和问答诊断', () => {
    // 建模、接入、审核是流程步骤，有先后；这两个不是——实体列表是结果
    // 视图，任何一步之后都可能用到；问答诊断是出问题时才来的地方。塞进
    // 流程末尾会让人以为它们是「最后一步」。
    //
    // Foundry 也是这么分的：Ontology Manager 管定义，Object Explorer 查
    // 实例，是两个独立应用。
    expect(NAV_STANDALONE.map((i) => i.path)).toEqual([
      ADMIN_ROUTES.terms,
      ADMIN_ROUTES.diagnostics,
    ])
  })

  it('不属于任何分组', () => {
    // 它高亮的是自己，不该让某个组跟着亮起来。
    for (const item of NAV_STANDALONE) {
      expect(groupIdForPath(item.path)).toBeNull()
    }
  })
})

describe('当前分组判定（侧边栏自动展开用）', () => {
  it('每个叶子路径都能判回它自己的组', () => {
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        expect(groupIdForPath(item.path)).toBe(group.id)
      }
    }
  })

  it('带子路径也能判对', () => {
    // 页面内部可能还有子路由（比如将来给本体结构加 /term-types 之类），
    // 前缀匹配保证这些也落在正确的组里。
    expect(groupIdForPath(`${ADMIN_ROUTES.ontology}/term-types`)).toBe('model')
  })

  it('未知路径返回 null 而不是猜一个组', () => {
    // 404 页不该让某个组高亮——那会让人以为自己在那个组里。
    expect(groupIdForPath('/admin/乱敲')).toBeNull()
    expect(groupIdForPath('/')).toBeNull()
  })

  it('旧路径不属于任何组', () => {
    // 旧路径只会短暂存在于重定向途中。如果它们能判出组，说明有人把垫片
    // 当成了真实目的地。
    for (const legacy of Object.keys(LEGACY_REDIRECTS)) {
      expect(groupIdForPath(legacy), `${legacy} 不该属于任何组`).toBeNull()
    }
  })
})

describe('租户依赖分类', () => {
  it('每个路由都被显式归类，一个都不漏', () => {
    // 照搬后端 tests/api/test_admin_route_shapes.py 的做法：没有"忘了归类"
    // 这一档。漏掉的那条会走进错误的分支——要么把一个不依赖租户的页面
    // 挡在空态后面（admin 被锁死在什么都点不动的界面里），要么让一个依赖
    // 租户的页面在没有当前租户时拿兜底值去取数（就是这次要修的静默失败）。
    const classified = [...TENANT_SCOPED_ROUTE_KEYS, ...NON_TENANT_ROUTE_KEYS].sort()
    expect(classified).toEqual(Object.keys(ADMIN_ROUTES).sort())
  })

  it('没有路由两边都沾', () => {
    const both = TENANT_SCOPED_ROUTE_KEYS.filter((k) =>
      (NON_TENANT_ROUTE_KEYS as readonly string[]).includes(k),
    )
    expect(both).toEqual([])
  })

  it('三个账号级页面不依赖租户——尤其是租户管理页', () => {
    // 把租户管理页一起挡住的话，admin 会被锁在一个什么都点不动的界面里：
    // 空态叫他去选一个租户，而唯一能新建/启用租户的页面也被空态盖着。
    expect([...NON_TENANT_ROUTE_KEYS].sort()).toEqual(['accounts', 'settings', 'tenants'])
  })

  it('归为不依赖租户的路径，判定为不需要租户', () => {
    for (const key of NON_TENANT_ROUTE_KEYS) {
      expect(routeRequiresTenant(ADMIN_ROUTES[key]), `${key} 不该需要租户`).toBe(false)
    }
  })

  it('归为依赖租户的路径（含子路径）判定为需要租户', () => {
    for (const key of TENANT_SCOPED_ROUTE_KEYS) {
      expect(routeRequiresTenant(ADMIN_ROUTES[key]), `${key} 应该需要租户`).toBe(true)
    }
    // 实体详情页在列表下一层，它同样按租户取数。
    expect(routeRequiresTenant(`${ADMIN_ROUTES.terms}/foo`)).toBe(true)
  })

  it('没归类过的路径默认按"需要租户"处理', () => {
    // 默认值选的是安全的那边：新加一个页面忘了归类时，它会被空态挡住
    // （用户看得见、能纠正），而不是拿兜底租户去读写别人的数据。
    expect(routeRequiresTenant('/admin')).toBe(true)
    expect(routeRequiresTenant('/admin/乱敲')).toBe(true)
  })
})
