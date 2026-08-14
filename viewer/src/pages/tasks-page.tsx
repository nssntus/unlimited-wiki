import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { BanIcon, RefreshCwIcon, RotateCcwIcon, WorkflowIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, type Task } from "@/lib/api"
import { StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"

const statusKind = (status: Task["status"]) => status === "succeeded" ? "good" : status === "failed" ? "bad" : status === "cancelled" ? "neutral" : "warn"

export function TasksPage() {
  const client = useQueryClient()
  const tasks = useQuery({ queryKey: queryKeys.tasks, queryFn: () => apiGet<Task[]>("/api/tasks"), refetchInterval: (query) => query.state.data?.some((item) => ["queued", "running"].includes(item.status)) ? 1500 : false })
  const action = useMutation({ mutationFn: ({ task, action }: { task: Task; action: "retry" | "cancel" }) => apiPost<Task>(`/api/tasks/${task.id}/${action}`, {}), onSuccess: () => { void client.invalidateQueries({ queryKey: queryKeys.tasks }); toast.success("任务状态已更新") }, onError: (error) => toast.error(error.message) })
  return <PageFrame><div className="mx-auto max-w-4xl"><PageTitle eyebrow="远端补证" title="任务" description="所有网页搜索、抓取和模型调用都在本地持久队列中运行；阅读与本地治理不等待网络。" actions={<Button variant="outline" size="sm" onClick={() => void tasks.refetch()}><RefreshCwIcon data-icon="inline-start" />刷新</Button>} />
    {tasks.isLoading ? <div className="flex flex-col gap-3"><Skeleton className="h-28 w-full" /><Skeleton className="h-28 w-full" /></div> : tasks.data?.length ? <div className="flex flex-col divide-y border-y">{tasks.data.map((task) => <article key={task.id} className="py-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2"><h2 className="font-medium">{task.subject}</h2><StatusBadge value={task.status} kind={statusKind(task.status)} /></div><p className="mt-1 text-sm text-muted-foreground">{task.kind} · 尝试 {task.attempts} 次 · {new Date(task.updated_at).toLocaleString()}</p></div><div className="flex gap-2">{(["failed","cancelled"].includes(task.status) || task.result?.conflict === true) && <Button size="sm" variant="outline" onClick={() => action.mutate({ task, action: "retry" })}><RotateCcwIcon data-icon="inline-start" />基于当前正文重试</Button>}{["queued","running"].includes(task.status) && <Button size="sm" variant="outline" onClick={() => action.mutate({ task, action: "cancel" })}><BanIcon data-icon="inline-start" />取消</Button>}{typeof task.payload.path === "string" && <Button size="sm" render={<Link to={`/${task.payload.path}`} />}>打开词条</Button>}</div></div>{task.status === "running" && <Progress value={65} className="mt-4" />}{task.status === "failed" && task.error_type && <Alert variant="destructive" className="mt-4"><AlertTitle>{task.error_type}</AlertTitle><AlertDescription>{task.error_message}</AlertDescription></Alert>}{task.result?.conflict === true && <Alert className="mt-4"><AlertTitle>发现正文冲突</AlertTitle><AlertDescription>没有覆盖已编辑正文；可基于当前版本重新补证。</AlertDescription></Alert>}</article>)}</div> : <Empty className="min-h-80"><EmptyHeader><EmptyMedia variant="icon"><WorkflowIcon /></EmptyMedia><EmptyTitle>没有任务</EmptyTitle><EmptyDescription>本地证据不足或启用模型时，生成任务会出现在这里。</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame>
}
