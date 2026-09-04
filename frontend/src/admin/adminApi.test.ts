import { describe, expect, it, vi, beforeEach } from 'vitest'
import { adminFetch } from './adminApi'

const CSRF_COOKIE = 'customer_rag_csrf'

function setCsrfCookie(value: string): void {
  document.cookie = `${CSRF_COOKIE}=${value}; path=/`
}

describe('adminFetch', () => {
  beforeEach(() => {
    // jsdom 的 cookie 在同一个测试文件里是共享的，所以每条用例先清干净、
    // 再由需要令牌的那几条自己设——否则「没有 CSRF Cookie」那条会被上一条
    // 用例留下的值污染，测不到它想测的分支。
    document.cookie = `${CSRF_COOKIE}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`
  })

  it('带上 Cookie，不再手工塞 Authorization 头', async () => {
    // 会话改成 HttpOnly Cookie 之后 JS 读不到 token，只能靠浏览器自动
    // 携带——credentials 不设成 include 的话同源请求也不会带 Cookie。

    // 显式标出参数类型，否则 mock.calls 的元组类型是 []，`calls[0][1]` 在
    // tsc 下是「长度 0 的元组没有下标 1」的类型错误。
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response('{}')),
    )
    vi.stubGlobal('fetch', fetchMock)
    setCsrfCookie('tok123')
    await adminFetch('/api/admin/whoami', '')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.credentials).toBe('include')
    expect(new Headers(init.headers).get('Authorization')).toBeNull()
  })

  it('写请求带上 X-CSRF-Token，读请求不带', async () => {
    // 显式标出参数类型，否则 mock.calls 的元组类型是 []，`calls[0][1]` 在
    // tsc 下是「长度 0 的元组没有下标 1」的类型错误。
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response('{}')),
    )
    vi.stubGlobal('fetch', fetchMock)
    setCsrfCookie('tok123')

    await adminFetch('/api/admin/x', '', { method: 'POST' })
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).get('X-CSRF-Token')).toBe('tok123')

    fetchMock.mockClear()
    await adminFetch('/api/admin/x', '')
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).get('X-CSRF-Token')).toBeNull()
  })

  it('没有 CSRF Cookie 时写请求根本不带这个头', async () => {
    // 未登录本来就没有令牌，硬塞一个空值只会把服务端的 401 变成更难懂的
    // 403。断言的是这个头「不存在」而不是「不等于 tok123」——空字符串既不
    // 等于 tok123 也不等于 null，用错断言的话塞空值的实现照样能溜过去。
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response('{}')),
    )
    vi.stubGlobal('fetch', fetchMock)

    await adminFetch('/api/admin/x', '', { method: 'POST' })
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers)
    expect(headers.has('X-CSRF-Token')).toBe(false)
  })
})
