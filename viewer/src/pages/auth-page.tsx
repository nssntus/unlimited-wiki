import { useEffect, useState, type FormEvent } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom"
import { ArchiveIcon, KeyRoundIcon } from "lucide-react"

import { apiPost, queryKeys, setCsrfToken, type Session } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { useSession } from "@/features/session-context"

type Mode = "login" | "register" | "recover"

export function AuthPage({ mode }: { mode: Mode }) {
  const { session, loading } = useSession()
  const client = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const [inviteLink] = useState(() => ({
    email: searchParams.get("email")?.trim() ?? "",
    token: searchParams.get("invite_token")?.trim() ?? "",
  }))
  const invitedFromLink = mode === "register" && Boolean(inviteLink.email && inviteLink.token)
  const [email, setEmail] = useState(invitedFromLink ? inviteLink.email : "")
  const [nickname, setNickname] = useState("")
  const [password, setPassword] = useState("")
  const [inviteToken, setInviteToken] = useState(invitedFromLink ? inviteLink.token : "")
  const [recoveryCode, setRecoveryCode] = useState("")
  const [issuedCode, setIssuedCode] = useState("")
  const [migrationRetryRequired, setMigrationRetryRequired] = useState(false)
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const title = mode === "login" ? "登录我的 Wiki" : mode === "register" ? "创建私有 Wiki" : "使用恢复码重置密码"

  useEffect(() => {
    if (invitedFromLink && searchParams.size) setSearchParams({}, { replace: true })
  }, [invitedFromLink, searchParams, setSearchParams])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setPending(true); setError("")
    try {
      if (mode === "recover") {
        await apiPost("/api/auth/recover", { email, recovery_code: recoveryCode, new_password: password }, false)
        navigate("/login", { replace: true })
        return
      }
      const payload = mode === "register"
        ? { email, nickname, password, ...(inviteToken ? { invite_token: inviteToken } : {}) }
        : { email, password }
      const result = await apiPost<Session & {
        recovery_code?: string
        migration?: { status?: string }
      }>(`/api/auth/${mode}`, payload, false)
      setCsrfToken(result.csrf_token ?? "")
      client.clear()
      client.setQueryData(queryKeys.session, result)
      if (result.recovery_code) {
        setIssuedCode(result.recovery_code)
        setMigrationRetryRequired(result.migration?.status === "retry_required")
        return
      }
      const from = (location.state as { from?: string } | null)?.from || "/"
      navigate(from, { replace: true })
    } catch (value) {
      setError(value instanceof Error ? value.message : "操作失败")
    } finally { setPending(false) }
  }

  if (issuedCode) return <main className="grid min-h-svh place-items-center px-4"><div className="w-full max-w-md space-y-6">
    <Alert><KeyRoundIcon /><AlertTitle>请保存一次性恢复码</AlertTitle><AlertDescription className="mt-3 break-all font-mono">{issuedCode}</AlertDescription></Alert>
    {migrationRetryRequired && <Alert variant="destructive"><ArchiveIcon /><AlertTitle>旧数据迁移尚未确认完成</AlertTitle><AlertDescription>账号已经创建，但迁移仍需恢复确认。请保留原始目录、暂勿编辑新空间，并联系运维完成处理。</AlertDescription></Alert>}
    <p className="text-sm text-muted-foreground">恢复码只显示这一次，24 小时内有效。它不会上传到 Wiki 或广场。</p>
    <Button className="w-full" onClick={() => navigate("/", { replace: true })}>进入我的 Wiki</Button>
  </div></main>

  if (mode === "register" && !invitedFromLink && !loading && session?.registration_enabled === false) {
    return <main className="grid min-h-svh place-items-center px-4"><div className="w-full max-w-sm space-y-5">
      <Alert><KeyRoundIcon /><AlertTitle>注册已关闭</AlertTitle><AlertDescription>此部署不接受自助注册。请联系管理员开通账号。</AlertDescription></Alert>
      <Button className="w-full" render={<Link to="/login" />}>返回登录</Button>
    </div></main>
  }

  return <main className="grid min-h-svh place-items-center px-4 py-10"><div className="w-full max-w-sm">
    <Link to="/square" className="mb-10 flex items-center justify-center gap-2 font-semibold"><span className="grid size-9 place-items-center rounded-md bg-primary text-primary-foreground"><ArchiveIcon /></span>知库</Link>
    <h1 className="text-2xl font-semibold">{title}</h1>
    <p className="mt-2 text-sm text-muted-foreground">每个账号只进入自己的私有空间；公开内容需另行投稿审核。</p>
    {invitedFromLink && <Alert className="mt-6"><KeyRoundIcon /><AlertTitle>管理员已邀请你创建账号</AlertTitle><AlertDescription>邀请与 {inviteLink.email} 绑定，只能使用一次。注册后仍需团队 Owner 单独邀请，才能访问团队空间。</AlertDescription></Alert>}
    <form className="mt-8" onSubmit={submit}><FieldGroup>
      <Field><FieldLabel htmlFor="auth-email">邮箱</FieldLabel><Input id="auth-email" type="email" autoComplete="email" required readOnly={invitedFromLink} value={email} onChange={(event) => setEmail(event.target.value)} />{invitedFromLink && <FieldDescription>该邮箱由邀请固定，不能在注册时更改。</FieldDescription>}</Field>
      {mode === "register" && <Field><FieldLabel htmlFor="auth-nickname">昵称</FieldLabel><Input id="auth-nickname" autoComplete="nickname" required value={nickname} onChange={(event) => setNickname(event.target.value)} /></Field>}
      {mode === "register" && !invitedFromLink && session?.registration_mode === "invite" && <Field><FieldLabel htmlFor="invite-token">邀请令牌</FieldLabel><Input id="invite-token" autoComplete="off" required value={inviteToken} onChange={(event) => setInviteToken(event.target.value)} /><FieldDescription>令牌与受邀邮箱绑定，只能使用一次。</FieldDescription></Field>}
      {mode === "recover" && <Field><FieldLabel htmlFor="recovery-code">恢复码</FieldLabel><Input id="recovery-code" required value={recoveryCode} onChange={(event) => setRecoveryCode(event.target.value)} /></Field>}
      <Field><FieldLabel htmlFor="auth-password">{mode === "recover" ? "新密码" : "密码"}</FieldLabel><Input id="auth-password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={10} required value={password} onChange={(event) => setPassword(event.target.value)} /><FieldDescription>至少 10 个字符。</FieldDescription></Field>
      {error && <FieldError>{error}</FieldError>}
      <Button type="submit" className="w-full" disabled={pending}>{pending && <Spinner data-icon="inline-start" />}{mode === "login" ? "登录" : mode === "register" ? "创建账号" : "重置密码"}</Button>
    </FieldGroup></form>
    <div className="mt-6 flex justify-between text-sm text-muted-foreground">
      {mode === "login" ? <>{session?.registration_enabled && <Link to="/register" className="hover:text-foreground">注册</Link>}<Link to="/recover" className="ml-auto hover:text-foreground">忘记密码</Link></> : <Link to="/login" className="hover:text-foreground">返回登录</Link>}
    </div>
  </div></main>
}
