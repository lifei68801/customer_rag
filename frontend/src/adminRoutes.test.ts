import { describe, expect, it } from 'vitest'
import { ADMIN_ROUTES, LEGACY_REDIRECTS, NAV_GROUPS, groupIdForPath } from './adminRoutes'

/**
 * 路由表和导航结构的契约测试。
 *
 * 这些是**可断言的行为**而不是视觉——路径长什么样、旧书签跳到哪、侧边栏
 * 有哪些叶子。视觉仍然靠人眼看，但这一层错了人眼未必看得出来（一条垫片
 * 指错地方，只有恰好用那个旧书签的人会撞上）。
 */

describe('新路由表', () => {
  it('七个目的地，路径按工作阶段分段', () => {
    expect(ADMIN_ROUTES).toEqual({
      documents: '/admin/ingest/documents',
      etl: '/admin/ingest/etl',
      ontology: '/admin/model/ontology',
      ontologyGraph: '/admin/model/graph',
      reviewRelations: '/admin/review/relations',
      reviewDuplicates: '/admin/review/duplicates',
      terms: '/admin/browse/terms',
    })
  })

  it('每条路径都在 /admin/<阶段>/<叶子> 这个形状上', () => {
    for (const path of Object.values(ADMIN_ROUTES)) {
      expect(path).toMatch(/^\/admin\/(ingest|model|review|browse)\/[a-z]+$/)
    }
  })
})

describe('旧路径垫片', () => {
  it('两代旧路径全部覆盖', () => {
    // 第一代（2026-08 之前）+ 第二代（data-entry/*）+ 单独改名的 ontology
    expect(Object.keys(LEGACY_REDIRECTS).sort()).toEqual([
      '/admin/data-entry/etl',
      '/admin/data-entry/manual',
      '/admin/data-entry/review',
      '/admin/graph-reviews',
      '/admin/ontology',
      '/admin/schema-etl',
      '/admin/terms',
    ])
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
  })
})

describe('导航分组', () => {
  it('四个阶段，顺序即工作顺序', () => {
    expect(NAV_GROUPS.map((g) => g.id)).toEqual(['ingest', 'model', 'review', 'browse'])
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
    const inNav = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.path)).sort()
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
