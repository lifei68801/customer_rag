import { readCsrfToken } from '../lib/csrf'

/**
 * `_sessionToken` 保留只是因为 21 个调用方还在传它：会话已经改成 HttpOnly
 * Cookie，JS 读不到、也不需要再手工塞 Authorization 头。Task 6 一并清理。
 */
export async function adminFetch(
  path: string,
  _sessionToken: string,
  options: RequestInit = {},
): Promise<Response> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers = new Headers(options.headers)
  if (method !== 'GET' && method !== 'HEAD') {
    const csrfToken = readCsrfToken()
    // 拿不到令牌时不加这个头——未登录本来就没有令牌，硬塞一个空值只会把
    // 401 变成更难懂的 403。
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken)
    }
  }
  const response = await fetch(path, {
    ...options,
    credentials: 'include',
    headers,
  })
  if (response.status === 401) {
    sessionStorage.removeItem('admin_session_token')
    window.location.href = '/admin/login'
    throw new Error('登录已过期')
  }
  return response
}

/**
 * FastAPI 的 `detail` 字段在普通错误里是字符串，但在 422 参数校验错误里是
 * 一个对象数组（每个带 `msg`），直接拿去拼 `${detail}` 会渲染成
 * "[object Object]"——这里统一抽取成人可读文案。
 */
export function extractErrorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string' && detail) {
      return detail
    }
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          item && typeof item === 'object' && 'msg' in item
            ? String((item as { msg?: unknown }).msg)
            : null,
        )
        .filter((msg): msg is string => Boolean(msg))
      if (messages.length > 0) {
        return messages.join('；')
      }
    }
  }
  return fallback
}

/**
 * 让服务端立即失效这个 session（而不是只清本地 sessionStorage）。网络失败
 * 时静默忽略——登出流程无论如何都要走完，服务端 session 反正也会在 8h
 * TTL 后自然过期，不值得为此打断用户登出、也不需要额外的 UI 报错。
 */
export async function logoutSession(sessionToken: string): Promise<void> {
  try {
    await fetch('/api/admin/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${sessionToken}` },
    })
  } catch {
    // 见上方注释：忽略网络错误
  }
}
