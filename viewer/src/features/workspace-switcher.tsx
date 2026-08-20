import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Building2Icon, CheckIcon, ChevronsUpDownIcon, ListTreeIcon, PlusIcon, UserRoundIcon } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, type WorkspaceSummary } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { useSession } from "@/features/session-context"
import { useUnsavedChanges } from "@/features/unsaved-changes-context"

export function WorkspaceSwitcher() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const { session, switchWorkspace, switchingWorkspace } = useSession()
  const { runAfterDiscard } = useUnsavedChanges()
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState("")
  const workspaces = useQuery({
    queryKey: queryKeys.workspaces,
    queryFn: () => apiGet<WorkspaceSummary[]>("/api/workspaces"),
  })
  const switching = useMutation({
    mutationFn: switchWorkspace,
    onError: (error) => toast.error(error.message),
  })
  const create = useMutation({
    mutationFn: () => apiPost<WorkspaceSummary>("/api/workspaces", { display_name: name }),
    onSuccess: async (workspace) => {
      await client.invalidateQueries({ queryKey: queryKeys.workspaces })
      setCreateOpen(false)
      setName("")
      toast.success("团队空间已创建")
      await switching.mutateAsync(workspace.id)
    },
  })
  const current = session?.workspace

  return <>
    <DropdownMenu>
      <DropdownMenuTrigger disabled={switchingWorkspace} render={<Button variant="outline" className="h-auto w-full justify-between px-2.5 py-2" />}>
        <span className="flex min-w-0 items-center gap-2">
          {current?.kind === "team" ? <Building2Icon className="size-4 shrink-0" /> : <UserRoundIcon className="size-4 shrink-0" />}
          <span className="min-w-0 text-left"><span className="block truncate text-sm font-medium">{current?.display_name ?? "选择空间"}</span><span className="block text-xs text-muted-foreground">{current?.kind === "team" ? "团队空间" : "个人空间"}</span></span>
        </span>
        {switchingWorkspace ? <Spinner /> : <ChevronsUpDownIcon className="size-4 text-muted-foreground" />}
      </DropdownMenuTrigger>
      <DropdownMenuContent className="min-w-64">
        <DropdownMenuGroup>
          <DropdownMenuLabel>切换 Wiki 空间</DropdownMenuLabel>
          {(workspaces.data ?? []).map((workspace) => <DropdownMenuItem key={workspace.id} disabled={switchingWorkspace} onClick={() => workspace.id !== current?.id && void runAfterDiscard(() => switching.mutateAsync(workspace.id)).catch(() => undefined)}>
            {workspace.kind === "team" ? <Building2Icon /> : <UserRoundIcon />}
            <span className="min-w-0 flex-1"><span className="block truncate">{workspace.display_name}</span><span className="text-xs text-muted-foreground">{workspace.role === "owner" ? "Owner" : workspace.role === "editor" ? "Editor" : "Viewer"}</span></span>
            {workspace.id === current?.id && <CheckIcon className="ml-auto" />}
          </DropdownMenuItem>)}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup><DropdownMenuItem onClick={() => navigate("/workspaces")}><ListTreeIcon />管理全部空间</DropdownMenuItem><DropdownMenuItem onClick={() => setCreateOpen(true)}><PlusIcon />创建团队空间</DropdownMenuItem></DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>

    <Dialog open={createOpen} onOpenChange={setCreateOpen}>
      <DialogContent>
        <DialogHeader><DialogTitle>创建团队空间</DialogTitle><DialogDescription>创建后可邀请已有账号协作。空间内容、任务和模型配置彼此隔离。</DialogDescription></DialogHeader>
        <Field><FieldLabel htmlFor="workspace-name">空间名称</FieldLabel><Input id="workspace-name" value={name} maxLength={80} autoFocus onChange={(event) => setName(event.target.value)} placeholder="例如：产品知识库" />{create.isError && <FieldError>{create.error.message}</FieldError>}</Field>
        <DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)}>取消</Button><Button disabled={!name.trim() || create.isPending} onClick={() => void runAfterDiscard(() => create.mutateAsync()).catch(() => undefined)}>{create.isPending && <Spinner data-icon="inline-start" />}创建</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </>
}
