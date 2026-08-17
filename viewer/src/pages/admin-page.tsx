import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams } from "react-router-dom"
import {
  CheckIcon,
  EyeIcon,
  FileWarningIcon,
  RotateCcwIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react"
import { toast } from "sonner"

import {
  apiGet,
  apiPost,
  queryKeys,
  type AdminPublicEntry,
  type AdminSquareState,
  type PublicReport,
  type PublicRevision,
  type Submission,
} from "@/lib/api"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"

export function AdminReviewsPage() {
  const rows = useQuery({
    queryKey: queryKeys.adminReviews,
    queryFn: () =>
      apiGet<Submission[]>("/api/admin/submissions?status=pending_admin"),
    refetchInterval: 3000,
  })
  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <p className="text-sm font-medium text-primary">Admin</p>
      <h1 className="mt-2 text-3xl font-semibold">待人工审核</h1>
      <p className="mt-3 text-muted-foreground">
        这里只显示投稿快照，不提供任何私有空间入口。
      </p>
      {rows.isLoading ? (
        <Skeleton className="mt-10 h-80" />
      ) : (
        <div className="mt-10 divide-y border-y">
          {rows.data?.map((item) => (
            <Link
              key={item.id}
              to={`/admin/reviews/${item.id}`}
              className="flex flex-wrap items-center justify-between gap-4 py-5 hover:bg-muted/30"
            >
              <div>
                <h2 className="font-medium">{item.snapshot.title}</h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  作者 {item.owner_id?.slice(0, 8)} ·{" "}
                  {new Date(item.created_at).toLocaleString()}
                </p>
              </div>
              <StatusBadge value="等待审核" kind="warn" />
            </Link>
          ))}
          {!rows.data?.length && (
            <div className="py-16 text-center text-sm text-muted-foreground">
              当前没有待审核投稿
            </div>
          )}
        </div>
      )}
    </main>
  )
}

