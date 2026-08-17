import { useMemo, useState } from "react"
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom"
import {
  BellIcon,
  BookCopyIcon,
  BookOpenIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  FlagIcon,
  FolderSearchIcon,
  HistoryIcon,
  SearchIcon,
  SettingsIcon,
  ShieldCheckIcon,
  UserRoundIcon,
} from "lucide-react"
import { toast } from "sonner"

import {
  ApiError,
  apiGet,
  apiPost,
  GOVERNANCE_RETENTION_TEXT,
  IMPORT_CONFIRMATION_TEXT,
  queryKeys,
  REUSE_POLICY_TEXT,
  type CursorPage,
  type PublicCategory,
  type PublicCollection,
  type PublicCorrection,
  type PublicEntry,
  type PublicEntrySummary,
  type PublicHome,
  type PublicProfile,
  type PublicRevision,
  type PublicTag,
} from "@/lib/api"
import { useSession } from "@/features/session-context"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
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
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { markdownToc, scrollToHeading } from "@/lib/markdown-toc"

type TocItem = ReturnType<typeof markdownToc>[number]

const REPORT_REASON_LABELS: Record<string, string> = {
  copyright: "版权或抄袭",
  privacy: "隐私或个人信息",
  illegal_or_dangerous: "违法或危险内容",
  harassment_or_fraud: "仇恨、骚扰或欺诈",
  spam: "垃圾内容",
  other: "其他内容问题",
}
const CORRECTION_KIND_LABELS: Record<string, string> = {
  factual: "事实问题",
  source: "来源不足",
  outdated: "内容过时",
  duplicate: "重复词条",
  supplement: "建议补充",
  clarity: "表述不清",
  typo: "文字错误",
  other: "其他",
}

function publicBodyMarkdown(markdown: string) {
  return markdown.replace(/^#\s+.*\n/, "").replace(/^(#{1,5})(\s+)/gm, "$1#$2")
}

function PublicEntryToc({
  headings,
  className = "",
}: {
  headings: TocItem[]
  className?: string
}) {
  if (!headings.length) return null
  return (
    <nav aria-label="词条目录" className={className}>
      <h2 className="text-sm font-semibold">目录</h2>
      <div className="mt-4 flex max-h-[40svh] flex-col gap-2 overflow-y-auto pr-2 text-sm">
        {headings.map((heading) => (
          <button
            key={heading.id}
            type="button"
            onClick={() => scrollToHeading(heading.id)}
            className="text-left leading-5 text-muted-foreground hover:text-foreground"
            style={{ paddingInlineStart: `${(heading.depth - 2) * 12}px` }}
          >
            {heading.title}
          </button>
        ))}
      </div>
    </nav>
  )
}

function EntryList({
  entries,
  empty = "暂无公开词条",
}: {
  entries: PublicEntrySummary[]
  empty?: string
}) {
  if (!entries.length)
    return <p className="py-8 text-sm text-muted-foreground">{empty}</p>
  return (
    <div className="divide-y border-y">
      {entries.map((entry) => (
        <Link
          key={entry.id}
          to={`/square/entries/${entry.id}`}
          className="grid gap-3 py-5 transition-colors hover:bg-muted/35 sm:grid-cols-[minmax(0,1fr)_auto] sm:px-3"
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {entry.featured && <Badge>精选</Badge>}
              <Badge variant="outline">{entry.category.name}</Badge>
              {entry.tags.slice(0, 3).map((tag) => (
                <span key={tag.id} className="text-xs text-muted-foreground">
                  #{tag.name}
                </span>
              ))}
            </div>
            <h3 className="mt-2 text-base font-semibold">{entry.title}</h3>
            <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
              {entry.summary || "查看经过平台审核的公开知识快照。"}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              {entry.attribution} · {entry.source_count} 个公开来源
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground sm:flex-col sm:items-end sm:justify-center">
            <span>v{entry.version}</span>
            <span>{new Date(entry.updated_at).toLocaleDateString()}</span>
          </div>
        </Link>
      ))}
    </div>
  )
}

function SearchForm({ initial = "" }: { initial?: string }) {
  const [query, setQuery] = useState(initial),
    navigate = useNavigate()
  return (
    <form
      className="flex w-full gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        navigate(`/square/search?q=${encodeURIComponent(query.trim())}`)
      }}
    >
      <div className="relative min-w-0 flex-1">
        <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="搜索 Wiki 广场"
          className="pl-9"
          value={query}
          maxLength={120}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索标题、摘要、正文、分类或标签"
        />
      </div>
      <Button type="submit">
        <SearchIcon data-icon="inline-start" />
        搜索
      </Button>
    </form>
  )
}

export function SquarePage() {
  const home = useQuery({
    queryKey: queryKeys.squareHome,
    queryFn: () => apiGet<PublicHome>("/api/public/home"),
  })
  if (home.isLoading)
    return (
      <main className="mx-auto max-w-6xl px-4 py-10">
        <Skeleton className="h-[70svh]" />
      </main>
    )
  const data = home.data ?? {
    categories: [],
    tags: [],
    featured: [],
    latest: [],
    updated: [],
    collections: [],
  }
  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-10 md:py-14">
      <section className="max-w-3xl">
        <p className="text-sm font-medium text-primary">公共知识</p>
        <h1 className="mt-2 text-3xl font-semibold">Wiki 广场</h1>
        <p className="mt-3 text-muted-foreground">
          检索经过平台 AI 预审与 Admin 人审的不可变公开版本。
        </p>
        <div className="mt-7">
          <SearchForm />
        </div>
      </section>
      <section className="mt-12">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">公共分类</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              广场分类独立于任何私人 Wiki。
            </p>
          </div>
          <Link className="text-sm text-link" to="/square/search">
            全部词条
          </Link>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {data.categories.length ? (
            data.categories.map((category) => (
              <Button
                key={category.id}
                size="sm"
                variant="outline"
                render={
                  <Link to={`/square/search?category=${category.slug}`} />
                }
              >
                <FolderSearchIcon data-icon="inline-start" />
                {category.name}
                <span className="text-muted-foreground">
                  {category.entry_count ?? 0}
                </span>
              </Button>
            ))
          ) : (
            <span className="text-sm text-muted-foreground">
              公共分类仍在治理中
            </span>
          )}
        </div>
      </section>
      <section className="mt-12">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">精选</h2>
          <span className="text-xs text-muted-foreground">
            人工策展，不按流量排序
          </span>
        </div>
        <EntryList entries={data.featured} empty="暂时没有精选词条" />
      </section>
      {data.collections.length > 0 && (
        <section className="mt-12">
          <h2 className="text-lg font-semibold">专题</h2>
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {data.collections.map((collection) => (
              <Link
                key={collection.id}
                to={`/square/collections/${collection.slug}`}
                className="border-l-2 border-primary px-4 py-2"
              >
                <h3 className="font-medium">{collection.title}</h3>
                <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                  {collection.description}
                </p>
                <span className="mt-3 block text-xs text-muted-foreground">
                  {collection.entry_count ?? 0} 篇词条
                </span>
              </Link>
            ))}
          </div>
        </section>
      )}
      <div className="mt-12 grid gap-12 lg:grid-cols-2">
        <section>
          <h2 className="text-lg font-semibold">最近更新</h2>
          <EntryList entries={data.updated} />
        </section>
        <section>
          <h2 className="text-lg font-semibold">最新发布</h2>
          <EntryList entries={data.latest} />
        </section>
      </div>
    </main>
  )
}

