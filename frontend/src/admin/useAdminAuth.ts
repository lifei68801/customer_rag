import { useCallback, useState } from 'react'
import { logoutSession } from './adminApi'

const SESSION_STORAGE_KEY = 'admin_session_token'
const USERNAME_KEY = 'admin_username'
const ROLE_KEY = 'admin_role'
//: 与 TenantContext 共用同一个键。member 登录后它就是那个绑定的租户，
//: admin 则可以在账号菜单里改。
const TENANT_KEY = 'admin_current_tenant'

export type AdminRole = 'admin' | 'member'

export function useAdminAuth() {
  const [sessionToken, setSessionToken] = useState<string | null>(() =>
    sessionStorage.getItem(SESSION_STORAGE_KEY),
  )
  const [username, setUsername] = useState<string | null>(() =>
    sessionStorage.getItem(USERNAME_KEY),
  )
  const [role, setRole] = useState<AdminRole | null>(
    () => sessionStorage.getItem(ROLE_KEY) as AdminRole | null,
  )

  const login = useCallback(async (name: string, password: string) => {
    const response = await fetch('/api/admin/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: name, password }),
    })
    if (!response.ok) {
      // 后端刻意不区分"用户不存在/密码错/账号禁用"（那会把登录接口变成
      // 用户名枚举器），前端也不该编一个更具体的说法。
      throw new Error('用户名或密码不正确')
    }
    const data = (await response.json()) as {
      session_token: string
      username: string
      role: AdminRole
      tenant_id: string | null
    }
    sessionStorage.setItem(SESSION_STORAGE_KEY, data.session_token)
    sessionStorage.setItem(USERNAME_KEY, data.username)
    sessionStorage.setItem(ROLE_KEY, data.role)
    if (data.tenant_id) {
      // member 绑定的租户，覆盖上次留下的值。admin 的 tenant_id 是 null，
      // 保留它上次选的那个。
      sessionStorage.setItem(TENANT_KEY, data.tenant_id)
    }
    setSessionToken(data.session_token)
    setUsername(data.username)
    setRole(data.role)
  }, [])

  const logout = useCallback(() => {
    const token = sessionStorage.getItem(SESSION_STORAGE_KEY)
    for (const key of [SESSION_STORAGE_KEY, USERNAME_KEY, ROLE_KEY, TENANT_KEY]) {
      sessionStorage.removeItem(key)
    }
    setSessionToken(null)
    setUsername(null)
    setRole(null)
    // 本地状态先清、立即生效；服务端撤销是尽力而为，不阻塞登出这个动作。
    if (token) {
      void logoutSession(token)
    }
  }, [])

  return { sessionToken, username, role, login, logout }
}
