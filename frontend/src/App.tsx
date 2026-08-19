import { Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AdminLayout } from './admin/AdminLayout'
import { LoginPage } from './admin/LoginPage'
import { DocumentsPage } from './admin/DocumentsPage'
import { GraphReviewsPage } from './admin/GraphReviewsPage'
import { TermsPage } from './admin/TermsPage'
import { SchemaEtlPage } from './admin/SchemaEtlPage'
import { OntologySchemaPage } from './admin/OntologySchemaPage'
import { DataEntryPage } from './admin/DataEntryPage'

function App() {
  return (
    <Routes>
      <Route path="/" element={<ChatPage />} />
      <Route path="/admin/login" element={<LoginPage />} />
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<Navigate to="documents" replace />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="data-entry" element={<DataEntryPage />}>
          <Route index element={<Navigate to="manual" replace />} />
          <Route path="manual" element={<TermsPage />} />
          <Route path="etl" element={<SchemaEtlPage />} />
          <Route path="review" element={<GraphReviewsPage />} />
        </Route>
        <Route path="graph-reviews" element={<Navigate to="/admin/data-entry/review" replace />} />
        <Route path="terms" element={<Navigate to="/admin/data-entry/manual" replace />} />
        <Route path="schema-etl" element={<Navigate to="/admin/data-entry/etl" replace />} />
        <Route path="ontology" element={<OntologySchemaPage />} />
      </Route>
    </Routes>
  )
}

export default App
