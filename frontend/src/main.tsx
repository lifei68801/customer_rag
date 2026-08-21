import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import { ConfirmProvider } from './admin/ConfirmContext'
import { ToastProvider } from './admin/ToastContext'
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

// SkinProvider/ConfirmProvider/ToastProvider 都包在最外层（而不是只包
// AdminLayout）——这三个都是站点级偏好/能力，前台聊天页和后台管理共用。
// 之前 ConfirmProvider 只在 AdminLayout 里挂载过，导致 ChatSidebar.tsx
// 拿不到 useConfirm()，只能退回原生 window.confirm()；ToastProvider 从
// 一开始就直接挂在这里，避免重蹈覆辙。
createRoot(rootElement).render(
  <StrictMode>
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>
  </StrictMode>,
)
