import { describe, expect, it } from 'vitest'
import {
  ADMIN_ROUTES,
  LEGACY_REDIRECTS,
  NAV_GROUPS,
  NAV_STANDALONE,
  groupIdForPath,
} from './adminRoutes'

/**
 * 路由表和导航结构的契约测试。
 *
 * 这些是**可断言的行为**而不是视觉——路径长什么样、旧书签跳到哪、侧边栏
 * 有哪些叶子。视觉仍然靠人眼看，但这一层错了人眼未必看得出来（一条垫片
 * 指错地方，只有恰好用那个旧书签的人会撞上）。
 */

describe('新路由表', () => {
  it('七个目的地，流程内的按阶段分段', () => {
    expect(ADMIN_ROUTES).toEqual({
      ontology: '/admin/model/ontology',
      ontologyGraph: '/admin/model/graph',
      documents: '/admin/ingest/documents',
      etl: '/admin/ingest/etl',
      reviewRelations: '/admin/review/relations',
      reviewDuplicates: '/admin/review/duplicates',
      terms: '/admin/terms',
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

  it('七个目的地全部出现在侧边栏，一个都不藏', () => {
    // 这条是这次重构的目的：此前「疑似重复」和「本体图」在第四层，侧边栏
    // 上一个字都看不到。任何新增页面如果忘了挂进导航，这里会失败。
    const inNav = [
      ...NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path)),
      ...NAV_STANDALONE.map((i) => i.path),
    ].sort()
    expect(inNav).toEqual(Object.values(ADMIN_ROUTES).sort())
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
  it('只有实体列表', () => {
    // 建模、接入、审核是流程步骤，有先后；实体列表是结果视图，任何一步
    // 之后都可能用到。塞进流程末尾会让人以为它是「最后一步」。
    // Foundry 也是这么分的：Ontology Manager 管定义，Object Explorer 查
    // 实例，是两个独立应用。
    expect(NAV_STANDALONE.map((i) => i.path)).toEqual([ADMIN_ROUTES.terms])
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
