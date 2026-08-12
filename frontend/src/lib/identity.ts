const STORAGE_KEY = 'customer-rag:anonymous-user-id'

/**
 * 匿名访客 ID：首次访问时生成一个随机 ID 存 localStorage，此后同一个浏览器
 * 一直复用同一个 ID——这是左边栏"我的会话列表"能跨页面刷新识别"这是同一个
 * 人"的唯一依据（这个产品目前没有真实的客户登录系统）。换浏览器/清缓存/
 * 隐私模式都会变成一个新身份，看不到旧会话，这是这个方案的已知代价。
 */
export function getAnonymousUserId(): string {
  const existing = window.localStorage.getItem(STORAGE_KEY)
  if (existing) return existing
  const generated = crypto.randomUUID()
  window.localStorage.setItem(STORAGE_KEY, generated)
  return generated
}
