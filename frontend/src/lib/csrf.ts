const CSRF_COOKIE_NAME = 'customer_rag_csrf'

/**
 * 读 CSRF 令牌。
 *
 * 这个 Cookie 刻意不是 HttpOnly——双提交令牌就建立在「会话 Cookie 读不到、
 * CSRF Cookie 读得到」这个不对称上：攻击者站点能让浏览器带上会话 Cookie，
 * 但读不到这个值、也就填不出请求头。
 */
export function readCsrfToken(): string | null {
  const hit = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${CSRF_COOKIE_NAME}=`))
  return hit ? decodeURIComponent(hit.slice(CSRF_COOKIE_NAME.length + 1)) : null
}
