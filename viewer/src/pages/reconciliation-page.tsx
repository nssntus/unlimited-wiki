import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { RefreshCwIcon } from "lucide-react"
import { toast } from "sonner"

import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { OperationRollbackAlert } from "@/features/operation-rollback-alert"
import { apiGet, apiPost, queryKeys } from "@/lib/api"

type ReconciliationPayload = {
  path?: string
  expected_directory?: string
  article_id?: string
  category_id?: string
  paths?: string[]
  application_record?: Record<string, unknown> | null
  disk_state?: { path?: string; exists?: boolean }
  affected_paths?: string[]
  available_actions: string[]
}
type Item = { id: string; kind: string; payload: ReconciliationPayload }
type Preview = {
  preview_id: string
  decision: string
  changes: ReconciliationPayload & { kind: string }
  conflicts: { kind: string }[]
  can_commit: boolean
}

export function ReconciliationPage() {
  const client = useQueryClient()
  const [preview, setPreview] = useState<Preview | null>(null)
  const [operationId, setOperationId] = useState<string | null>(null)
  const items = useQuery({
    queryKey: queryKeys.reconciliation,
    queryFn: () => apiGet<{ items: Item[] }>("/api/reconciliation"),
  })
  const scan = useMutation({
    mutationFn: () => apiPost<{ count: number }>("/api/reconciliation/scan", {}),
    onSuccess: async (result) => {
      toast.success(`扫描完成，发现 ${result.count} 个待确认项`)
      await client.invalidateQueries({ queryKey: queryKeys.reconciliation })
    },
    onError: (error) => toast.error(error.message),
  })
  const makePreview = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) =>
      apiPost<Preview>("/api/reconciliation/preview", { reconciliation_id: id, decision }, false),
    onSuccess: setPreview,
    onError: (error) => toast.error(error.message),
  })
  const commit = useMutation({
    mutationFn: () => apiPost<{ operation_id: string | null }>("/api/reconciliation/commit", { preview_id: preview?.preview_id }),
    onSuccess: async (result) => {
      if (result.operation_id) setOperationId(result.operation_id)
      toast.success(result.operation_id ? `对账完成 · ${result.operation_id}` : "已暂缓该变更")
      setPreview(null)
      await client.invalidateQueries({ queryKey: queryKeys.reconciliation })
    },
    onError: async (error) => {
      setPreview(null)
      await items.refetch()
      toast.error(error.message)
    },
  })

  if (items.isLoading) return <PageFrame><Skeleton className="h-80" /></PageFrame>
  if (items.isError) {
    return (
      <PageFrame>
        <Alert variant="destructive">
          <AlertTitle>文件对账加载失败</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{items.error.message}</span>
            <Button variant="outline" onClick={() => void items.refetch()}><RefreshCwIcon data-icon="inline-start" />重试</Button>
          </AlertDescription>
        </Alert>
      </PageFrame>
    )
  }

  return (
    <PageFrame>
      <div className="mx-auto max-w-5xl">
        <PageTitle
          title="文件对账"
          description="应用只检测外部目录和文件变化，不会自动采纳或恢复。"
          actions={<Button onClick={() => scan.mutate()} disabled={scan.isPending}><RefreshCwIcon data-icon="inline-start" />扫描文件系统</Button>}
        />
        <OperationRollbackAlert operationId={operationId} label="文件对账" onRolledBack={() => setOperationId(null)} />
        {!items.data?.items.length ? (
          <Alert><AlertTitle>没有待处理的外部变更</AlertTitle><AlertDescription>扫描不会移动文件或创建正式分类。</AlertDescription></Alert>
        ) : (
          <div className="divide-y border-y">
            {items.data.items.map((item) => (
              <section key={item.id} className="py-5">
                <h2 className="font-medium">{item.kind}</h2>
                <div className="mt-3 grid gap-4 text-sm md:grid-cols-3">
                  <div><p className="font-medium">应用记录</p><code className="mt-1 block break-all text-muted-foreground">{item.payload.application_record ? JSON.stringify(item.payload.application_record) : "未登记"}</code></div>
                  <div><p className="font-medium">磁盘现状</p><code className="mt-1 block break-all text-muted-foreground">{item.payload.disk_state ? JSON.stringify(item.payload.disk_state) : item.payload.path ?? "未知"}</code></div>
                  <div><p className="font-medium">影响文件</p>{item.payload.affected_paths?.length ? <ul className="mt-1 text-muted-foreground">{item.payload.affected_paths.map((path) => <li key={path} className="break-all">{path}</li>)}</ul> : <p className="mt-1 text-muted-foreground">无受管文件</p>}</div>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  {item.payload.available_actions.map((decision) => (
                    <Button key={decision} size="sm" variant={decision === "defer" ? "ghost" : "outline"} onClick={() => makePreview.mutate({ id: item.id, decision })}>
                      {decision === "adopt" ? "采纳磁盘变更" : decision === "restore" ? "恢复应用记录" : "暂缓"}
                    </Button>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
        <Dialog open={Boolean(preview)} onOpenChange={(open) => !open && setPreview(null)}>
          <DialogContent>
            <DialogHeader><DialogTitle>确认对账动作</DialogTitle><DialogDescription>系统不会自动处理外部变更。请核对后再提交。</DialogDescription></DialogHeader>
            <Alert><AlertTitle>{preview?.changes.kind}</AlertTitle><AlertDescription><code className="break-all">{preview?.changes.path}</code><span className="mt-2 block">选择：{preview?.decision}</span></AlertDescription></Alert>
            {preview?.conflicts.length ? <Alert variant="destructive"><AlertTitle>存在冲突</AlertTitle><AlertDescription>{preview.conflicts.map((item) => item.kind).join("、")}</AlertDescription></Alert> : null}
            <DialogFooter><Button variant="outline" onClick={() => setPreview(null)}>取消</Button><Button disabled={!preview?.can_commit || commit.isPending} onClick={() => commit.mutate()}>确认提交</Button></DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </PageFrame>
  )
}
