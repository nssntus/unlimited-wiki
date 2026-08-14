import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams } from "react-router-dom"
import { CheckIcon, EyeIcon, FileWarningIcon, RotateCcwIcon, Trash2Icon, XIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, type AdminPublicEntry, type PublicReport, type Submission } from "@/lib/api"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { Button } from "@/components/ui/button"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"

export function AdminReviewsPage() {
  const rows = useQuery({ queryKey: queryKeys.adminReviews, queryFn: () => apiGet<Submission[]>("/api/admin/submissions?status=pending_admin"), refetchInterval: 3000 })
  return <main className="mx-auto max-w-6xl px-4 py-10"><p className="text-sm font-medium text-primary">Admin</p><h1 className="mt-2 text-3xl font-semibold">待人工审核</h1><p className="mt-3 text-muted-foreground">这里只显示投稿快照，不提供任何私有空间入口。</p>
    {rows.isLoading ? <Skeleton className="mt-10 h-80" /> : <div className="mt-10 divide-y border-y">{rows.data?.map((item) => <Link key={item.id} to={`/admin/reviews/${item.id}`} className="flex flex-wrap items-center justify-between gap-4 py-5 hover:bg-muted/30"><div><h2 className="font-medium">{item.snapshot.title}</h2><p className="mt-1 text-xs text-muted-foreground">作者 {item.owner_id?.slice(0, 8)} · {new Date(item.created_at).toLocaleString()}</p></div><StatusBadge value="等待审核" kind="warn" /></Link>)}{!rows.data?.length && <div className="py-16 text-center text-sm text-muted-foreground">当前没有待审核投稿</div>}</div>}
  </main>
}

