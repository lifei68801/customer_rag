import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AdminLayout } from './admin/AdminLayout'
import { LoginPage } from './admin/LoginPage'

function DocumentsPlaceholder() {
  return <p className="text-ink">文档管理页面开发中</p>
}

function GraphReviewsPlaceholder() {
  return <p className="text-ink">知识图谱审核页面开发中</p>
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<ChatPage />} />
      <Route path="/admin/login" element={<LoginPage />} />
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to="documents" replace />} />
        <Route path="documents" element={<DocumentsPlaceholder />} />
        <Route path="graph-reviews" element={<GraphReviewsPlaceholder />} />
      </Route>
    </Routes>
  )
}

export default App
