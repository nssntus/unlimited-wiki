import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArchiveRestoreIcon, Building2Icon, LogOutIcon, Trash2Icon, UserRoundIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, queryKeys, type WorkspaceSummary } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useSession } from "@/features/session-context"

type LifecycleAction = "suspend" | "restore" | "delete" | "leave"

export function WorkspaceDirectoryPage() {
  const { switchWorkspace, changeWorkspaceLifecycle, signOut } = useSession()
  return <WorkspaceSelectionGate onSwitch={switchWorkspace} onLifecycle={changeWorkspaceLifecycle} onSignOut={signOut} />
}

export function WorkspaceSelectionGate({
  onSwitch,
  onLifecycle,
  onSignOut,
}: {
  onSwitch: (workspaceId: string) => Promise<void>
  onLifecycle: (workspaceId: string, action: LifecycleAction) => Promise<void>
  onSignOut: () => Promise<void>
}) {
  const client = useQueryClient()
  const [leaveTarget, setLeaveTarget] = useState<WorkspaceSummary | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<WorkspaceSummary | null>(null)
  const [confirmation, setConfirmation] = useState("")
  const workspaces = useQuery({
    queryKey: [...queryKeys.workspaces, "all"],
    queryFn: () => apiGet<WorkspaceSummary[]>("/api/workspaces?include_inactive=1"),
    retry: false,
  })
  const switching = useMutation({ mutationFn: onSwitch, onError: (error) => toast.error(error.message) })
  const lifecycle = useMutation({
    mutationFn: ({ workspace, action }: { workspace: WorkspaceSummary; action: LifecycleAction }) => onLifecycle(workspace.id, action),
    onSuccess: async (_, variables) => {
      setLeaveTarget(null)
      setDeleteTarget(null)
      setConfirmation("")
      await client.invalidateQueries({ queryKey: queryKeys.workspaces })
      toast.success(variables.action === "restore" ? "空间已恢复，请选择进入" : variables.action === "delete" ? "空间已软删除" : "已退出团队空间")
    },
    onError: (error) => toast.error(error.message),
  })

  const rows = workspaces.data ?? []
  const active = rows.filter((workspace) => workspace.status === "active")
  const inactive = rows.filter((workspace) => workspace.status !== "active")

  return <main className="min-h-svh bg-background px-4 py-10 sm:px-6">
    <div className="mx-auto w-full max-w-2xl">
      <div className="flex items-start justify-between gap-4">
        <div><p className="text-sm text-muted-foreground">Wiki 空间</p><h1 className="mt-1 text-2xl font-semibold">选择要进入的空间</h1></div>
        <Button variant="ghost" onClick={() => void onSignOut()}><LogOutIcon data-icon="inline-start" />退出登录</Button>
      </div>

      {workspaces.isLoading ? <div className="mt-8 space-y-3"><Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" /></div> : workspaces.isError ? <Alert className="mt-8" variant="destructive"><AlertTitle>无法加载空间</AlertTitle><AlertDescription>{workspaces.error.message}<Button className="mt-3" variant="outline" onClick={() => workspaces.refetch()}>重试</Button></AlertDescription></Alert> : <>
        <section className="mt-8" aria-labelledby="active-workspaces"><h2 id="active-workspaces" className="text-sm font-medium text-muted-foreground">可用空间</h2>
          <div className="mt-2 divide-y border-y">{active.map((workspace) => <div key={workspace.id} className="flex min-w-0 items-center gap-3 py-4">
            {workspace.kind === "team" ? <Building2Icon className="size-5 shrink-0" /> : <UserRoundIcon className="size-5 shrink-0" />}
            <div className="min-w-0 flex-1"><div className="truncate font-medium">{workspace.display_name}</div><div className="text-sm text-muted-foreground">{workspace.kind === "team" ? "团队空间" : "个人空间"} · {roleLabel(workspace.role)}</div></div>
            <Button disabled={switching.isPending || lifecycle.isPending} onClick={() => switching.mutate(workspace.id)}>{switching.isPending && switching.variables === workspace.id ? <Spinner /> : "进入"}</Button>
          </div>)}{active.length === 0 && <p className="py-6 text-sm text-muted-foreground">当前没有可进入的空间。</p>}</div>
        </section>

        {inactive.length > 0 && <section className="mt-10" aria-labelledby="inactive-workspaces"><h2 id="inactive-workspaces" className="text-sm font-medium text-muted-foreground">不可用空间</h2>
          <div className="mt-2 divide-y border-y">{inactive.map((workspace) => <div key={workspace.id} className="flex min-w-0 flex-col gap-3 py-4 sm:flex-row sm:items-center">
            <div className="min-w-0 flex-1"><div className="flex items-center gap-2"><span className="truncate font-medium">{workspace.display_name}</span><Badge variant="outline">{workspace.status === "suspended" ? "已停用" : "已删除"}</Badge></div><div className="mt-1 text-sm text-muted-foreground">{roleLabel(workspace.role)}</div></div>
            <div className="flex flex-wrap gap-2">{workspace.can_restore && <Button variant="outline" disabled={lifecycle.isPending} onClick={() => lifecycle.mutate({ workspace, action: "restore" })}><ArchiveRestoreIcon data-icon="inline-start" />恢复</Button>}{workspace.can_leave && <Button variant="outline" disabled={lifecycle.isPending} onClick={() => setLeaveTarget(workspace)}>退出团队</Button>}{workspace.can_delete && <Button variant="destructive" disabled={lifecycle.isPending} onClick={() => setDeleteTarget(workspace)}><Trash2Icon data-icon="inline-start" />删除</Button>}</div>
          </div>)}</div>
        </section>}
      </>}
    </div>

    <AlertDialog open={Boolean(leaveTarget)} onOpenChange={(open) => !open && setLeaveTarget(null)}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>退出团队空间</AlertDialogTitle><AlertDialogDescription>退出 {leaveTarget?.display_name} 后，你将无法再访问其中的 Wiki、任务和模型配置。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction disabled={lifecycle.isPending} onClick={() => leaveTarget && lifecycle.mutate({ workspace: leaveTarget, action: "leave" })}>{lifecycle.isPending && <Spinner />}确认退出</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>

    <Dialog open={Boolean(deleteTarget)} onOpenChange={(open) => { if (!open) { setDeleteTarget(null); setConfirmation("") } }}><DialogContent><DialogHeader><DialogTitle>软删除团队空间</DialogTitle><DialogDescription>空间将不能再访问或恢复，但磁盘正文、模型配置和公开快照会保留。输入空间名称确认。</DialogDescription></DialogHeader><Field><FieldLabel htmlFor="delete-workspace-confirmation">空间名称</FieldLabel><Input id="delete-workspace-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></Field><DialogFooter><Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button><Button variant="destructive" disabled={!deleteTarget || confirmation !== deleteTarget.display_name || lifecycle.isPending} onClick={() => deleteTarget && lifecycle.mutate({ workspace: deleteTarget, action: "delete" })}>{lifecycle.isPending && <Spinner data-icon="inline-start" />}删除空间</Button></DialogFooter></DialogContent></Dialog>
  </main>
}

function roleLabel(role: WorkspaceSummary["role"]) {
  return role === "owner" ? "Owner" : role === "editor" ? "Editor" : "Viewer"
}