export function SquareSearchPage() {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const q = params.get("q") ?? "",
    category = params.get("category") ?? "",
    tag = params.get("tag") ?? "",
    sort = params.get("sort") ?? (q ? "relevance" : "updated")
  const requestParams = new URLSearchParams({
    q,
    category,
    tag,
    sort,
    limit: "24",
  })
  const requestSignature = requestParams.toString()
  const searchKey = queryKeys.squareSearch(requestParams.toString())
  const results = useInfiniteQuery({
    queryKey: searchKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiGet<CursorPage<PublicEntrySummary>>(
        `/api/public/search?${requestSignature}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`,
        { signal }
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  })
  const visible = useMemo(
    () =>
      (results.data?.pages.flatMap((page) => page.items) ?? []).filter(
        (value, index, all) =>
          all.findIndex((item) => item.id === value.id) === index
      ),
    [results.data]
  )
  const change = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
  }
  const loadMore = async () => {
    const result = await results.fetchNextPage()
    if (result.error instanceof ApiError && result.error.status === 409 && result.error.payload.code === "cursor_expired") {
      await client.cancelQueries({ queryKey: searchKey })
      await client.resetQueries({ queryKey: searchKey })
      toast.info("广场内容已更新，搜索结果已从第一页重新加载")
    }
  }
  const sortLabel =
    sort === "latest" ? "最新发布" : sort === "updated" ? "最近更新" : "相关度"
  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <div className="max-w-3xl">
        <h1 className="text-2xl font-semibold">搜索广场</h1>
        <div className="mt-5">
          <SearchForm key={q} initial={q} />
        </div>
        <div className="mt-4 flex flex-wrap gap-3">
          <Select
            value={sort}
            onValueChange={(value) => value && change("sort", value)}
          >
            <SelectTrigger className="w-40">
              <span>{sortLabel}</span>
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="relevance">相关度</SelectItem>
                <SelectItem value="updated">最近更新</SelectItem>
                <SelectItem value="latest">最新发布</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
          {(category || tag) && (
            <Button
              variant="ghost"
              onClick={() => {
                setParams(q ? { q } : {})
              }}
            >
              清除筛选
            </Button>
          )}
        </div>
      </div>
      <div className="mt-9">
        {results.isLoading ? (
          <Skeleton className="h-80" />
        ) : (
          <EntryList entries={visible} empty="没有符合条件的公开词条" />
        )}
        {results.hasNextPage && (
          <div className="mt-6 flex justify-center">
            <Button
              variant="outline"
              disabled={results.isFetchingNextPage}
              onClick={() => void loadMore()}
            >
              {results.isFetchingNextPage && <Spinner data-icon="inline-start" />}
              加载更多
            </Button>
          </div>
        )}
      </div>
    </main>
  )
}

export function PublicCategoriesPage() {
  const categories = useQuery({
    queryKey: queryKeys.publicCategories,
    queryFn: () => apiGet<PublicCategory[]>("/api/public/categories"),
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">公共知识体系</p>
      <h1 className="mt-2 text-3xl font-semibold">公共分类</h1>
      <p className="mt-3 max-w-2xl text-muted-foreground">
        这些稳定分类由 Admin 治理，不复制任何私人 Wiki 目录。
      </p>
      {categories.isLoading ? (
        <Skeleton className="mt-10 h-64" />
      ) : (
        <div className="mt-10 divide-y border-y">
          {categories.data?.map((category) => (
            <Link
              key={category.id}
              to={`/square/categories/${category.slug}`}
              className="flex items-center justify-between gap-4 py-5"
            >
              <div>
                <h2 className="font-semibold">{category.name}</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  {category.description || "浏览该公共分类下的已发布词条"}
                </p>
              </div>
              <span className="shrink-0 text-sm text-muted-foreground">
                {category.entry_count ?? 0} 篇
              </span>
            </Link>
          ))}
        </div>
      )}
    </main>
  )
}

export function PublicCategoryPage() {
  const { slug = "" } = useParams()
  const category = useQuery({
    queryKey: queryKeys.publicCategory(slug),
    queryFn: () =>
      apiGet<PublicCategory & { redirected_from?: string | null }>(
        `/api/public/categories/${slug}`
      ),
    retry: false,
  })
  const results = useQuery({
    queryKey: queryKeys.squareSearch(`category=${slug}`),
    queryFn: () =>
      apiGet<CursorPage<PublicEntrySummary>>(
        `/api/public/search?category=${encodeURIComponent(slug)}&sort=updated&limit=50`
      ),
    enabled: Boolean(category.data),
    retry: false,
  })
  if (!category.data)
    return (
      <main className="mx-auto max-w-5xl px-4 py-12">
        <Skeleton className="h-72" />
      </main>
    )
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">公共分类</p>
      <h1 className="mt-2 text-3xl font-semibold">{category.data.name}</h1>
      <p className="mt-3 max-w-2xl text-muted-foreground">
        {category.data.description}
      </p>
      {category.data.redirected_from && (
        <p className="mt-3 text-xs text-muted-foreground">
          旧分类地址已合并到当前分类，深链继续有效。
        </p>
      )}
      <div className="mt-10">
        <EntryList
          entries={results.data?.items ?? []}
          empty="这个分类暂时没有公开词条"
        />
      </div>
    </main>
  )
}

