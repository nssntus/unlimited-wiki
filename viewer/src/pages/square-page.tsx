import { useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import { BookOpenIcon, CalendarIcon, FlagIcon, ShieldCheckIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, type PublicEntry, type PublicEntrySummary } from "@/lib/api"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { markdownToc, scrollToHeading } from "@/lib/markdown-toc"

type TocItem = ReturnType<typeof markdownToc>[number]

function publicBodyMarkdown(markdown: string) {
  return markdown
    .replace(/^#\s+.*\n/, "")
    .replace(/^(#{1,5})(\s+)/gm, "$1#$2")
}

function PublicEntryToc({ headings, className = "" }: { headings: TocItem[]; className?: string }) {
  if (!headings.length) return null
  return <nav aria-label="词条目录" className={className}>
    <h2 className="text-sm font-semibold">目录</h2>
    <div className="mt-4 flex max-h-[40svh] flex-col gap-2 overflow-y-auto pr-2 text-sm">
      {headings.map((heading) => <button key={heading.id} type="button" onClick={() => scrollToHeading(heading.id)} className="text-left leading-5 text-muted-foreground hover:text-foreground" style={{ paddingInlineStart: `${(heading.depth - 2) * 12}px` }}>{heading.title}</button>)}
    </div>
  </nav>
}

export function SquarePage() {
  const entries = useQuery({ queryKey: queryKeys.square, queryFn: () => apiGet<PublicEntrySummary[]>("/api/public/entries") })
  return <main className="mx-auto w-full max-w-6xl px-4 py-10 md:py-14">
    <div className="max-w-2xl"><p className="text-sm font-medium text-primary">公开知识</p><h1 className="mt-2 text-3xl font-semibold">Wiki 广场</h1><p className="mt-3 text-muted-foreground">这里只展示经过平台 AI 预审和 Admin 人工审核的不可变版本。</p></div>
    {entries.isLoading ? <div className="mt-10 grid gap-4 md:grid-cols-2"><Skeleton className="h-40" /><Skeleton className="h-40" /></div> : entries.data?.length ? <div className="mt-10 grid gap-4 md:grid-cols-2">
      {entries.data.map((entry) => <Link key={entry.id} to={`/square/${entry.id}`} className="rounded-lg border p-5 transition-colors hover:border-primary/40 hover:bg-muted/40">
        <div className="flex items-center justify-between gap-4"><Badge variant="outline">{entry.category}</Badge><span className="text-xs text-muted-foreground">v{entry.version}</span></div>
        <h2 className="mt-4 text-lg font-semibold">{entry.title}</h2><p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{entry.summary || "查看经过审核的公开词条。"}</p>
        <div className="mt-5 flex items-center gap-4 text-xs text-muted-foreground"><span>{entry.attribution}</span><span className="flex items-center gap-1"><CalendarIcon className="size-3.5" />{new Date(entry.published_at).toLocaleDateString()}</span></div>
      </Link>)}
    </div> : <Empty className="mt-12 min-h-72"><EmptyHeader><EmptyMedia variant="icon"><BookOpenIcon /></EmptyMedia><EmptyTitle>广场还没有公开词条</EmptyTitle><EmptyDescription>投稿只有在 AI 预审和 Admin 审核均通过后才会出现。</EmptyDescription></EmptyHeader></Empty>}
  </main>
}

export function PublicEntryPage() {
  const { id = "" } = useParams()
  const [reportOpen, setReportOpen] = useState(false)
  const [reportDetail, setReportDetail] = useState("")
  const entry = useQuery({ queryKey: queryKeys.publicEntry(id), queryFn: () => apiGet<PublicEntry>(`/api/public/entries/${id}`), enabled: Boolean(id), retry: false })
  const report = useMutation({ mutationFn: () => apiPost(`/api/public/entries/${id}/reports`, { reason_code: "content_concern", detail: reportDetail }), onSuccess: () => { setReportOpen(false); setReportDetail(""); toast.success("举报已提交给 Admin") }, onError: (error) => toast.error(error.message) })
  if (entry.isLoading) return <main className="mx-auto max-w-3xl px-4 py-12"><Skeleton className="h-96" /></main>
  if (!entry.data) return <main className="mx-auto max-w-3xl px-4 py-12"><Empty><EmptyHeader><EmptyMedia variant="icon"><BookOpenIcon /></EmptyMedia><EmptyTitle>公开词条不可用</EmptyTitle><EmptyDescription>它可能已被作者撤回或由 Admin 下架。</EmptyDescription></EmptyHeader></Empty></main>
  const data = entry.data
  const bodyMarkdown = publicBodyMarkdown(data.snapshot.markdown)
  const headings = markdownToc(bodyMarkdown)
  return <main className="mx-auto max-w-6xl px-4 py-10 md:py-14">
    <div className="grid min-w-0 gap-12 lg:grid-cols-[minmax(0,760px)_minmax(200px,260px)] lg:justify-between">
      <article className="min-w-0"><div className="flex flex-wrap gap-2"><StatusBadge value="已审核发布" kind="good" /><StatusBadge value={`版本 ${data.version}`} /></div>
        <h1 className="mt-5 text-3xl font-semibold">{data.snapshot.title}</h1><div className="mt-3 flex flex-wrap gap-4 text-sm text-muted-foreground"><span>{data.attribution}</span><span>{new Date(data.published_at).toLocaleString()}</span></div>
        <div className="mt-8 flex flex-wrap items-center justify-between gap-3 border-y py-3 text-xs text-muted-foreground"><span className="flex items-center gap-2"><ShieldCheckIcon className="size-4 text-success" />公开快照哈希 <code className="break-all">{data.content_hash.slice(0, 16)}</code></span><Dialog open={reportOpen} onOpenChange={setReportOpen}><DialogTrigger render={<Button size="sm" variant="ghost" />}><FlagIcon data-icon="inline-start" />举报</DialogTrigger><DialogContent><DialogHeader><DialogTitle>举报公开词条</DialogTitle><DialogDescription>举报只提交给 Admin，不会修改作者的私有 Wiki。</DialogDescription></DialogHeader><Textarea value={reportDetail} onChange={(event) => setReportDetail(event.target.value)} placeholder="说明具体问题" /><DialogFooter><Button variant="outline" onClick={() => setReportOpen(false)}>取消</Button><Button disabled={report.isPending} onClick={() => report.mutate()}>提交举报</Button></DialogFooter></DialogContent></Dialog></div>
        <PublicEntryToc headings={headings} className="mt-8 border-b pb-6 lg:hidden" />
        <MarkdownContent className="mt-10" markdown={bodyMarkdown} fromPath="" publicMode />
      </article>
      <aside className="hidden lg:block"><PublicEntryToc headings={headings} className="sticky top-8 border-l pl-6" /></aside>
    </div>
  </main>
}
