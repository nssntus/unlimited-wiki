export type Category = {
  id: string
  category_id: string
  label: string
  name: string
  blurb: string
  description: string
  directory_name: string
  aliases: string[]
  status: "active" | "archived"
  sort_order: number
  article_count: number
  revision: number
}

export type ArticleSummary = {
  path: string
  title: string
  aliases: string[]
  category: string
  category_label: string
  content_status: string
  completeness: string
  evidence_status: string
  article_id: string
  primary_category_id: string | null
  tags: string[]
  classification_status: "pending" | "confirmed" | "sync_conflict"
}

export type Article = ArticleSummary & {
  markdown: string
  redirected_from: string | null
  missing_sections: string[]
  generation: string | null
  remote_task: Task | null
  sources: string[]
  raw: string[]
  backlinks: { path: string; title: string }[]
  revision: string
  publication: {
    state: "not_published" | "submitted" | "published" | "update_available" | "update_pending" | "removed" | "relist_available" | "relist_pending"
    public_entry_id: string | null
    public_revision_id: string | null
    public_version: number | null
    published_at: string | null
    submission_id: string | null
    submission_status: string | null
    submission_matches_current: boolean
    moderation_reason: string | null
    moderated_at: string | null
  }
}

export type Task = {
  id: string
  kind: string
  subject: string
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled"
  attempts: number
  error_type: string | null
  error_message: string | null
  created_at: string
  updated_at: string
  payload: Record<string, unknown>
  result: Record<string, unknown> | null
}

export type ModelSettings = {
  configured: boolean
  provider: string | null
  base_url: string
  model: string | null
  has_api_key: boolean
  insecure_http: boolean
}

export type Session = {
  authenticated: boolean
  user?: { id: string; email: string; nickname: string; role: "user" | "admin" }
  workspace?: WorkspaceSummary
  csrf_token?: string
  session_expires_at?: string
  registration_enabled: boolean
}

export type WorkspaceSummary = {
  id: string
  organization_id: string
  kind: "personal" | "team"
  display_name: string
  status: "active" | "suspended" | "deleted"
  role: "owner" | "editor" | "viewer"
  organization_role: "owner" | "admin" | "member"
  permissions: string[]
  current?: boolean
}

export type WorkspaceMember = {
  user_id: string
  email: string
  nickname: string
  role: "owner" | "editor" | "viewer"
  status: "active"
  organization_role: "owner" | "admin" | "member"
  created_at: string
  is_current_user: boolean
}

export type WorkspaceInvitation = {
  id: string
  workspace_id: string
  display_name: string
  role: "editor" | "viewer"
  status: "pending"
  expires_at: string
  created_at: string
  invited_by_nickname: string
}

export type Submission = {
  id: string
  status: string
  snapshot: { title: string; category: string; content_status: string; markdown: string; summary: string; attribution: string; source_summaries: string[] }
  content_hash: string
  ai_report: { decision?: string; summary?: string; issues?: string[] } | null
  reason: string | null
  public_entry_id: string | null
  owner_id?: string
  created_at: string
  updated_at: string
}

export type PublicEntrySummary = { id: string; revision_id: string; version: number; title: string; category: string; attribution: string; summary: string; published_at: string; content_hash: string }
export type PublicEntry = { id: string; revision_id: string; version: number; snapshot: Submission["snapshot"]; attribution: string; published_at: string; content_hash: string }
export type PublicReport = { id: string; entry_id: string; reason_code: string; detail: string; status: string; created_at: string }
export type Notification = { id: string; kind: "public_removed" | "public_relisted"; object_type: string; object_id: string; title: string; message: string; read_at: string | null; created_at: string }
export type AdminPublicEntry = { id: string; status: "published" | "removed_by_admin"; author_id: string; author_nickname: string; revision_id: string; version: number; snapshot: Submission["snapshot"]; content_hash: string; published_at: string; moderation_reason: string | null; moderated_at: string | null }

export type RawInboxItem = {
  path: string
  title?: string
  size: number
  ingestable: boolean
  reason: string | null
  status: "unlinked" | "duplicate" | "ingested" | "integrity_changed" | "rejected"
  linked_target?: string | null
  source_format?: string
  extracted_chars?: number
  used_ocr?: boolean
}

export type LintIssue = {
  id: string
  kind: "dead_link" | "missing_category" | "missing_backlink" | "near_duplicate" | "content_quality"
  path: string
  title: string
  target?: string
  detail: string
}

export type TodoItem = {
  term: string
  mentions: number
  sources: { path: string; title: string; heading: string; passage: string }[]
}

const apiBase = import.meta.env.DEV ? "" : ""
let csrfToken = ""
let unauthorizedHandler: (() => void) | null = null

export function setCsrfToken(value: string) {
  csrfToken = value
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

export class ApiError extends Error {
  status: number
  payload: Record<string, unknown>

  constructor(status: number, payload: Record<string, unknown>) {
    super(String(payload.error || `Request failed (${status})`))
    this.status = status
    this.payload = payload
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => ({ error: "响应格式无效" }))) as Record<string, unknown>
  if (response.status === 401) unauthorizedHandler?.()
  if (!response.ok) throw new ApiError(response.status, payload)
  return payload as T
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, { credentials: "same-origin", headers: { Accept: "application/json" } })
  return parseResponse<T>(response)
}

export async function apiPost<T>(path: string, body: Record<string, unknown>, idempotent = true): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json" }
  if (idempotent) headers["Idempotency-Key"] = crypto.randomUUID()
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken
  const response = await fetch(`${apiBase}${path}`, { method: "POST", credentials: "same-origin", headers, body: JSON.stringify(body) })
  return parseResponse<T>(response)
}

export const queryKeys = {
  articles: ["articles"] as const,
  categories: ["categories"] as const,
  classifications: ["classifications"] as const,
  reconciliation: ["reconciliation"] as const,
  article: (path: string) => ["article", path] as const,
  raw: (path: string) => ["raw", path] as const,
  tasks: ["tasks"] as const,
  inbox: ["inbox"] as const,
  todo: ["todo"] as const,
  lint: ["lint"] as const,
  status: ["status"] as const,
  modelSettings: ["model-settings"] as const,
  session: ["session"] as const,
  workspaces: ["workspaces"] as const,
  workspaceMembers: ["workspace-members"] as const,
  invitations: ["workspace-invitations"] as const,
  submissions: ["submissions"] as const,
  submission: (id: string) => ["submission", id] as const,
  notifications: ["notifications"] as const,
  square: ["square"] as const,
  publicEntry: (id: string) => ["public-entry", id] as const,
  adminReviews: ["admin-reviews"] as const,
  adminPublicEntries: (status: string) => ["admin-public-entries", status] as const,
}