export function PublicTagPage() {
  const { slug = "" } = useParams()
  const tags = useQuery({
    queryKey: queryKeys.publicTags,
    queryFn: () => apiGet<PublicTag[]>("/api/public/tags"),
  })
  const tag = tags.data?.find((item) => item.slug === slug)
  const results = useQuery({
    queryKey: queryKeys.squareSearch(`tag=${slug}`),
    queryFn: () =>
      apiGet<CursorPage<PublicEntrySummary>>(
        `/api/public/search?tag=${encodeURIComponent(slug)}&sort=updated&limit=50`
      ),
    enabled: Boolean(tag),
    retry: false,
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">公共标签</p>
      <h1 className="mt-2 text-3xl font-semibold">#{tag?.name ?? slug}</h1>
      <div className="mt-10">
        {tags.isLoading ? (
          <Skeleton className="h-64" />
        ) : (
          <EntryList
            entries={results.data?.items ?? []}
            empty="这个标签暂时没有公开词条"
          />
        )}
      </div>
    </main>
  )
}

function ReportDialog({ entryId }: { entryId: string }) {
  const [open, setOpen] = useState(false),
    [reason, setReason] = useState("other"),
    [detail, setDetail] = useState("")
  const report = useMutation({
    mutationFn: () =>
      apiPost(`/api/public/entries/${entryId}/reports`, {
        reason_code: reason,
        detail,
      }),
    onSuccess: () => {
      setOpen(false)
      setDetail("")
      toast.success("举报已提交")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" variant="ghost" />}>
        <FlagIcon data-icon="inline-start" />
        举报
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>举报公开词条</DialogTitle>
          <DialogDescription>
            举报绑定当前公开版本，只供 Admin 处理，不会修改作者私人 Wiki。
          </DialogDescription>
        </DialogHeader>
        <Alert>
          <ShieldCheckIcon />
          <AlertTitle>记录保留说明</AlertTitle>
          <AlertDescription>{GOVERNANCE_RETENTION_TEXT}</AlertDescription>
        </Alert>
        <FieldGroup>
          <Field>
            <FieldLabel>原因</FieldLabel>
            <Select
              value={reason}
              onValueChange={(value) => value && setReason(value)}
              >
                <SelectTrigger className="w-full">
                <SelectValue>{REPORT_REASON_LABELS[reason]}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="copyright">版权或抄袭</SelectItem>
                  <SelectItem value="privacy">隐私或个人信息</SelectItem>
                  <SelectItem value="illegal_or_dangerous">
                    违法或危险内容
                  </SelectItem>
                  <SelectItem value="harassment_or_fraud">
                    仇恨、骚扰或欺诈
                  </SelectItem>
                  <SelectItem value="spam">垃圾内容</SelectItem>
                  <SelectItem value="other">其他内容问题</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="report-detail">具体说明</FieldLabel>
            <Textarea
              id="report-detail"
              value={detail}
              onChange={(event) => setDetail(event.target.value)}
            />
          </Field>
        </FieldGroup>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            取消
          </Button>
          <Button disabled={report.isPending} onClick={() => report.mutate()}>
            提交举报
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CorrectionDialog({ entryId }: { entryId: string }) {
  const [open, setOpen] = useState(false),
    [kind, setKind] = useState("factual"),
    [detail, setDetail] = useState(""),
    [evidence, setEvidence] = useState("")
  const correction = useMutation({
    mutationFn: () =>
      apiPost(`/api/public/entries/${entryId}/corrections`, {
        kind,
        detail,
        evidence_url: evidence,
      }),
    onSuccess: () => {
      setOpen(false)
      setDetail("")
      setEvidence("")
      toast.success("纠错建议已提交")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" variant="outline" />}>
        提交纠错
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>提交结构化纠错</DialogTitle>
          <DialogDescription>
            建议绑定当前公开版本；作者接受后仍需另行修改并发布新修订。
          </DialogDescription>
        </DialogHeader>
        <Alert>
          <ShieldCheckIcon />
          <AlertTitle>记录保留说明</AlertTitle>
          <AlertDescription>{GOVERNANCE_RETENTION_TEXT}</AlertDescription>
        </Alert>
        <FieldGroup>
          <Field>
            <FieldLabel>类型</FieldLabel>
            <Select
              value={kind}
              onValueChange={(value) => value && setKind(value)}
              >
              <SelectTrigger className="w-full">
                <SelectValue>{CORRECTION_KIND_LABELS[kind]}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="factual">事实问题</SelectItem>
                  <SelectItem value="source">来源不足</SelectItem>
                  <SelectItem value="outdated">内容过时</SelectItem>
                  <SelectItem value="duplicate">重复词条</SelectItem>
                  <SelectItem value="supplement">建议补充</SelectItem>
                  <SelectItem value="clarity">表述不清</SelectItem>
                  <SelectItem value="typo">文字错误</SelectItem>
                  <SelectItem value="other">其他</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="correction-detail">建议</FieldLabel>
            <Textarea
              id="correction-detail"
              value={detail}
              onChange={(event) => setDetail(event.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="correction-evidence">
              公开证据 URL（可选）
            </FieldLabel>
            <Input
              id="correction-evidence"
              type="url"
              value={evidence}
              onChange={(event) => setEvidence(event.target.value)}
            />
          </Field>
        </FieldGroup>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            取消
          </Button>
          <Button
            disabled={!detail.trim() || correction.isPending}
            onClick={() => correction.mutate()}
          >
            提交
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function AuthorCorrections({
  entryId,
  active,
}: {
  entryId: string
  active: boolean
}) {
  const client = useQueryClient()
  const [responses, setResponses] = useState<Record<string, string>>({})
  const corrections = useQuery({
    queryKey: queryKeys.publicEntryCorrections(entryId),
    queryFn: () =>
      apiGet<PublicCorrection[]>(`/api/public/entries/${entryId}/corrections`),
    enabled: active,
  })
  const decide = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      apiPost(`/api/public/corrections/${id}/decision`, {
        status,
        response: responses[id] || "已审阅该建议",
      }),
    onSuccess: async () => {
      await client.invalidateQueries({
        queryKey: queryKeys.publicEntryCorrections(entryId),
      })
      toast.success("纠错处理状态已更新")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <section className="border-t pt-5">
      <h3 className="text-sm font-semibold">收到的纠错建议</h3>
      {corrections.isLoading ? (
        <Skeleton className="mt-3 h-24" />
      ) : corrections.data?.length ? (
        <div className="mt-3 max-h-64 space-y-4 overflow-y-auto pr-1">
          {corrections.data.map((item) => (
            <article key={item.id} className="border-l-2 pl-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-medium">{item.kind}</span>
                <Badge variant="outline">{item.status}</Badge>
              </div>
              <p className="mt-2 text-sm">{item.detail}</p>
              {item.evidence_url && (
                <div className="mt-2 text-xs">
                  <a className="block break-all text-link" href={item.evidence_url} target="_blank" rel="noreferrer noopener">
                    公开证据 · 外部来源
                  </a>
                  <p className="mt-1 text-muted-foreground">
                    将在新标签页打开；平台未抓取或核验第三方页面内容。
                    {item.evidence_url.startsWith("http://") && " 此来源使用非加密连接。"}
                  </p>
                </div>
              )}
              {item.status === "open" || item.status === "acknowledged" ? (
                <div className="mt-3 space-y-2">
                  <Textarea
                    value={responses[item.id] || ""}
                    onChange={(event) =>
                      setResponses((current) => ({
                        ...current,
                        [item.id]: event.target.value,
                      }))
                    }
                    placeholder="给建议者的处理说明"
                  />
                  <div className="flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={decide.isPending}
                      onClick={() =>
                        decide.mutate({ id: item.id, status: "acknowledged" })
                      }
                    >
                      已查看
                    </Button>
                    <Button
                      size="sm"
                      disabled={decide.isPending || !responses[item.id]?.trim()}
                      onClick={() =>
                        decide.mutate({ id: item.id, status: "accepted" })
                      }
                    >
                      接受
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={decide.isPending || !responses[item.id]?.trim()}
                      onClick={() =>
                        decide.mutate({ id: item.id, status: "rejected" })
                      }
                    >
                      拒绝
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={decide.isPending || !responses[item.id]?.trim()}
                      onClick={() =>
                        decide.mutate({ id: item.id, status: "resolved" })
                      }
                    >
                      标记已处理
                    </Button>
                  </div>
                </div>
              ) : (
                item.author_response && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    处理说明：{item.author_response}
                  </p>
                )
              )}
            </article>
          ))}
        </div>
      ) : (
        <p className="mt-3 text-sm text-muted-foreground">当前没有纠错建议。</p>
      )}
    </section>
  )
}

export function PublicEntryPage() {
  const { id = "" } = useParams(),
    { session } = useSession(),
    client = useQueryClient()
  const [importOpen, setImportOpen] = useState(false),
    [manageOpen, setManageOpen] = useState(false)
  const [withdrawReason, setWithdrawReason] = useState("")
  const [importAcknowledged, setImportAcknowledged] = useState(false)
  const [subscribeImport, setSubscribeImport] = useState(true)
  const [pendingReuse, setPendingReuse] = useState<"view_only" | "allow_private_copy" | null>(null)
  const [reuseAcknowledged, setReuseAcknowledged] = useState(false)
  const entry = useQuery({
    queryKey: queryKeys.publicEntry(id),
    queryFn: () => apiGet<PublicEntry>(`/api/public/entries/${id}`),
    enabled: Boolean(id),
    retry: false,
  })
  const versions = useQuery({
    queryKey: queryKeys.publicVersions(id),
    queryFn: () =>
      apiGet<PublicRevision[]>(`/api/public/entries/${id}/versions`),
    enabled: Boolean(entry.data),
  })
  const subscribe = useMutation({
    mutationFn: (active: boolean) =>
      apiPost(
        `/api/public/entries/${id}/${active ? "subscribe" : "unsubscribe"}`,
        {}
      ),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: queryKeys.publicEntry(id) }),
    onError: (error) => toast.error(error.message),
  })
  const importEntry = useMutation({
    mutationFn: () =>
      apiPost<{ article: { path: string } }>("/api/public/import", {
        entry_id: id,
        revision_id: entry.data!.revision_id,
        expected_workspace_id: session!.workspace!.id,
        policy_version: entry.data!.reuse_policy_version,
        acknowledged: importAcknowledged,
        subscribe: subscribeImport,
      }),
    onSuccess: (result) => {
      setImportOpen(false)
      void client.invalidateQueries({ queryKey: queryKeys.publicEntry(id) })
      toast.success("已收入当前 Wiki 的待归类区")
      window.location.hash = `#/${result.article.path}`
    },
    onError: (error) => toast.error(error.message),
  })
  const reuse = useMutation({
    mutationFn: (permission: "view_only" | "allow_private_copy") =>
      apiPost(`/api/public/entries/${id}/reuse`, {
        permission,
        policy_version: entry.data!.reuse_policy_version,
        acknowledged: permission === "allow_private_copy" && reuseAcknowledged,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.publicEntry(id) })
      setPendingReuse(null)
      setReuseAcknowledged(false)
      toast.success("复用许可已更新")
    },
    onError: (error) => toast.error(error.message),
  })
  const withdraw = useMutation({
    mutationFn: () =>
      apiPost(`/api/public/entries/${id}/withdraw`, { reason: withdrawReason }),
    onSuccess: async () => {
      await client.cancelQueries({ queryKey: queryKeys.square })
      client.removeQueries({ queryKey: queryKeys.square })
      toast.success("公开词条已撤回")
      window.location.hash = "#/square"
    },
    onError: (error) => toast.error(error.message),
  })
  if (entry.isLoading)
    return (
      <main className="mx-auto max-w-3xl px-4 py-12">
        <Skeleton className="h-96" />
      </main>
    )
  if (!entry.data)
    return (
      <main className="mx-auto max-w-3xl px-4 py-12">
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <BookOpenIcon />
            </EmptyMedia>
            <EmptyTitle>公开词条不可用</EmptyTitle>
            <EmptyDescription>
              {(entry.error as Error | null)?.message || "它可能已被作者撤回、由 Admin 下架，或当前修订已隔离。"}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </main>
    )
  const data = entry.data,
    bodyMarkdown = publicBodyMarkdown(data.snapshot.markdown),
    headings = markdownToc(bodyMarkdown)
  return (
    <main className="mx-auto max-w-6xl px-4 py-10 md:py-14">
      <div className="grid min-w-0 gap-12 lg:grid-cols-[minmax(0,760px)_minmax(220px,280px)] lg:justify-between">
        <article className="min-w-0">
          <div className="flex flex-wrap gap-2">
            <StatusBadge value="平台审核后发布" kind="good" />
            <StatusBadge value={`版本 ${data.version}`} />
            {data.featured && <StatusBadge value="精选" />}
          </div>
          <h1 className="mt-5 text-3xl font-semibold">{data.snapshot.title}</h1>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
            {data.category.slug ? (
              <Link
                className="text-link"
                to={`/square/categories/${data.category.slug}`}
              >
                {data.category.name}
              </Link>
            ) : (
              <span>{data.category.name}</span>
            )}
            {data.tags.map((tag) => (
              <Link key={tag.id} to={`/square/tags/${tag.slug}`}>
                #{tag.name}
              </Link>
            ))}
            <span>
              {data.author_profile ? (
                <Link
                  className="text-link"
                  to={`/square/authors/${data.author_profile.id}`}
                >
                  {data.author_profile.display_name}
                </Link>
              ) : (
                data.attribution
              )}
            </span>
            <span>{new Date(data.published_at).toLocaleString()}</span>
          </div>
          <Alert className="mt-6">
            <UserRoundIcon />
            <AlertTitle>单一发布负责人</AlertTitle>
            <AlertDescription>
              此公开词条由 {data.steward_label || "匿名发布者"} 负责管理。只有该发布负责人可以提交新版本、撤回词条或修改复用许可。团队中的其他成员可以另行投稿，但不会自动加入此词条的版本历史。
              {session?.authenticated && !data.can_manage && " 你不是此公开词条的发布负责人，无法提交新版本或修改治理设置。你可以创建独立投稿。"}
            </AlertDescription>
          </Alert>
          <div className="mt-7 flex flex-wrap items-center gap-2 border-y py-3">
            {session?.authenticated ? (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={subscribe.isPending}
                  onClick={() => subscribe.mutate(!data.subscribed)}
                >
                  <BellIcon data-icon="inline-start" />
                  {data.subscribed ? "取消订阅" : "订阅更新"}
                </Button>
                {data.reuse_permission === "allow_private_copy" &&
                  (session.workspace ? (
                    <Button
                      size="sm"
                      disabled={data.imported}
                      onClick={() => { setImportAcknowledged(false); setImportOpen(true) }}
                    >
                      <BookCopyIcon data-icon="inline-start" />
                      {data.imported ? "已收入 Wiki" : "收入我的 Wiki"}
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      render={<Link to="/workspaces" />}
                    >
                      <BookCopyIcon data-icon="inline-start" />
                      选择导入空间
                    </Button>
                  ))}
                <CorrectionDialog entryId={id} />
                {data.can_manage && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setManageOpen(true)}
                  >
                    <SettingsIcon data-icon="inline-start" />
                    管理公开词条
                  </Button>
                )}
              </>
            ) : (
              <Button size="sm" variant="outline" render={<Link to="/login" />}>
                登录后订阅或纠错
              </Button>
            )}
            <ReportDialog entryId={id} />
          </div>
          {data.reuse_permission === "allow_private_copy" && (
            <Alert className="mt-6">
              <BookCopyIcon />
              <AlertTitle>允许私人复制</AlertTitle>
              <AlertDescription>
                {REUSE_POLICY_TEXT}
              </AlertDescription>
            </Alert>
          )}
          <PublicEntryToc
            headings={headings}
            className="mt-8 border-b pb-6 lg:hidden"
          />
          <MarkdownContent
            className="mt-10"
            markdown={bodyMarkdown}
            fromPath=""
            publicMode
          />
          <section className="mt-12 border-t pt-8">
            <h2 className="text-lg font-semibold">公开来源</h2>
            {data.sources.length ? (
              <ul className="mt-4 flex flex-col gap-3">
                {data.sources.map((source, index) => (
                  <li key={`${source.url}-${index}`}>
                    <a
                      className="inline-flex max-w-full items-center gap-2 text-sm break-all text-link"
                      href={source.url}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      {source.label}
                      <ExternalLinkIcon className="size-3.5 shrink-0" />
                    </a>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {source.kind}
                    </span>
                    <p className="mt-1 text-xs text-muted-foreground">
                      外部来源 · 将在新标签页打开。该链接由作者提供，平台未抓取或核验第三方页面内容。
                      {source.url.startsWith("http://") && " 此来源使用非加密连接。"}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-3 text-sm text-muted-foreground">
                本版本没有作者明确选择的公开来源。
              </p>
            )}
          </section>
          <section className="mt-12 border-t pt-8">
            <h2 className="text-lg font-semibold">继续阅读</h2>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {[...data.related, ...data.references]
                .filter(
                  (value, index, all) =>
                    all.findIndex((item) => item.id === value.id) === index
                )
                .map((item) => (
                  <Link
                    key={item.id}
                    className="border-l-2 px-4 py-1"
                    to={`/square/entries/${item.id}`}
                  >
                    <h3 className="font-medium">{item.title}</h3>
                    <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                      {item.summary}
                    </p>
                  </Link>
                ))}
            </div>
          </section>
        </article>
        <aside className="flex flex-col gap-8 lg:sticky lg:top-20 lg:self-start">
          <PublicEntryToc
            headings={headings}
            className="hidden border-l pl-6 lg:block"
          />
          <section className="border-l pl-6">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <ShieldCheckIcon className="size-4" />
              可信度信息
            </h2>
            <dl className="mt-4 flex flex-col gap-3 text-sm">
              <div>
                <dt className="text-muted-foreground">审核政策</dt>
                <dd>{data.review.ai_policy_version ?? "历史审核记录"}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Admin 说明</dt>
                <dd>{data.review.admin_reason || "已通过人工审核"}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">公开来源</dt>
                <dd>{data.source_count} 条</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">纠错记录</dt>
                <dd>{data.correction_count ? `${data.correction_count} 条` : "暂无"}</dd>
              </div>
            </dl>
            <Collapsible className="mt-4">
              <CollapsibleTrigger className="text-xs text-link">
                版本技术信息
              </CollapsibleTrigger>
              <CollapsibleContent>
                <code className="mt-2 block text-xs break-all text-muted-foreground">
                  SHA-256 {data.content_hash}
                </code>
              </CollapsibleContent>
            </Collapsible>
          </section>
          <section className="border-l pl-6">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <HistoryIcon className="size-4" />
              公开版本
            </h2>
            <div className="mt-3 flex flex-col gap-2">
              {versions.data?.map((version) => (
                <Link
                  key={version.id}
                  className="flex items-center justify-between text-sm text-link"
                  to={`/square/entries/${id}/versions/${version.version}`}
                >
                  v{version.version}
                  <span className="text-xs text-muted-foreground">
                    {new Date(version.published_at).toLocaleDateString()}
                  </span>
                </Link>
              ))}
            </div>
            <Link
              className="mt-4 inline-block text-xs text-link"
              to={`/square/entries/${id}/versions`}
            >
              查看完整版本历史与差异
            </Link>
          </section>
        </aside>
      </div>
      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>收入当前 Wiki？</DialogTitle>
            <DialogDescription>
              复制的是公开词条《{data.snapshot.title}》v{data.version}，署名为“
              {data.attribution}”，目标是“
              {session?.workspace?.display_name ?? "未选择空间"}”。
            </DialogDescription>
          </DialogHeader>
          <Alert>
            <BookCopyIcon />
            <AlertTitle>私人副本独立演化</AlertTitle>
            <AlertDescription>
              {IMPORT_CONFIRMATION_TEXT}
            </AlertDescription>
          </Alert>
          <label className="flex items-start gap-2 text-sm">
            <Checkbox checked={importAcknowledged} onCheckedChange={(value) => setImportAcknowledged(Boolean(value))} />
            <span>我已阅读并确认本次复制条款（{data.reuse_policy_version}）</span>
          </label>
          <label className="flex items-start gap-2 text-sm">
            <Checkbox checked={subscribeImport} onCheckedChange={(value) => setSubscribeImport(Boolean(value))} />
            <span>同时订阅该公开词条的后续版本通知（不会自动覆盖私人副本）</span>
          </label>
          <DialogFooter>
            <Button variant="outline" onClick={() => setImportOpen(false)}>
              取消
            </Button>
            <Button
              disabled={!session?.workspace || !importAcknowledged || importEntry.isPending}
              onClick={() => importEntry.mutate()}
            >
              {importEntry.isPending && <Spinner data-icon="inline-start" />}
              确认复制
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={manageOpen} onOpenChange={setManageOpen}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>管理公开词条</DialogTitle>
            <DialogDescription>
              许可调整只影响未来复制；撤回会让所有公开版本立即不可访问，但不删除审计和既有合法私人副本。
            </DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <Field>
              <FieldLabel>私人复制许可</FieldLabel>
              <Select
                value={pendingReuse ?? data.reuse_permission}
                onValueChange={(value) => {
                  if (value !== "view_only" && value !== "allow_private_copy") return
                  setPendingReuse(value)
                  setReuseAcknowledged(false)
                }}
              >
                <SelectTrigger className="w-full">
                  <SelectValue>
                    {(pendingReuse ?? data.reuse_permission) === "allow_private_copy"
                      ? "允许复制到私人 Wiki"
                      : "仅公开阅读"}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="view_only">仅公开阅读</SelectItem>
                    <SelectItem value="allow_private_copy">
                      允许复制到私人 Wiki
                    </SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
              {(pendingReuse ?? data.reuse_permission) === "allow_private_copy" && (
                <Alert className="mt-3">
                  <AlertTitle>允许复制到私人 Wiki</AlertTitle>
                  <AlertDescription>
                    <p>{REUSE_POLICY_TEXT}</p>
                    <label className="mt-3 flex items-start gap-2 text-foreground">
                      <Checkbox checked={reuseAcknowledged} onCheckedChange={(value) => setReuseAcknowledged(Boolean(value))} />
                      <span>我已阅读并确认上述许可（{data.reuse_policy_version}）</span>
                    </label>
                  </AlertDescription>
                </Alert>
              )}
              {pendingReuse && pendingReuse !== data.reuse_permission && (
                <Button
                  className="mt-3"
                  disabled={reuse.isPending || (pendingReuse === "allow_private_copy" && !reuseAcknowledged)}
                  onClick={() => reuse.mutate(pendingReuse)}
                >
                  保存复用许可
                </Button>
              )}
            </Field>
            <Field>
              <FieldLabel htmlFor="withdraw-reason">撤回理由</FieldLabel>
              <Textarea
                id="withdraw-reason"
                value={withdrawReason}
                onChange={(event) => setWithdrawReason(event.target.value)}
              />
            </Field>
          </FieldGroup>
          <AuthorCorrections entryId={id} active={manageOpen} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setManageOpen(false)}>
              完成
            </Button>
            <Button
              variant="destructive"
              disabled={!withdrawReason.trim() || withdraw.isPending}
              onClick={() => withdraw.mutate()}
            >
              撤回整个公开词条
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}

export function PublicVersionPage() {
  const { id = "", version = "" } = useParams(),
    numeric = Number(version)
  const item = useQuery({
    queryKey: queryKeys.publicVersion(id, numeric),
    queryFn: () =>
      apiGet<PublicRevision>(`/api/public/entries/${id}/versions/${numeric}`),
    enabled: Boolean(id && Number.isInteger(numeric)),
    retry: false,
  })
  const diff = useQuery({
    queryKey: queryKeys.publicDiff(id, numeric - 1, numeric),
    queryFn: () =>
      apiGet<{ diff: string }>(
        `/api/public/entries/${id}/diff?from=${numeric - 1}&to=${numeric}`
      ),
    enabled: numeric > 1,
    retry: false,
  })
  if (!item.data)
    return (
      <main className="mx-auto max-w-4xl px-4 py-12">
        <Skeleton className="h-80" />
      </main>
    )
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-muted-foreground">不可变公开历史</p>
          <h1 className="mt-1 text-2xl font-semibold">
            {item.data.snapshot?.title} · v{numeric}
          </h1>
        </div>
        <Button
          variant="outline"
          render={<Link to={`/square/entries/${id}`} />}
        >
          当前版本
        </Button>
      </div>
      <MarkdownContent
        className="mt-10"
        markdown={publicBodyMarkdown(item.data.snapshot?.markdown ?? "")}
        fromPath=""
        publicMode
      />
      {numeric > 1 && (
        <section className="mt-12 border-t pt-8">
          <h2 className="text-lg font-semibold">
            与 v{numeric - 1} 的文本差异
          </h2>
          <pre className="mt-4 max-h-[32rem] overflow-auto border bg-muted/30 p-4 text-xs whitespace-pre-wrap">
            {diff.data?.diff || "没有可显示的差异"}
          </pre>
        </section>
      )}
    </main>
  )
}

export function PublicVersionsPage() {
  const { id = "" } = useParams()
  const versions = useQuery({
    queryKey: queryKeys.publicVersions(id),
    queryFn: () =>
      apiGet<PublicRevision[]>(`/api/public/entries/${id}/versions`),
    retry: false,
  })
  return (
    <main className="mx-auto max-w-4xl px-4 py-10">
      <p className="text-sm font-medium text-primary">不可变公开历史</p>
      <div className="mt-2 flex flex-wrap items-end justify-between gap-4">
        <h1 className="text-3xl font-semibold">版本历史</h1>
        <Button variant="outline" render={<Link to={`/square/entries/${id}`} />}>
          返回当前版本
        </Button>
      </div>
      <div className="mt-10 divide-y border-y">
        {versions.data?.map((version, index, all) => {
          const older = all[index + 1]
          return (
            <div
              key={version.id}
              className="flex flex-wrap items-center justify-between gap-3 py-4"
            >
              <div>
                <Link
                  className="font-medium text-link"
                  to={`/square/entries/${id}/versions/${version.version}`}
                >
                  公开版本 v{version.version}
                </Link>
                <p className="mt-1 text-xs text-muted-foreground">
                  {new Date(version.published_at).toLocaleString()}
                </p>
              </div>
              {older && (
                <Button
                  size="sm"
                  variant="ghost"
                  render={
                    <Link
                      to={`/square/entries/${id}/diff?from=${older.version}&to=${version.version}`}
                    />
                  }
                >
                  与 v{older.version} 比较
                </Button>
              )}
            </div>
          )
        })}
      </div>
    </main>
  )
}

export function PublicDiffPage() {
  const { id = "" } = useParams()
  const [params] = useSearchParams()
  const from = Number(params.get("from")),
    to = Number(params.get("to"))
  const valid =
    Number.isInteger(from) &&
    Number.isInteger(to) &&
    from > 0 &&
    to > 0 &&
    Math.abs(from - to) === 1
  const diff = useQuery({
    queryKey: queryKeys.publicDiff(id, from, to),
    queryFn: () =>
      apiGet<{ diff: string }>(
        `/api/public/entries/${id}/diff?from=${from}&to=${to}`
      ),
    enabled: valid,
    retry: false,
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">安全纯文本差异</p>
      <div className="mt-2 flex flex-wrap items-end justify-between gap-4">
        <h1 className="text-2xl font-semibold">
          {valid ? `v${from} → v${to}` : "版本参数无效"}
        </h1>
        <Button
          variant="outline"
          render={<Link to={`/square/entries/${id}/versions`} />}
        >
          版本历史
        </Button>
      </div>
      <pre className="mt-8 max-h-[70dvh] overflow-auto border bg-muted/30 p-4 text-xs whitespace-pre-wrap">
        {valid ? diff.data?.diff || "没有可显示的差异" : "只能比较相邻公开版本。"}
      </pre>
    </main>
  )
}

export function PublicCollectionsPage() {
  const collections = useQuery({
    queryKey: queryKeys.publicCollections,
    queryFn: () => apiGet<PublicCollection[]>("/api/public/collections"),
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm font-medium text-primary">Admin 人工策展</p>
      <h1 className="mt-2 text-3xl font-semibold">精选专题</h1>
      <p className="mt-3 text-muted-foreground">
        专题按人工策展记录展示，不使用浏览量或隐藏热度排序。
      </p>
      <div className="mt-10 divide-y border-y">
        {collections.data?.map((collection) => (
          <Link
            key={collection.id}
            to={`/square/collections/${collection.slug}`}
            className="grid gap-2 py-5 sm:grid-cols-[minmax(0,1fr)_auto]"
          >
            <div>
              <h2 className="font-semibold">{collection.title}</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {collection.description}
              </p>
            </div>
            <span className="text-sm text-muted-foreground">
              {collection.entry_count ?? 0} 篇
            </span>
          </Link>
        ))}
        {!collections.data?.length && (
          <p className="py-10 text-sm text-muted-foreground">暂时没有公开专题</p>
        )}
      </div>
    </main>
  )
}

export function PublicCollectionPage() {
  const { slug = "" } = useParams(),
    query = useQuery({
      queryKey: queryKeys.publicCollection(slug),
      queryFn: () =>
        apiGet<PublicCollection>(`/api/public/collections/${slug}`),
      retry: false,
    })
  if (query.isLoading)
    return (
      <main className="mx-auto max-w-5xl px-4 py-12">
        <Skeleton className="h-80" />
      </main>
    )
  if (!query.data)
    return (
      <main className="mx-auto max-w-5xl px-4 py-12">
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon"><UserRoundIcon /></EmptyMedia>
            <EmptyTitle>作者主页不可用</EmptyTitle>
            <EmptyDescription>{(query.error as Error | null)?.message || "该作者主页已停用。"}</EmptyDescription>
          </EmptyHeader>
        </Empty>
      </main>
    )
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <p className="text-sm text-primary">Admin 策展专题</p>
      <h1 className="mt-2 text-3xl font-semibold">{query.data.title}</h1>
      <p className="mt-3 max-w-2xl text-muted-foreground">
        {query.data.description}
      </p>
      <div className="mt-10 divide-y border-y">
        {query.data.items?.map((item) => (
          <Link
            key={item.id}
            to={`/square/entries/${item.id}`}
            className="block py-5"
          >
            <h2 className="font-semibold">{item.title}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{item.summary}</p>
            {item.curator_note && (
              <p className="mt-3 border-l-2 pl-3 text-sm">
                策展说明：{item.curator_note}
              </p>
            )}
          </Link>
        ))}
      </div>
    </main>
  )
}

export function PublicProfilePage() {
  const { id = "" } = useParams(),
    query = useQuery({
      queryKey: queryKeys.publicProfile(id),
      queryFn: () => apiGet<PublicProfile>(`/api/public/authors/${id}`),
      retry: false,
    })
  if (!query.data)
    return (
      <main className="mx-auto max-w-5xl px-4 py-12">
        <Skeleton className="h-80" />
      </main>
    )
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <div className="flex items-center gap-3">
        <UserRoundIcon className="size-8 text-primary" />
        <div>
          <h1 className="text-2xl font-semibold">{query.data.display_name}</h1>
          <p className="text-sm text-muted-foreground">公开作者主页</p>
        </div>
      </div>
      <p className="mt-6 max-w-2xl whitespace-pre-wrap">{query.data.bio}</p>
      <section className="mt-10">
        <h2 className="text-lg font-semibold">已公开词条</h2>
        <div className="mt-4 divide-y border-y">
          {query.data.entries.map((entry) => (
            <Link
              key={entry.id}
              className="flex items-center justify-between py-4"
              to={`/square/entries/${entry.id}`}
            >
              <span>{entry.title}</span>
              <ChevronRightIcon className="size-4" />
            </Link>
          ))}
        </div>
      </section>
    </main>
  )
}

export function SquareLibraryPage() {
  const { session } = useSession(),
    client = useQueryClient()
  const library = useQuery({
    queryKey: queryKeys.publicLibrary,
    queryFn: () =>
      apiGet<{
        imports: Array<{
          id: string
          entry_id: string
          private_path: string
          workspace_name: string
          title: string
          status: string
          policy_version: string
          source_available: boolean
        }>
        subscriptions: Array<{
          entry_id: string
          title: string
          status: string
        }>
        profile: { display_name: string; bio: string; status: string } | null
      }>("/api/public/me/library"),
  })
  const reports = useQuery({
    queryKey: ["my-public-reports"],
    queryFn: () =>
      apiGet<
        Array<{
          id: string
          entry_id: string
          reason_code: string
          status: string
          resolution_detail: string | null
        }>
      >("/api/public/me/reports"),
  })
  const corrections = useQuery({
    queryKey: ["my-public-corrections"],
    queryFn: () => apiGet<PublicCorrection[]>("/api/public/me/corrections"),
  })
  const [profileName, setProfileName] = useState(""),
    [profileBio, setProfileBio] = useState("")
  const saveProfile = useMutation({
    mutationFn: (enabled: boolean) =>
      apiPost("/api/public/profile", {
        enabled,
        display_name:
          profileName ||
          library.data?.profile?.display_name ||
          session?.user?.nickname ||
          "作者",
        bio: profileBio || library.data?.profile?.bio || "",
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.publicLibrary })
      toast.success("公开主页设置已保存")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <h1 className="text-2xl font-semibold">我的广场</h1>
      <section className="mt-10 border-y py-6">
        <h2 className="font-semibold">公开作者主页</h2>
        <FieldGroup className="mt-4 max-w-xl">
          <Field>
            <FieldLabel>显示名称</FieldLabel>
            <Input
              value={profileName}
              onChange={(event) => setProfileName(event.target.value)}
              placeholder={
                library.data?.profile?.display_name || session?.user?.nickname
              }
            />
          </Field>
          <Field>
            <FieldLabel>简介</FieldLabel>
            <Textarea
              value={profileBio}
              onChange={(event) => setProfileBio(event.target.value)}
              placeholder={library.data?.profile?.bio || "公开简介"}
            />
          </Field>
          <div className="flex gap-2">
            <Button
              disabled={saveProfile.isPending}
              onClick={() => saveProfile.mutate(true)}
            >
              启用或更新主页
            </Button>
            {library.data?.profile?.status === "active" && (
              <Button
                variant="outline"
                onClick={() => saveProfile.mutate(false)}
              >
                关闭主页
              </Button>
            )}
          </div>
        </FieldGroup>
      </section>
      <div className="mt-10 grid gap-12 lg:grid-cols-2">
        <section>
          <h2 className="font-semibold">已收入私人 Wiki</h2>
          <div className="mt-4 divide-y border-y">
            {library.data?.imports.map((item) => (
              <div key={item.id} className="py-4">
                <div className="flex justify-between gap-3">
                  {item.source_available ? (
                    <Link className="text-link" to={`/square/entries/${item.entry_id}`}>
                      {item.title}
                    </Link>
                  ) : (
                    <span>{item.title}</span>
                  )}
                  <Badge variant="outline">{item.status}</Badge>
                </div>
                <p className="mt-2 text-xs text-muted-foreground">
                  {item.workspace_name} · {item.private_path}
                </p>
                {!item.source_available && (
                  <p className="mt-2 text-sm text-muted-foreground">原公开来源已不可用；你的私人副本不受影响。</p>
                )}
              </div>
            ))}
          </div>
        </section>
        <section>
          <h2 className="font-semibold">订阅</h2>
          <div className="mt-4 divide-y border-y">
            {library.data?.subscriptions.map((item) => (
              <div
                key={item.entry_id}
                className="flex items-center justify-between gap-3 py-4"
              >
                <Link
                  className="text-link"
                  to={`/square/entries/${item.entry_id}`}
                >
                  {item.title}
                </Link>
                <Badge variant="outline">{item.status}</Badge>
              </div>
            ))}
          </div>
        </section>
        <section>
          <h2 className="font-semibold">纠错建议</h2>
          <div className="mt-4 divide-y border-y">
            {corrections.data?.map((item) => (
              <div key={item.id} className="py-4">
                <div className="flex justify-between gap-3">
                  <Link
                    className="text-link"
                    to={`/square/entries/${item.entry_id}`}
                  >
                    {item.kind}
                  </Link>
                  <Badge variant="outline">{item.status}</Badge>
                </div>
                <p className="mt-2 line-clamp-3 text-sm">{item.detail}</p>
                {item.author_response && (
                  <p className="mt-2 text-sm text-muted-foreground">
                    处理说明：{item.author_response}
                  </p>
                )}
              </div>
            ))}
          </div>
        </section>
        <section>
          <h2 className="flex items-center gap-2 font-semibold">
            <FlagIcon className="size-4" />
            举报记录
          </h2>
          <div className="mt-4 divide-y border-y">
            {reports.data?.map((item) => (
              <div key={item.id} className="py-4">
                <div className="flex justify-between gap-3">
                  <Link
                    className="text-link"
                    to={`/square/entries/${item.entry_id}`}
                  >
                    {item.reason_code}
                  </Link>
                  <Badge variant="outline">{item.status}</Badge>
                </div>
                {item.resolution_detail && (
                  <p className="mt-2 text-sm text-muted-foreground">
                    处理说明：{item.resolution_detail}
                  </p>
                )}
              </div>
            ))}
          </div>
        </section>
      </div>
    </main>
  )
}

export function SquareCorrectionsPage() {
  const corrections = useQuery({
    queryKey: ["my-public-corrections"],
    queryFn: () => apiGet<PublicCorrection[]>("/api/public/me/corrections"),
  })
  return (
    <main className="mx-auto max-w-4xl px-4 py-10">
      <p className="text-sm font-medium text-primary">我的广场互动</p>
      <div className="mt-2 flex flex-wrap items-end justify-between gap-4">
        <h1 className="text-3xl font-semibold">纠错处理进度</h1>
        <Button variant="outline" render={<Link to="/square/library" />}>
          返回广场资料库
        </Button>
      </div>
      <div className="mt-10 divide-y border-y">
        {corrections.data?.map((item) => (
          <article key={item.id} className="py-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <Link
                className="font-medium text-link"
                to={`/square/entries/${item.entry_id}`}
              >
                {item.kind}
              </Link>
              <Badge variant="outline">{item.status}</Badge>
            </div>
            <p className="mt-3 whitespace-pre-wrap text-sm">{item.detail}</p>
            <p className="mt-2 text-xs text-muted-foreground">
              目标公开修订 {item.revision_id.slice(0, 12)} · {new Date(item.created_at).toLocaleString()}
            </p>
            {item.author_response && (
              <p className="mt-3 border-l-2 pl-3 text-sm text-muted-foreground">
                处理说明：{item.author_response}
              </p>
            )}
          </article>
        ))}
        {!corrections.data?.length && (
          <p className="py-10 text-sm text-muted-foreground">尚未提交纠错建议</p>
        )}
      </div>
    </main>
  )
}
