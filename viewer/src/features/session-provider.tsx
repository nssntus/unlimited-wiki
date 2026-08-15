import { useEffect, useRef, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Navigate, useLocation, useNavigate } from "react-router-dom"

import { apiGet, apiPost, queryKeys, setCsrfToken, setUnauthorizedHandler, type Session } from "@/lib/api"
import { Skeleton } from "@/components/ui/skeleton"
import { SessionContext, useSession } from "@/features/session-context"

export function SessionProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const switchingRef = useRef(false)
  const [switchingWorkspace, setSwitchingWorkspace] = useState(false)
  const query = useQuery({ queryKey: queryKeys.session, queryFn: () => apiGet<Session>("/api/auth/session"), retry: false, staleTime: 0 })
  useEffect(() => setCsrfToken(query.data?.csrf_token ?? ""), [query.data?.csrf_token])
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setCsrfToken("")
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
  const switchWorkspace = async (workspaceId: string) => {
    if (switchingRef.current) return
    switchingRef.current = true
    setSwitchingWorkspace(true)
    try {
      await apiPost<Session>("/api/workspaces/switch", { workspace_id: workspaceId })
      const next = await apiGet<Session>("/api/auth/session")
      await client.cancelQueries()
      client.removeQueries({ predicate: (entry) => entry.queryKey[0] !== "session" })
      client.setQueryData(queryKeys.session, next)
      setCsrfToken(next.csrf_token ?? "")
      navigate("/", { replace: true })
    } finally {
      switchingRef.current = false
      setSwitchingWorkspace(false)
    }
  }
  const hasPermission = (permission: string) => query.data?.workspace?.permissions.includes(permission) ?? false
  return <SessionContext.Provider value={{ session: query.data, loading: query.isLoading, signOut, switchWorkspace, switchingWorkspace, hasPermission }}>{children}</SessionContext.Provider>
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
