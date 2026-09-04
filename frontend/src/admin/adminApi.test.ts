import { describe, expect, it, vi, beforeEach } from 'vitest'
import { adminFetch } from './adminApi'

describe('adminFetch', () => {
  beforeEach(() => {
    document.cookie = 'customer_rag_csrf=tok123; path=/'
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

    await adminFetch('/api/admin/x', '', { method: 'POST' })
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).get('X-CSRF-Token')).toBe('tok123')

    fetchMock.mockClear()
    await adminFetch('/api/admin/x', '')
    expect(new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers).get('X-CSRF-Token')).toBeNull()
  })
})
