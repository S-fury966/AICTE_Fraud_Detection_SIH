import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { IdentityProvider } from './lib/identity'
import ApplicationPage from './pages/application/ApplicationPage'
import AuditPage from './pages/AuditPage'
import DashboardPage from './pages/DashboardPage'
import EvaluationPage from './pages/EvaluationPage'
import FindingsPage from './pages/FindingsPage'
import HowItWorksPage from './pages/HowItWorksPage'
import NetworkPage from './pages/NetworkPage'
import SettingsPage from './pages/SettingsPage'
import SubmitPage from './pages/SubmitPage'

export default function App() {
  return (
    <IdentityProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="/applications/:id" element={<ApplicationPage />} />
          <Route path="/findings" element={<FindingsPage />} />
          <Route path="/submit" element={<SubmitPage />} />
          <Route path="/evaluation" element={<EvaluationPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/connections" element={<NetworkPage />} />
          <Route path="/about" element={<HowItWorksPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </IdentityProvider>
  )
}
