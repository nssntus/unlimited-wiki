import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CrownIcon, MailPlusIcon, MoreHorizontalIcon, PencilIcon, UserMinusIcon, UsersIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, type WorkspaceInvitation, type WorkspaceMember } from "@/lib/api"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Field, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useSession } from "@/features/session-context"

type Role = "editor" | "viewer"
type PendingAction = { kind: "remove" | "transfer"; member: WorkspaceMember } | null

export function WorkspacePage() {
  const client = useQueryClient()
  const { session } = useSession()
  const workspace = session?.workspace
  const canManage = workspace?.permissions.includes("workspace.manage") ?? false
  const [inviteOpen, setInviteOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [role, setRole] = useState<Role>("editor")
  const [renameValue, setRenameValue] = useState(workspace?.display_name ?? "")
  const [pendingAction, setPendingAction] = useState<PendingAction>(null)
  const members = useQuery({ queryKey: queryKeys.workspaceMembers, queryFn: () => apiGet<WorkspaceMember[]>("/api/workspace/members"), enabled: canManage })
  const invitations = useQuery({ queryKey: queryKeys.invitations, queryFn: () => apiGet<WorkspaceInvitation[]>("/api/invitations") })
  const refreshMembers = () => client.invalidateQueries({ queryKey: queryKeys.workspaceMembers })
  const mutationError = (error: Error) => { toast.error(error.message); void refreshMembers() }
  const invite = useMutation({
    mutationFn: () => apiPost("/api/workspace/invitations", { email, role }),
    onSuccess: () => { setInviteOpen(false); setEmail(""); toast.success("邀请已发送到对方账号") },
  })
  const changeRole = useMutation({
    mutationFn: ({ userId, nextRole }: { userId: string; nextRole: Role }) => apiPost(`/api/workspace/members/${userId}/role`, { role: nextRole }),
    onSuccess: async () => { await refreshMembers(); toast.success("成员角色已更新") }, onError: mutationError,
  })
  const remove = useMutation({
    mutationFn: (userId: string) => apiPost(`/api/workspace/members/${userId}/remove`, {}),
    onSuccess: async () => { setPendingAction(null); await refreshMembers(); toast.success("成员已移除") }, onError: mutationError,
  })
  const transfer = useMutation({
    mutationFn: (userId: string) => apiPost("/api/workspace/owner-transfer", { user_id: userId }),
    onSuccess: async () => { setPendingAction(null); await Promise.all([refreshMembers(), client.invalidateQueries({ queryKey: queryKeys.session }), client.invalidateQueries({ queryKey: queryKeys.workspaces })]); toast.success("Owner 已转移") }, onError: mutationError,
  })
  const rename = useMutation({
    mutationFn: () => apiPost("/api/workspace/rename", { display_name: renameValue }),
    onSuccess: async () => { await Promise.all([client.invalidateQueries({ queryKey: queryKeys.session }), client.invalidateQueries({ queryKey: queryKeys.workspaces })]); toast.success("空间名称已更新") },
  })

  if (!workspace) return null
  if (canManage && members.isLoading) return <PageFrame><Skeleton className="h-[70svh] w-full" /></PageFrame>
  if (members.isError) return <PageFrame><Alert variant="destructive"><AlertTitle>无法加载成员</AlertTitle><AlertDescription>{members.error.message}</AlertDescription><Button className="mt-3" variant="outline" onClick={() => members.refetch()}>重试</Button></Alert></PageFrame>

  return <PageFrame><div className="mx-auto max-w-4xl">
    <PageTitle eyebrow={workspace.kind === "team" ? "团队空间" : "个人空间"} title={workspace.display_name} description={workspace.kind === "team" ? "成员只在当前空间内获得权限；平台管理员不会因此获得私有正文访问权。" : "个人空间仅属于你，不能邀请成员或转移 Owner。"} actions={workspace.kind === "team" && canManage ? <Button onClick={() => setInviteOpen(true)}><MailPlusIcon data-icon="inline-start" />邀请成员</Button> : undefined} />

    {workspace.kind === "personal" ? <Alert><UsersIcon /><AlertTitle>个人空间不可共享</AlertTitle><AlertDescription>需要多人协作时，请从侧栏空间切换器创建团队空间。</AlertDescription></Alert> : !canManage ? <Alert><UsersIcon /><AlertTitle>团队成员</AlertTitle><AlertDescription>你可以在当前空间协作，但只有 Owner 可以查看成员账号并管理角色。</AlertDescription></Alert> : <div className="flex flex-col gap-10">
      {canManage && <FieldSet><FieldLegend>空间资料</FieldLegend><FieldGroup><Field orientation="responsive"><div><FieldLabel htmlFor="workspace-display-name">名称</FieldLabel></div><div className="flex w-full max-w-md gap-2"><Input id="workspace-display-name" value={renameValue} maxLength={80} onChange={(event) => setRenameValue(event.target.value)} /><Button variant="outline" disabled={!renameValue.trim() || renameValue === workspace.display_name || rename.isPending} onClick={() => rename.mutate()}>{rename.isPending ? <Spinner /> : <PencilIcon />}<span className="sr-only">保存名称</span></Button></div></Field></FieldGroup></FieldSet>}

      <section aria-labelledby="members-heading"><div className="mb-4 flex items-end justify-between"><div><h2 id="members-heading" className="text-xl font-semibold">成员</h2><p className="mt-1 text-sm text-muted-foreground">角色变更和移除会在下一次请求立即生效。</p></div><Badge variant="secondary">{members.data?.length ?? 0} 人</Badge></div>
        <div className="hidden md:block"><Table><TableHeader><TableRow><TableHead>成员</TableHead><TableHead>角色</TableHead><TableHead className="w-12"><span className="sr-only">操作</span></TableHead></TableRow></TableHeader><TableBody>{(members.data ?? []).map((member) => <MemberRow key={member.user_id} member={member} canManage={canManage} onRole={(nextRole) => changeRole.mutate({ userId: member.user_id, nextRole })} onRemove={() => setPendingAction({ kind: "remove", member })} onTransfer={() => setPendingAction({ kind: "transfer", member })} />)}</TableBody></Table></div>
        <div className="divide-y md:hidden">{(members.data ?? []).map((member) => <div key={member.user_id} className="flex min-w-0 items-center gap-3 py-4"><div className="min-w-0 flex-1"><div className="truncate font-medium">{member.nickname}{member.is_current_user ? "（你）" : ""}</div><div className="truncate text-sm text-muted-foreground">{member.email}</div></div><Badge variant="outline">{roleLabel(member.role)}</Badge>{canManage && member.role !== "owner" && <MemberMenu member={member} onRole={(nextRole) => changeRole.mutate({ userId: member.user_id, nextRole })} onRemove={() => setPendingAction({ kind: "remove", member })} onTransfer={() => setPendingAction({ kind: "transfer", member })} />}</div>)}</div>
      </section>
    </div>}

    {invitations.data?.length ? <section className="mt-10"><h2 className="text-xl font-semibold">待处理邀请</h2><div className="mt-3 divide-y">{invitations.data.map((item) => <div key={item.id} className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center"><div className="min-w-0 flex-1"><div className="font-medium">{item.display_name}</div><div className="text-sm text-muted-foreground">{item.invited_by_nickname} 邀请你以 {roleLabel(item.role)} 身份加入</div></div><InvitationActions invitation={item} /></div>)}</div></section> : null}

    <Dialog open={inviteOpen} onOpenChange={setInviteOpen}><DialogContent><DialogHeader><DialogTitle>邀请已有账号</DialogTitle><DialogDescription>邀请只发送到应用内。对方接受前不会获得任何空间权限。</DialogDescription></DialogHeader><FieldGroup><Field><FieldLabel htmlFor="invite-email">账号邮箱</FieldLabel><Input id="invite-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></Field><Field><FieldLabel>角色</FieldLabel><Select value={role} onValueChange={(value) => value && setRole(value as Role)}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="editor">Editor · 可读写内容</SelectItem><SelectItem value="viewer">Viewer · 只读</SelectItem></SelectGroup></SelectContent></Select></Field>{invite.isError && <FieldError>{invite.error.message}</FieldError>}</FieldGroup><DialogFooter><Button variant="outline" onClick={() => setInviteOpen(false)}>取消</Button><Button disabled={!email.trim() || invite.isPending} onClick={() => invite.mutate()}>{invite.isPending && <Spinner data-icon="inline-start" />}发送邀请</Button></DialogFooter></DialogContent></Dialog>

    <AlertDialog open={Boolean(pendingAction)} onOpenChange={(open) => !open && setPendingAction(null)}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{pendingAction?.kind === "transfer" ? "转移 Owner" : "移除成员"}</AlertDialogTitle><AlertDialogDescription>{pendingAction?.kind === "transfer" ? `转移后，${pendingAction.member.nickname} 将拥有空间管理权，你将变为 Editor。` : `移除 ${pendingAction?.member.nickname ?? "该成员"} 后，其当前团队会话会立即失效。`}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction variant={pendingAction?.kind === "remove" ? "destructive" : "default"} onClick={() => pendingAction && (pendingAction.kind === "transfer" ? transfer.mutate(pendingAction.member.user_id) : remove.mutate(pendingAction.member.user_id))}>确认</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </div></PageFrame>
}

function roleLabel(role: WorkspaceMember["role"] | WorkspaceInvitation["role"]) { return role === "owner" ? "Owner" : role === "editor" ? "Editor" : "Viewer" }

function MemberRow({ member, canManage, onRole, onRemove, onTransfer }: { member: WorkspaceMember; canManage: boolean; onRole: (role: Role) => void; onRemove: () => void; onTransfer: () => void }) {
  return <TableRow><TableCell><div className="font-medium">{member.nickname}{member.is_current_user ? "（你）" : ""}</div><div className="text-muted-foreground">{member.email}</div></TableCell><TableCell><Badge variant={member.role === "owner" ? "default" : "outline"}>{roleLabel(member.role)}</Badge></TableCell><TableCell>{canManage && member.role !== "owner" && <MemberMenu member={member} onRole={onRole} onRemove={onRemove} onTransfer={onTransfer} />}</TableCell></TableRow>
}

function MemberMenu({ member, onRole, onRemove, onTransfer }: { member: WorkspaceMember; onRole: (role: Role) => void; onRemove: () => void; onTransfer: () => void }) {
  return <DropdownMenu><DropdownMenuTrigger render={<Button size="icon-sm" variant="ghost" aria-label={`管理 ${member.nickname}`} />}><MoreHorizontalIcon /></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuGroup><DropdownMenuItem onClick={() => onRole(member.role === "editor" ? "viewer" : "editor")}>{member.role === "editor" ? "设为 Viewer" : "设为 Editor"}</DropdownMenuItem><DropdownMenuItem onClick={onTransfer}><CrownIcon />转移 Owner</DropdownMenuItem><DropdownMenuItem variant="destructive" onClick={onRemove}><UserMinusIcon />移除成员</DropdownMenuItem></DropdownMenuGroup></DropdownMenuContent></DropdownMenu>
}

function InvitationActions({ invitation }: { invitation: WorkspaceInvitation }) {
  const client = useQueryClient()
  const respond = useMutation({ mutationFn: (action: "accept" | "decline") => apiPost(`/api/invitations/${invitation.id}/${action}`, {}), onSuccess: async (_, action) => { await Promise.all([client.invalidateQueries({ queryKey: queryKeys.invitations }), client.invalidateQueries({ queryKey: queryKeys.workspaces })]); toast.success(action === "accept" ? "已加入团队空间" : "已拒绝邀请") }, onError: (error) => toast.error(error.message) })
  return <div className="flex gap-2"><Button variant="outline" disabled={respond.isPending} onClick={() => respond.mutate("decline")}>拒绝</Button><Button disabled={respond.isPending} onClick={() => respond.mutate("accept")}>接受</Button></div>
}
