import { useState, type FormEvent } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { Link, useLocation, useNavigate } from "react-router-dom"
import { ArchiveIcon, KeyRoundIcon } from "lucide-react"

import { apiPost, queryKeys, setCsrfToken, type Session } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"

type Mode = "login" | "register" | "recover"

export function AuthPage({ mode }: { mode: Mode }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState("")
  const [nickname, setNickname] = useState("")
  const [password, setPassword] = useState("")
  const [recoveryCode, setRecoveryCode] = useState("")
  const [issuedCode, setIssuedCode] = useState("")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const title = mode === "login" ? "登录我的 Wiki" : mode === "register" ? "创建私有 Wiki" : "使用恢复码重置密码"

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setPending(true); setError("")
    try {
      if (mode === "recover") {
        await apiPost("/api/auth/recover", { email, recovery_code: recoveryCode, new_password: password }, false)
        navigate("/login", { replace: true })
        return
      }
      const payload = mode === "register" ? { email, nickname, password } : { email, password }
      const result = await apiPost<Session & { recovery_code?: string }>(`/api/auth/${mode}`, payload, false)
      setCsrfToken(result.csrf_token ?? "")
      client.clear()
      client.setQueryData(queryKeys.session, result)
      if (result.recovery_code) {
        setIssuedCode(result.recovery_code)
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
    <p className="text-sm text-muted-foreground">恢复码只显示这一次，24 小时内有效。它不会上传到 Wiki 或广场。</p>
    <Button className="w-full" onClick={() => navigate("/", { replace: true })}>进入我的 Wiki</Button>
  </div></main>

  return <main className="grid min-h-svh place-items-center px-4 py-10"><div className="w-full max-w-sm">
    <Link to="/square" className="mb-10 flex items-center justify-center gap-2 font-semibold"><span className="grid size-9 place-items-center rounded-md bg-primary text-primary-foreground"><ArchiveIcon /></span>知库</Link>
    <h1 className="text-2xl font-semibold">{title}</h1>
    <p className="mt-2 text-sm text-muted-foreground">每个账号只进入自己的私有空间；公开内容需另行投稿审核。</p>
    <form className="mt-8" onSubmit={submit}><FieldGroup>
      <Field><FieldLabel htmlFor="auth-email">邮箱</FieldLabel><Input id="auth-email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></Field>
      {mode === "register" && <Field><FieldLabel htmlFor="auth-nickname">昵称</FieldLabel><Input id="auth-nickname" autoComplete="nickname" required value={nickname} onChange={(event) => setNickname(event.target.value)} /></Field>}
      {mode === "recover" && <Field><FieldLabel htmlFor="recovery-code">恢复码</FieldLabel><Input id="recovery-code" required value={recoveryCode} onChange={(event) => setRecoveryCode(event.target.value)} /></Field>}
      <Field><FieldLabel htmlFor="auth-password">{mode === "recover" ? "新密码" : "密码"}</FieldLabel><Input id="auth-password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={10} required value={password} onChange={(event) => setPassword(event.target.value)} /><FieldDescription>至少 10 个字符。</FieldDescription></Field>
      {error && <FieldError>{error}</FieldError>}
      <Button type="submit" className="w-full" disabled={pending}>{pending && <Spinner data-icon="inline-start" />}{mode === "login" ? "登录" : mode === "register" ? "创建账号" : "重置密码"}</Button>
    </FieldGroup></form>
    <div className="mt-6 flex justify-between text-sm text-muted-foreground">
      {mode === "login" ? <><Link to="/register" className="hover:text-foreground">注册</Link><Link to="/recover" className="hover:text-foreground">忘记密码</Link></> : <Link to="/login" className="hover:text-foreground">返回登录</Link>}
    </div>
  </div></main>
}
