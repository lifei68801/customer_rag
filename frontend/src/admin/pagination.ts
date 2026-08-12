export type PageToken = number | 'ellipsis'

/**
 * 生成分页器要渲染的页码序列：总页数 <= 7 时全部列出；超过 7 页时固定
 * 展示首页、尾页、当前页前后各 1 页，中间用 'ellipsis' 断开——常见的
 * "1 2 ... 5 6 7 ... 20" 分页器样式，避免页数很多时把所有页码平铺出来。
 */
export function getPageNumbers(current: number, total: number): PageToken[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1)
  }
  const pages: PageToken[] = [1]
  if (current > 3) {
    pages.push('ellipsis')
  }
  const start = Math.max(2, current - 1)
  const end = Math.min(total - 1, current + 1)
  for (let page = start; page <= end; page += 1) {
    pages.push(page)
  }
  if (current < total - 2) {
    pages.push('ellipsis')
  }
  pages.push(total)
  return pages
}
