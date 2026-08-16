import { lazy, Suspense } from "react"
import { HashRouter, Route, Routes } from "react-router-dom"

import { WikiShell } from "@/layouts/wiki-shell"
import { PublicShell } from "@/layouts/public-shell"
import { AdminShell } from "@/layouts/admin-shell"
import { Skeleton } from "@/components/ui/skeleton"
import { RequireSession, SessionProvider } from "@/features/session-provider"
import { WorkspaceDirectoryPage } from "@/features/workspace-selection-gate"

const ArticlePage = lazy(() => import("@/pages/article-page").then((module) => ({ default: module.ArticlePage })))
const EditorPage = lazy(() => import("@/pages/editor-page").then((module) => ({ default: module.EditorPage })))
const HealthPage = lazy(() => import("@/pages/health-page").then((module) => ({ default: module.HealthPage })))
const InboxPage = lazy(() => import("@/pages/inbox-page").then((module) => ({ default: module.InboxPage })))
const IngestPage = lazy(() => import("@/pages/ingest-page").then((module) => ({ default: module.IngestPage })))
const MergePage = lazy(() => import("@/pages/merge-page").then((module) => ({ default: module.MergePage })))
const RawPage = lazy(() => import("@/pages/raw-page").then((module) => ({ default: module.RawPage })))
const SettingsPage = lazy(() => import("@/pages/settings-page").then((module) => ({ default: module.SettingsPage })))
const TasksPage = lazy(() => import("@/pages/tasks-page").then((module) => ({ default: module.TasksPage })))
const TodoPage = lazy(() => import("@/pages/todo-page").then((module) => ({ default: module.TodoPage })))
const AuthPage = lazy(() => import("@/pages/auth-page").then((module) => ({ default: module.AuthPage })))
const SquarePage = lazy(() => import("@/pages/square-page").then((module) => ({ default: module.SquarePage })))
const PublicEntryPage = lazy(() => import("@/pages/square-page").then((module) => ({ default: module.PublicEntryPage })))
const SharePage = lazy(() => import("@/pages/share-page").then((module) => ({ default: module.SharePage })))
const SubmissionsPage = lazy(() => import("@/pages/submissions-page").then((module) => ({ default: module.SubmissionsPage })))
const SubmissionDetailPage = lazy(() => import("@/pages/submissions-page").then((module) => ({ default: module.SubmissionDetailPage })))
const NotificationsPage = lazy(() => import("@/pages/notifications-page").then((module) => ({ default: module.NotificationsPage })))
const ClassificationPage = lazy(() => import("@/pages/classification-page").then((module) => ({ default: module.ClassificationPage })))
const CategoriesPage = lazy(() => import("@/pages/categories-page").then((module) => ({ default: module.CategoriesPage })))
const ReconciliationPage = lazy(() => import("@/pages/reconciliation-page").then((module) => ({ default: module.ReconciliationPage })))
const WorkspacePage = lazy(() => import("@/pages/workspace-page").then((module) => ({ default: module.WorkspacePage })))
const AdminReviewsPage = lazy(() => import("@/pages/admin-page").then((module) => ({ default: module.AdminReviewsPage })))
const AdminReviewDetailPage = lazy(() => import("@/pages/admin-page").then((module) => ({ default: module.AdminReviewDetailPage })))
const AdminReportsPage = lazy(() => import("@/pages/admin-page").then((module) => ({ default: module.AdminReportsPage })))
const AdminContentPage = lazy(() => import("@/pages/admin-page").then((module) => ({ default: module.AdminContentPage })))

export function App() {
  return (
    <HashRouter>
      <SessionProvider>
        <Suspense fallback={<div className="p-8"><Skeleton className="h-[70svh] w-full" /></div>}>
          <Routes>
            <Route path="/login" element={<AuthPage mode="login" />} />
            <Route path="/register" element={<AuthPage mode="register" />} />
            <Route path="/recover" element={<AuthPage mode="recover" />} />
            <Route element={<PublicShell />}>
              <Route path="/square" element={<SquarePage />} />
              <Route path="/square/:id" element={<PublicEntryPage />} />
              <Route path="/forbidden" element={<main className="mx-auto max-w-xl px-4 py-20"><h1 className="text-2xl font-semibold">没有访问权限</h1><p className="mt-3 text-muted-foreground">当前账号没有执行此操作所需的角色。</p></main>} />
            </Route>
            <Route path="/workspaces" element={<RequireSession><WorkspaceDirectoryPage /></RequireSession>} />
            <Route element={<RequireSession><WikiShell /></RequireSession>}>
            <Route path="/inbox" element={<RequireSession permission="wiki.write"><InboxPage /></RequireSession>} />
            <Route path="/ingest/*" element={<RequireSession permission="wiki.write"><IngestPage /></RequireSession>} />
            <Route path="/todo" element={<RequireSession permission="wiki.write"><TodoPage /></RequireSession>} />
            <Route path="/health" element={<HealthPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/merge" element={<RequireSession permission="wiki.write"><MergePage /></RequireSession>} />
            <Route path="/edit/*" element={<RequireSession permission="wiki.write"><EditorPage /></RequireSession>} />
            <Route path="/raw/*" element={<RawPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/share" element={<RequireSession permission="wiki.write"><SharePage /></RequireSession>} />
            <Route path="/submissions" element={<RequireSession permission="wiki.write"><SubmissionsPage /></RequireSession>} />
            <Route path="/submissions/:id" element={<RequireSession permission="wiki.write"><SubmissionDetailPage /></RequireSession>} />
            <Route path="/notifications" element={<NotificationsPage />} />
            <Route path="/classification" element={<RequireSession permission="wiki.write"><ClassificationPage /></RequireSession>} />
            <Route path="/categories" element={<RequireSession permission="wiki.write"><CategoriesPage /></RequireSession>} />
            <Route path="/reconciliation" element={<RequireSession permission="wiki.write"><ReconciliationPage /></RequireSession>} />
            <Route path="/workspace" element={<WorkspacePage />} />
            <Route path="/*" element={<ArticlePage />} />
            </Route>
            <Route element={<RequireSession role="admin"><AdminShell /></RequireSession>}>
              <Route path="/admin/reviews" element={<AdminReviewsPage />} />
              <Route path="/admin/reviews/:id" element={<AdminReviewDetailPage />} />
              <Route path="/admin/reports" element={<AdminReportsPage />} />
              <Route path="/admin/content" element={<AdminContentPage />} />
            </Route>
          </Routes>
        </Suspense>
      </SessionProvider>
    </HashRouter>
  )
}

export default App
