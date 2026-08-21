import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import { Clock3Icon, ExternalLinkIcon, RotateCcwIcon, SendIcon, XIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, currentSystemStatus, queryKeys, type Submission, type SystemStatus } from "@/lib/api"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"

const labels: Record<string, string> = { ai_queued: "等待 AI 预审", ai_reviewing: "AI 预审中", ai_failed: "AI 预审失败", needs_revision: "需要修改", ai_rejected: "AI 拒绝", pending_admin: "等待人工审核", admin_changes_requested: "Admin 退回修改", admin_rejected: "Admin 拒绝", approved: "已发布", withdrawn: "已撤回" }

function reviewLabel(status: string, system: SystemStatus | undefined) {
  if (status === "ai_queued" && !system) return "等待 AI 服务状态"
  if (status === "ai_queued" && system && !system.platform_review.enabled) return "AI 预审服务未启用"
  return labels[status] || status
}

function shouldPollReview(status: string, system: SystemStatus | undefined) {
  return status === "pending_admin" || status === "ai_reviewing" || (status === "ai_queued" && Boolean(system?.platform_review.enabled))
}

export function SubmissionsPage() {
  const status = useQuery({ queryKey: queryKeys.status, queryFn: () => apiGet<SystemStatus>("/api/status") })
  const system = currentSystemStatus(status)
  const rows = useQuery({ queryKey: queryKeys.submissions, queryFn: () => apiGet<Submission[]>("/api/submissions"), refetchInterval: (query) => query.state.data?.some((item) => shouldPollReview(item.status, system)) ? 1500 : false })
  return <PageFrame><div className="mx-auto max-w-4xl"><PageTitle eyebrow="Wiki 广场" title="我的投稿" description="每次投稿都是独立快照；退回后请修改私有词条并创建新投稿。" />
    {status.isLoading ? <Alert className="mb-6"><Clock3Icon /><AlertTitle>正在确认 AI 预审服务</AlertTitle><AlertDescription>投稿记录仍可查看。</AlertDescription></Alert> : null}
    {status.isError ? <Alert className="mb-6" variant="destructive"><AlertTitle>无法确认 AI 预审服务</AlertTitle><AlertDescription>{status.error.message}<Button className="mt-3" variant="outline" onClick={() => status.refetch()}>重试</Button></AlertDescription></Alert> : null}
    {system?.platform_review.blocked_queued ? <Alert className="mb-6"><Clock3Icon /><AlertTitle>AI 预审服务未启用</AlertTitle><AlertDescription>已排队投稿会保持原状，不会自动外发；管理员启用 Worker 后再继续处理。</AlertDescription></Alert> : null}
    {rows.isLoading ? <Skeleton className="h-80" /> : rows.data?.length ? <div className="divide-y border-y">{rows.data.map((item) => <Link key={item.id} to={`/submissions/${item.id}`} className="flex flex-wrap items-center justify-between gap-4 py-5 hover:bg-muted/30"><div><h2 className="font-medium">{item.snapshot.title}</h2><p className="mt-1 text-xs text-muted-foreground">{new Date(item.created_at).toLocaleString()} · {item.content_hash.slice(0, 12)}</p></div><StatusBadge value={reviewLabel(item.status, system)} kind={item.status === "approved" ? "good" : item.status.includes("failed") || item.status.includes("rejected") ? "bad" : "warn"} /></Link>)}</div> : <Empty><EmptyHeader><EmptyMedia variant="icon"><SendIcon /></EmptyMedia><EmptyTitle>还没有投稿</EmptyTitle><EmptyDescription>从私有词条页选择“分享到广场”。</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame>
}

export function SubmissionDetailPage() {
  const { id = "" } = useParams(); const client = useQueryClient()
  const status = useQuery({ queryKey: queryKeys.status, queryFn: () => apiGet<SystemStatus>("/api/status") })
  const system = currentSystemStatus(status)
  const item = useQuery({ queryKey: queryKeys.submission(id), queryFn: () => apiGet<Submission>(`/api/submissions/${id}`), refetchInterval: (query) => shouldPollReview(query.state.data?.status || "", system) ? 1500 : false })
  const action = useMutation({ mutationFn: (kind: "ai-retry" | "withdraw") => apiPost<Submission>(`/api/submissions/${id}/${kind}`, {}), onSuccess: async () => { await client.invalidateQueries({ queryKey: queryKeys.submission(id) }); await client.invalidateQueries({ queryKey: queryKeys.submissions }); toast.success("投稿状态已更新") }, onError: (error) => toast.error(error.message) })
  if (item.isLoading) return <PageFrame><Skeleton className="mx-auto h-96 max-w-3xl" /></PageFrame>
  if (!item.data) return <PageFrame><Empty><EmptyHeader><EmptyTitle>找不到投稿</EmptyTitle><EmptyDescription>它不存在或不属于当前账号。</EmptyDescription></EmptyHeader></Empty></PageFrame>
  const data = item.data
  const polling = shouldPollReview(data.status, system)
  return <PageFrame><div className="mx-auto max-w-3xl"><PageTitle eyebrow={<Link to="/submissions">我的投稿</Link>} title={data.snapshot.title} description={<div className="flex flex-wrap gap-2"><StatusBadge value={reviewLabel(data.status, system)} kind={data.status === "approved" ? "good" : "warn"} /><StatusBadge value={data.content_hash.slice(0, 16)} /></div>} actions={<>{data.status === "ai_failed" && <Button variant="outline" disabled={!system?.platform_review.enabled} onClick={() => action.mutate("ai-retry")}><RotateCcwIcon data-icon="inline-start" />重试 AI 预审</Button>}{!new Set(["withdrawn", "admin_rejected", "ai_rejected"]).has(data.status) && <Button variant="outline" onClick={() => action.mutate("withdraw")}><XIcon data-icon="inline-start" />撤回</Button>}{data.public_entry_id && <Button render={<Link to={`/square/${data.public_entry_id}`} />}><ExternalLinkIcon data-icon="inline-start" />查看公开版</Button>}</>} />
    {status.isLoading && <Alert className="mb-8"><Clock3Icon /><AlertTitle>正在确认 AI 预审服务</AlertTitle><AlertDescription>投稿快照保持不变。</AlertDescription></Alert>}
    {status.isError && <Alert className="mb-8" variant="destructive"><AlertTitle>无法确认 AI 预审服务</AlertTitle><AlertDescription>{status.error.message}<Button className="mt-3" variant="outline" onClick={() => status.refetch()}>重试</Button></AlertDescription></Alert>}
    {data.status === "ai_queued" && system && !system.platform_review.enabled ? <Alert className="mb-8"><Clock3Icon /><AlertTitle>AI 预审服务未启用</AlertTitle><AlertDescription>投稿快照已保留，但不会自动外发。你仍可撤回投稿。</AlertDescription></Alert> : polling && <Alert className="mb-8"><Clock3Icon /><AlertTitle>{labels[data.status]}</AlertTitle><AlertDescription>状态会自动刷新，网络任务不会阻塞私有 Wiki 阅读。</AlertDescription></Alert>}
    {data.ai_report && <Alert className="mb-8"><AlertTitle>AI 预审报告</AlertTitle><AlertDescription>{data.ai_report.summary || data.ai_report.decision}</AlertDescription></Alert>}
    {data.reason && <Alert className="mb-8" variant={data.status.includes("rejected") ? "destructive" : "default"}><AlertTitle>审核意见</AlertTitle><AlertDescription>{data.reason}</AlertDescription></Alert>}
    <MarkdownContent markdown={data.snapshot.markdown.replace(/^#\s+.*\n/, "")} fromPath="" publicMode />
  </div></PageFrame>
}
