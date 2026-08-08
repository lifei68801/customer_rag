export async function adminFetch(
  path: string,
  sessionToken: string,
  options: RequestInit = {},
): Promise<Response> {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...options.headers,
      Authorization: `Bearer ${sessionToken}`,
    },
  })
  if (response.status === 401) {
    sessionStorage.removeItem('admin_session_token')
    window.location.href = '/admin/login'
    throw new Error('登录已过期')
  }
  return response
}
