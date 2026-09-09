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
import { PersonaEditorPage } from './admin/PersonaEditorPage'
import { TermDetailPage } from './admin/TermDetailPage'
import { DiagnosticsPage } from './admin/DiagnosticsPage'
import { AccountsPage } from './admin/AccountsPage'
import { TenantsPage } from './admin/TenantsPage'
import { SettingsPage } from './admin/SettingsPage'
import { NotFoundPage } from './admin/NotFoundPage'
import { GuidedOntologyPage } from './admin/guidedOntology/GuidedOntologyPage'
import {
  DatabaseImportPage,
} from './admin/ComingSoonPages'
import { DataGraphPage } from './admin/DataGraphPage'
import { ErrorLogPage } from './admin/ErrorLogPage'
import { DirtyEdgesPage } from './admin/DirtyEdgesPage'
import { AttributeConflictsPage } from './admin/AttributeConflictsPage'
import { DashboardPage } from './admin/DashboardPage'
import { ADMIN_ROUTES, LEGACY_REDIRECTS } from './adminRoutes'
import { useAdminAuth } from './admin/useAdminAuth'

/**
 * 前台的登录门。
 *
 * 前台面向内部坐席，本来就该登录：租户和用户身份现在都从服务端会话取，
 * 没有会话就一个请求也发不出去（后端会 401）。未登录直接渲染登录表单，
 * 而不是 <Navigate to="/admin/login">——那会把人从 `/` 弹到后台的地址上，
 * 登录完还得自己走回来。用的是同一个 LoginPage 组件，不另写一套。
 *
 * 'loading' 不能当成未登录：whoami 还没回来时把登录表单闪给一个其实还
 * 登录着的人，他会以为自己被登出了。
 */
function ChatRoute() {
  const { status } = useAdminAuth()
  if (status === 'loading') return null
  if (status === 'anonymous') return <LoginPage />
  return <ChatPage />
}

/**
 * 路径直接取自 adminRoutes.ts，不在这里另写一份字面量——侧边栏、⌘K、
 * 空状态链接读的都是那张表，这里再抄一遍就会出现"改了表但路由没跟上"。
 */
function App() {
  return (
    <Routes>
      <Route path="/" element={<ChatRoute />} />
      <Route path="/admin/login" element={<LoginPage />} />
      <Route path="/admin" element={<AdminLayout />}>
        {/* 落地页是看板：它跨领域、不需要"当前租户"，新登录的 admin
            （tenant_id 恒为 None）第一眼就能看到自己有哪些领域。
            此前这里是 AdminLanding，按本体确认状态在本体结构页和文档上传
            之间分流——那个分流需要一个当前租户，而登录那一刻没有。 */}
        <Route index element={<Navigate to={ADMIN_ROUTES.dashboard} replace />} />

        <Route path="dashboard" element={<DashboardPage />} />

        <Route path="ontology/ontology" element={<OntologySchemaPage />} />
        {/* 本体图和疑似重复此前埋在别人的 tab 里。先给它们自己的 URL，
            页面本体的拆分是下一步的事——先有地址才谈得上被发现。 */}
        <Route path="ontology/graph" element={<OntologyGraphPage />} />
        <Route path="ontology/guided" element={<GuidedOntologyPage />} />
        <Route path="ontology/persona" element={<PersonaEditorPage />} />

        <Route path="import/documents" element={<DocumentsPage />} />
        <Route path="import/table" element={<SchemaEtlPage />} />
        <Route path="import/database" element={<DatabaseImportPage />} />

        <Route path="review/relations" element={<GraphReviewsPage />} />
        <Route path="review/conflicts" element={<AttributeConflictsPage />} />
        <Route path="review/duplicates" element={<DuplicatesPage />} />
        <Route path="review/dirty-edges" element={<DirtyEdgesPage />} />

        <Route path="browse/terms" element={<TermsPage />} />
        <Route path="browse/graph" element={<DataGraphPage />} />
        {/* 详情页在列表下一层。node_key 含冒号和中文，靠 encodeURIComponent
            过 URL；它不含斜杠，所以 :nodeKey 够用。 */}
        <Route path="browse/terms/:nodeKey" element={<TermDetailPage />} />

        <Route path="logs/qa" element={<DiagnosticsPage />} />
        <Route path="logs/errors" element={<ErrorLogPage />} />

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