export function AdminReviewDetailPage() {
  const { id = "" } = useParams(); const client = useQueryClient(); const navigate = useNavigate(); const [reason, setReason] = useState("")
  const item = useQuery({ queryKey: ["admin-review", id], queryFn: () => apiGet<Submission>(`/api/admin/submissions/${id}`) })
  const decide = useMutation({ mutationFn: (decision: string) => apiPost(`/api/admin/submissions/${id}/decision`, { decision, reason }), onSuccess: async () => { await client.invalidateQueries({ queryKey: queryKeys.adminReviews }); toast.success("审核决定已记录"); navigate("/admin/reviews", { replace: true }) }, onError: (error) => toast.error(error.message) })
  if (item.isLoading) return <main className="mx-auto max-w-6xl px-4 py-10"><Skeleton className="h-[70svh]" /></main>
  if (!item.data) return <main className="p-10">投稿不存在。</main>
  const data = item.data
  return <main className="mx-auto grid max-w-6xl gap-8 px-4 py-10 lg:grid-cols-[minmax(0,1fr)_320px]"><section><div className="flex flex-wrap gap-2"><StatusBadge value={data.status} kind="warn" /><StatusBadge value={data.content_hash.slice(0, 16)} /></div><h1 className="mt-4 text-3xl font-semibold">{data.snapshot.title}</h1><div className="mt-8 rounded-lg border p-5"><MarkdownContent markdown={data.snapshot.markdown.replace(/^#\s+.*\n/, "")} fromPath="" publicMode /></div></section>
    <aside className="flex flex-col gap-6"><div><h2 className="text-sm font-semibold">AI 预审</h2><p className="mt-2 text-sm text-muted-foreground">{data.ai_report?.summary || "未提供摘要"}</p></div><Field><FieldLabel htmlFor="review-reason">审核理由</FieldLabel><Textarea id="review-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="必须填写可公开的审核理由" /></Field><div className="grid gap-2"><Button disabled={!reason || decide.isPending} onClick={() => decide.mutate("approve")}><CheckIcon data-icon="inline-start" />通过并发布</Button><Button variant="outline" disabled={!reason || decide.isPending} onClick={() => decide.mutate("request_changes")}><FileWarningIcon data-icon="inline-start" />退回修改</Button><Button variant="destructive" disabled={!reason || decide.isPending} onClick={() => decide.mutate("reject")}><XIcon data-icon="inline-start" />拒绝投稿</Button></div><p className="text-xs text-muted-foreground">Admin 可以审核自己的投稿；作者、审核人、理由及是否自审都会写入审计记录。</p></aside>
  </main>
}

export function AdminReportsPage() {
  const client = useQueryClient(); const [reasons, setReasons] = useState<Record<string, string>>({})
  const reports = useQuery({ queryKey: ["admin-reports"], queryFn: () => apiGet<PublicReport[]>("/api/admin/reports"), refetchInterval: 3000 })
  const decide = useMutation({ mutationFn: ({ id, action }: { id: string; action: string }) => apiPost(`/api/admin/reports/${id}/decision`, { action, reason: reasons[id] || "已人工核验" }), onSuccess: async () => { await client.invalidateQueries({ queryKey: ["admin-reports"] }); toast.success("举报已处理") }, onError: (error) => toast.error(error.message) })
  return <main className="mx-auto max-w-5xl px-4 py-10"><p className="text-sm font-medium text-primary">Admin</p><h1 className="mt-2 text-3xl font-semibold">举报处理</h1><div className="mt-10 divide-y border-y">{reports.data?.map((report) => <div key={report.id} className="grid gap-4 py-5 md:grid-cols-[1fr_280px]"><div><div className="font-medium">{report.reason_code}</div><p className="mt-2 text-sm text-muted-foreground">{report.detail || "未提供补充说明"}</p><code className="mt-3 block text-xs text-muted-foreground">公开词条 {report.entry_id}</code></div><div className="space-y-2"><Textarea value={reasons[report.id] || ""} placeholder="处理理由" onChange={(event) => setReasons((current) => ({ ...current, [report.id]: event.target.value }))} /><div className="flex justify-end gap-2"><Button size="sm" variant="outline" onClick={() => decide.mutate({ id: report.id, action: "dismiss" })}>驳回举报</Button><Button size="sm" variant="destructive" onClick={() => decide.mutate({ id: report.id, action: "remove" })}>下架词条</Button></div></div></div>)}{!reports.data?.length && <div className="py-16 text-center text-sm text-muted-foreground">没有待处理举报</div>}</div></main>
}

export function AdminContentPage() {
  const client = useQueryClient()
  const [status, setStatus] = useState<"published" | "removed_by_admin">("published")
  const [preview, setPreview] = useState<AdminPublicEntry | null>(null)
  const [target, setTarget] = useState<AdminPublicEntry | null>(null)
  const [reason, setReason] = useState("")
  const rows = useQuery({
    queryKey: queryKeys.adminPublicEntries(status),
    queryFn: () => apiGet<AdminPublicEntry[]>(`/api/admin/public-entries?status=${status}`),
  })
  const moderate = useMutation({
    mutationFn: () => apiPost(`/api/admin/public-entries/${target!.id}/${target!.status === "published" ? "remove" : "relist"}`, { reason }),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.adminPublicEntries("published") }),
        client.invalidateQueries({ queryKey: queryKeys.adminPublicEntries("removed_by_admin") }),
        client.invalidateQueries({ queryKey: queryKeys.square }),
      ])
      toast.success(target?.status === "published" ? "内容已下架并通知作者" : "内容已重新上架并通知作者")
      setTarget(null); setReason("")
    },
    onError: (error) => toast.error(error.message),
  })

  return <main className="mx-auto max-w-6xl px-4 py-10"><p className="text-sm font-medium text-primary">Admin</p><h1 className="mt-2 text-3xl font-semibold">公开内容管理</h1><p className="mt-3 text-muted-foreground">审核者可以查看全部公开与下架快照；操作不会进入或修改作者的私有 Wiki。</p>
    <Tabs value={status} onValueChange={(value) => setStatus(value as typeof status)} className="mt-8"><TabsList><TabsTrigger value="published">已发布</TabsTrigger><TabsTrigger value="removed_by_admin">已下架</TabsTrigger></TabsList></Tabs>
    {rows.isLoading ? <Skeleton className="mt-6 h-72" /> : rows.data?.length ? <Table className="mt-6"><TableHeader><TableRow><TableHead>词条</TableHead><TableHead>作者</TableHead><TableHead>版本</TableHead><TableHead>处理信息</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{rows.data.map((entry) => <TableRow key={entry.id}><TableCell><div className="max-w-72 truncate font-medium">{entry.snapshot.title}</div><code className="text-xs text-muted-foreground">{entry.content_hash.slice(0, 12)}</code></TableCell><TableCell>{entry.author_nickname}</TableCell><TableCell>v{entry.version}</TableCell><TableCell><div className="max-w-72 whitespace-normal text-xs text-muted-foreground">{entry.moderation_reason || new Date(entry.published_at).toLocaleString()}</div></TableCell><TableCell><div className="flex justify-end gap-2"><Button size="sm" variant="outline" onClick={() => setPreview(entry)}><EyeIcon data-icon="inline-start" />预览</Button><Button size="sm" variant={entry.status === "published" ? "destructive" : "default"} onClick={() => setTarget(entry)}>{entry.status === "published" ? <Trash2Icon data-icon="inline-start" /> : <RotateCcwIcon data-icon="inline-start" />}{entry.status === "published" ? "手动下架" : "重新上架"}</Button></div></TableCell></TableRow>)}</TableBody></Table> : <div className="mt-6 border-y py-16 text-center text-sm text-muted-foreground">{status === "published" ? "当前没有已发布内容" : "当前没有已下架内容"}</div>}
    <Dialog open={Boolean(preview)} onOpenChange={(open) => !open && setPreview(null)}><DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl"><DialogHeader><DialogTitle>{preview?.snapshot.title}</DialogTitle><DialogDescription>不可变公开快照 · v{preview?.version} · {preview?.author_nickname}</DialogDescription></DialogHeader>{preview && <MarkdownContent markdown={preview.snapshot.markdown} fromPath="" publicMode />}</DialogContent></Dialog>
    <AlertDialog open={Boolean(target)} onOpenChange={(open) => { if (!open) { setTarget(null); setReason("") } }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{target?.status === "published" ? "手动下架该内容？" : "重新上架该内容？"}</AlertDialogTitle><AlertDialogDescription>{target?.status === "published" ? "内容会立即从广场列表和公开详情消失，作者会收到理由和修改后重新申请的入口。" : "将直接恢复当前不可变公开版本，作者会收到重新上架通知。"}</AlertDialogDescription></AlertDialogHeader><Field><FieldLabel htmlFor="moderation-reason">处理理由</FieldLabel><Textarea id="moderation-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="必须填写可供作者查看的理由" /></Field><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction variant={target?.status === "published" ? "destructive" : "default"} disabled={!reason.trim() || moderate.isPending} onClick={() => moderate.mutate()}>{target?.status === "published" ? "确认下架" : "确认重新上架"}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </main>
}