export function AdminReviewDetailPage() {
  const { id = "" } = useParams()
  const client = useQueryClient()
  const navigate = useNavigate()
  const [reason, setReason] = useState("")
  const [categoryOverride, setCategoryOverride] = useState<string | null>(null)
  const [tagOverride, setTagOverride] = useState<string[] | null>(null)
  const [taxonomyResolutions, setTaxonomyResolutions] = useState<
    Record<string, { action: "create" | "map"; target_id?: string }>
  >({})
  const [duplicateAction, setDuplicateAction] = useState("independent")
  const item = useQuery({
    queryKey: ["admin-review", id],
    queryFn: () => apiGet<Submission>(`/api/admin/submissions/${id}`),
  })
  const square = useQuery({
    queryKey: queryKeys.adminSquare,
    queryFn: () => apiGet<AdminSquareState>("/api/admin/square"),
  })
  const categoryId =
    categoryOverride ?? item.data?.proposed_public_category_id ?? ""
  const tagIds = tagOverride ?? item.data?.proposed_tags ?? []
  const proposals = [
    ...(item.data?.taxonomy?.category.kind === "proposal"
      ? [{ item: item.data.taxonomy.category, objectKind: "category" as const }]
      : []),
    ...(item.data?.taxonomy?.tags.flatMap((tag) =>
      tag.kind === "proposal" ? [{ item: tag, objectKind: "tag" as const }] : []
    ) ?? []),
  ]
  const taxonomyReady = proposals.every(
    ({ item: proposal }) =>
      taxonomyResolutions[proposal.key] &&
      (taxonomyResolutions[proposal.key].action === "create" ||
        Boolean(taxonomyResolutions[proposal.key].target_id))
  )
  const decide = useMutation({
    mutationFn: (decision: string) =>
      apiPost(`/api/admin/submissions/${id}/decision`, {
        decision,
        reason,
        public_category_id: categoryId,
        tag_ids: tagIds,
        duplicate_action: duplicateAction,
        taxonomy_decision: { version: 1, resolutions: taxonomyResolutions },
      }),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.adminReviews }),
        client.invalidateQueries({ queryKey: queryKeys.adminSquare }),
        client.invalidateQueries({ queryKey: queryKeys.square }),
      ])
      toast.success("审核决定已记录")
      navigate("/admin/reviews", { replace: true })
    },
    onError: (error) => toast.error(error.message),
  })
  if (item.isLoading)
    return (
      <main className="mx-auto max-w-6xl px-4 py-10">
        <Skeleton className="h-[70svh]" />
      </main>
    )
  if (!item.data) return <main className="p-10">投稿不存在。</main>
  const data = item.data
  const issues = data.ai_report?.issues ?? []
  return (
    <main className="mx-auto grid max-w-6xl gap-8 px-4 py-10 lg:grid-cols-[minmax(0,1fr)_340px]">
      <section>
        <div className="flex flex-wrap gap-2">
          <StatusBadge value={data.status} kind="warn" />
          <StatusBadge value={data.content_hash.slice(0, 16)} />
        </div>
        <h1 className="mt-4 text-3xl font-semibold">{data.snapshot.title}</h1>
        {data.duplicate_candidates?.length ? (
          <section className="mt-8 border-y py-5">
            <h2 className="text-sm font-semibold">公开重复候选</h2>
            <div className="mt-3 flex flex-col gap-2">
              {data.duplicate_candidates.map((candidate) => (
                <Link
                  className="text-sm text-link"
                  key={candidate.id}
                  to={`/square/entries/${candidate.id}`}
                >
                  {candidate.title} · v{candidate.version}
                </Link>
              ))}
            </div>
          </section>
        ) : null}
        <div className="mt-8 border p-5">
          <MarkdownContent
            markdown={data.snapshot.markdown.replace(/^#\s+.*\n/, "")}
            fromPath=""
            publicMode
          />
        </div>
      </section>
      <aside className="flex flex-col gap-6">
        <section>
          <h2 className="text-sm font-semibold">平台 AI 预审</h2>
          <p className="mt-2 text-xs text-muted-foreground">
            AI 只检查内容安全和重复候选，不参与分类或标签决定。
          </p>
          <p className="mt-2 text-sm text-muted-foreground">
            {data.ai_report?.summary || "未提供摘要"}
          </p>
          {issues.length ? (
            <ul className="mt-3 flex flex-col gap-2 text-xs text-muted-foreground">
              {issues.map((issue, index) => (
                <li key={index}>
                  {typeof issue === "string"
                    ? issue
                    : [issue.code, issue.location, issue.explanation]
                        .filter(Boolean)
                        .join(" · ")}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
        <FieldGroup>
          {data.taxonomy ? (
            <>
              <Field>
                <FieldLabel>投稿主分类</FieldLabel>
                <p className="text-sm">
                  {data.taxonomy.category.name}
                  {data.taxonomy.category.kind === "proposal"
                    ? " · 新提案"
                    : " · 已有分类"}
                </p>
              </Field>
              <Field>
                <FieldLabel>投稿标签</FieldLabel>
                <div className="flex flex-wrap gap-2 text-sm">
                  {data.taxonomy.tags.length ? (
                    data.taxonomy.tags.map((tag) => (
                      <span
                        className="border px-2 py-1"
                        key={tag.kind === "proposal" ? tag.key : tag.id}
                      >
                        {tag.name} · {tag.kind === "proposal" ? "新提案" : "已有标签，批准时沿用"}
                      </span>
                    ))
                  ) : (
                    <span className="text-muted-foreground">未选择标签</span>
                  )}
                </div>
              </Field>
              {proposals.map(({ item: proposal, objectKind }) => (
                <Field key={proposal.key}>
                  <FieldLabel>
                    处理{objectKind === "category" ? "分类" : "标签"}提案“
                    {proposal.name}”
                  </FieldLabel>
                  <Select
                    value={taxonomyResolutions[proposal.key]?.action ?? ""}
                    onValueChange={(value) =>
                      setTaxonomyResolutions((current) => ({
                        ...current,
                        [proposal.key]: { action: value as "create" | "map" },
                      }))
                    }
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="选择创建或映射" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="create">确认创建</SelectItem>
                        <SelectItem value="map">映射到已有对象</SelectItem>
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  {taxonomyResolutions[proposal.key]?.action === "map" && (
                    <Select
                      value={taxonomyResolutions[proposal.key]?.target_id ?? ""}
                      onValueChange={(value) =>
                        setTaxonomyResolutions((current) => ({
                          ...current,
                          [proposal.key]: {
                            action: "map",
                            target_id: value ?? "",
                          },
                        }))
                      }
                    >
                      <SelectTrigger className="mt-2 w-full">
                        <SelectValue
                          placeholder={`选择已有${objectKind === "category" ? "分类" : "标签"}`}
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {(objectKind === "category"
                            ? square.data?.categories
                            : square.data?.tags
                          )
                            ?.filter((value) => value.status === "active")
                            .map(
                              (value) =>
                                value.id && (
                                  <SelectItem key={value.id} value={value.id}>
                                    {value.name}
                                  </SelectItem>
                                )
                            )}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  )}
                </Field>
              ))}
            </>
          ) : (
            <>
              <Field>
                <FieldLabel>公共分类</FieldLabel>
                <Select
                  value={categoryId}
                  onValueChange={(value) => setCategoryOverride(value ?? "")}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="待公共分类" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {square.data?.categories
                        .filter((category) => category.status === "active")
                        .map(
                          (category) =>
                            category.id && (
                              <SelectItem key={category.id} value={category.id}>
                                {category.name}
                              </SelectItem>
                            )
                        )}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field>
                <FieldLabel>公共标签</FieldLabel>
                <div className="flex flex-wrap gap-3">
                  {square.data?.tags
                    .filter((tag) => tag.status === "active")
                    .map((tag) => (
                      <label
                        className="flex items-center gap-2 text-sm"
                        key={tag.id}
                      >
                        <Checkbox
                          checked={tagIds.includes(tag.id)}
                          onCheckedChange={(checked) =>
                            setTagOverride(
                              checked
                                ? [...new Set([...tagIds, tag.id])]
                                : tagIds.filter((value) => value !== tag.id)
                            )
                          }
                        />
                        {tag.name}
                      </label>
                    ))}
                </div>
              </Field>
            </>
          )}
          <Field>
            <FieldLabel>重复项处置</FieldLabel>
            <Select
              value={duplicateAction}
              onValueChange={(value) =>
                setDuplicateAction(value ?? "independent")
              }
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="independent">作为独立词条</SelectItem>
                  <SelectItem value="request_changes">退回说明关系</SelectItem>
                  <SelectItem value="reject_duplicate">拒绝重复投稿</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="review-reason">审核理由</FieldLabel>
            <Textarea
              id="review-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="必须填写可公开的审核理由"
            />
          </Field>
        </FieldGroup>
        <div className="grid gap-2">
          <Button
            disabled={
              !reason ||
              (!data.taxonomy && !categoryId) ||
              !taxonomyReady ||
              decide.isPending
            }
            onClick={() => decide.mutate("approve")}
          >
            <CheckIcon data-icon="inline-start" />
            通过并发布
          </Button>
          <Button
            variant="outline"
            disabled={!reason || decide.isPending}
            onClick={() => decide.mutate("request_changes")}
          >
            <FileWarningIcon data-icon="inline-start" />
            退回修改
          </Button>
          <Button
            variant="destructive"
            disabled={!reason || decide.isPending}
            onClick={() => decide.mutate("reject")}
          >
            <XIcon data-icon="inline-start" />
            拒绝投稿
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          分类、标签、重复项判断、审核人及公开理由都会写入审计。平台模型配置不向作者或普通
          Admin 响应披露密钥。
        </p>
      </aside>
    </main>
  )
}

export function AdminCurationPage() {
  const client = useQueryClient()
  const state = useQuery({
    queryKey: queryKeys.adminSquare,
    queryFn: () => apiGet<AdminSquareState>("/api/admin/square"),
  })
  const [correctionResponses, setCorrectionResponses] = useState<
    Record<string, string>
  >({})
  const [collection, setCollection] = useState({
    title: "",
    slug: "",
    description: "",
    entries: "",
    reason: "",
  })
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.adminSquare }),
      client.invalidateQueries({ queryKey: queryKeys.square }),
    ])
  }
  const closeCorrection = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      apiPost(`/api/public/corrections/${id}/decision`, {
        status,
        response: correctionResponses[id] || "Admin 已审阅并关闭该建议",
      }),
    onSuccess: async () => {
      await refresh()
      toast.success("纠错建议已处理")
    },
    onError: (error) => toast.error(error.message),
  })
  const saveCollection = useMutation({
    mutationFn: () =>
      apiPost("/api/admin/public-collections", {
        slug: collection.slug,
        title: collection.title,
        description: collection.description,
        status: "published",
        reason: collection.reason,
        items: collection.entries
          .split(/\s+/)
          .filter(Boolean)
          .map((entry_id) => ({ entry_id })),
      }),
    onSuccess: async () => {
      await refresh()
      setCollection({
        title: "",
        slug: "",
        description: "",
        entries: "",
        reason: "",
      })
      toast.success("专题已发布")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">Admin</p>
      <h1 className="mt-2 text-3xl font-semibold">广场策展</h1>
      <p className="mt-3 text-muted-foreground">
        公共分类与标签只在投稿审核中确认；这里仅处理专题和纠错。
      </p>
      <section className="mt-10">
        <h2 className="font-semibold">待处理纠错</h2>
        <div className="mt-5 divide-y border-y">
          {state.data?.corrections.map((item) => (
            <article
              key={item.id}
              className="grid gap-4 py-4 md:grid-cols-[minmax(0,1fr)_280px]"
            >
              <div>
                <Link
                  className="font-medium text-link"
                  to={`/square/entries/${item.entry_id}`}
                >
                  {item.entry_title}
                </Link>
                <p className="mt-2 text-sm">{item.detail}</p>
              </div>
              <div>
                <Textarea
                  value={correctionResponses[item.id] || ""}
                  onChange={(event) =>
                    setCorrectionResponses((current) => ({
                      ...current,
                      [item.id]: event.target.value,
                    }))
                  }
                  placeholder="处理说明"
                />
                <div className="mt-2 flex justify-end gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={
                      !correctionResponses[item.id]?.trim() ||
                      closeCorrection.isPending
                    }
                    onClick={() =>
                      closeCorrection.mutate({
                        id: item.id,
                        status: "rejected",
                      })
                    }
                  >
                    关闭
                  </Button>
                  <Button
                    size="sm"
                    disabled={
                      !correctionResponses[item.id]?.trim() ||
                      closeCorrection.isPending
                    }
                    onClick={() =>
                      closeCorrection.mutate({
                        id: item.id,
                        status: "resolved",
                      })
                    }
                  >
                    标记已处理
                  </Button>
                </div>
              </div>
            </article>
          ))}
          {!state.data?.corrections.length && (
            <p className="py-8 text-sm text-muted-foreground">没有待处理纠错</p>
          )}
        </div>
      </section>
      <section className="mt-14 border-t pt-10">
        <h2 className="font-semibold">发布专题</h2>
        <FieldGroup className="mt-6 max-w-2xl">
          <Field>
            <FieldLabel>标题</FieldLabel>
            <Input
              value={collection.title}
              onChange={(event) =>
                setCollection((current) => ({
                  ...current,
                  title: event.target.value,
                }))
              }
            />
          </Field>
          <Field>
            <FieldLabel>Slug</FieldLabel>
            <Input
              value={collection.slug}
              onChange={(event) =>
                setCollection((current) => ({
                  ...current,
                  slug: event.target.value,
                }))
              }
            />
          </Field>
          <Field>
            <FieldLabel>说明</FieldLabel>
            <Textarea
              value={collection.description}
              onChange={(event) =>
                setCollection((current) => ({
                  ...current,
                  description: event.target.value,
                }))
              }
            />
          </Field>
          <Field>
            <FieldLabel>公开词条 ID</FieldLabel>
            <Textarea
              value={collection.entries}
              onChange={(event) =>
                setCollection((current) => ({
                  ...current,
                  entries: event.target.value,
                }))
              }
            />
          </Field>
          <Field>
            <FieldLabel>策展理由</FieldLabel>
            <Textarea
              value={collection.reason}
              onChange={(event) =>
                setCollection((current) => ({
                  ...current,
                  reason: event.target.value,
                }))
              }
            />
          </Field>
          <Button
            disabled={
              !collection.title ||
              !collection.slug ||
              !collection.reason ||
              saveCollection.isPending
            }
            onClick={() => saveCollection.mutate()}
          >
            发布专题
          </Button>
        </FieldGroup>
      </section>
    </main>
  )
}

