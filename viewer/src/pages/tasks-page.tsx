import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import {
  BanIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  WorkflowIcon,
} from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, currentSystemStatus, queryKeys, type SystemStatus, type Task } from "@/lib/api"
import { StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { useSession } from "@/features/session-context"

const statusKind = (status: Task["status"]) =>
  status === "succeeded"
    ? "good"
    : status === "failed"
      ? "bad"
      : status === "cancelled"
        ? "neutral"
        : "warn"

function taskLabel(task: Task) {
  if (task.kind === "governance") return "AI 治理"
  return task.kind
}

function taskHref(task: Task) {
  return typeof task.payload.path === "string" ? `/${task.payload.path}` : null
}

function isTaskAvailable(status: SystemStatus | undefined, task: Task) {
  return Boolean(status?.remote_tasks.enabled && status.remote_tasks.allowed_kinds.includes(task.kind))
}

function taskStatusLabel(task: Task, status: SystemStatus | undefined) {
  if (task.status === "queued") return !status ? "等待服务状态" : isTaskAvailable(status, task) ? "排队中" : "生成服务未启用"
  if (task.status === "running") return "生成中"
  return task.status
}

const actionableTaskKinds = new Set(["generate", "supplement", "governance"])

export function TasksPage() {
  const { hasPermission } = useSession()
  const canWrite = hasPermission("wiki.write")
  const client = useQueryClient()
  const status = useQuery({ queryKey: queryKeys.status, queryFn: () => apiGet<SystemStatus>("/api/status") })
  const system = currentSystemStatus(status)
  const tasks = useQuery({
    queryKey: queryKeys.tasks,
    queryFn: () => apiGet<Task[]>("/api/tasks"),
    refetchInterval: (query) =>
      query.state.data?.some((item) =>
        item.status === "running" || (item.status === "queued" && isTaskAvailable(system, item))
      )
        ? 1500
        : false,
  })
  const action = useMutation({
    mutationFn: ({
      task,
      action,
    }: {
      task: Task
      action: "retry" | "cancel"
    }) => apiPost<Task>(`/api/tasks/${task.id}/${action}`, {}),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.tasks })
      toast.success("任务状态已更新")
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <PageFrame>
      <div className="mx-auto max-w-4xl">
        <PageTitle
          eyebrow="远端补证"
          title="任务"
          description="所有网页搜索、抓取和模型调用都在本地持久队列中运行；阅读与本地治理不等待网络。"
          actions={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void Promise.all([tasks.refetch(), status.refetch()])}
            >
              <RefreshCwIcon data-icon="inline-start" />
              刷新
            </Button>
          }
        />
        {status.isLoading ? <Alert className="mb-6"><AlertTitle>正在确认后台服务</AlertTitle><AlertDescription>任务记录仍可查看和取消。</AlertDescription></Alert> : null}
        {status.isError ? <Alert className="mb-6" variant="destructive"><AlertTitle>无法确认后台服务状态</AlertTitle><AlertDescription>{status.error.message}<Button className="mt-3" variant="outline" onClick={() => status.refetch()}>重试</Button></AlertDescription></Alert> : null}
        {system?.remote_tasks.blocked_queued ? <Alert className="mb-6"><AlertTitle>后台生成服务未启用</AlertTitle><AlertDescription>已有排队任务会原样保留，但不会自动执行。管理员启用对应 Worker 后，可回到这里刷新或取消任务。</AlertDescription></Alert> : null}
        {tasks.isLoading ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : tasks.data?.length ? (
          <div className="flex flex-col divide-y border-y">
            {tasks.data.map((task) => (
              <article key={task.id} className="py-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <h2 className="font-medium">{task.subject}</h2>
                      <StatusBadge
                        value={taskStatusLabel(task, system)}
                        kind={statusKind(task.status)}
                      />
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {taskLabel(task)} · 尝试 {task.attempts} 次 ·{" "}
                      {new Date(task.updated_at).toLocaleString()}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    {canWrite && actionableTaskKinds.has(task.kind) && task.error_type !== "feature_removed" && ((["failed", "cancelled"].includes(task.status) ||
                      task.result?.conflict === true) && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!isTaskAvailable(system, task)}
                        onClick={() => action.mutate({ task, action: "retry" })}
                      >
                        <RotateCcwIcon data-icon="inline-start" />
                        基于当前正文重试
                      </Button>
                    ))}
                    {canWrite && actionableTaskKinds.has(task.kind) && ["queued", "running"].includes(task.status) && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          action.mutate({ task, action: "cancel" })
                        }
                      >
                        <BanIcon data-icon="inline-start" />
                        取消
                      </Button>
                    )}
                    {taskHref(task) && (
                      <Button
                        size="sm"
                        render={<Link to={taskHref(task)!} />}
                      >
                        打开词条
                      </Button>
                    )}
                  </div>
                </div>
                {task.status === "running" && (
                  <Progress value={65} className="mt-4" />
                )}
                {task.status === "failed" && task.error_type && (
                  <Alert variant="destructive" className="mt-4">
                    <AlertTitle>{task.error_type}</AlertTitle>
                    <AlertDescription>{task.error_message}</AlertDescription>
                  </Alert>
                )}
                {task.result?.conflict === true && (
                  <Alert className="mt-4">
                    <AlertTitle>发现正文冲突</AlertTitle>
                    <AlertDescription>
                      没有覆盖已编辑正文；可基于当前版本重新补证。
                    </AlertDescription>
                  </Alert>
                )}
              </article>
            ))}
          </div>
        ) : (
          <Empty className="min-h-80">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <WorkflowIcon />
              </EmptyMedia>
              <EmptyTitle>没有任务</EmptyTitle>
              <EmptyDescription>
                本地证据不足或启用模型时，生成任务会出现在这里。
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
      </div>
    </PageFrame>
  )
}
