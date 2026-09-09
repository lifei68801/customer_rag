import { describe, expect, it } from 'vitest'
import {
  ADMIN_ROUTES,
  LEGACY_REDIRECTS,
  NAV_GROUPS,
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
  it('六个模块共十六个目的地，加上账号页、租户页和设置页', () => {
    expect(ADMIN_ROUTES).toEqual({
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
      // 这三个都不在侧边栏里，入口在左下角的账号菜单。账号页对 member
      // 根本不存在——放进侧边栏会让两种角色看到不同的侧边栏。
      accounts: '/admin/accounts',
      tenants: '/admin/tenants',
      settings: '/admin/settings',
    })
  })

  it('每个目的地的路径第二段就是它所属模块的 id', () => {
    // 这不是审美：groupIdForPath 靠前缀匹配决定侧边栏默认展开哪一组，
    // 分组和路径脱节的话它会返回 null，展开就失灵了。
    //
    // 允许连字符：/admin/review/dirty-edges。收在 [a-z-]+ 而不是放开成
    // 任意字符，是为了挡住大写和下划线——URL 里混进两套命名法之后，
    // 「这一页的地址长什么样」就没法凭记忆敲了。
    const ids = NAV_GROUPS.map((g) => g.id).join('|')
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        expect(item.path).toMatch(new RegExp(`^/admin/(${ids})(/[a-z-]+)?$`))
      }
    }
  })
})

