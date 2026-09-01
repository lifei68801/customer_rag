import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 禁止在组件里硬编码 /admin/* 路径。
 *
 * 这条规则不是洁癖。查出这条规则时，⌘K 的「实体列表」指向
 * `/admin/data-entry/terms`——一个**从来没有存在过**的路径（真实路径是
 * `/admin/data-entry/manual`）。因为当时没有 404 兜底，点它只会渲染一片
 * 空白，看起来像页面在加载，没人报过 bug。
 *
 * 硬编码的路径不会在改路由时报错，只会在用户点到时静默失效。走
 * ADMIN_ROUTES 的话，改名会立刻变成编译错误。
 */

const ROOT = join(__dirname)
const ALLOWED = new Set(['adminRoutes.ts'])

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sourceFiles(path)
    if (!/\.tsx?$/.test(name) || /\.test\.tsx?$/.test(name)) return []
    if (ALLOWED.has(name)) return []
    return [path]
  })
}

describe('路径硬编码', () => {
  it('组件里不出现写死的 /admin/<段>/... 字面量', () => {
    // 允许 '/admin' 和 '/admin/login'：前者是布局的根，后者不属于按工作
    // 阶段分组的那七个目的地，没有进 ADMIN_ROUTES 的理由。
    const offenders: string[] = []
    for (const file of sourceFiles(ROOT)) {
      const source = readFileSync(file, 'utf8')
      for (const [index, line] of source.split('\n').entries()) {
        const match = line.match(/['"`]\/admin\/(?!login['"`])[a-z-]+/)
        if (match) offenders.push(`${file.slice(ROOT.length + 1)}:${index + 1} ${line.trim()}`)
      }
    }
    expect(offenders, `改用 ADMIN_ROUTES：\n${offenders.join('\n')}`).toEqual([])
  })
})