export function AdminPublicIndexPage() {
  const state = useQuery({
    queryKey: queryKeys.adminSquare,
    queryFn: () => apiGet<AdminSquareState>("/api/admin/square"),
    refetchInterval: 3000,
  })
  const retry = useMutation({
    mutationFn: (entryId?: string) =>
      apiPost(
        "/api/admin/public-index/retry",
        entryId ? { entry_id: entryId } : {}
      ),
    onSuccess: async () => {
      await state.refetch()
      toast.success("公开索引任务已重新排队")
    },
    onError: (error) => toast.error(error.message),
  })
  const jobs = state.data?.index_jobs ?? []
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">Admin</p>
      <h1 className="mt-2 text-3xl font-semibold">公开索引补偿</h1>
      <p className="mt-3 text-muted-foreground">
        公开事实已提交但搜索派生投影失败时，后台会按退避策略重试；达到上限后可在此手动重排。
      </p>
      <div className="mt-8 flex justify-end">
        <Button
          variant="outline"
          disabled={
            retry.isPending || !jobs.some((job) => job.status === "dead")
          }
          onClick={() => retry.mutate(undefined)}
        >
          重试全部失败任务
        </Button>
      </div>
      <div className="mt-4 divide-y border-y">
        {jobs.map((job) => (
          <div
            key={job.entry_id}
            className="flex flex-wrap items-center justify-between gap-3 py-4 text-sm"
          >
            <div>
              <code>{job.entry_id.slice(0, 12)}</code>
              <span className="ml-2 text-muted-foreground">
                {job.status} · 尝试 {job.attempts}
              </span>
              {job.last_error && (
                <p className="mt-1 max-w-2xl text-xs break-words text-muted-foreground">
                  {job.last_error}
                </p>
              )}
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={retry.isPending || job.status !== "dead"}
              onClick={() => retry.mutate(job.entry_id)}
            >
              重试
            </Button>
          </div>
        ))}
        {!jobs.length && (
          <p className="py-12 text-center text-sm text-muted-foreground">
            当前没有待补偿的索引任务
          </p>
        )}
      </div>
    </main>
  )
}