describe('旧路径垫片', () => {
  it('历史路径全部覆盖', () => {
    // 第一代（data-entry 之前）+ 第二代（data-entry/*）+ 第三代
    // （按工作阶段分的 model/ingest/review 与两个两段式的孤儿）。
    //
    // '/admin/browse/terms' 不在这里：它这次回来了，就是实体明细的正式
    // 路径，旧书签直接命中。留一条指向自己的垫片会变成无限重定向。
    expect(Object.keys(LEGACY_REDIRECTS).sort()).toEqual([
      '/admin/data-entry/etl',
      '/admin/data-entry/manual',
      '/admin/data-entry/review',
      '/admin/diagnostics',
      '/admin/graph-reviews',
      '/admin/ingest/documents',
      '/admin/ingest/etl',
      '/admin/model/graph',
      '/admin/model/guided',
      '/admin/model/ontology',
      '/admin/model/persona',
      '/admin/ontology',
      '/admin/schema-etl',
      '/admin/terms',
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
    expect(LEGACY_REDIRECTS['/admin/terms']).toBe(ADMIN_ROUTES.terms)
    expect(LEGACY_REDIRECTS['/admin/data-entry/manual']).toBe(ADMIN_ROUTES.terms)
    expect(LEGACY_REDIRECTS['/admin/graph-reviews']).toBe(ADMIN_ROUTES.reviewRelations)
    expect(LEGACY_REDIRECTS['/admin/data-entry/review']).toBe(ADMIN_ROUTES.reviewRelations)
    expect(LEGACY_REDIRECTS['/admin/schema-etl']).toBe(ADMIN_ROUTES.etl)
    expect(LEGACY_REDIRECTS['/admin/data-entry/etl']).toBe(ADMIN_ROUTES.etl)
    expect(LEGACY_REDIRECTS['/admin/ontology']).toBe(ADMIN_ROUTES.ontology)
    expect(LEGACY_REDIRECTS['/admin/model/ontology']).toBe(ADMIN_ROUTES.ontology)
    expect(LEGACY_REDIRECTS['/admin/model/graph']).toBe(ADMIN_ROUTES.ontologyGraph)
    expect(LEGACY_REDIRECTS['/admin/model/guided']).toBe(ADMIN_ROUTES.guidedOntology)
    // 数字人页是上一代末尾才加的（阶段二），写这份重排计划时它还不存在
    // ——漏掉的话，刚发出去的那个后台链接立刻变死链。
    expect(LEGACY_REDIRECTS['/admin/model/persona']).toBe(ADMIN_ROUTES.persona)
    expect(LEGACY_REDIRECTS['/admin/ingest/documents']).toBe(ADMIN_ROUTES.documents)
    expect(LEGACY_REDIRECTS['/admin/ingest/etl']).toBe(ADMIN_ROUTES.etl)
    expect(LEGACY_REDIRECTS['/admin/diagnostics']).toBe(ADMIN_ROUTES.diagnostics)
  })
})

describe('导航分组', () => {
  it('六个模块，顺序即用户的工作顺序', () => {
    // 看板在最前面：它是落地页。其余五个是依赖顺序——本体没确认，ETL 会
    // 拒绝（admin_schema_etl_routes.py），文档管线会跳过图谱抽取
    // （ingestion/pipeline.py）。把导入排在建模前面等于教用户走一条产品
    // 会拒绝的路。审核→预览→日志是数据进来之后的三步。
    expect(NAV_GROUPS.map((g) => g.id)).toEqual([
      'dashboard',
      'ontology',
      'import',
      'review',
      'browse',
      'logs',
    ])
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
    }
    const inNav = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path)).sort()
    const shouldBeInNav = Object.entries(ADMIN_ROUTES)
      .filter(([key]) => !(key in NOT_IN_NAV))
      .map(([, path]) => path)
      .sort()
    expect(inNav).toEqual(shouldBeInNav)
  })

  it('每个叶子的所属分组与它的路径段一致', () => {
    // "等于或前缀"两种都算：看板那一组只有一个叶子，路径就是
    // /admin/dashboard，没有第三段。判据钉住的仍是"分组 id 必须是路径的
    // 第二段"——那才是 groupIdForPath 真正依赖的东西。
    for (const group of NAV_GROUPS) {
      for (const item of group.items) {
        const base = `/admin/${group.id}`
        expect(item.path === base || item.path.startsWith(`${base}/`)).toBe(true)
      }
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
    expect(groupIdForPath(`${ADMIN_ROUTES.ontology}/term-types`)).toBe('ontology')
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

  it('三个账号级页面加看板不依赖租户', () => {
    // 把租户管理页一起挡住的话，admin 会被锁在一个什么都点不动的界面里：
    // 空态叫他去选一个租户，而唯一能新建/启用租户的页面也被空态盖着。
    expect([...NON_TENANT_ROUTE_KEYS].sort()).toEqual([
      'accounts',
      'dashboard',
      'settings',
      'tenants',
    ])
  })

  it('看板在没有当前租户时也照常打开，不被空态挡住', () => {
    // 看板是登录后的落地页，而新登录的 admin（admin_users.tenant_id 恒为
    // None）那一刻还没有当前租户。归成租户内的话，他第一眼看到的是
    // 「请先选择一个租户」而不是他的看板——而看板恰恰是**跨领域**的，
    // 它一屏列出所有领域，本来就不属于其中任何一个。
    expect(routeRequiresTenant(ADMIN_ROUTES.dashboard)).toBe(false)
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
    expect(routeRequiresTenant('/admin/乱敲')).toBe(true)
    expect(routeRequiresTenant('/admin/model/ontology')).toBe(true)
  })

  it('/admin 自己是跳转站，不需要租户', () => {
    // 它不渲染任何内容，只把人送去看板。此前它落在上面那条默认规则里
    // 被判成"需要租户"——而 AdminLayout 的闸门在 <Outlet/> 之外判定，
    // 于是那条 <Navigate> 根本没机会渲染：admin 的 current_tenant_id
    // 恒为 None，每个新 admin 账号第一屏都是空态，永远到不了看板。
    //
    // 这不是放宽默认规则：上面那条仍然成立，/admin 是从"未归类"变成
    // "显式归类为跳转站"。
    expect(routeRequiresTenant('/admin')).toBe(false)
    expect(routeRequiresTenant('/admin/')).toBe(false)
  })
})
