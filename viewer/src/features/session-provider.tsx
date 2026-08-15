import { useEffect, useRef, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react"
import { Navigate, useLocation, useNavigate } from "react-router-dom"

import { ApiError, apiGet, apiPost, queryKeys, setCsrfToken, setUnauthorizedHandler, type Session } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { SessionContext, useSession } from "@/features/session-context"

type SwitchState =
  | { kind: "idle" }
  | { kind: "requesting"; target: string }
  | { kind: "confirming"; target: string }
  | { kind: "error"; target: string; message: string }

export function SessionProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const switchingRef = useRef(false)
  const [switchState, setSwitchState] = useState<SwitchState>({ kind: "idle" })
  const query = useQuery({ queryKey: queryKeys.session, queryFn: () => apiGet<Session>("/api/auth/session"), retry: false, staleTime: 0 })
  useEffect(() => setCsrfToken(query.data?.csrf_token ?? ""), [query.data?.csrf_token])
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setCsrfToken("")
      switchingRef.current = false
      setSwitchState({ kind: "idle" })
      void client.cancelQueries().then(() => client.clear())
      navigate("/login", { replace: true })
    })
    return () => setUnauthorizedHandler(null)
  }, [client, navigate])
  const signOut = async () => {
    await apiPost("/api/auth/logout", {})
    setCsrfToken("")
    client.clear()
    navigate("/login", { replace: true })
  }
  const clearTenantCache = async () => {
    await client.cancelQueries()
    client.removeQueries({ predicate: (entry) => entry.queryKey[0] !== "session" })
  }
  const confirmWorkspace = async (target: string) => {
    setSwitchState({ kind: "confirming", target })
    try {
      const next = await apiGet<Session>("/api/auth/session")
      if (!next.authenticated || !next.workspace) {
        setCsrfToken("")
        client.clear()
        switchingRef.current = false
        setSwitchState({ kind: "idle" })
        navigate("/login", { replace: true })
        return next
      }
      client.setQueryData(queryKeys.session, next)
      setCsrfToken(next.csrf_token ?? "")
      switchingRef.current = false
      setSwitchState({ kind: "idle" })
      navigate("/", { replace: true })
      return next
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        switchingRef.current = false
        setSwitchState({ kind: "idle" })
        throw error
      }
      const message = error instanceof Error ? error.message : "无法确认当前空间"
      setSwitchState({ kind: "error", target, message })
      throw error
    }
  }
  const switchWorkspace = async (workspaceId: string) => {
    if (switchingRef.current) return
    switchingRef.current = true
    setSwitchState({ kind: "requesting", target: workspaceId })
    let postError: unknown = null
    try {
      await apiPost<Session>("/api/workspaces/switch", { workspace_id: workspaceId })
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        switchingRef.current = false
        setSwitchState({ kind: "idle" })
        throw error
      }
      postError = error
    }
    await clearTenantCache()
    const next = await confirmWorkspace(workspaceId)
    if (next.workspace?.id === workspaceId) return
    if (postError) throw postError
    throw new Error(`空间切换未生效，当前仍是 ${next.workspace?.display_name ?? "其他空间"}`)
  }
  const retryWorkspaceConfirmation = async () => {
    if (switchState.kind !== "error") return
    await clearTenantCache()
    try {
      await confirmWorkspace(switchState.target)
    } catch {
      // The blocking state remains visible until the server session can be confirmed.
    }
  }
  const switchingWorkspace = switchState.kind !== "idle"
  const hasPermission = (permission: string) => query.data?.workspace?.permissions.includes(permission) ?? false
  const value = { session: query.data, loading: query.isLoading, signOut, switchWorkspace, switchingWorkspace, hasPermission }
  return <SessionContext.Provider value={value}>{switchState.kind === "confirming" ? <WorkspaceSwitchGate /> : switchState.kind === "error" ? <WorkspaceSwitchGate error={switchState.message} onRetry={retryWorkspaceConfirmation} /> : children}</SessionContext.Provider>
}

function WorkspaceSwitchGate({ error, onRetry }: { error?: string; onRetry?: () => Promise<void> }) {
  return <main className="grid min-h-svh place-items-center bg-background p-6"><div className="w-full max-w-md">
    {error ? <Alert variant="destructive"><AlertTriangleIcon /><AlertTitle>无法确认当前 Wiki 空间</AlertTitle><AlertDescription><p>旧空间数据已从浏览器清除。在确认服务端当前空间前，页面保持锁定，避免把操作写入错误空间。</p><p className="break-words">{error}</p>{onRetry && <Button className="mt-4" variant="outline" onClick={() => void onRetry()}><RefreshCwIcon data-icon="inline-start" />重试确认</Button>}</AlertDescription></Alert> : <div className="flex items-center justify-center gap-3 text-sm text-muted-foreground"><Spinner />正在确认当前 Wiki 空间…</div>}
  </div></main>
}

export function RequireSession({ children, role, permission }: { children: ReactNode; role?: "admin"; permission?: string }) {
  const { session, loading } = useSession()
  const location = useLocation()
  if (loading) return <div className="p-8"><Skeleton className="h-[70svh] w-full" /></div>
  if (!session?.authenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (role && session.user?.role !== role) return <Navigate to="/forbidden" replace />
  if (permission && !session.workspace?.permissions.includes(permission)) return <Navigate to="/forbidden" replace />
  return children
}
