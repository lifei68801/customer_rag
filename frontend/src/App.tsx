import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AdminLayout } from './admin/AdminLayout'
import { LoginPage } from './admin/LoginPage'
import { DocumentsPage } from './admin/DocumentsPage'
import { GraphReviewsPage } from './admin/GraphReviewsPage'
import { TermsPage } from './admin/TermsPage'
import { SchemaEtlPage } from './admin/SchemaEtlPage'
import { OntologySchemaPage } from './admin/OntologySchemaPage'
import { DuplicatesPage } from './admin/DuplicatesPage'
import { OntologyGraphPage } from './admin/OntologyGraphPage'
import { TermDetailPage } from './admin/TermDetailPage'
import { DiagnosticsPage } from './admin/DiagnosticsPage'
import { AccountsPage } from './admin/AccountsPage'
import { TenantsPage } from './admin/TenantsPage'
import { SettingsPage } from './admin/SettingsPage'
import { NotFoundPage } from './admin/NotFoundPage'
import { GuidedOntologyPage } from './admin/guidedOntology/GuidedOntologyPage'
import { ADMIN_ROUTES, LEGACY_REDIRECTS } from './adminRoutes'

/**
 * 路径直接取自 adminRoutes.ts，不在这里另写一份字面量——侧边栏、⌘K、
 * 空状态链接读的都是那张表，这里再抄一遍就会出现"改了表但路由没跟上"。
 */
function App() {
  return (
    <Routes>
      <Route path="/" element={<ChatPage />} />
      <Route path="/admin/login" element={<LoginPage />} />
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to={ADMIN_ROUTES.documents} replace />} />

        <Route path="ingest/documents" element={<DocumentsPage />} />
        <Route path="ingest/etl" element={<SchemaEtlPage />} />
        <Route path="model/ontology" element={<OntologySchemaPage />} />
        {/* 本体图和疑似重复此前埋在别人的 tab 里。先给它们自己的 URL，
            页面本体的拆分是下一步的事——先有地址才谈得上被发现。 */}
        <Route path="model/graph" element={<OntologyGraphPage />} />
        {/* 首次建模的入口。不进 NAV_GROUPS——从本体结构页跳进来，不是
            常驻目的地。 */}
        <Route path="model/guided" element={<GuidedOntologyPage />} />
        <Route path="review/relations" element={<GraphReviewsPage />} />
        <Route path="review/duplicates" element={<DuplicatesPage />} />
        <Route path="terms" element={<TermsPage />} />
        {/* 详情页在列表下一层。node_key 含冒号和中文，靠 encodeURIComponent
            过 URL；它不含斜杠，所以 :nodeKey 够用。 */}
        <Route path="terms/:nodeKey" element={<TermDetailPage />} />
        <Route path="diagnostics" element={<DiagnosticsPage />} />
        <Route path="accounts" element={<AccountsPage />} />
        <Route path="tenants" element={<TenantsPage />} />
        <Route path="settings" element={<SettingsPage />} />

        {/* 旧书签。每条一跳直达，不经过中间那一代。 */}
        {Object.entries(LEGACY_REDIRECTS).map(([from, to]) => (
          <Route key={from} path={from.replace('/admin/', '')} element={<Navigate to={to} replace />} />
        ))}

        {/* 兜底。此前没有这条，敲错的地址会渲染成空白。 */}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}

export default App
