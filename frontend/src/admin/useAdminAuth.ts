import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { readCsrfToken } from '../lib/csrf'

export type AdminRole = 'admin' | 'member'

/**
 * 'loading' 不是可以省掉的中间态：会话 token 在 HttpOnly Cookie 里，JS 读
 * 不到，所以「有没有登录」这件事在 whoami 回来之前是未知的。把未知当成
 * 未登录会把已登录的人踢回登录页，当成已登录则会渲染一整屏取不到数据的
 * 后台。
 */
export type AdminSessionStatus = 'loading' | 'authenticated' | 'anonymous'

interface AdminSession {
  status: AdminSessionStatus
  username: string | null
  role: AdminRole | null
  currentTenantId: string | null
}

const LOADING_SESSION: AdminSession = {
  status: 'loading',
  username: null,
  role: null,
  currentTenantId: null,
}

const ANONYMOUS_SESSION: AdminSession = {
  status: 'anonymous',
  username: null,
  role: null,
  currentTenantId: null,
}

/**
 * 会话状态放在模块级、而不是每个 useAdminAuth() 各存一份。
 *
 * useAdminAuth 在同一棵树里被 AdminLayout、AccountMenu、各个页面分别调用；
 * 各自 useState 的话它们会各拉一次 whoami、各存一份身份，然后在切租户或
 * 401 之后各说各的。此前它们之所以一致，是因为都从 sessionStorage 这个
 * 共享的地方读——Cookie 化之后那个共享点没有了，就得在这里补一个。
 */
let session: AdminSession = LOADING_SESSION
const listeners = new Set<() => void>()

function publish(next: AdminSession): void {
  session = next
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function getSnapshot(): AdminSession {
  return session
}

let inflight: Promise<void> | null = null

/**
 * 拉一次 whoami，把结果落进模块级状态。
 *
 * 同一时刻只发一个请求：一个页面里有好几个组件在用这个 hook，挂载顺序
 * 相近，不合并的话首屏会打出好几个一模一样的 whoami。
 */
export function loadAdminSession(force = false): Promise<void> {
  if (inflight && !force) return inflight
  inflight = (async () => {
    try {
      const response = await fetch('/api/admin/auth/whoami', { credentials: 'include' })
      if (!response.ok) {
        // 会话是进程内的，后端一重启就全员失效：Cookie 还留在浏览器里，
        // 服务端却已经不认。这时必须落到 anonymous，界面才会把人送回
        // 登录页，而不是停在一个「显示已登录但什么都点不动」的后台。
        publish(ANONYMOUS_SESSION)
        return
      }
      const data = (await response.json()) as {
        username: string
        role: AdminRole
        tenant_id: string | null
        current_tenant_id: string | null
      }
      publish({
        status: 'authenticated',
        username: data.username,
        role: data.role,
        currentTenantId: data.current_tenant_id,
      })
    } catch {
      // 网络失败同样当未登录：停在 loading 会渲染一屏永远空白的后台。
      publish(ANONYMOUS_SESSION)
    } finally {
      inflight = null
    }
  })()
  return inflight
}

/** 服务端已经不认这个会话（whoami 之外的接口回 401 时由 adminFetch 调用）。 */
export function markSessionExpired(): void {
  if (session.status !== 'anonymous') {
    publish(ANONYMOUS_SESSION)
  }
}

/** 服务端已经把当前租户切过去了，本地状态跟上。 */
export function setCurrentTenantId(next: string): void {
  publish({ ...session, currentTenantId: next })
}

/**
 * 把会话状态清回「未知」。
 *
 * @internal 只给测试用：模块级状态在同一个测试文件里跨用例存活，不重置的话
 * 上一条用例登录出来的身份会漏进下一条。生产代码调它会把当前用户打回
 * loading（界面整个空白，直到下一次 whoami 回来）——登出请用 logout()。
 */
export function resetAdminSession(): void {
  inflight = null
  publish(LOADING_SESSION)
}

async function revokeSession(): Promise<void> {
  try {
    const headers = new Headers({ 'Content-Type': 'application/json' })
    const csrfToken = readCsrfToken()
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken)
    }
    await fetch('/api/admin/auth/logout', { method: 'POST', credentials: 'include', headers })
  } catch {
    // 登出流程无论如何都要走完：本地状态已经清了，服务端撤销失败也不该
    // 把用户拦在后台里。
  }
}

export function useAdminAuth() {
  const state = useSyncExternalStore(subscribe, getSnapshot)

  useEffect(() => {
    if (state.status === 'loading') {
      void loadAdminSession()
    }
  }, [state.status])

  const login = useCallback(async (name: string, password: string) => {
    const response = await fetch('/api/admin/auth/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: name, password }),
    })
    if (!response.ok) {
      // 后端刻意不区分"用户不存在/密码错/账号禁用"（那会把登录接口变成
      // 用户名枚举器），前端也不该编一个更具体的说法。
      throw new Error('用户名或密码不正确')
    }
    // 登录响应里也带着 username/role，但会话状态只认 whoami 一个来源：
    // 两条路径各自解析同一份身份，早晚会读出两个不一样的答案。
    await loadAdminSession(true)
  }, [])

  const logout = useCallback(() => {
    // 本地状态先清、立即生效；服务端撤销是尽力而为，不阻塞登出这个动作。
    publish(ANONYMOUS_SESSION)
    void revokeSession()
  }, [])

  return {
    status: state.status,
    username: state.username,
    role: state.role,
    currentTenantId: state.currentTenantId,
    /**
     * 不是真的 token——Cookie 是 HttpOnly，JS 读不到值。调用方拿它当两件
     * 事用：「会话已就绪，可以发请求了」的开关，以及 adminFetch 那个已经
     * 不再被使用的占位参数。占位参数删掉时这个字段一起消失。
     */
    sessionToken: state.status === 'authenticated' ? 'cookie' : null,
    login,
    logout,
  }
}
