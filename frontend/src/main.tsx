import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import '@fontsource/space-grotesk/400.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/space-mono/400.css'
import '@fontsource/space-mono/700.css'
import './styles/index.css'
import 'katex/dist/katex.min.css'

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('未找到 #root 挂载节点')
}

// SkinProvider 包在最外层（而不是只包 AdminLayout）——皮肤是站点级偏好，
// 前台聊天页和后台管理共用同一个 <html data-skin> 属性和 localStorage
// 键。之前只在 AdminLayout 里挂载过一次 SkinProvider，导致前台路由完全
// 拿不到这个 Provider：刷新前台页面时 useEffect 从未执行，data-skin
// 属性从未被设置，:root 的默认皮肤值生效，看起来像是"选择被重置了"。
createRoot(rootElement).render(
  <StrictMode>
    <SkinProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </SkinProvider>
  </StrictMode>,
)
