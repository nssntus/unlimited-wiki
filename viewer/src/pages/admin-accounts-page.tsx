import { useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CheckIcon, CopyIcon, MailPlusIcon, RotateCcwIcon, UserRoundPlusIcon } from "lucide-react"
import { toast } from "sonner"

import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { useSession } from "@/features/session-context"
import { apiGet, apiPost, queryKeys, type IssuedRegistrationInvite, type RegistrationInvite } from "@/lib/api"

const STATUS_LABELS: Record<RegistrationInvite["status"], string> = {
  pending: "待使用",
  used: "已使用",
  revoked: "已撤销",
  expired: "已过期",
}

const EXPIRY_OPTIONS = [
  { value: "24", label: "24 小时" },
  { value: "72", label: "3 天" },
  { value: "168", label: "7 天" },
  { value: "720", label: "30 天" },
]

function registrationUrl(invite: IssuedRegistrationInvite) {
  const root = window.location.href.split("#", 1)[0]
  const params = new URLSearchParams({ email: invite.email, invite_token: invite.token })
  return `${root}#/register?${params.toString()}`
}

export function AdminAccountsPage() {
  const client = useQueryClient()
  const { session } = useSession()
  const registrationClosed = session?.registration_mode === "closed"
  const [email, setEmail] = useState("")
  const [hours, setHours] = useState("72")
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState("")
  const [issued, setIssued] = useState<IssuedRegistrationInvite | null>(null)
  const [copied, setCopied] = useState(false)
  const [revokeTarget, setRevokeTarget] = useState<RegistrationInvite | null>(null)
  const invites = useQuery({
    queryKey: queryKeys.adminRegistrationInvites,
    queryFn: () => apiGet<RegistrationInvite[]>("/api/admin/registration-invites"),
  })
  const revoke = useMutation({
    mutationFn: (id: string) => apiPost<RegistrationInvite>(`/api/admin/registration-invites/${id}/revoke`, {}),
    onSuccess: async () => {
      setRevokeTarget(null)
      await client.invalidateQueries({ queryKey: queryKeys.adminRegistrationInvites })
      toast.success("账号邀请已撤销")
    },
    onError: (error) => toast.error(error.message),
  })

  const createInvite = async (event: FormEvent) => {
    event.preventDefault()
    if (registrationClosed) return
    setCreating(true)
    setCreateError("")
    setIssued(null)
    try {
      // The response contains a one-time secret, so it must not enter the idempotency response store.
      const result = await apiPost<IssuedRegistrationInvite>(
        "/api/admin/registration-invites",
        { email, hours: Number(hours) },
        false,
      )
      setIssued(result)
      setCopied(false)
      setEmail("")
      await client.invalidateQueries({ queryKey: queryKeys.adminRegistrationInvites })
    } catch (value) {
      setCreateError(value instanceof Error ? value.message : "无法创建邀请")
    } finally {
      setCreating(false)
    }
  }

  const copyLink = async () => {
    if (!issued) return
    try {
      await navigator.clipboard.writeText(registrationUrl(issued))
      setCopied(true)
      toast.success("注册链接已复制")
    } catch {
      toast.error("浏览器未允许复制，请手动复制链接")
    }
  }

  return <PageFrame><div className="mx-auto max-w-5xl">
    <PageTitle
      eyebrow="账户级管理"
      title="账号邀请"
      description="先创建一次性账号邀请；对方注册后，再由团队空间 Owner 决定是否授予某个团队空间的访问权限。平台 Admin 不会因此获得用户私有正文访问权。"
    />

    <section aria-labelledby="create-account-invite" className="border-b pb-10">
      <h2 id="create-account-invite" className="text-xl font-semibold">创建账号邀请</h2>
      <p className="mt-1 text-sm text-muted-foreground">邀请与邮箱绑定，只能使用一次。同一邮箱重新签发时，旧的待使用邀请会立即失效。</p>
      {registrationClosed && <Alert className="mt-5">
        <AlertTitle>账号注册已关闭</AlertTitle>
        <AlertDescription>当前部署使用 closed 模式，不能生成新的账号邀请。已有邀请仍可在下方查看或撤销。</AlertDescription>
      </Alert>}
      <form className="mt-6 max-w-2xl" onSubmit={createInvite}>
        <FieldGroup>
          <div className="grid gap-5 sm:grid-cols-[minmax(0,1fr)_10rem]">
            <Field>
              <FieldLabel htmlFor="registration-invite-email">受邀邮箱</FieldLabel>
              <Input id="registration-invite-email" type="email" autoComplete="off" required disabled={registrationClosed} value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.com" />
            </Field>
            <Field>
              <FieldLabel htmlFor="registration-invite-expiry">有效期</FieldLabel>
              <Select disabled={registrationClosed} value={hours} onValueChange={(value) => value && setHours(value)}>
                <SelectTrigger id="registration-invite-expiry" className="w-full"><SelectValue>{EXPIRY_OPTIONS.find((option) => option.value === hours)?.label}</SelectValue></SelectTrigger>
                <SelectContent>{EXPIRY_OPTIONS.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
              </Select>
            </Field>
          </div>
          {createError && <FieldError>{createError}</FieldError>}
          <Field orientation="horizontal">
            <Button type="submit" disabled={registrationClosed || creating || !email.trim()}>{creating ? <Spinner data-icon="inline-start" /> : <UserRoundPlusIcon data-icon="inline-start" />}生成注册链接</Button>
            <FieldDescription>链接只在创建后显示一次，服务端仅保存令牌哈希。</FieldDescription>
          </Field>
        </FieldGroup>
      </form>

      {issued && <Alert className="mt-6" aria-live="polite">
        <MailPlusIcon />
        <AlertTitle>一次性注册链接已生成</AlertTitle>
        <AlertDescription className="mt-2 space-y-3">
          <p>请通过可信渠道发送给 <strong>{issued.email}</strong>。关闭或刷新本页后，令牌不会再次显示。</p>
          <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
            <Input readOnly aria-label="一次性注册链接" className="min-w-0 font-mono text-xs" value={registrationUrl(issued)} onFocus={(event) => event.currentTarget.select()} />
            <Button type="button" variant="outline" onClick={copyLink}>{copied ? <CheckIcon data-icon="inline-start" /> : <CopyIcon data-icon="inline-start" />}{copied ? "已复制" : "复制链接"}</Button>
          </div>
        </AlertDescription>
      </Alert>}
    </section>

    <section aria-labelledby="account-invite-history" className="pt-10">
      <div className="flex items-center justify-between gap-4">
        <div><h2 id="account-invite-history" className="text-xl font-semibold">邀请记录</h2><p className="mt-1 text-sm text-muted-foreground">列表不包含令牌明文。</p></div>
        <Button variant="outline" size="sm" disabled={invites.isFetching} onClick={() => invites.refetch()}><RotateCcwIcon data-icon="inline-start" />刷新</Button>
      </div>
      {invites.isLoading ? <div className="mt-6 space-y-3"><Skeleton className="h-20" /><Skeleton className="h-20" /></div>
        : invites.isError ? <Alert className="mt-6" variant="destructive"><AlertTitle>无法加载邀请</AlertTitle><AlertDescription>{invites.error.message}<Button className="mt-3" variant="outline" onClick={() => invites.refetch()}>重试</Button></AlertDescription></Alert>
        : invites.data?.length ? <div className="mt-6 divide-y border-y">{invites.data.map((invite) => <article key={invite.id} className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h3 className="break-all font-medium">{invite.email}</h3><Badge variant={invite.status === "pending" ? "secondary" : "outline"}>{STATUS_LABELS[invite.status]}</Badge></div><p className="mt-1 text-xs text-muted-foreground">创建于 {new Date(invite.created_at).toLocaleString()} · 到期 {new Date(invite.expires_at).toLocaleString()}</p></div>
          {invite.status === "pending" && <Button className="sm:shrink-0" size="sm" variant="outline" onClick={() => setRevokeTarget(invite)}>撤销</Button>}
        </article>)}</div>
        : <Empty className="mt-6 min-h-56"><EmptyHeader><EmptyMedia variant="icon"><UserRoundPlusIcon /></EmptyMedia><EmptyTitle>还没有账号邀请</EmptyTitle><EmptyDescription>在上方填写邮箱并生成一次性注册链接。</EmptyDescription></EmptyHeader></Empty>}
    </section>

    <AlertDialog open={Boolean(revokeTarget)} onOpenChange={(open) => { if (!open) setRevokeTarget(null) }}>
      <AlertDialogContent>
        <AlertDialogHeader><AlertDialogTitle>撤销这个账号邀请？</AlertDialogTitle><AlertDialogDescription>{revokeTarget?.email} 将无法再使用对应链接注册，操作不可恢复。</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction disabled={revoke.isPending} onClick={() => revokeTarget && revoke.mutate(revokeTarget.id)}>{revoke.isPending && <Spinner data-icon="inline-start" />}确认撤销</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div></PageFrame>
}
