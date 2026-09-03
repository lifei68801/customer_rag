import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { NAV_GROUPS, NAV_STANDALONE } from '../adminRoutes'

/**
 * 空状态里的引导链接，文字得跟它去的地方对得上。
 *
 * 上一步把硬编码路径换成了 ADMIN_ROUTES，链接不再断——但文案没跟着改：
 * DocumentsPage 写着"去『数据加工』上传"，而「数据加工」这个页面已经不
 * 存在了；TermsPage 指着「文档抽取」，那个名字现在叫「待审关系」。
 *
 * 链接指对了地方而文字指着另一个地方，比断链更难发现：点了会到一个陌生
 * 的页面，用户以为自己点错了。
 */

const ROOT = join(__dirname, '..')
// NAV_STANDALONE（「实体列表」「问答诊断」）也是合法的导航目的地——分组
// 之外不代表不是目的地，只是不属于任何流程阶段（见 adminRoutes.ts 里
// NAV_STANDALONE 上方的注释）。此前这里只取了 NAV_GROUPS：全仓库 grep 过，
// 此前没有任何单行纯文本 <Link> 指向过这两个目的地，所以这个缺口一直没被
// 触发过；forwardLinks 的「实体列表」出口第一次用到，才会被误判成文案对
// 不上，因此在这里补上。
const KNOWN_LABELS = new Set([
  ...NAV_GROUPS.flatMap((g) => g.items.map((i) => i.label)),
  ...NAV_STANDALONE.map((i) => i.label),
])
// 侧边栏之外的合法去处：登录页和前台不在那七个目的地里。
const ALSO_FINE = new Set(['返回前台', '登出', '管理后台'])
// 「返回X」是导航方向，不是目的地名——它天然跟着来路走，不会因为页面
// 改名而失效。
const BACK_LINK = /^返回/

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sourceFiles(path)
    if (!/\.tsx$/.test(name) || /\.test\.tsx$/.test(name)) return []
    return [path]
  })
}

describe('引导链接的文案', () => {
  it('<Link> 的文字要么是某个导航目的地的名字，要么是明确的例外', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(ROOT)) {
      const source = readFileSync(file, 'utf8')
      // 只看单行、纯文本的 <Link>……</Link>——多行的那些（比如按钮样式的
      // 大块内容）不是"指向某个页面的名字"这种用法。
      for (const [index, line] of source.split('\n').entries()) {
        const text = line.match(/^\s*([^<>{}\s][^<>{}]*?)\s*$/)?.[1]
        if (!text) continue
        // 多行 <Link> 的属性行（className=… 之类）不是链接文字。不排掉的话
        // 这条规则会把样式字符串报成「文案不对」——一条只会误报的规则，
        // 用不了几次就会被人加 skip 绕过去。
        if (text.includes('=')) continue
        const prev = source.split('\n')[index - 1] ?? ''
        if (!/<Link\s|to=\{ADMIN_ROUTES/.test(prev)) continue
        if (KNOWN_LABELS.has(text) || ALSO_FINE.has(text) || BACK_LINK.test(text)) continue
        offenders.push(`${file.slice(ROOT.length + 1)}:${index + 1} 「${text}」`)
      }
    }
    expect(
      offenders,
      `链接文字不是任何一个导航目的地的名字：\n${offenders.join('\n')}`,
    ).toEqual([])
  })
})
