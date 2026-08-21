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

export type PrivateTaxonomy = {
  categories: Category[]
  archived_categories: Category[]
  tags: string[]
}

export type SubmissionTaxonomySelection = {
  version: 1
  category: { kind: "existing"; id: string; name: string } | { kind: "proposal"; key: string; name: string; normalized_name: string }
  tags: Array<{ kind: "existing"; id: string; name: string } | { kind: "proposal"; key: string; name: string; normalized_name: string }>
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
    publication_fingerprint: string | null
    moderation_reason: string | null
    moderated_at: string | null
  }
}

export type Task = {
  id: string
  kind: string
  subject: string
  status: "queued" | "running" | "paused" | "succeeded" | "failed" | "cancelled"
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
  workspace_selection_required?: boolean
  csrf_token?: string
  session_expires_at?: string
  registration_enabled: boolean
  registration_mode?: "open" | "bootstrap" | "invite" | "closed"
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
  can_suspend: boolean
  can_restore: boolean
  can_delete: boolean
  can_leave: boolean
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

export type RegistrationInvite = {
  id: string
  email: string
  status: "pending" | "used" | "revoked" | "expired"
  expires_at: string
  created_at: string
  used_at: string | null
}

export type IssuedRegistrationInvite = RegistrationInvite & { token: string }

export type Submission = {
  id: string
  status: string
  snapshot: { title: string; category: string; content_status: string; markdown: string; summary: string; attribution: string; source_summaries: string[]; public_sources?: PublicSource[] }
  content_hash: string
  ai_report: { decision?: string; summary?: string; issues?: Array<string | { code?: string; location?: string; explanation?: string }> } | null
  reason: string | null
  public_entry_id: string | null
  owner_id?: string
  created_at: string
  updated_at: string
  proposed_public_category_id?: string | null
  proposed_tags?: string[]
  taxonomy?: SubmissionTaxonomySelection | null
  taxonomy_decision?: { version: 1; resolutions: Array<{ kind: string; action: string; id: string; key?: string }> } | null
  reuse_permission?: ReusePermission
  link_public_profile?: boolean
  duplicate_candidates?: PublicEntrySummary[]
}

export type CursorPage<T> = { items: T[]; next_cursor: string | null }
export type PublicCategory = { id: string | null; slug: string | null; name: string; description?: string; sort_order?: number; entry_count?: number }
export type PublicTag = { id: string; slug: string; name: string; entry_count?: number }
export type PublicSource = { label: string; url: string; kind: string }
export type ReusePermission = "view_only" | "allow_private_copy"
export const REUSE_POLICY_VERSION = "square-reuse-v1"
export const REUSE_POLICY_TEXT = "允许登录用户将本词条当时的当前公开版本复制到其私人 Wiki，作为独立副本保存和编辑。副本会保留来源词条、公开版本和作者署名，不会随公开词条更新而自动覆盖。关闭本许可只会阻止关闭后的新复制，不会删除此前按本许可生成的私人副本。本许可不等同于授权将副本再次公开发布或用于平台外传播。"
export const IMPORT_CONFIRMATION_TEXT = "将把本词条当前公开版本复制到你的私人 Wiki。复制后会形成可独立编辑的副本，并保留来源词条、公开版本和作者署名；后续公开更新不会自动覆盖。再次公开或向平台外传播时，仍需遵守原始来源和平台规则。"
export const GOVERNANCE_RETENTION_TEXT = "为处理争议、防止重复滥用并保留必要的处理依据，举报与纠错记录目前会长期保留，直至平台发布并实施独立的数据保留政策。相关记录不会公开，仅限获得授权的人员按职责访问。"
export type PublicEntrySummary = { id: string; revision_id: string; version: number; title: string; category: PublicCategory; tags: PublicTag[]; attribution: string; summary: string; published_at: string; first_published_at: string; updated_at: string; source_count: number; content_hash: string; featured: boolean }
export type PublicEntry = { id: string; revision_id: string; version: number; snapshot: Submission["snapshot"]; attribution: string; published_at: string; first_published_at: string; content_hash: string; category: PublicCategory; tags: PublicTag[]; sources: PublicSource[]; source_count: number; correction_count: number; review: { ai_policy_version: string | null; ai_model: string | null; ai_rules_version: string | null; issues: Array<{ code: string }>; admin_reason: string }; author_profile: { id: string; display_name: string } | null; reuse_permission: ReusePermission; reuse_policy_version: string; steward_label: string; subscribed: boolean; imported: boolean; featured: boolean; can_manage: boolean; related: Array<{ id: string; title: string; summary: string }>; references: Array<{ id: string; title: string; summary: string }> }
export type PublicHome = { categories: PublicCategory[]; tags: PublicTag[]; featured: PublicEntrySummary[]; latest: PublicEntrySummary[]; updated: PublicEntrySummary[]; collections: PublicCollection[] }
export type PublicRevision = { id: string; version: number; snapshot?: Submission["snapshot"]; content_hash: string; published_at: string; visibility?: "public" | "isolated"; isolation_reason?: string | null }
export type PublicCollection = { id: string; slug: string; title: string; description: string; published_at: string; entry_count?: number; items?: Array<{ id: string; title: string; summary: string; curator_note: string }> }
export type PublicProfile = { id: string; display_name: string; bio: string; entries: Array<{ id: string; title: string; summary: string; updated_at: string }> }
export type PublicCorrection = { id: string; entry_id: string; revision_id: string; kind: string; detail: string; evidence_url: string | null; status: string; author_response: string | null; created_at: string; updated_at: string }
export type PublicReport = { id: string; entry_id: string; revision_id: string; reason_code: string; detail: string; status: string; created_at: string; version?: number; snapshot?: Submission["snapshot"]; sources?: PublicSource[]; content_hash?: string }
export type AdminSquareState = {
  categories: Array<PublicCategory & { status: "active" | "disabled"; created_at: string; updated_at: string }>
  tags: Array<PublicTag & { status: "active" | "disabled"; created_at: string; updated_at: string }>
  collections: Array<PublicCollection & { status: "draft" | "published" | "disabled"; updated_at: string }>
  category_mappings: Array<{ private_label: string; category_id: string | null; status: string; updated_at: string }>
  corrections: Array<PublicCorrection & { entry_title: string }>
  index_jobs: Array<{ entry_id: string; status: "pending" | "running" | "retry" | "dead"; attempts: number; last_error: string | null; not_before: string | null; updated_at: string }>
  uncategorized_entries: Array<{ id: string; revision_id: string; version: number; title: string; summary: string; published_at: string; updated_at: string }>
}
export type Notification = { id: string; kind: string; object_type: string; object_id: string; title: string; message: string; read_at: string | null; created_at: string }
export type AdminPublicEntry = { id: string; status: "published" | "removed_by_admin"; author_id: string; author_nickname: string; revision_id: string; version: number; snapshot: Submission["snapshot"]; content_hash: string; published_at: string; moderation_reason: string | null; moderated_at: string | null; featured: boolean; featured_order: number | null }

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
let workspaceUnavailableHandler: (() => void) | null = null

export function setCsrfToken(value: string) {
  csrfToken = value
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

export function setWorkspaceUnavailableHandler(handler: (() => void) | null) {
  workspaceUnavailableHandler = handler
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
  if (response.status === 409 && payload.code === "workspace_selection_required") workspaceUnavailableHandler?.()
  if (!response.ok) throw new ApiError(response.status, payload)
  return payload as T
}

export async function apiGet<T>(path: string, options: { signal?: AbortSignal } = {}): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, { credentials: "same-origin", headers: { Accept: "application/json" }, signal: options.signal })
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
  taxonomy: ["taxonomy"] as const,
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
  squareHome: ["square", "home"] as const,
  squareSearch: (params: string) => ["square", "search", params] as const,
  publicCategories: ["square", "categories"] as const,
  publicTags: ["square", "tags"] as const,
  publicCategory: (slug: string) => ["square", "category", slug] as const,
  publicCollections: ["square", "collections"] as const,
  publicEntry: (id: string) => ["square", "entry", id] as const,
  publicVersions: (id: string) => ["square", "entry", id, "versions"] as const,
  publicVersion: (id: string, version: number) => ["square", "entry", id, "version", version] as const,
  publicDiff: (id: string, from: number, to: number) => ["square", "entry", id, "diff", from, to] as const,
  publicEntryCorrections: (id: string) => ["square", "entry", id, "corrections"] as const,
  publicCollection: (slug: string) => ["square", "collection", slug] as const,
  publicProfile: (id: string) => ["square", "profile", id] as const,
  publicLibrary: ["square", "me", "library"] as const,
  adminReviews: ["admin-reviews"] as const,
  adminRegistrationInvites: ["admin-registration-invites"] as const,
  adminSquare: ["admin-square"] as const,
  adminPublicEntries: (status: string) => ["admin-public-entries", status] as const,
}