export function AdminReportsPage() {
  const client = useQueryClient()
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const reports = useQuery({
    queryKey: ["admin-reports"],
    queryFn: () => apiGet<PublicReport[]>("/api/admin/reports"),
    refetchInterval: 3000,
  })
  const decide = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      apiPost(`/api/admin/reports/${id}/decision`, {
        action,
        reason: reasons[id] || "已人工核验",
      }),
    onSuccess: async (_data, variables) => {
      if (variables.action === "remove") await purgePublicSquare(client)
      await client.invalidateQueries({ queryKey: ["admin-reports"] })
      toast.success("举报已处理")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">Admin</p>
      <h1 className="mt-2 text-3xl font-semibold">举报处理</h1>
      <div className="mt-10 divide-y border-y">
        {reports.data?.map((report) => (
          <div
            key={report.id}
            className="grid gap-4 py-5 md:grid-cols-[1fr_280px]"
          >
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{report.reason_code}</span>
                {report.version && (
                  <StatusBadge value={`目标版本 v${report.version}`} />
                )}
              </div>
              <Link
                className="mt-2 block text-link"
                to={`/square/entries/${report.entry_id}/versions/${report.version}`}
              >
                {report.snapshot?.title || "查看被举报版本"}
              </Link>
              <p className="mt-2 text-sm text-muted-foreground">
                {report.detail || "未提供补充说明"}
              </p>
              <p className="mt-3 text-xs text-muted-foreground">
                公开来源 {report.sources?.length ?? 0} 条 · 修订{" "}
                {report.revision_id.slice(0, 12)}
              </p>
            </div>
            <div className="space-y-2">
              <Textarea
                value={reasons[report.id] || ""}
                placeholder="处理理由"
                onChange={(event) =>
                  setReasons((current) => ({
                    ...current,
                    [report.id]: event.target.value,
                  }))
                }
              />
              <div className="flex justify-end gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={decide.isPending}
                  onClick={() =>
                    decide.mutate({ id: report.id, action: "dismiss" })
                  }
                >
                  驳回举报
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={decide.isPending}
                  onClick={() =>
                    decide.mutate({ id: report.id, action: "remove" })
                  }
                >
                  下架词条
                </Button>
              </div>
            </div>
          </div>
        ))}
        {!reports.data?.length && (
          <div className="py-16 text-center text-sm text-muted-foreground">
            没有待处理举报
          </div>
        )}
      </div>
    </main>
  )
}

