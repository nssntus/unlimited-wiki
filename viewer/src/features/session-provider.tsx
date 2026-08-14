import { useEffect, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Navigate, useLocation, useNavigate } from "react-router-dom"

import { apiGet, apiPost, queryKeys, setCsrfToken, type Session } from "@/lib/api"
import { Skeleton } from "@/components/ui/skeleton"
import { SessionContext, useSession } from "@/features/session-context"

export function SessionProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const query = useQuery({ queryKey: queryKeys.session, queryFn: () => apiGet<Session>("/api/auth/session"), retry: false, staleTime: 0 })
  useEffect(() => setCsrfToken(query.data?.csrf_token ?? ""), [query.data?.csrf_token])
  const signOut = async () => {
    await apiPost("/api/auth/logout", {})
    setCsrfToken("")
    client.clear()
    navigate("/login", { replace: true })
  }
  return <SessionContext.Provider value={{ session: query.data, loading: query.isLoading, signOut }}>{children}</SessionContext.Provider>
}

export function RequireSession({ children, role }: { children: ReactNode; role?: "admin" }) {
  const { session, loading } = useSession()
  const location = useLocation()
  if (loading) return <div className="p-8"><Skeleton className="h-[70svh] w-full" /></div>
  if (!session?.authenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (role && session.user?.role !== role) return <Navigate to="/forbidden" replace />
  return children
}
