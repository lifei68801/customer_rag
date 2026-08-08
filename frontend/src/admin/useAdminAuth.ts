import { useCallback, useState } from 'react'

const SESSION_STORAGE_KEY = 'admin_session_token'

export function useAdminAuth() {
  const [sessionToken, setSessionToken] = useState<string | null>(() =>
    sessionStorage.getItem(SESSION_STORAGE_KEY),
  )

  const login = useCallback(async (adminToken: string) => {
    const response = await fetch('/api/admin/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ admin_token: adminToken }),
    })
    if (!response.ok) {
      throw new Error('管理员 token 不正确')
    }
    const data = (await response.json()) as { session_token: string }
    sessionStorage.setItem(SESSION_STORAGE_KEY, data.session_token)
    setSessionToken(data.session_token)
  }, [])

  const logout = useCallback(() => {
    sessionStorage.removeItem(SESSION_STORAGE_KEY)
    setSessionToken(null)
  }, [])

  return { sessionToken, login, logout }
}