function AdminEntryTools({
  entry,
  onClose,
}: {
  entry: AdminPublicEntry | null
  onClose: () => void
}) {
  const client = useQueryClient()
  const [reason, setReason] = useState("")
  const versions = useQuery({
    queryKey: ["admin-public-revisions", entry?.id],
    queryFn: () =>
      apiGet<PublicRevision[]>(
        `/api/admin/public-entries/${entry!.id}/revisions`
      ),
    enabled: Boolean(entry),
  })
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({
        queryKey: ["admin-public-revisions", entry?.id],
      }),
      client.invalidateQueries({
        queryKey: queryKeys.adminPublicEntries("published"),
      }),
      client.invalidateQueries({ queryKey: queryKeys.square }),
    ])
  }
  const feature = useMutation({
    mutationFn: () =>
      apiPost(`/api/admin/public-entries/${entry!.id}/featured`, {
        featured: !entry!.featured,
        reason,
        sort_order: entry!.featured ? 0 : 0,
      }),
    onSuccess: async () => {
      await refresh()
      toast.success(entry?.featured ? "已取消精选" : "已加入精选")
      onClose()
    },
    onError: (error) => toast.error(error.message),
  })
  const isolate = useMutation({
    mutationFn: ({
      revision,
      isolateRevision,
    }: {
      revision: string
      isolateRevision: boolean
    }) =>
      apiPost(
        `/api/admin/public-entries/${entry!.id}/revisions/${revision}/${isolateRevision ? "isolate" : "restore"}`,
        { reason }
      ),
    onSuccess: async () => {
      await purgePublicSquare(client)
      await refresh()
      toast.success("历史修订可见性已更新")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <Dialog open={Boolean(entry)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[88dvh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{entry?.snapshot.title}</DialogTitle>
          <DialogDescription>
            不可变公开快照 · v{entry?.version} · {entry?.author_nickname}
          </DialogDescription>
        </DialogHeader>
        {entry && (
          <MarkdownContent
            markdown={entry.snapshot.markdown}
            fromPath=""
            publicMode
          />
        )}
        <section className="border-t pt-5">
          <Field>
            <FieldLabel htmlFor="curation-reason">策展或隔离理由</FieldLabel>
            <Textarea
              id="curation-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="该理由会进入审计记录"
            />
          </Field>
          <div className="mt-3">
            <Button
              variant="outline"
              disabled={
                !reason.trim() ||
                feature.isPending ||
                entry?.status !== "published"
              }
              onClick={() => feature.mutate()}
            >
              {entry?.featured ? "取消精选" : "加入精选"}
            </Button>
          </div>
        </section>
        <section className="border-t pt-5">
          <h3 className="text-sm font-semibold">历史修订</h3>
          <div className="mt-3 divide-y border-y">
            {versions.data?.map((version) => (
              <div
                key={version.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div>
                  <span className="text-sm font-medium">
                    v{version.version}
                  </span>
                  <span className="ml-2 text-xs text-muted-foreground">
                    {version.visibility === "isolated" ? "已隔离" : "公开"}
                  </span>
                  {version.isolation_reason && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {version.isolation_reason}
                    </p>
                  )}
                </div>
                {version.id !== entry?.revision_id && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!reason.trim() || isolate.isPending}
                    onClick={() =>
                      isolate.mutate({
                        revision: version.id,
                        isolateRevision: version.visibility !== "isolated",
                      })
                    }
                  >
                    {version.visibility === "isolated"
                      ? "恢复公开"
                      : "隔离版本"}
                  </Button>
                )}
              </div>
            ))}
          </div>
        </section>
      </DialogContent>
    </Dialog>
  )
}

export function AdminContentPage() {
  const client = useQueryClient()
  const [status, setStatus] = useState<"published" | "removed_by_admin">(
    "published"
  )
  const [preview, setPreview] = useState<AdminPublicEntry | null>(null)
  const [target, setTarget] = useState<AdminPublicEntry | null>(null)
  const [reason, setReason] = useState("")
  const rows = useQuery({
    queryKey: queryKeys.adminPublicEntries(status),
    queryFn: () =>
      apiGet<AdminPublicEntry[]>(`/api/admin/public-entries?status=${status}`),
  })
  const moderate = useMutation({
    mutationFn: () =>
      apiPost(
        `/api/admin/public-entries/${target!.id}/${target!.status === "published" ? "remove" : "relist"}`,
        { reason }
      ),
    onSuccess: async () => {
      if (target?.status === "published") await purgePublicSquare(client)
      await Promise.all([
        client.invalidateQueries({
          queryKey: queryKeys.adminPublicEntries("published"),
        }),
        client.invalidateQueries({
          queryKey: queryKeys.adminPublicEntries("removed_by_admin"),
        }),
        client.invalidateQueries({ queryKey: queryKeys.square }),
      ])
      toast.success(
        target?.status === "published"
          ? "内容已下架并通知作者"
          : "内容已重新上架并通知作者"
      )
      setTarget(null)
      setReason("")
    },
    onError: (error) => toast.error(error.message),
  })

  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <p className="text-sm font-medium text-primary">Admin</p>
      <h1 className="mt-2 text-3xl font-semibold">公开内容管理</h1>
      <p className="mt-3 text-muted-foreground">
        审核者可以查看全部公开与下架快照；操作不会进入或修改作者的私有 Wiki。
      </p>
      <Tabs
        value={status}
        onValueChange={(value) => setStatus(value as typeof status)}
        className="mt-8"
      >
        <TabsList>
          <TabsTrigger value="published">已发布</TabsTrigger>
          <TabsTrigger value="removed_by_admin">已下架</TabsTrigger>
        </TabsList>
      </Tabs>
      {rows.isLoading ? (
        <Skeleton className="mt-6 h-72" />
      ) : rows.data?.length ? (
        <Table className="mt-6">
          <TableHeader>
            <TableRow>
              <TableHead>词条</TableHead>
              <TableHead>作者</TableHead>
              <TableHead>版本</TableHead>
              <TableHead>处理信息</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.data.map((entry) => (
              <TableRow key={entry.id}>
                <TableCell>
                  <div className="max-w-72 truncate font-medium">
                    {entry.snapshot.title}
                  </div>
                  <code className="text-xs text-muted-foreground">
                    {entry.content_hash.slice(0, 12)}
                  </code>
                </TableCell>
                <TableCell>{entry.author_nickname}</TableCell>
                <TableCell>v{entry.version}</TableCell>
                <TableCell>
                  <div className="max-w-72 text-xs whitespace-normal text-muted-foreground">
                    {entry.moderation_reason ||
                      new Date(entry.published_at).toLocaleString()}
                  </div>
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setPreview(entry)}
                    >
                      <EyeIcon data-icon="inline-start" />
                      预览
                    </Button>
                    <Button
                      size="sm"
                      variant={
                        entry.status === "published" ? "destructive" : "default"
                      }
                      onClick={() => setTarget(entry)}
                    >
                      {entry.status === "published" ? (
                        <Trash2Icon data-icon="inline-start" />
                      ) : (
                        <RotateCcwIcon data-icon="inline-start" />
                      )}
                      {entry.status === "published" ? "手动下架" : "重新上架"}
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <div className="mt-6 border-y py-16 text-center text-sm text-muted-foreground">
          {status === "published" ? "当前没有已发布内容" : "当前没有已下架内容"}
        </div>
      )}
      <AdminEntryTools entry={preview} onClose={() => setPreview(null)} />
      <AlertDialog
        open={Boolean(target)}
        onOpenChange={(open) => {
          if (!open) {
            setTarget(null)
            setReason("")
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {target?.status === "published"
                ? "手动下架该内容？"
                : "重新上架该内容？"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {target?.status === "published"
                ? "内容会立即从广场列表和公开详情消失，作者会收到理由和修改后重新申请的入口。"
                : "将直接恢复当前不可变公开版本，作者会收到重新上架通知。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Field>
            <FieldLabel htmlFor="moderation-reason">处理理由</FieldLabel>
            <Textarea
              id="moderation-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="必须填写可供作者查看的理由"
            />
          </Field>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              variant={
                target?.status === "published" ? "destructive" : "default"
              }
              disabled={!reason.trim() || moderate.isPending}
              onClick={() => moderate.mutate()}
            >
              {target?.status === "published" ? "确认下架" : "确认重新上架"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </main>
  )
}
async function purgePublicSquare(client: ReturnType<typeof useQueryClient>) {
  await client.cancelQueries({ queryKey: queryKeys.square })
  client.removeQueries({ queryKey: queryKeys.square })
}
